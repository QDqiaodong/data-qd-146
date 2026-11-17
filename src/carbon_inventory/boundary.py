"""Dated ownership boundary: strict parsing, whole-timeline checks, slices.

A boundary is an interval graph.  Every relationship is active on the
half-open interval ``[valid_from, valid_to)``.  Validation happens on the
complete timeline, not only on dates that happen to carry activities:

* per-child direct share sums never exceed one;
* two relationships with the same (parent, child) pair never overlap;
* every graph active on any date, including date intervals without any
  activity, is acyclic.

Validation uses a sweep over all interval endpoints.  Removals are applied
before additions at an endpoint, so an edge expiring on a date is absent on
that date while an edge starting on that date is present.  This is exactly
the half-open interpretation used later for activity slices.

``BoundaryIndex.states`` is the lazy counterpart of a pre-built endpoint slice
table: it walks the same event stream once and presents the unique live
adjacency state for each requested activity interval.  Activities sharing one
endpoint interval reuse the same exact DAG pass.
"""
import bisect
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from .common import ident, rows, require

ORG_LIMIT = 10000
RELATIONSHIP_LIMIT = 50000


@dataclass(frozen=True, slots=True)
class Relationship:
    parent: str
    child: str
    share: Fraction
    operational_control: bool
    start: date
    end: date | None
    index: int


def _day(value):
    if not isinstance(value, str):
        raise ValueError('expected ISO date')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError('invalid ISO date')


def _share(value):
    text = ident(value)
    if text.count('/') != 1:
        raise ValueError('expected rational share n/d')
    numerator, denominator = text.split('/')
    if not (numerator.isdigit() and denominator.isdigit()):
        raise ValueError('expected rational share n/d with decimal digits')
    numerator = int(numerator)
    denominator = int(denominator)
    if not 0 <= numerator <= 10 ** 12 or not 1 <= denominator <= 10 ** 12:
        raise ValueError('share outside allowed bounds')
    share = Fraction(numerator, denominator)
    if share > 1:
        raise ValueError('share must be between 0 and 1')
    return share


def parse_boundary(data):
    """Parse and fully validate a boundary document."""
    require(data, 'relationships')
    raw = rows(data['relationships'], RELATIONSHIP_LIMIT)
    relationships = []
    organizations = set()
    for index, item in enumerate(raw):
        require(item, 'parent child share operational_control valid_from valid_to')
        parent = ident(item['parent'])
        child = ident(item['child'])
        share = _share(item['share'])
        control = item['operational_control']
        if type(control) is not bool:
            raise ValueError('operational_control must be a boolean')
        start = _day(item['valid_from'])
        end = None if item['valid_to'] is None else _day(item['valid_to'])
        if end is not None and end <= start:
            raise ValueError('relationship interval must be nonempty')
        relationships.append(Relationship(parent, child, share, control,
                                          start, end, index))
        organizations.add(parent)
        organizations.add(child)
    if len(organizations) > ORG_LIMIT:
        raise ValueError('too many organizations')
    return BoundaryIndex(relationships, organizations)


class BoundaryIndex:
    """Validated interval graph plus deterministic activity-date slices."""

    def __init__(self, relationships, organizations):
        self.relationships = tuple(relationships)
        self.organizations = frozenset(organizations)
        self._starts = defaultdict(list)
        self._ends = defaultdict(list)
        endpoints = set()
        for edge in self.relationships:
            self._starts[edge.start].append(edge)
            endpoints.add(edge.start)
            if edge.end is not None:
                self._ends[edge.end].append(edge)
                endpoints.add(edge.end)
        # Keep event lists in input order: this only affects diagnostics,
        # never the active edge set or the resulting fractions.
        self._endpoints = sorted(endpoints)
        self._check_pair_overlap()
        self._check_timeline()

    @property
    def endpoints(self):
        return tuple(self._endpoints)

    def _check_pair_overlap(self):
        """The same pair may not have two effective edges on one date."""
        by_pair = defaultdict(list)
        for edge in self.relationships:
            by_pair[(edge.parent, edge.child)].append(edge)
        for edges in by_pair.values():
            edges.sort(key=lambda edge: (edge.start,
                                         date.max if edge.end is None
                                         else edge.end))
            current_end = date.min
            infinite = False
            for edge in edges:
                # left-closed/right-open: [1, 5) and [5, 9) touch, no overlap
                if infinite or edge.start < current_end:
                    raise ValueError(
                        'overlapping relationships for the same organization pair')
                if edge.end is None:
                    infinite = True
                elif edge.end > current_end:
                    current_end = edge.end

    def _check_timeline(self):
        """Check share sums and acyclicity for every active interval.

        A topological order is maintained incrementally.  Removing edges
        cannot invalidate it, and a cycle can only be created by an insertion.
        On insertion we look for a path from child back to parent among nodes
        ordered at or before the parent; if found, reject.  Otherwise the
        affected nodes are moved just after the parent, which restores a valid
        topological order without ever treating intervals as closed.

        Order labels, not an array, define the order: relabeling all nodes is
        rare and amortized, so a long chain inserted in reverse order does not
        cost a full renumbering per edge.
        """
        total_share = defaultdict(Fraction)
        adjacency = defaultdict(set)     # parent -> children
        label = {}
        before = {}
        after = {}
        gap = 2 ** 40

        def walk():
            node = head[0]
            while node is not None:
                yield node
                node = after[node]

        def relabel():
            cursor = None
            for node in walk():
                label[node] = (0 if cursor is None
                               else label[cursor] + gap)
                cursor = node

        head = [None]
        tail = [None]

        def append(node):
            label[node] = 0 if tail[0] is None else label[tail[0]] + gap
            before[node] = tail[0]
            after[node] = None
            if tail[0] is None:
                head[0] = node
            else:
                after[tail[0]] = node
            tail[0] = node

        def insert_after(anchor, node):
            succ = after[anchor]
            low = label[anchor]
            high = label[succ] if succ is not None else low + 2 * gap
            if high - low < 2:
                relabel()
                return insert_after(anchor, node)
            label[node] = (low + high) // 2
            before[node] = anchor
            after[node] = succ
            after[anchor] = node
            if succ is None:
                tail[0] = node
            else:
                before[succ] = node

        def insert_before(anchor, node):
            pred = before[anchor]
            if pred is not None:
                insert_after(pred, node)
                return
            low = label[anchor] - 2 * gap
            high = label[anchor]
            label[node] = (low + high) // 2
            before[node] = None
            after[node] = anchor
            before[anchor] = node
            head[0] = node

        def ensure_parent_before_child(parent, child):
            """Make ``parent`` precede ``child``; reject a closing cycle."""
            if parent not in label:
                if child in label:
                    insert_before(child, parent)
                else:
                    append(parent)
                    append(child)
                return
            if child not in label:
                insert_after(parent, child)
                return
            if label[parent] < label[child]:
                return
            limit = label[parent]
            seen = set()
            stack = [child]
            while stack:
                node = stack.pop()
                if node in seen:
                    continue
                seen.add(node)
                if node == parent:
                    raise ValueError('ownership graph has a cycle')
                for nxt in adjacency.get(node, ()):
                    # A path back to parent can only use nodes ordered at
                    # or below the parent in the old valid order.
                    if nxt not in seen and label[nxt] <= limit:
                        stack.append(nxt)
            affected = sorted(seen, key=label.__getitem__)
            for node in affected:
                pred, succ = before[node], after[node]
                if pred is None:
                    head[0] = succ
                else:
                    after[pred] = succ
                if succ is None:
                    tail[0] = pred
                else:
                    before[succ] = pred
            anchor = parent
            for node in affected:
                insert_after(anchor, node)
                anchor = node

        for moment in self._endpoints:
            changed = set()
            # Half-open intervals: expire first, then activate.
            for edge in self._ends.get(moment, ()):
                adjacency[edge.parent].discard(edge.child)
                total_share[edge.child] -= edge.share
                changed.add(edge.child)
            for edge in self._starts.get(moment, ()):
                if edge.parent == edge.child:
                    raise ValueError('ownership graph has a cycle')
                adjacency[edge.parent].add(edge.child)
                ensure_parent_before_child(edge.parent, edge.child)
                total_share[edge.child] += edge.share
                changed.add(edge.child)
            for child_org in changed:
                if total_share[child_org] > 1:
                    raise ValueError(
                        'direct share sum exceeds one for %s' % child_org)

    def day_key(self, day):
        """Endpoint-interval key for one activity date (bisect on slices)."""
        return bisect.bisect_right(self._endpoints, day) - 1

    def states(self, days):
        """Yield one live adjacency state per endpoint interval.

        Yields ``(day, parents, children)`` for each day in ``days`` (sorted,
        unique) whose endpoint interval differs from the previous one.
        ``parents`` maps child -> ``{parent: edge}`` and ``children`` maps
        parent -> ``{child: edge}``.  The dictionaries are live views over the
        sweep: the consumer must finish with a state before requesting the
        next one.  Expiry precedes activation at an endpoint, so a day equal
        to ``valid_to`` already sees the new graph and a day equal to
        ``valid_from`` already sees the new edge.
        """
        parents = defaultdict(dict)
        children = defaultdict(dict)
        cursor = 0
        previous_key = None
        for day in sorted(set(days)):
            while (cursor < len(self._endpoints)
                   and self._endpoints[cursor] <= day):
                moment = self._endpoints[cursor]
                # Expire before activating: the graph on the endpoint date
                # excludes edges ending there and includes edges starting.
                for edge in self._ends.get(moment, ()):
                    parents[edge.child].pop(edge.parent, None)
                    children[edge.parent].pop(edge.child, None)
                for edge in self._starts.get(moment, ()):
                    parents[edge.child][edge.parent] = edge
                    children[edge.parent][edge.child] = edge
                cursor += 1
            if cursor != previous_key:
                previous_key = cursor
                yield day, parents, children
