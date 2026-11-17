"""Non-anticipatory adaptive audit policy over a shared-risk domain.

The policy is computed for every reachable posterior, never for one *given*
true state.  A node is a stop node when the posterior already meets the
overall and every group tolerance; otherwise it may only query an unrevealed,
auditable variable whose two answers both leave a non-empty posterior.
Variables fixed by constraints or earlier answers are known and are never
paid for.

``worst_cost`` is the least worst-case additional cost from that observation:
each query is paid before its answer arrives, then the branch with the larger
remaining optimum is added.  Equal worst costs select the smallest variable
ID, and both answer branches recurse under the same rule.

``Policy`` computes the exact posterior cost function directly.  ``Budget``
uses the alternative design: it decides whether a cost bound is attainable and
then extracts the policy at the least attainable bound.  Both must agree on
every posterior; the test suite compares them against an independent oracle.

``solve(mask)`` returns ``None`` when no amount of auditing reaches the
precision: some feasible state in the posterior keeps a target interval wider
than its tolerance even after every auditable variable is revealed.  The root
then reports the audit as impossible instead of publishing a policy tree with
a missing failure branch.
"""
import sys

sys.setrecursionlimit(50000)

MISSING = object()


class Policy:
    """Memoised exact minimax policy nodes over posterior masks."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.ids = scenario.variable_ids
        self.costs = [scenario.audit_costs[name] for name in self.ids]
        self.nodes = {}
        self.impossible_mask = None
        self._solve(scenario.all_mask)

    # -- posterior helpers ---------------------------------------------
    def answers(self, mask, index):
        return (mask & self.scenario.masks[index][0],
                mask & self.scenario.masks[index][1])

    def candidates(self, mask):
        """Unrevealed, auditable variables with two reachable answers."""
        result = []
        for index in range(len(self.ids)):
            if self.costs[index] is None:
                continue
            zero, one = self.answers(mask, index)
            if zero and one:
                result.append(index)
        return result

    # -- direct exact posterior cost function --------------------------
    def _solve(self, mask):
        cached = self.nodes.get(mask, False)
        if cached is not False:
            return cached
        if self.scenario.within_precision(mask):
            node = {'kind': 'stop', 'worst_cost': 0}
            node.update(self.scenario.interval_block(mask))
            self.nodes[mask] = node
            return node
        candidates = self.candidates(mask)
        best = None
        chosen = None
        for index in candidates:
            zero, one = self.answers(mask, index)
            z = self._solve(zero)
            o = self._solve(one)
            if z is None or o is None:
                continue
            worst = self.costs[index] + max(z['worst_cost'],
                                            o['worst_cost'])
            if best is None or worst < best:
                best = worst
                chosen = index
        if chosen is None:
            if not candidates and self.impossible_mask is None:
                self.impossible_mask = mask
            self.nodes[mask] = None
            return None
        zero, one = self.answers(mask, chosen)
        node = {
            'kind': 'query',
            'variable': self.ids[chosen],
            'cost': self.costs[chosen],
            'worst_cost': best,
            'zero': self._solve(zero),
            'one': self._solve(one),
        }
        self.nodes[mask] = node
        return node

    def root(self):
        return self.nodes[self.scenario.all_mask]


class Budget:
    """Attainability-first design: decide a cost bound, then extract.

    ``cost(mask)`` is the least worst-case remaining cost (``None`` if
    impossible).  It is found by monotone binary search over an integer bound;
    ``build(mask)`` then extracts the smallest-ID optimal query at that bound.
    """

    def __init__(self, scenario):
        self.scenario = scenario
        self.ids = scenario.variable_ids
        self.costs = [scenario.audit_costs[name] for name in self.ids]
        self.ceiling = sum(c for c in self.costs if c is not None)
        self._feasible = {}
        self._minimal = {}
        self._resolvable = {}
        self._upper = {}
        self._candidates = {}
        self._answers = {}
        self.nodes = {}
        self.impossible_mask = None
        self.build(scenario.all_mask)

    def resolvable(self, mask):
        """Whether unlimited auditing suffices for this posterior."""
        cached = self._resolvable.get(mask, MISSING)
        if cached is not MISSING:
            return cached
        if self.scenario.within_precision(mask):
            result = True
        else:
            result = False
            for index in self.candidates(mask):
                zero, one = self.answers(mask, index)
                if self.resolvable(zero) and self.resolvable(one):
                    result = True
                    break
        self._resolvable[mask] = result
        return result

    def upper_bound(self, mask):
        """Cost of revealing every still-unknown auditable variable."""
        if mask in self._upper:
            return self._upper[mask]
        total = 0
        for index in range(len(self.ids)):
            if self.costs[index] is None:
                continue
            zero, one = self.answers(mask, index)
            if zero and one:
                total += self.costs[index]
        self._upper[mask] = total
        return total

    def answers(self, mask, index):
        key = (mask, index)
        cached = self._answers.get(key, MISSING)
        if cached is MISSING:
            cached = (mask & self.scenario.masks[index][0],
                      mask & self.scenario.masks[index][1])
            self._answers[key] = cached
        return cached

    def candidates(self, mask):
        cached = self._candidates.get(mask, MISSING)
        if cached is MISSING:
            cached = tuple(index for index in range(len(self.ids))
                           if self.costs[index] is not None
                           and all(self.answers(mask, index)))
            self._candidates[mask] = cached
        return cached

    def feasible(self, mask, budget):
        key = (mask, budget)
        cached = self._feasible.get(key, MISSING)
        if cached is not MISSING:
            return cached
        if not self.resolvable(mask):
            result = False
        elif self.scenario.within_precision(mask):
            result = budget >= 0
        elif budget >= self.upper_bound(mask):
            # Every remaining variable can be revealed within this budget.
            result = True
        elif budget < 0:
            result = False
        else:
            result = False
            for index in self.candidates(mask):
                remaining = budget - self.costs[index]
                if remaining < 0:
                    continue
                zero, one = self.answers(mask, index)
                if self.feasible(zero, remaining) and self.feasible(
                        one, remaining):
                    result = True
                    break
        self._feasible[key] = result
        return result

    def cost(self, mask):
        if mask in self._minimal:
            return self._minimal[mask]
        if not self.feasible(mask, self.ceiling):
            self._minimal[mask] = None
            return None
        low, high = 0, self.ceiling
        while low < high:
            middle = (low + high) // 2
            if self.feasible(mask, middle):
                high = middle
            else:
                low = middle + 1
        self._minimal[mask] = low
        return low

    def build(self, mask):
        node = self.nodes.get(mask)
        if node is not None:
            return node
        if self.scenario.within_precision(mask):
            node = {'kind': 'stop', 'worst_cost': 0}
            node.update(self.scenario.interval_block(mask))
            self.nodes[mask] = node
            return node
        bound = self.cost(mask)
        if bound is None:
            candidates = self.candidates(mask)
            if not candidates and self.impossible_mask is None:
                self.impossible_mask = mask
            self.nodes[mask] = None
            return None
        chosen = None
        for index in self.candidates(mask):
            remaining = bound - self.costs[index]
            zero, one = self.answers(mask, index)
            if self.feasible(zero, remaining) and self.feasible(one, remaining):
                chosen = index
                break
        if chosen is None:
            raise AssertionError('budget bound attained without a witness')
        zero, one = self.answers(mask, chosen)
        node = {
            'kind': 'query',
            'variable': self.ids[chosen],
            'cost': self.costs[chosen],
            'worst_cost': bound,
            'zero': self.build(zero),
            'one': self.build(one),
        }
        self.nodes[mask] = node
        return node

    def root(self):
        return self.nodes[self.scenario.all_mask]


def render_node(node):
    """Deterministic JSON form of a policy node."""
    if node is None:
        return None
    if node['kind'] == 'stop':
        return {'groups': node['groups'], 'overall': node['overall']}
    return {
        'variable': node['variable'],
        'cost': node['cost'],
        'worst_cost': node['worst_cost'],
        'zero': render_node(node['zero']),
        'one': render_node(node['one']),
    }
