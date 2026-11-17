"""Exact consolidation over one ownership slice.

For one activity date the boundary index supplies exactly one active edge
set.  Consolidation computes, in one topological pass over the part of that
DAG that can reach an activity organization:

* equity: ``w[root] = 1`` and ``w[v] = sum(w[u] * share(u, v))``.  Converging
  paths are therefore added exactly, without enumerating them.
* operational control: a boolean reachability, so an organization reached by
  several fully controlled paths is still counted once.

Fractions are exact; no floating point and no path expansion are used.
"""
import sys
from collections import defaultdict
from datetime import date
from fractions import Fraction


class SliceGraph:
    """Adjacency of one active slice, borrowed from the boundary sweep.

    ``parents`` maps a child to ``{parent: edge}`` and ``children`` maps a
    parent to ``{child: edge}``.  Both are live views owned by
    ``BoundaryIndex.states``; a consumer must finish with them before
    advancing the iterator.
    """

    __slots__ = ('parents', 'children')

    def __init__(self, parents, children):
        self.parents = parents
        self.children = children

    def weights(self, root, needed):
        """Return ``{org: (equity_share, controlled)}`` for ``needed``."""
        parents = self.parents
        children = self.children
        # Only ancestors of the needed organizations can carry a nonzero
        # share into them, so the pass is restricted to that set plus root.
        # Edges from outside the set cannot lie on a root-to-needed path,
        # and nodes outside it never receive weight from the root.  This is a
        # DAG cut, not an approximation: every root-to-needed path remains.
        nodes = set(needed)
        stack = list(nodes)
        while stack:
            node = stack.pop()
            for parent in parents.get(node, ()):
                if parent not in nodes:
                    nodes.add(parent)
                    stack.append(parent)
        nodes.add(root)

        indegree = dict.fromkeys(nodes, 0)
        for node in nodes:
            for parent in parents.get(node, ()):
                if parent in nodes:
                    indegree[node] += 1

        equity = dict.fromkeys(nodes, Fraction(0))
        equity[root] = Fraction(1)
        control = dict.fromkeys(nodes, False)
        control[root] = True

        # Any topological order is valid here: exact Fraction sums and
        # boolean ors are order independent, so no heap is needed.
        ready = [node for node in nodes if indegree[node] == 0]
        processed = 0
        while ready:
            node = ready.pop()
            processed += 1
            base = equity[node]
            reached = control[node]
            for child, edge in children.get(node, {}).items():
                if child not in nodes:
                    continue
                if base:
                    equity[child] += base * edge.share
                if reached and edge.operational_control:
                    control[child] = True
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if processed != len(nodes):  # pragma: no cover - index validates cycles
            raise ValueError('ownership graph has a cycle')

        return {org: (equity.get(org, Fraction(0)),
                      control.get(org, False))
                for org in needed}


def attribute(activity_rows, index, root):
    """Attribute each activity under equity and operational-control views."""
    # Equity weights are products and sums of up to 50000 rational shares, so
    # an exact numerator or denominator can exceed Python's default 4300-digit
    # cap for int-to-str conversion when the report string is built.  Input
    # integers stay bounded (10^12) and shares have 12-digit components, so
    # this only widens the representation of exact results.
    if hasattr(sys, 'set_int_max_str_digits'):
        sys.set_int_max_str_digits(0)
    by_day = defaultdict(list)
    for row in activity_rows:
        by_day[date.fromisoformat(row['date'])].append(row)
    days = sorted(by_day)

    # Group activity days by endpoint interval.  The boundary index yields
    # exactly one live adjacency state per interval, so the exact DAG pass is
    # run once per interval for all organizations needed in that interval.
    keys = {day: index.day_key(day) for day in days}
    slots = {}
    needed = defaultdict(set)
    for day in days:
        slot = keys[day]
        slots.setdefault(slot, len(slots))
        needed[slots[slot]].update(row['organization']
                                   for row in by_day[day])

    weights = {}
    for day, parents, children in index.states(days):
        slot = slots[keys[day]]
        if slot in weights:
            continue
        weights[slot] = SliceGraph(parents, children).weights(
            root, needed[slot])

    rows_out = []
    totals = defaultdict(lambda: [Fraction(0), Fraction(0)])
    unattributed = []
    rendered = {}
    for row in sorted(activity_rows, key=lambda item: item['id']):
        slot = slots[keys[date.fromisoformat(row['date'])]]
        cache_key = (slot, row['organization'])
        cached = rendered.get(cache_key)
        if cached is None:
            equity_share, controlled = weights[slot][row['organization']]
            cached = rendered[cache_key] = (
                equity_share,
                Fraction(1) if controlled else Fraction(0),
                str(equity_share),
                '1' if controlled else '0')
        equity_share, control_share, equity_text, control_text = cached
        emission = Fraction(row['kgco2e'])
        equity_amount = emission * equity_share
        control_amount = emission * control_share
        rows_out.append({
            'id': row['id'],
            'organization': row['organization'],
            'date': row['date'],
            'scope': row['scope'],
            'kgco2e': str(emission),
            'equity_share': equity_text,
            'equity_kgco2e': str(equity_amount),
            'control_share': control_text,
            'control_kgco2e': str(control_amount),
        })
        totals[row['scope']][0] += equity_amount
        totals[row['scope']][1] += control_amount
        if equity_share == 0 and control_share == 0:
            unattributed.append(row['id'])
    return rows_out, totals, unattributed


def consolidate(source_result, index, root):
    """Build the deterministic group consolidation report body."""
    rows, totals, unattributed = attribute(
        source_result['activities'], index, root)
    return {
        'root': root,
        'activities': rows,
        'totals': [{'scope': scope,
                    'equity_kgco2e': str(values[0]),
                    'control_kgco2e': str(values[1])}
                   for scope, values in sorted(totals.items())],
        'unattributed_activities': unattributed,
    }
