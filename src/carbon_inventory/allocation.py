"""Exact lexicographic allocation of electricity to (certificate, activity) edges.

Four objectives, each compared only among solutions already optimal on every
previous level (no floating point, no fixed-weight collapse into one number):

1. maximize covered kWh
2. minimize total market-based emissions
3. minimize total absolute change versus the previous accepted allocation
4. minimize the allocation vector lexicographically (ordered legal edges)

A single min-cost flow on a source -> certificate -> activity -> sink network
finds the exact optimum.  Every edge carries an ordered composite cost

    (market digit, deviation digit, lattice unit vector)

whose components are added componentwise and compared lexicographically.  The
two scalar digits are integers: fractional emission rates are scaled by one
common denominator, and positional bases strictly larger than any attainable
lower-level total keep the objective priority intact.  One Johnson-potential
successive-shortest-path run on top of the maximum flow yields the exact
four-level optimum.  The convex deviation |x - previous| uses at most two
segment edges per pair with segment widths as capacities, so the number of
iterations depends on the graph, never on the kWh magnitudes (no per-kWh
enumeration).  All arithmetic is int or fractions.Fraction.
"""
from collections import deque
from fractions import Fraction
from math import lcm


class Lattice:
    """Sparse integer lattice Z^d with true lexicographic order.

    Stored as a sorted tuple of (index, coefficient) for non-zero entries.
    Order compares the first differing component (ordered abelian group), so
    it is valid even when residual reverse edges negate costs.  Addition and
    subtraction merge the two sorted tuples in linear time; shortest-path
    vectors stay sparse because each path touches few legal pairs.
    """

    __slots__ = ('items',)

    def __init__(self, items=()):
        self.items = tuple(sorted((i, c) for i, c in items if c))

    @staticmethod
    def configure(dimension):
        # Dimension is implicit in the stored indices; configure() exists so
        # callers can fix the legal-pair ordering once per optimization.
        Lattice._dim = dimension

    @staticmethod
    def zero():
        return Lattice(())

    @staticmethod
    def unit(i):
        return Lattice(((i, 1),))

    def __eq__(self, other):
        return self.items == other.items

    def __lt__(self, other):
        i = j = 0
        a, b = self.items, other.items
        while i < len(a) or j < len(b):
            si = a[i] if i < len(a) else None
            oj = b[j] if j < len(b) else None
            if si is None:
                return oj is not None and oj[1] > 0
            if oj is None:
                return si[1] < 0
            if si[0] == oj[0]:
                if si[1] != oj[1]:
                    return si[1] < oj[1]
                i += 1
                j += 1
            elif si[0] < oj[0]:
                return si[1] < 0
            else:
                return oj[1] > 0
        return False

    def __le__(self, other):
        return self == other or self < other

    def __add__(self, other):
        if not self.items:
            return other
        if not other.items:
            return self
        return Lattice(_merge_add(self.items, other.items))

    def __sub__(self, other):
        if not other.items:
            return self
        if not self.items:
            return Lattice(tuple((i, -c) for i, c in other.items))
        return Lattice(_merge_add(
            self.items, tuple((i, -c) for i, c in other.items)))

    def __neg__(self):
        return Lattice((i, -c) for i, c in self.items)

    def __hash__(self):
        return hash(self.items)

    def __repr__(self):
        return 'Lattice%r' % (self.items,)


def _merge_add(a, b):
    """Merge sorted (index, coefficient) sequences, summing coefficients."""
    i = j = 0
    out = []
    while i < len(a) and j < len(b):
        ia, ca = a[i]
        ib, cb = b[j]
        if ia == ib:
            total = ca + cb
            if total:
                out.append((ia, total))
            i += 1
            j += 1
        elif ia < ib:
            out.append((ia, ca))
            i += 1
        else:
            out.append((ib, cb))
            j += 1
    out.extend(a[i:])
    out.extend(b[j:])
    return out


ZERO_LATTICE = Lattice(())


class Composite:
    """Ordered triple (int market digit, int deviation digit, Lattice)."""

    __slots__ = ('market', 'move', 'vector')

    def __init__(self, market=0, move=0, vector=ZERO_LATTICE):
        self.market = market
        self.move = move
        self.vector = vector

    def __eq__(self, other):
        return (self.market == other.market and self.move == other.move
                and self.vector == other.vector)

    def __lt__(self, other):
        if self.market != other.market:
            return self.market < other.market
        if self.move != other.move:
            return self.move < other.move
        return self.vector < other.vector

    def __le__(self, other):
        return self == other or self < other

    def __add__(self, other):
        if not other.vector.items:
            if other.market == 0 and other.move == 0:
                return self
            return Composite(self.market + other.market,
                             self.move + other.move, self.vector)
        if not self.vector.items and self.market == 0 and self.move == 0:
            return other
        return Composite(self.market + other.market, self.move + other.move,
                         self.vector + other.vector)

    def __sub__(self, other):
        if not other.vector.items and other.market == 0 and other.move == 0:
            return self
        return Composite(self.market - other.market, self.move - other.move,
                         self.vector - other.vector)

    def __neg__(self):
        return Composite(-self.market, -self.move, -self.vector)


class _Net:
    """Residual network; records[i] = [u, v, cap, flow, rev_index]."""

    def __init__(self):
        self.adj = {}
        self.records = []

    def node(self, n):
        self.adj.setdefault(n, [])

    def add(self, u, v, cap):
        self.node(u)
        self.node(v)
        fwd = [u, v, cap, 0, len(self.records) + 1]
        rev = [v, u, 0, 0, len(self.records)]
        self.records.append(fwd)
        self.records.append(rev)
        self.adj[u].append(len(self.records) - 2)
        self.adj[v].append(len(self.records) - 1)
        return len(self.records) - 2

    def reset_flows(self):
        for rec in self.records:
            rec[3] = 0


def legal_edges(certificates, activities):
    edges = []
    scope2 = [a for a in activities if a['scope'] == 2]
    for c in certificates:
        for a in scope2:
            if a['organization'] not in c['organizations']:
                continue
            if a['date'] < c['start'] or (c['end'] is not None
                                          and a['date'] >= c['end']):
                continue
            edges.append((c['id'], a['id']))
    edges.sort()
    return edges


def _max_flow(net, source, sink):
    total = 0
    records = net.records
    while True:
        prev_edge = {}
        queue = deque([source])
        seen = {source}
        while queue and sink not in prev_edge:
            u = queue.popleft()
            for ei in net.adj[u]:
                rec = records[ei]
                if rec[3] < rec[2] and rec[1] not in seen:
                    seen.add(rec[1])
                    prev_edge[rec[1]] = ei
                    queue.append(rec[1])
        if sink not in prev_edge:
            return total
        delta, v = None, sink
        while v != source:
            rec = records[prev_edge[v]]
            room = rec[2] - rec[3]
            delta = room if delta is None else min(delta, room)
            v = rec[0]
        v = sink
        while v != source:
            ei = prev_edge[v]
            rec = records[ei]
            rec[3] += delta
            records[rec[4]][3] -= delta
            v = rec[0]
        total += delta


def _zero_composite():
    return Composite(0, 0, ZERO_LATTICE)


def _spfa(net, costs, source):
    """Shortest-path distances from source over the current residual network."""
    adj, records = net.adj, net.records
    dist = {n: None for n in adj}
    dist[source] = _zero_composite()
    in_queue = {source}
    queue = deque([source])
    while queue:
        u = queue.popleft()
        in_queue.discard(u)
        for ei in adj[u]:
            rec = records[ei]
            if rec[3] >= rec[2]:
                continue
            nd = dist[u] + costs[ei]
            if dist[rec[1]] is None or nd < dist[rec[1]]:
                dist[rec[1]] = nd
                if rec[1] not in in_queue:
                    queue.append(rec[1])
                    in_queue.add(rec[1])
    return dist


def _min_cost_flow(net, costs, source, sink, required):
    """Successive shortest augmenting paths with Johnson potentials.

    The first SPFA builds feasible potentials so every reduced cost is
    non-negative (including the negative deviation down-segments).  Heap
    Dijkstra then drives each augmentation.  Because the convex deviation is
    encoded as at most two segment edges per legal pair, every augmentation
    saturates one such segment or a bottleneck edge: the iteration count is
    bounded by the number of segments O(E), independent of the kWh
    magnitudes (no per-kWh enumeration).
    """
    import heapq
    adj, records = net.adj, net.records
    zero = _zero_composite()
    first = _spfa(net, costs, source)
    potential = {n: (zero if d is None else d) for n, d in first.items()}
    total = 0
    while total < required:
        dist = {n: None for n in adj}
        prev_edge = {}
        dist[source] = zero
        heap = [(zero, source)]
        while heap:
            base, u = heapq.heappop(heap)
            if base != dist[u]:
                continue
            for ei in adj[u]:
                rec = records[ei]
                if rec[3] >= rec[2]:
                    continue
                nd = base + costs[ei] + potential[u] - potential[rec[1]]
                if dist[rec[1]] is None or nd < dist[rec[1]]:
                    dist[rec[1]] = nd
                    prev_edge[rec[1]] = ei
                    heapq.heappush(heap, (nd, rec[1]))
        if dist[sink] is None:
            raise ValueError('allocation graph cannot carry required flow')
        for n, d in dist.items():
            if d is not None:
                potential[n] = potential[n] + d
        bottleneck, v = required - total, sink
        while v != source:
            rec = records[prev_edge[v]]
            bottleneck = min(bottleneck, rec[2] - rec[3])
            v = rec[0]
        v = sink
        while v != source:
            ei = prev_edge[v]
            rec = records[ei]
            rec[3] += bottleneck
            records[rec[4]][3] -= bottleneck
            v = rec[0]
        total += bottleneck
    return total


def optimize(certificates, activities, previous):
    edges = legal_edges(certificates, activities)
    Lattice.configure(len(edges))
    zero_lattice = Lattice.zero()
    cert_by_id = {c['id']: c for c in certificates}
    act_by_id = {a['id']: a for a in activities}
    residual_rate = {a['id']: a['residual_rate'] for a in activities
                     if a['scope'] == 2}
    source, sink = 'S', 'T'
    n_pairs = max(len(edges), 1)
    base_change = 2 * 10 ** 12 * n_pairs + 1
    base_market = (10 ** 12 * (10 ** 12 + 1) * n_pairs + 1) * base_change
    # Common denominator turns every rate difference into an integer slope.
    common_den = 1
    for (cid, aid) in edges:
        diff = cert_by_id[cid]['rate'] - residual_rate[aid]
        common_den = lcm(common_den, diff.denominator)

    def market_digit(pair):
        cid, aid = pair
        diff = cert_by_id[cid]['rate'] - residual_rate[aid]
        return (diff.numerator * (common_den // diff.denominator)) * base_market

    net = _Net()
    net.node(source)
    net.node(sink)
    zero_composite = Composite(0, 0, zero_lattice)
    costs = []
    for c in certificates:
        net.add(source, 'C:' + c['id'], c['quantity'])
        costs.extend((zero_composite, zero_composite))
    for a in activities:
        if a['scope'] == 2:
            net.add('A:' + a['id'], sink, a['quantity'])
            costs.extend((zero_composite, zero_composite))
    pair_forward = {}
    unit_vectors = [Lattice.unit(i) for i in range(len(edges))]
    for index, (cid, aid) in enumerate(edges):
        cu, av = 'C:' + cid, 'A:' + aid
        cap = min(cert_by_id[cid]['quantity'], act_by_id[aid]['quantity'])
        md = market_digit((cid, aid))
        prev = int(previous.get((cid, aid), 0))
        forwards = []
        unit = unit_vectors[index]
        down = min(prev, cap)
        if down:
            ei = net.add(cu, av, down)
            value = Composite(md, -1, unit)
            costs.extend((value, -value))
            forwards.append(ei)
        up = cap - down
        if up:
            ei = net.add(cu, av, up)
            value = Composite(md, 1, unit)
            costs.extend((value, -value))
            forwards.append(ei)
        pair_forward[(cid, aid)] = forwards
    covered = _max_flow(net, source, sink)
    net.reset_flows()
    _min_cost_flow(net, costs, source, sink, covered)
    amounts = [sum(net.records[ei][3] for ei in pair_forward[pair])
               for pair in edges]

    by_activity = {a['id']: 0 for a in activities if a['scope'] == 2}
    by_certificate = {c['id']: 0 for c in certificates}
    market_total = Fraction(0)
    for (cid, aid), amount in zip(edges, amounts):
        by_activity[aid] += amount
        by_certificate[cid] += amount
        market_total += amount * cert_by_id[cid]['rate']
    for a in activities:
        if a['scope'] != 2:
            continue
        uncovered = a['quantity'] - by_activity[a['id']]
        market_total += uncovered * residual_rate[a['id']]
    deviation = 0
    for cid, aid in set(previous) | set(edges):
        pos = _index(edges, (cid, aid))
        current = amounts[pos] if pos is not None else 0
        deviation += abs(current - int(previous.get((cid, aid), 0)))
    objectives = {
        'covered_kwh': covered,
        'market_kgco2e': market_total,
        'total_absolute_change': deviation,
        'allocation_vector': list(amounts),
    }
    return edges, amounts, objectives


def _index(edges, pair):
    lo, hi = 0, len(edges)
    while lo < hi:
        mid = (lo + hi) // 2
        if edges[mid] < pair:
            lo = mid + 1
        else:
            hi = mid
    if lo < len(edges) and edges[lo] == pair:
        return lo
    return None
