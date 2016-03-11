# This file is part of COFFEE
#
# COFFEE is Copyright (c) 2014, Imperial College London.
# Please see the AUTHORS file in the main source directory for
# a full list of copyright holders.  All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * The name of Imperial College London or that of other
#       contributors may not be used to endorse or promote products
#       derived from this software without specific prior written
#       permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTERS
# ''AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDERS OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
# OF THE POSSIBILITY OF SUCH DAMAGE.

from collections import defaultdict, OrderedDict, Counter
from copy import deepcopy as dcopy
from warnings import warn as warning
import itertools
import operator
import pulp as ilp
from sys import maxint

from base import *
from utils import *
from ast_analyzer import ExpressionGraph, StmtTracker
from coffee.visitors import *
from expression import MetaExpr


class ExpressionRewriter(object):
    """Provide operations to re-write an expression:

    * Loop-invariant code motion: find and hoist sub-expressions which are
      invariant with respect to a loop
    * Expansion: transform an expression ``(a + b)*c`` into ``(a*c + b*c)``
    * Factorization: transform an expression ``a*b + a*c`` into ``a*(b+c)``"""

    def __init__(self, stmt, expr_info, decls, header=None, hoisted=None, expr_graph=None):
        """Initialize the ExpressionRewriter.

        :param stmt: AST statement containing the expression to be rewritten
        :param expr_info: ``MetaExpr`` object describing the expression in ``stmt``
        :param decls: declarations for the various symbols in ``stmt``.
        :param header: the parent Block of the loop in which ``stmt`` was found.
        :param hoisted: dictionary that tracks hoisted expressions
        :param expr_graph: expression graph that tracks symbol dependencies
        """
        self.stmt = stmt
        self.expr_info = expr_info
        self.decls = decls
        self.header = header or Root()
        self.hoisted = hoisted if hoisted is not None else StmtTracker()
        self.expr_graph = expr_graph or ExpressionGraph(self.header)

        # Expression manipulators used by the Expression Rewriter
        self.expr_hoister = ExpressionHoister(self.stmt,
                                              self.expr_info,
                                              self.header,
                                              self.decls,
                                              self.hoisted,
                                              self.expr_graph)
        self.expr_expander = ExpressionExpander(self.stmt,
                                                self.expr_info,
                                                self.hoisted,
                                                self.expr_graph)
        self.expr_factorizer = ExpressionFactorizer(self.stmt)

    def licm(self, mode='normal', **kwargs):
        """Perform generalized loop-invariant code motion, a transformation
        detailed in a paper available at:

            http://dl.acm.org/citation.cfm?id=2687415

        :param mode: drive code motion by specifying what subexpressions should
            be hoisted
            * normal: (default) all subexpressions that depend on one loop at most
            * aggressive: all subexpressions, depending on any number of loops.
                This may require introducing N-dimensional temporaries.
            * only_const: only all constant subexpressions
            * only_domain: only all domain-dependent subexpressions
            * only_outdomain: only all subexpressions independent of the domain loops
        :param kwargs:
            * look_ahead: (default: False) should be set to True if only a projection
                of the hoistable subexpressions is needed (i.e., hoisting not performed)
            * max_sharing: (default: False) should be set to True if hoisting should be
                avoided in case the same set of symbols appears in different hoistable
                sub-expressions. By not hoisting, factorization opportunities are preserved
            * iterative: (default: True) should be set to False if interested in
                hoisting only the smallest subexpressions matching /mode/
            * lda: an up-to-date loop dependence analysis, as returned by a call
                to ``ldanalysis(node, 'dim'). By providing this information, loop
                dependence analysis can be avoided, thus speeding up the transformation.
            * global_cse: (default: False) search for common sub-expressions across
                all previously hoisted terms. Note that no data dependency analysis is
                performed, so this is at caller's risk.
        """

        if kwargs.get('look_ahead'):
            return self.expr_hoister.extract(mode, **kwargs)
        if mode == 'aggressive':
            # Reassociation may promote more hoisting in /aggressive/ mode
            self.reassociate()
        self.expr_hoister.licm(mode, **kwargs)
        return self

    def expand(self, mode='standard', **kwargs):
        """Expand expressions based on different rules. For example: ::

            (X[i] + Y[j])*F + ...

        can be expanded into: ::

            (X[i]*F + Y[j]*F) + ...

        The expanded term could also be lifted. For example, if we have: ::

            Y[j] = f(...)
            (X[i]*Y[j])*F + ...

        where ``Y`` was produced by code motion, expansion results in: ::

            Y[j] = f(...)*F
            (X[i]*Y[j]) + ...

        Reasons for expanding expressions include:

        * Exposing factorization opportunities
        * Exposing higher level operations (e.g., matrix multiplies)
        * Relieving register pressure

        :param mode: multiple expansion strategies are possible
            * mode == 'standard': expand along the loop dimension appearing most
                often in different symbols
            * mode == 'dimensions': expand along the loop dimensions provided in
                /kwargs['dimensions']/
            * mode == 'all': expand when symbols depend on at least one of the
                expression's dimensions
            * mode == 'domain': expand when symbols depending on the expressions's
                domain are encountered.
            * mode == 'outdomain': expand when symbols independent of the
                expression's domain are encountered.
        :param kwargs:
            * not_aggregate: True if should not try to aggregate expanded symbols
                with previously hoisted expressions.
            * subexprs: an iterator of subexpressions rooted in /self.stmt/. If
                provided, expansion will be performed only within these trees,
                rather than within the whole expression.
            * lda: an up-to-date loop dependence analysis, as returned by a call
                to ``ldanalysis(node, 'symbol', 'dim'). By providing this information,
                loop dependence analysis can be avoided, thus speeding up the
                transformation.
        """

        if mode == 'standard':
            retval = FindInstances.default_retval()
            symbols = FindInstances(Symbol).visit(self.stmt.rvalue, ret=retval)[Symbol]
            # The heuristics privileges domain dimensions
            dims = self.expr_info.out_domain_dims
            if not dims or self.expr_info.dimension >= 2:
                dims = self.expr_info.domain_dims
            # Get the dimension occurring most often
            occurrences = [tuple(r for r in s.rank if r in dims) for s in symbols]
            occurrences = [i for i in occurrences if i]
            if not occurrences:
                return self
            # Finally, establish the expansion dimension
            dimension = Counter(occurrences).most_common(1)[0][0]
            should_expand = lambda n: set(dimension).issubset(set(n.rank))
        elif mode == 'dimensions':
            dimensions = kwargs.get('dimensions', ())
            should_expand = lambda n: set(dimensions).issubset(set(n.rank))
        elif mode in ['all', 'domain', 'outdomain']:
            lda = kwargs.get('lda') or ldanalysis(self.expr_info.outermost_loop,
                                                  key='symbol', value='dim')
            if mode == 'all':
                should_expand = lambda n: lda.get(n.symbol) and \
                    any(r in self.expr_info.dims for r in lda[n.symbol])
            elif mode == 'domain':
                should_expand = lambda n: lda.get(n.symbol) and \
                    any(r in self.expr_info.domain_dims for r in lda[n.symbol])
            elif mode == 'outdomain':
                should_expand = lambda n: lda.get(n.symbol) and \
                    not lda[n.symbol].issubset(set(self.expr_info.domain_dims))
        else:
            warning('Unknown expansion strategy. Skipping.')
            return

        # Perform the expansion
        self.expr_expander.expand(should_expand, **kwargs)
        self.decls.update(self.expr_expander.expanded_decls)

        return self

    def factorize(self, mode='standard', **kwargs):
        """Factorize terms in the expression. For example: ::

            A[i]*B[j] + A[i]*C[j]

        becomes ::

            A[i]*(B[j] + C[j]).

        :param mode: multiple factorization strategies are possible. Note that
                     different strategies may expose different code motion opportunities

            * mode == 'standard': factorize symbols along the dimension that appears
                most often in the expression.
            * mode == 'dimensions': factorize symbols along the loop dimensions provided
                in /kwargs['dimensions']/
            * mode == 'all': factorize symbols depending on at least one of the
                expression's dimensions.
            * mode == 'domain': factorize symbols depending on the expression's domain.
            * mode == 'outdomain': factorize symbols independent of the expression's
                domain.
            * mode == 'constants': factorize symbols independent of any loops enclosing
                the expression.
            * mode == 'adhoc': factorize only symbols in /kwargs['adhoc']/ (details below)
            * mode == 'heuristic': no global factorization rule is used; rather, within
                each Sum tree, factorize the symbols appearing most often in that tree
        :param kwargs:
            * subexprs: an iterator of subexpressions rooted in /self.stmt/. If
                provided, factorization will be performed only within these trees,
                rather than within the whole expression
            * adhoc: a list of symbols that can be factorized and, for each symbol,
                a list of symbols that can be grouped. For example, if we have
                ``kwargs['adhoc'] = [(A, [B, C]), (D, [E, F, G])]``, and the
                expression is ``A*B + D*E + A*C + A*F``, the result will be
                ``A*(B+C) + A*F + D*E``. If the A's list were empty, all of the
                three symbols B, C, and F would be factorized. Recall that this
                option is ignored unless ``mode == 'adhoc'``.
            * lda: an up-to-date loop dependence analysis, as returned by a call
                to ``ldanalysis(node, 'symbol', 'dim'). By providing this information,
                loop dependence analysis can be avoided, thus speeding up the
                transformation.
        """

        if mode == 'standard':
            retval = FindInstances.default_retval()
            symbols = FindInstances(Symbol).visit(self.stmt.rvalue, ret=retval)[Symbol]
            # The heuristics privileges domain dimensions
            dims = self.expr_info.out_domain_dims
            if not dims or self.expr_info.dimension >= 2:
                dims = self.expr_info.domain_dims
            # Get the dimension occurring most often
            occurrences = [tuple(r for r in s.rank if r in dims) for s in symbols]
            occurrences = [i for i in occurrences if i]
            if not occurrences:
                return self
            # Finally, establish the factorization dimension
            dimension = Counter(occurrences).most_common(1)[0][0]
            should_factorize = lambda n: set(dimension).issubset(set(n.rank))
        elif mode == 'dimensions':
            dimensions = kwargs.get('dimensions', ())
            should_factorize = lambda n: set(dimensions).issubset(set(n.rank))
        elif mode == 'adhoc':
            adhoc = kwargs.get('adhoc')
            if not adhoc:
                return self
            should_factorize = lambda n: n.urepr in adhoc
        elif mode == 'heuristic':
            kwargs['heuristic'] = True
            should_factorize = lambda n: False
        elif mode in ['all', 'domain', 'outdomain', 'constants']:
            lda = kwargs.get('lda') or ldanalysis(self.expr_info.outermost_loop,
                                                  key='symbol', value='dim')
            if mode == 'all':
                should_factorize = lambda n: lda.get(n.symbol) and \
                    any(r in self.expr_info.dims for r in lda[n.symbol])
            elif mode == 'domain':
                should_factorize = lambda n: lda.get(n.symbol) and \
                    any(r in self.expr_info.domain_dims for r in lda[n.symbol])
            elif mode == 'outdomain':
                should_factorize = lambda n: lda.get(n.symbol) and \
                    not lda[n.symbol].issubset(set(self.expr_info.domain_dims))
            elif mode == 'constants':
                should_factorize = lambda n: not lda.get(n.symbol)
        else:
            warning('Unknown factorization strategy. Skipping.')
            return

        # Perform the factorization
        self.expr_factorizer.factorize(should_factorize, **kwargs)
        return self

    def reassociate(self, reorder=None):
        """Reorder symbols in associative operations following a convention.
        By default, the convention is to order the symbols based on their rank.
        For example, the terms in the expression ::

            a*b[i]*c[i][j]*d

        are reordered as ::

            a*d*b[i]*c[i][j]

        This as achieved by reorganizing the AST of the expression.
        """

        def _reassociate(node, parent):
            if isinstance(node, (Symbol, Div)):
                return

            elif isinstance(node, Par):
                _reassociate(node.child, node)

            elif isinstance(node, (Sum, Sub, FunCall)):
                for n in node.children:
                    _reassociate(n, node)

            elif isinstance(node, Prod):
                children = explore_operator(node)
                # Reassociate symbols
                symbols = [n for n, p in children if isinstance(n, Symbol)]
                # Capture the other children and recur on them
                other_nodes = [(n, p) for n, p in children if not isinstance(n, Symbol)]
                for n, p in other_nodes:
                    _reassociate(n, p)
                # Create the reassociated product and modify the original AST
                children = zip(*other_nodes)[0] if other_nodes else ()
                children += tuple(sorted(symbols, key=reorder))
                reassociated_node = ast_make_expr(Prod, children, balance=False)
                parent.children[parent.children.index(node)] = reassociated_node

            else:
                warning('Unexpect node of type %s while reassociating', typ(node))

        reorder = reorder if reorder else lambda n: (n.rank, n.dim)
        _reassociate(self.stmt.rvalue, self.stmt)
        return self

    def replacediv(self):
        """Replace divisions by a constant with multiplications."""
        retval = FindInstances.default_retval()
        divisions = FindInstances(Div).visit(self.stmt.rvalue, ret=retval)[Div]
        to_replace = {}
        for i in divisions:
            if isinstance(i.right, Symbol):
                if isinstance(i.right.symbol, (int, float)):
                    to_replace[i] = Prod(i.left, 1.0 / i.right.symbol)
                elif isinstance(i.right.symbol, str) and i.right.symbol.isdigit():
                    to_replace[i] = Prod(i.left, 1.0 / float(i.right.symbol))
                else:
                    to_replace[i] = Prod(i.left, Div(1.0, i.right))
        ast_replace(self.stmt, to_replace, copy=True, mode='symbol')
        return self

    def preevaluate(self):
        """Preevaluates subexpressions which values are compile-time constants.
        In this process, reduction loops might be removed if the reduction itself
        could be pre-evaluated."""
        # Aliases
        stmt, expr_info = self.stmt, self.expr_info

        # Simplify reduction loops
        if not isinstance(stmt, (Incr, Decr, IMul, IDiv)):
            # Not a reduction expression, give up
            return
        retval = FindInstances.default_retval()
        expr_syms = FindInstances(Symbol).visit(stmt.rvalue, ret=retval)[Symbol]
        reduction_loops = expr_info.out_domain_loops_info
        if any([not is_perfect_loop(l) for l, p in reduction_loops]):
            # Unsafe if not a perfect loop nest
            return
        # The following check is because it is unsafe to simplify if non-loop or
        # non-constant dimensions are present
        hoisted_stmts = self.hoisted.all_stmts
        hoisted_syms = [FindInstances(Symbol).visit(h)[Symbol] for h in hoisted_stmts]
        hoisted_dims = [s.rank for s in flatten(hoisted_syms)]
        hoisted_dims = set([r for r in flatten(hoisted_dims) if not is_const_dim(r)])
        if any(d not in expr_info.dims for d in hoisted_dims):
            # Non-loop dimension or non-constant dimension found, e.g. A[i], with /i/
            # not being a loop iteration variable
            return
        for i, (l, p) in enumerate(reduction_loops):
            retval = SymbolDependencies.default_retval()
            syms_dep = SymbolDependencies().visit(l, ret=retval,
                                                  **SymbolDependencies.default_args)
            if not all([tuple(syms_dep[s]) == expr_info.loops and
                        s.dim == len(expr_info.loops) for s in expr_syms if syms_dep[s]]):
                # A sufficient (although not necessary) condition for loop reduction to
                # be safe is that all symbols in the expression are either constants or
                # tensors assuming a distinct value in each point of the iteration space.
                # So if this condition fails, we give up
                return
            # At this point, tensors can be reduced along the reducible dimensions
            reducible_syms = [s for s in expr_syms if not s.is_const]
            # All involved symbols must result from hoisting
            if not all([s.symbol in self.hoisted for s in reducible_syms]):
                return
            # Replace hoisted assignments with reductions
            finder = FindInstances(Assign, stop_when_found=True, with_parent=True)
            for hoisted_loop in self.hoisted.all_loops:
                retval = FindInstances.default_retval()
                for assign, parent in finder.visit(hoisted_loop, ret=retval)[Assign]:
                    sym, expr = assign.children
                    decl = self.hoisted[sym.symbol].decl
                    if sym.symbol in [s.symbol for s in reducible_syms]:
                        parent.children[parent.children.index(assign)] = Incr(sym, expr)
                        sym.rank = self.expr_info.domain_dims
                        decl.sym.rank = decl.sym.rank[i+1:]
            # Remove the reduction loop
            p.children[p.children.index(l)] = l.body[0]
            # Update symbols' ranks
            for s in reducible_syms:
                s.rank = self.expr_info.domain_dims
            # Update expression metadata
            self.expr_info._loops_info.remove((l, p))

        # Precompute constant expressions
        evaluator = Evaluate(self.decls, any(d.nonzero for s, d in self.decls.items()))
        for hoisted_loop in self.hoisted.all_loops:
            evals = evaluator.visit(hoisted_loop, **Evaluate.default_args)
            # First, find out identical tables
            mapper = defaultdict(list)
            for s, values in evals.items():
                mapper[str(values)].append(s)
            # Then, map identical tables to a single symbol
            for values, symbols in mapper.items():
                to_replace = {s: symbols[0] for s in symbols[1:]}
                ast_replace(self.stmt, to_replace, copy=True)
                # Clean up
                for s in symbols[1:]:
                    s_decl = self.hoisted[s.symbol].decl
                    self.header.children.remove(s_decl)
                    self.hoisted.pop(s.symbol)
                    evals.pop(s)
            # Finally, update the hoisted symbols
            for s, values in evals.items():
                hoisted = self.hoisted[s.symbol]
                hoisted.decl.init = values
                hoisted.decl.qual = ['static', 'const']
                self.hoisted.pop(s.symbol)
                # Move all decls at the top of the kernel
                self.header.children.remove(hoisted.decl)
                self.header.children.insert(0, hoisted.decl)
            self.header.children.insert(0, FlatBlock("// Preevaluated tables"))
            # Clean up
            self.header.children.remove(hoisted_loop)
        return self

    def SGrewrite(self):
        """Apply rewrite rules based on the sharing graph of the expression."""
        lda = ldanalysis(self.expr_info.domain_loops[0], key='symbol', value='dim')
        sg_visitor = SharingGraph(self.expr_info, lda)

        # First, eliminate sharing "locally", i.e., within Sums
        sgraph, mapper = sg_visitor.visit(self.stmt.rvalue)
        handled = set()
        for n in sgraph.nodes():
            mapped = mapper.get((n,), [])
            with_sharing = [e for e in mapped if e and e not in handled]
            for e in with_sharing:
                self.expand(mode='dimensions', subexprs=[e],
                            not_aggregate=True, dimensions=n[1])
                self.factorize(mode='dimensions', subexprs=[e], dimensions=n[1])
                handled.add(e)
        self.factorize(mode='heuristic')
        self.licm(mode='only_outdomain')

        # Then, apply rewrite rules A, B, and C
        sgraph, mapper = sg_visitor.visit(self.stmt.rvalue)
        if 'topsum' in mapper:
            self.expand(mode='domain', subexprs=[mapper['topsum']], not_aggregate=True)
            sgraph, mapper = sg_visitor.visit(self.stmt.rvalue)

        # Now set the ILP problem:
        nodes, edges = sgraph.nodes(), sgraph.edges()
        if not edges:
            return
        # Note: need to use short variable names otherwise Pulp might complair
        nodes_vars = {i: n for i, n in enumerate(nodes)}
        vars_nodes = {n: i for i, n in nodes_vars.items()}
        edges = [(vars_nodes[i], vars_nodes[j]) for i, j in edges]

        # ... declare variables
        x = ilp.LpVariable.dicts('x', nodes_vars.keys(), 0, 1, ilp.LpBinary)
        y = ilp.LpVariable.dicts('y', [(i, j) for i, j in edges] + [(j, i) for i, j in edges],
                                 0, 1, ilp.LpBinary)
        limits = defaultdict(int)
        for i, j in edges:
            limits[i] += 1
            limits[j] += 1

        # ... define the problem
        prob = ilp.LpProblem("Factorizer", ilp.LpMinimize)

        # ... define the constraints
        for i in nodes_vars:
            prob += ilp.lpSum(y[(i, j)] for j in nodes_vars if (i, j) in y) <= limits[i]*x[i]

        for i, j in edges:
            prob += y[(i, j)] + y[(j, i)] == 1

        # ... define the objective function (min number of factorizations)
        prob += ilp.lpSum(x[i] for i in nodes_vars)

        # ... solve the problem
        status = prob.solve(ilp.GLPK(msg=0))

        # Finally, factorize and hoist (note: the order in which factorizations are carried
        # out is crucial)
        nodes = [nodes_vars[n] for n, v in x.items() if v.value() == 1]
        other_nodes = [nodes_vars[n] for n, v in x.items() if nodes_vars[n] not in nodes]
        for n in nodes + other_nodes:
            self.factorize(mode='adhoc', adhoc={n: []})
        self.licm()

    def unpickCSE(self):
        """Search for factorization opportunities across temporaries created by
        common sub-expression elimination. If a gain in operation count is detected,
        unpick CSE and apply factorization + code motion."""
        cse_unpicker = CSEUnpicker(self.stmt, self.expr_info, self.header, self.hoisted,
                                   self.decls, self.expr_graph)
        cse_unpicker.unpick()

    @staticmethod
    def reset():
        ExpressionHoister._handled = 0
        ExpressionExpander._handled = 0


class ExpressionExtractor():

    EXT = 0  # expression marker: extract
    STOP = 1  # expression marker: do not extract

    def __init__(self, stmt, expr_info):
        self.stmt = stmt
        self.expr_info = expr_info
        self.counter = 0

    def _try(self, node, dep):
        if isinstance(node, Symbol):
            # Never extract individual symbols
            return False
        should_extract = True
        if self.mode == 'aggressive':
            # Do extract everything
            should_extract = True
        elif self.mode == 'only_const':
            # Do not extract unless constant in all loops
            if dep and dep.issubset(set(self.expr_info.dims)):
                should_extract = False
        elif self.mode == 'only_domain':
            # Do not extract unless dependent on domain loops
            if dep.issubset(set(self.expr_info.out_domain_dims)):
                should_extract = False
        elif self.mode == 'only_outdomain':
            # Do not extract unless independent of the domain loops
            if not dep.issubset(set(self.expr_info.out_domain_dims)):
                should_extract = False
        if should_extract or self.look_ahead:
            dep = sorted(dep, key=lambda i: self.expr_info.dims.index(i))
            self.extracted.setdefault(tuple(dep), []).append(node)
        return should_extract

    def _soft(self, left, right, dep_l, dep_r, dep_n, info_l, info_r):
        if info_l == self.EXT and info_r == self.EXT:
            if dep_l == dep_r:
                # E.g. alpha*beta, A[i] + B[i]
                return (dep_l, self.EXT)
            elif dep_l.issubset(dep_r):
                # E.g. A[i]*B[i,j]
                if not self._try(right, dep_r):
                    return (dep_n, self.EXT)
            elif dep_r.issubset(dep_l):
                # E.g. A[i,j]*B[i]
                if not self._try(left, dep_l):
                    return (dep_n, self.EXT)
            else:
                # E.g. A[i]*B[j]
                self._try(left, dep_l)
                self._try(right, dep_r)
        elif info_r == self.EXT:
            self._try(right, dep_r)
        elif info_l == self.EXT:
            self._try(left, dep_l)
        return (dep_n, self.STOP)

    def _normal(self, left, right, dep_l, dep_r, dep_n, info_l, info_r):
        if info_l == self.EXT and info_r == self.EXT:
            if dep_l == dep_r:
                # E.g. alpha*beta, A[i] + B[i]
                return (dep_l, self.EXT)
            elif not dep_l:
                # E.g. alpha*A[i,j]
                if not set(self.expr_info.domain_dims) & dep_r or \
                        not (self._try(left, dep_l) or self._try(right, dep_r)):
                    return (dep_r, self.EXT)
            elif not dep_r:
                # E.g. A[i,j]*alpha
                if not set(self.expr_info.domain_dims) & dep_l or \
                        not (self._try(right, dep_r) or self._try(left, dep_l)):
                    return (dep_l, self.EXT)
            elif dep_l.issubset(dep_r):
                # E.g. A[i]*B[i,j]
                if not self._try(left, dep_l):
                    return (dep_n, self.EXT)
            elif dep_r.issubset(dep_l):
                # E.g. A[i,j]*B[i]
                if not self._try(right, dep_r):
                    return (dep_n, self.EXT)
            else:
                # E.g. A[i]*B[j]
                self._try(left, dep_l)
                self._try(right, dep_r)
        elif info_r == self.EXT:
            self._try(right, dep_r)
        elif info_l == self.EXT:
            self._try(left, dep_l)
        return (dep_n, self.STOP)

    def _aggressive(self, left, right, dep_l, dep_r, dep_n, info_l, info_r):
        if info_l == self.EXT and info_r == self.EXT:
            if dep_l == dep_r:
                # E.g. alpha*beta, A[i] + B[i]
                return (dep_l, self.EXT)
            elif not dep_l:
                # E.g. alpha*A[i,j], not hoistable anymore
                self._try(right, dep_r)
            elif not dep_r:
                # E.g. A[i,j]*alpha, not hoistable anymore
                self._try(left, dep_l)
            elif dep_l.issubset(dep_r):
                # E.g. A[i]*B[i,j]
                if not self._try(left, dep_l):
                    return (dep_n, self.EXT)
            elif dep_r.issubset(dep_l):
                # E.g. A[i,j]*B[i]
                if not self._try(right, dep_r):
                    return (dep_n, self.EXT)
            else:
                # E.g. A[i]*B[j], hoistable in TMP[i,j]
                return (dep_n, self.EXT)
        elif info_r == self.EXT:
            self._try(right, dep_r)
        elif info_l == self.EXT:
            self._try(left, dep_l)
        return (dep_n, self.STOP)

    def _extract(self, node):
        if isinstance(node, Symbol):
            return (self.lda[node], self.EXT)

        elif isinstance(node, Par):
            return self._extract(node.child)

        elif isinstance(node, (FunCall, Ternary)):
            arg_deps = [self._extract(n) for n in node.children]
            dep = tuple(set(flatten([dep for dep, _ in arg_deps])))
            info = self.EXT if all(i == self.EXT for _, i in arg_deps) else self.STOP
            return (dep, info)

        else:
            # Traverse the expression tree
            left, right = node.children
            dep_l, info_l = self._extract(left)
            dep_r, info_r = self._extract(right)

            # Filter out false dependencies
            dep_l = {d for d in dep_l if d in self.expr_info.dims}
            dep_r = {d for d in dep_r if d in self.expr_info.dims}
            dep_n = dep_l | dep_r

            args = left, right, dep_l, dep_r, dep_n, info_l, info_r

            if self.mode in ['normal', 'only_const', 'only_outdomain']:
                return self._normal(*args)
            elif self.mode == 'only_domain':
                return self._soft(*args)
            elif self.mode == 'aggressive':
                return self._aggressive(*args)
            else:
                raise RuntimeError("licm: unexpected hoisting mode (%s)" % self.mode)

    def __call__(self, mode, look_ahead, lda):
        """Extract invariant subexpressions from /self.expr/."""

        self.mode = mode
        self.look_ahead = look_ahead
        self.lda = lda
        self.extracted = OrderedDict()
        self._extract(self.stmt.rvalue)

        self.counter += 1
        return self.extracted


class ExpressionHoister(object):

    # How many times the hoister was invoked
    _handled = 0
    # Temporary variables template
    _hoisted_sym = "%(loop_dep)s_%(expr_id)d_%(round)d_%(i)d"

    def __init__(self, stmt, expr_info, header, decls, hoisted, expr_graph):
        """Initialize the ExpressionHoister."""
        self.stmt = stmt
        self.expr_info = expr_info
        self.header = header
        self.decls = decls
        self.hoisted = hoisted
        self.expr_graph = expr_graph
        self.extractor = ExpressionExtractor(self.stmt, self.expr_info)

        # Increment counters for unique variable names
        self.expr_id = ExpressionHoister._handled
        ExpressionHoister._handled += 1

    def _filter(self, dep, subexprs, make_unique=True, sharing=None):
        """Filter hoistable subexpressions."""
        if make_unique:
            # Uniquify expressions
            subexprs = uniquify(subexprs)

        if sharing:
            # Partition expressions such that expressions sharing the same
            # set of symbols are in the same partition
            if dep == self.expr_info.dims:
                return []
            sharing = [str(s) for s in sharing]
            finder = FindInstances(Symbol)
            partitions = defaultdict(list)
            for e in subexprs:
                retval = FindInstances.default_retval()
                symbols = tuple(set(str(s) for s in finder.visit(e, ret=retval)[Symbol]
                                    if str(s) in sharing))
                partitions[symbols].append(e)
            for shared, partition in partitions.items():
                if len(partition) > len(shared):
                    subexprs = [e for e in subexprs if e not in partition]

        return subexprs

    def extract(self, mode, **kwargs):
        """Return a dictionary of hoistable subexpressions."""
        lda = kwargs.get('lda') or ldanalysis(self.header, value='dim')
        return self.extractor(mode, True, lda)

    def licm(self, mode, **kwargs):
        """Perform generalized loop-invariant code motion."""
        max_sharing = kwargs.get('max_sharing', False)
        iterative = kwargs.get('iterative', True)
        lda = kwargs.get('lda') or ldanalysis(self.header, value='dim')
        global_cse = kwargs.get('global_cse', False)

        expr_dims_loops = self.expr_info.loops_from_dims
        expr_outermost_loop = self.expr_info.outermost_loop

        mapper = {}
        extracted = True
        while extracted:
            extracted = self.extractor(mode, False, lda)
            for dep, subexprs in extracted.items():
                # 1) Filter subexpressions that will be hoisted
                sharing = []
                if max_sharing:
                    sharing = uniquify([s for s, d in lda.items() if d == dep])
                subexprs = self._filter(dep, subexprs, sharing=sharing)
                if not subexprs:
                    continue

                # 2) Determine the loop nest level where invariant expressions
                # should be hoisted. The goal is to hoist them as far as possible
                # in the loop nest, while minimising temporary storage.
                # We distinguish six hoisting cases:
                if len(dep) == 0:
                    # As scalar (/wrap_loop=None/), outside of the loop nest;
                    place = self.header
                    wrap_loop = ()
                    next_loop = expr_outermost_loop
                elif len(dep) == 1 and is_perfect_loop(expr_outermost_loop):
                    # As scalar, outside of the loop nest;
                    place = self.header
                    wrap_loop = (expr_dims_loops[dep[0]],)
                    next_loop = expr_outermost_loop
                elif len(dep) == 1 and len(expr_dims_loops) > 1:
                    # As scalar, within the loop imposing the dependency
                    place = expr_dims_loops[dep[0]].children[0]
                    wrap_loop = ()
                    next_loop = od_find_next(expr_dims_loops, dep[0])
                elif len(dep) == 1:
                    # As scalar, right before the expression (which is enclosed
                    # in just a single loop, we can claim at this point)
                    place = expr_dims_loops[dep[0]].children[0]
                    wrap_loop = ()
                    next_loop = place.children[place.children.index(self.stmt)]
                elif mode == 'aggressive' and set(dep) == set(self.expr_info.dims) and \
                        not any([self.expr_graph.is_written(e) for e in subexprs]):
                    # As n-dimensional vector, where /n == len(dep)/, outside of
                    # the loop nest
                    place = self.header
                    wrap_loop = tuple(expr_dims_loops.values())
                    next_loop = expr_outermost_loop
                elif not is_perfect_loop(expr_dims_loops[dep[-1]]):
                    # As scalar, within the closest loop imporsing the dependency
                    place = expr_dims_loops[dep[-1]].children[0]
                    wrap_loop = ()
                    next_loop = od_find_next(expr_dims_loops, dep[-1])
                else:
                    # As vector, within the outermost loop imposing the dependency
                    place = expr_dims_loops[dep[0]].children[0]
                    wrap_loop = tuple(expr_dims_loops[dep[i]] for i in range(1, len(dep)))
                    next_loop = od_find_next(expr_dims_loops, dep[0])

                loop_size = tuple([l.size for l in wrap_loop])
                loop_dim = tuple([l.dim for l in wrap_loop])

                # 3) Create the required new AST nodes
                symbols, decls, stmts = [], [], []
                for i, e in enumerate(subexprs):
                    if global_cse and self.hoisted.get_symbol(e):
                        name = self.hoisted.get_symbol(e)
                    else:
                        name = self._hoisted_sym % {
                            'loop_dep': '_'.join(dep) if dep else 'c',
                            'expr_id': self.expr_id,
                            'round': self.extractor.counter,
                            'i': i
                        }
                        stmts.append(Assign(Symbol(name, loop_dim), dcopy(e)))
                        decl = Decl(self.expr_info.type, Symbol(name, loop_size))
                        decl.scope = LOCAL
                        decls.append(decl)
                        self.decls[name] = decl
                    symbols.append(Symbol(name, loop_dim))

                # 4) Replace invariant sub-expressions with temporaries
                to_replace = dict(zip(subexprs, symbols))
                n_replaced = ast_replace(self.stmt.rvalue, to_replace)

                # 5) Update data dependencies
                for s, e in zip(symbols, subexprs):
                    self.expr_graph.add_dependency(s, e)
                    if n_replaced[str(s)] > 1:
                        self.expr_graph.add_dependency(s, s)
                    lda[s] = dep

                # 6) Track necessary information for AST construction
                info = (loop_dim, place, next_loop, wrap_loop)
                if info not in mapper:
                    mapper[info] = (decls, stmts)
                else:
                    mapper[info][0].extend(decls)
                    mapper[info][1].extend(stmts)

            if not iterative:
                break

        for info, (decls, stmts) in sorted(mapper.items()):
            loop_dim, place, next_loop, wrap_loop = info
            # Create the hoisted code
            if wrap_loop:
                outer_wrap_loop = ast_make_for(stmts, wrap_loop[-1])
                for l in reversed(wrap_loop[:-1]):
                    outer_wrap_loop = ast_make_for([outer_wrap_loop], l)
                code = decls + [outer_wrap_loop]
                wrap_loop = outer_wrap_loop
            else:
                code = decls + stmts
                wrap_loop = None
            # Insert the new nodes at the right level in the loop nest
            ofs = place.children.index(next_loop)
            place.children[ofs:ofs] = code + [FlatBlock("\n")]
            # Track hoisted symbols
            for i, j in zip(stmts, decls):
                self.hoisted[j.sym.symbol] = (i, j, wrap_loop, place)

        # Finally, make sure symbols are unique in the AST
        self.stmt.rvalue = dcopy(self.stmt.rvalue)


class ExpressionExpander(object):

    # Constants used by the expand method to charaterize sub-expressions:
    GROUP = 0  # Expression /will/ not trigger expansion
    EXPAND = 1  # Expression /could/ be expanded

    # How many times the expander was invoked
    _handled = 0
    # Temporary variables template
    _expanded_sym = "%(loop_dep)s_EXP_%(expr_id)d_%(i)d"

    class Cache():
        """A cache for expanded expressions."""

        def __init__(self):
            self._map = {}
            self._hits = defaultdict(int)

        def make_key(self, exp, grp):
            return (str(exp), str(grp))

        def retrieve(self, key):
            exp = self._map.get(key)
            if exp:
                self._hits[key] += 1
            return exp

        def invalidate(self, exp):
            was_hit = False
            for i, j in self._map.items():
                if str(j) == str(exp):
                    self._map.pop(i)
                    if self._hits[i] > 0:
                        was_hit = True
            return was_hit

        def add(self, key, exp):
            self._map[key] = exp

    def __init__(self, stmt, expr_info=None, hoisted=None, expr_graph=None):
        self.stmt = stmt
        self.expr_info = expr_info
        self.hoisted = hoisted
        self.expr_graph = expr_graph
        self.expanded_decls = {}
        self.cache = self.Cache()

        # Increment counters for unique variable names
        self.expr_id = ExpressionExpander._handled
        ExpressionExpander._handled += 1

    def _hoist(self, expansion, info):
        """Try to aggregate an expanded expression E with a previously hoisted
        expression H. If there are no dependencies, H is expanded with E, so
        no new symbols need be introduced. Otherwise (e.g., the H temporary
        appears in multiple places), create a new symbol."""
        exp, grp = expansion.left, expansion.right

        # First, check if any of the symbols in /exp/ have been hoisted
        try:
            retval = FindInstances.default_retval()
            exp = [s for s in FindInstances(Symbol).visit(exp, ret=retval)[Symbol]
                   if s.symbol in self.hoisted and self.should_expand(s)][0]
        except:
            # No hoisted symbols in the expanded expression, so return
            return {}

        # Before moving on, access the cache to check whether the same expansion
        # has alredy been performed. If that's the case, we retrieve and return the
        # result of that expansion, since there is no need to add further temporaries
        cache_key = self.cache.make_key(exp, grp)
        cached = self.cache.retrieve(cache_key)
        if cached:
            return {exp: cached}

        # Aliases
        hoisted_stmt = self.hoisted[exp.symbol].stmt
        hoisted_decl = self.hoisted[exp.symbol].decl
        hoisted_loop = self.hoisted[exp.symbol].loop
        hoisted_place = self.hoisted[exp.symbol].place
        op = expansion.__class__

        # Is the grouped symbol hoistable, or does it break some data dependency?
        retval = SymbolReferences.default_retval()
        grp_syms = SymbolReferences().visit(grp, ret=retval).keys()
        for l in reversed(self.expr_info.loops):
            for g in grp_syms:
                g_refs = info['symbol_refs'][g]
                g_deps = set(flatten([info['symbols_dep'].get(r[0], []) for r in g_refs]))
                if any([l.dim in g.dim for g in g_deps]):
                    return {}
            if l in hoisted_place.children:
                break

        # Perform the expansion in place unless cache conflicts are detected
        if not self.expr_graph.is_read(exp) and not self.cache.invalidate(exp):
            hoisted_stmt.rvalue = op(hoisted_stmt.rvalue, dcopy(grp))
            self.expr_graph.add_dependency(exp, grp)
            return {exp: exp}

        # Create new symbol, expression, and declaration
        expr = op(dcopy(exp), grp)
        hoisted_exp = dcopy(exp)
        hoisted_exp.symbol = self._expanded_sym % {'loop_dep': exp.symbol,
                                                   'expr_id': self.expr_id,
                                                   'i': len(self.expanded_decls)}
        decl = dcopy(hoisted_decl)
        decl.sym.symbol = hoisted_exp.symbol
        decl.scope = LOCAL
        stmt = Assign(hoisted_exp, expr)
        # Update the AST
        hoisted_loop.body.append(stmt)
        insert_at_elem(hoisted_place.children, hoisted_decl, decl)
        # Update tracked information
        self.expanded_decls[decl.sym.symbol] = decl
        self.hoisted[hoisted_exp.symbol] = (stmt, decl, hoisted_loop, hoisted_place)
        self.expr_graph.add_dependency(hoisted_exp, expr)
        self.cache.add(cache_key, hoisted_exp)
        return {exp: hoisted_exp}

    def _build(self, exp, grp):
        """Create a node for the expansion and keep track of it."""
        expansion = Prod(exp, dcopy(grp))
        # Track the new expansion
        self.expansions.append(expansion)
        # Untrack any expansions occured in children nodes
        if grp in self.expansions:
            self.expansions.remove(grp)
        return expansion

    def _expand(self, node, parent):
        if isinstance(node, Symbol):
            return ([node], self.EXPAND) if self.should_expand(node) \
                else ([node], self.GROUP)

        elif isinstance(node, Par):
            return self._expand(node.child, node)

        elif isinstance(node, (Div, FunCall)):
            # Try to expand /within/ the children, but then return saying "I'm not
            # expandable any further"
            for n in node.children:
                self._expand(n, node)
            return ([node], self.GROUP)

        elif isinstance(node, Prod):
            l_exps, l_type = self._expand(node.left, node)
            r_exps, r_type = self._expand(node.right, node)
            if l_type == self.GROUP and r_type == self.GROUP:
                return ([node], self.GROUP)
            # At least one child is expandable (marked as EXPAND), whereas the
            # other could either be expandable as well or groupable (marked
            # as GROUP): so we can perform the expansion
            groupable = l_exps if l_type == self.GROUP else r_exps
            expandable = r_exps if l_type == self.GROUP else l_exps
            to_replace = OrderedDict()
            for exp, grp in itertools.product(expandable, groupable):
                expansion = self._build(exp, grp)
                to_replace.setdefault(exp, []).append(expansion)
            ast_replace(node, {k: ast_make_expr(Sum, v) for k, v in to_replace.items()},
                        mode='symbol')
            # Update the parent node, since an expression has just been expanded
            expanded = node.right if l_type == self.GROUP else node.left
            parent.children[parent.children.index(node)] = expanded
            return (list(flatten(to_replace.values())) or [expanded], self.EXPAND)

        elif isinstance(node, (Sum, Sub)):
            l_exps, l_type = self._expand(node.left, node)
            r_exps, r_type = self._expand(node.right, node)
            if l_type == self.EXPAND and r_type == self.EXPAND and isinstance(node, Sum):
                return (l_exps + r_exps, self.EXPAND)
            elif l_type == self.EXPAND and r_type == self.EXPAND and isinstance(node, Sub):
                return (l_exps + [Neg(r) for r in r_exps], self.EXPAND)
            else:
                return ([node], self.GROUP)

        else:
            raise RuntimeError("Expansion error: unknown node: %s" % str(node))

    def expand(self, should_expand, **kwargs):
        not_aggregate = kwargs.get('not_aggregate')
        expressions = kwargs.get('subexprs', [(self.stmt.rvalue, self.stmt)])

        self.should_expand = should_expand

        for node, parent in expressions:
            self.expansions = []
            self._expand(node, parent)

            if not_aggregate:
                continue
            info = visit(self.expr_info.outermost_loop) if self.expr_info else visit(parent)
            for expansion in self.expansions:
                hoisted = self._hoist(expansion, info)
                if hoisted:
                    ast_replace(parent, hoisted, copy=True, mode='symbol')
                    ast_remove(parent, expansion.right, mode='symbol')


class ExpressionFactorizer(object):

    class Term():
        """A Term represents a product between 'operands' and 'factors'. In a
        product /a*(b+c)/, /a/ is the 'operand', while /b/ and /c/ are the 'factors'.
        The symbol /+/ is the 'op' of the Term.
        """

        def __init__(self, operands, factors=None, op=None):
            self.operands = operands
            self.factors = factors or []
            self.op = op

        @property
        def operands_ast(self):
            return ast_make_expr(Prod, self.operands)

        @property
        def factors_ast(self):
            return ast_make_expr(self.op, self.factors)

        @property
        def generate_ast(self):
            if len(self.factors) == 0:
                return self.operands_ast
            elif len(self.operands) == 0:
                return self.factors_ast
            elif len(self.factors) == 1 and \
                    all(isinstance(i, Symbol) and i.symbol == 1.0 for i in self.factors):
                return self.operands_ast
            else:
                return Prod(self.operands_ast, self.factors_ast)

        def add_operands(self, operands):
            for o in operands:
                if o not in self.operands:
                    self.operands.append(o)

        def remove_operands(self, operands):
            for o in operands:
                if o in self.operands:
                    self.operands.remove(o)

        def add_factors(self, factors):
            for f in factors:
                if f not in self.factors:
                    self.factors.append(f)

        def remove_factors(self, factors):
            for f in factors:
                if f in self.factors:
                    self.factors.remove(f)

        @staticmethod
        def process(symbols, should_factorize, op=None):
            operands = [s for s in symbols if should_factorize(s)]
            factors = [s for s in symbols if not should_factorize(s)]
            return ExpressionFactorizer.Term(operands, factors, op)

    def __init__(self, stmt):
        self.stmt = stmt

    def _simplify_sum(self, terms):
        unique_terms = OrderedDict()
        for t in terms:
            unique_terms.setdefault(str(t.generate_ast), list()).append(t)

        for t_repr, t_list in unique_terms.items():
            occurrences = len(t_list)
            unique_terms[t_repr] = t_list[0]
            if occurrences > 1:
                unique_terms[t_repr].add_factors([Symbol(occurrences)])

        terms[:] = unique_terms.values()

    def _heuristic_collection(self, terms):
        if not self.heuristic or any(t.operands for t in terms):
            return
        tracker = OrderedDict()
        for t in terms:
            symbols = [s for s in t.factors if isinstance(s, Symbol)]
            for s in symbols:
                tracker.setdefault(s.urepr, []).append(t)
        reverse_tracker = OrderedDict()
        for s, ts in tracker.items():
            reverse_tracker.setdefault(tuple(ts), []).append(s)
        # 1) At least one symbol appearing in all terms: use that as operands ...
        operands = [(ts, s) for ts, s in reverse_tracker.items() if ts == tuple(terms)]
        # 2) ... Or simply pick operands greedily
        if not operands:
            handled = set()
            for ts, s in reverse_tracker.items():
                if len(ts) > 1 and all(t not in handled for t in ts):
                    operands.append((ts, s))
                    handled |= set(ts)
        for ts, s in operands:
            for t in ts:
                new_operands = [i for i in t.factors if
                                isinstance(i, Symbol) and i.urepr in s]
                t.remove_factors(new_operands)
                t.add_operands(new_operands)

    def _premultiply_symbols(self, symbols):
        floats = [s for s in symbols if isinstance(s.symbol, (int, float))]
        if len(floats) > 1:
            other_symbols = [s for s in symbols if s not in floats]
            prem = reduce(operator.mul, [s.symbol for s in floats], 1.0)
            prem = [Symbol(prem)] if prem not in [1, 1.0] else []
            return prem + other_symbols
        else:
            return symbols

    def _filter(self, factorizable_term):
        o = factorizable_term.operands_ast
        grp = self.adhoc.get(o.urepr, []) if isinstance(o, Symbol) else []
        if not grp:
            return False
        for f in factorizable_term.factors:
            retval = FindInstances.default_retval()
            symbols = FindInstances(Symbol).visit(f, ret=retval)[Symbol]
            if any(s.urepr in grp for s in symbols):
                return False
        return True

    def _factorize(self, node, parent):
        if isinstance(node, Symbol):
            return self.Term.process([node], self.should_factorize)

        elif isinstance(node, Par):
            return self._factorize(node.child, node)

        elif isinstance(node, (FunCall, Div)):
            # Try to factorize /within/ the children, but then return saying
            # "I'm not factorizable any further"
            for n in node.children:
                self._factorize(n, node)
            return self.Term([], [node])

        elif isinstance(node, Prod):
            children = explore_operator(node)
            symbols = [n for n, _ in children if isinstance(n, Symbol)]
            other_nodes = [(n, p) for n, p in children if n not in symbols]
            symbols = self._premultiply_symbols(symbols)
            factorized = self.Term.process(symbols, self.should_factorize, Prod)
            terms = [self._factorize(n, p) for n, p in other_nodes]
            for t in terms:
                factorized.add_operands(t.operands)
                factorized.add_factors(t.factors)
            return factorized

        # The fundamental case is when /node/ is a Sum (or Sub, equivalently).
        # Here, we try to factorize the terms composing the operation
        elif isinstance(node, (Sum, Sub)):
            children = explore_operator(node)
            # First try to factorize within /node/'s children
            terms = [self._factorize(n, p) for n, p in children]
            # Check if it's possible to aggregate operations
            # Example: replace (a*b)+(a*b) with 2*(a*b)
            self._simplify_sum(terms)
            # No global factorization rule is used, so just try to maximize
            # factorization within /this/ Sum/Sub
            self._heuristic_collection(terms)
            # Finally try to factorize some of the operands composing the operation
            factorized = OrderedDict()
            for t in terms:
                operand = [t.operands_ast] if t.operands else []
                factor = [t.factors_ast] if t.factors else [Symbol(1.0)]
                factorizable_term = self.Term(operand, factor, node.__class__)
                if self._filter(factorizable_term):
                    # Skip
                    factorized[t] = t
                else:
                    # Do factorize
                    _t = factorized.setdefault(str(t.operands_ast), factorizable_term)
                    _t.add_factors(factor)
            factorized = [t.generate_ast for t in factorized.values()]
            factorized = ast_make_expr(Sum, factorized)
            parent.children[parent.children.index(node)] = factorized
            return self.Term([], [factorized])

        else:
            return self.Term([], [node])
            raise RuntimeError("Factorization error: unknown node: %s" % str(node))

    def factorize(self, should_factorize, **kwargs):
        expressions = kwargs.get('subexprs', [(self.stmt.rvalue, self.stmt)])
        adhoc = kwargs.get('adhoc', {})

        self.should_factorize = should_factorize
        self.adhoc = adhoc if any(v for v in adhoc.values()) else {}
        self.heuristic = kwargs.get('heuristic', False)

        for node, parent in expressions:
            self._factorize(node, parent)


class CSEUnpicker(object):

    class Temporary():

        def __init__(self, node, loop, reads_costs=None):
            self.level = -1
            self.node = node
            self.loop = loop
            self.reads_costs = reads_costs or {}
            self.is_read = []
            self.cost = EstimateFlops().visit(node)

        @property
        def symbol(self):
            if isinstance(self.node, Writer):
                return self.node.lvalue
            elif isinstance(self.node, Symbol):
                return self.node
            else:
                return None

        @property
        def expr(self):
            if isinstance(self.node, Writer):
                return self.node.rvalue
            else:
                return None

        @property
        def urepr(self):
            return self.symbol.urepr

        @property
        def reads(self):
            return self.reads_costs.keys() if self.reads_costs else []

        @property
        def project(self):
            return len(self.reads)

        @property
        def dependencies(self):
            return [s.urepr for s in self.reads]

        def dependson(self, other):
            return other.urepr in self.dependencies

        def reconstruct(self):
            temporary = CSEUnpicker.Temporary(self.node, self.loop, dict(self.reads_costs))
            temporary.level = self.level
            temporary.is_read = list(self.is_read)
            return temporary

        def __str__(self):
            return "%s: level=%d, cost=%d, reads=[%s], isread=[%s]" % \
                (self.symbol, self.level, self.cost,
                 ", ".join([str(i) for i in self.reads]),
                 ", ".join([str(i) for i in self.is_read]))

    def __init__(self, stmt, expr_info, header, hoisted, decls, expr_graph):
        self.stmt = stmt
        self.expr_info = expr_info
        self.header = header
        self.hoisted = hoisted
        self.decls = decls
        self.expr_graph = expr_graph

    def _push_temporaries(self, trace, levels, loop, cur_level, global_trace):
        assert [i in levels.keys() for i in [cur_level-1, cur_level]]

        # Remove temporaries being pushed
        for t in levels[cur_level-1]:
            if t.node in t.loop.body and all(ir.urepr in trace for ir in t.is_read):
                t.loop.body.remove(t.node)

        # Track temporaries to be pushed from /level-1/ into the later /level/s
        to_replace, modified_temporaries = {}, OrderedDict()
        for t in levels[cur_level-1]:
            to_replace[t.symbol] = t.expr or t.symbol
            for ir in t.is_read:
                modified_temporaries[ir.urepr] = trace.get(ir.urepr,
                                                           global_trace[ir.urepr])

        # Update the temporaries
        replaced = [t.urepr for t in to_replace.keys()]
        for t in modified_temporaries.values():
            ast_replace(t.node, to_replace, copy=True)
            for r, c in t.reads_costs.items():
                if r.urepr in replaced:
                    t.reads_costs.pop(r)
                    for p, p_c in global_trace[r.urepr].reads_costs.items() or [(r, 0)]:
                        t.reads_costs[p] = c + p_c

    def _transform_temporaries(self, temporaries, loop, nest):
        lda = ldanalysis(self.header, key='symbol', value='dim')

        # Expand + Factorize
        rewriters = OrderedDict()
        for t in temporaries:
            expr_info = MetaExpr(self.expr_info.type, loop.children[0], nest, (loop.dim,))
            ew = ExpressionRewriter(t.node, expr_info, self.decls, self.header,
                                    self.hoisted, self.expr_graph)
            ew.replacediv()
            ew.expand(mode='all', not_aggregate=True, lda=lda)
            ew.factorize(mode='adhoc', adhoc={i.urepr: [] for i in t.reads}, lda=lda)
            ew.factorize(mode='heuristic')
            rewriters[t] = ew

        lda = ldanalysis(self.header, value='dim')

        # Code motion
        for t, ew in rewriters.items():
            ew.licm(mode='only_outdomain', lda=lda, global_cse=True)

    def _analyze_expr(self, expr, loop, lda):
        finder = FindInstances(Symbol)
        syms = finder.visit(expr, ret=FindInstances.default_retval())[Symbol]
        syms = [s for s in syms
                if any(l in self.expr_info.domain_dims for l in lda[s])]

        syms_costs = defaultdict(int)

        def wrapper(node, found=0):
            if isinstance(node, Symbol):
                if node in syms:
                    syms_costs[node] += found
                return
            elif isinstance(node, (Prod, Div)):
                found += 1
            operands = zip(*explore_operator(node))[0]
            for o in operands:
                wrapper(o, found)
        wrapper(expr)

        return syms_costs

    def _analyze_loop(self, loop, lda, global_trace):
        trace = OrderedDict()

        for stmt in loop.body:
            if not isinstance(stmt, Writer):
                continue
            syms_costs = self._analyze_expr(stmt.rvalue, loop, lda)
            for s in syms_costs.keys():
                if s.urepr in global_trace:
                    temporary = global_trace[s.urepr]
                    temporary.is_read.append(stmt.lvalue)
                    temporary = temporary.reconstruct()
                    temporary.level = -1
                    trace[s.urepr] = temporary
                else:
                    temporary = trace.setdefault(s.urepr, CSEUnpicker.Temporary(s, loop))
                    temporary.is_read.append(stmt.lvalue)
            new_temporary = CSEUnpicker.Temporary(stmt, loop, syms_costs)
            new_temporary.level = max([trace[s.urepr].level for s in new_temporary.reads]) + 1
            trace[stmt.lvalue.urepr] = new_temporary

        return trace

    def _group_by_level(self, trace):
        levels = defaultdict(list)
            
        for temporary in trace.values():
            levels[temporary.level].append(temporary)
        return levels

    def _cost_cse(self, loop, levels, keys=None):
        if keys is not None:
            levels = {k: levels[k] for k in keys}
        cost = 0
        for level, temporaries in levels.items():
            cost += sum(t.cost for t in temporaries)
        return cost*loop.size

    def _cost_fact(self, trace, levels, loop, bounds):
        # Check parameters
        bounds = bounds or (min(levels.keys()), max(levels.keys()))
        assert len(bounds) == 2 and bounds[1] >= bounds[0]
        assert [i in levels.keys() for i in bounds]
        fact_levels = OrderedDict([(k, v) for k, v in levels.items()
                                   if k > bounds[0] and k <= bounds[1]])

        # Cost of levels that won't be factorized
        cse_cost = self._cost_cse(loop, levels, range(min(levels.keys()), bounds[0] + 1))

        # We are going to modify a copy of the temporaries dict
        new_trace = OrderedDict()
        for s, t in trace.items():
            new_trace[s] = t.reconstruct()

        best = (bounds[0], bounds[0], maxint)
        total_outloop_cost = 0
        for level, temporaries in sorted(fact_levels.items(), key=lambda (i, j): i):
            level_inloop_cost = 0
            for t in temporaries:

                # Calculate the operation count for /t/ if we applied expansion + fact
                reads = []
                for read, cost in t.reads_costs.items():
                    reads.extend(new_trace[read.urepr].reads or [read.urepr])
                    # The number of operations induced outside /loop/ (after hoisting)
                    total_outloop_cost += new_trace[read.urepr].project*cost

                # Factorization will kill duplicates and increase the number of sums
                # in the outer loop
                fact_syms = {s.urepr if isinstance(s, Symbol) else s for s in reads}
                total_outloop_cost += len(reads) - len(fact_syms)

                # The operation count, after factorization, within /loop/, induced by /t/
                # Note: if n=len(fact_syms), then we'll have n prods, n-1 sums
                level_inloop_cost += 2*len(fact_syms) - 1

                # Update the trace because we want to track the cost after "pushing" the
                # temporaries on which /t/ depends into /t/ itself
                new_trace[t.urepr].reads_costs = {s: 1 for s in fact_syms}

            # Some temporaries at levels < /i/ may also appear in:
            # 1) subsequent loops
            # 2) levels beyond /i/
            for t in list(flatten([levels[j] for j in range(level)])):
                if any(ir.urepr not in new_trace for ir in t.is_read) or \
                        any(new_trace[ir.urepr].level > level for ir in t.is_read):
                    # Note: condition 1) is basically saying "if I'm read from
                    # a temporary that is not in this loop's trace, then I must
                    # be read in some other loops".
                    level_inloop_cost += t.cost

            # Total cost = cost_after_fact_up_to_level + cost_inloop_cse
            #            = cost_hoisted_subexprs + cost_inloop_fact + cost_inloop_cse
            uptolevel_cost = cse_cost + total_outloop_cost + loop.size*level_inloop_cost
            uptolevel_cost += self._cost_cse(loop, fact_levels, range(level + 1, bounds[1] + 1))

            # Update the best alternative
            if uptolevel_cost < best[2]:
                best = (bounds[0], level, uptolevel_cost)

            cse = self._cost_cse(loop, fact_levels, range(level + 1, bounds[1] + 1))
            print "Cost after pushing up to level", level, ":", uptolevel_cost, \
                "(", cse_cost, "+", total_outloop_cost, "+", loop.size*level_inloop_cost, "+", cse, ")"
        print "************************"

        return best

    def unpick(self):
        fors = visit(self.header, info_items=['fors'])['fors']
        lda = ldanalysis(self.header, value='dim')

        # Collect all loops to be analyzed
        nests = OrderedDict()
        for nest in fors:
            for loop, parent in nest:
                if loop.is_linear:
                    nests[loop] = nest

        # Analysis of loops
        global_trace = OrderedDict()
        mapper = OrderedDict()
        for loop in nests.keys():
            trace = self._analyze_loop(loop, lda, global_trace)
            if trace:
                mapper[loop] = trace
                global_trace.update(trace)

        for loop, trace in mapper.items():
            # Do not attempt to transform the main loop nest
            nest = nests[loop]
            if self.expr_info.loops_info == nest:
                continue

            # Compute the best cost alternative
            levels = self._group_by_level(trace)
            min_level, max_level = min(levels.keys()), max(levels.keys())
            global_best = (min_level, max_level, maxint)
            for i in sorted(levels.keys()):
                local_best = self._cost_fact(trace, levels, loop, (i, max_level))
                if local_best[2] < global_best[2]:
                    global_best = local_best

            # Transform the loop
            for i in range(global_best[0] + 1, global_best[1] + 1):
                self._push_temporaries(trace, levels, loop, i, global_trace)
                self._transform_temporaries(levels[i], loop, nest)

        # Clean up
        for transformed_loop, nest in reversed(nests.items()):
            for loop, parent in nest:
                if loop == transformed_loop and not loop.body:
                    parent.children.remove(loop)
