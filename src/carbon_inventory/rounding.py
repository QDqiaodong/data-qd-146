"""Bidirectionally controlled rounding disclosure (schema version 5).

Exact Fraction emissions are aggregated into an organization x
(scope, category) table and rounded to integer kgCO2e so that prescribed
row and column totals both hold at the same time.  Every cell may only take
the mathematical floor or ceiling of its exact value; a lock pins one cell
to one direction.

The whole computation is exact (int and Fraction, never float) and never
enumerates rounding combinations or expands values per kg:

* feasibility is one bipartite max flow on the post-lock lower/upper bounds.
  Bounds violations and the row/column balance check are reported first, in
  fixed order; an otherwise infeasible instance publishes the source-reachable
  residual cut (A, B) with a recomputable capacity and deficit.
* the optimum first minimizes the total absolute rounding error and only then
  the lexicographically smallest rounded vector (cells in row/column order,
  zero and locked entries included).  Both levels are one min-cost flow whose
  ordered composite edge cost is (exact error integer, positional bit vector);
  Dijkstra with Johnson potentials and blocking augmentations keeps every
  number exact and the iteration count tied to the graph.
"""
from fractions import Fraction

from .common import ident, number, require, rows
from .model import parse_inventory

ROW_LIMIT = 100
COLUMN_LIMIT = 100
LOCK_LIMIT = 10_000
TOTAL_LIMIT = 10 ** 30
SOURCE = 'S'
SINK = 'T'


def _nonneg_int(value, limit, label):
    # bool is a subclass of int; the contract rejects booleans explicitly.
    if type(value) is not int or not 0 <= value <= limit:
        raise ValueError('%s must be an integer in [0, %d]' % (label, limit))
    return value


def parse_rounding(document, organizations, columns):
    """Validate the rounding specification against the source row/columns.

    ``organizations`` is the sorted list of source organization IDs and
    ``columns`` the sorted (scope, category) tuples actually occurring in the
    source.  Coverage must be exact, so an empty source only accepts three
    empty arrays.
    """
    require(document, 'rows columns locks')
    row_set = set(organizations)
    col_set = set(columns)
    if not row_set and not col_set:
        if document['rows'] or document['columns'] or document['locks']:
            raise ValueError(
                'empty source requires empty rows, columns and locks')

    targets_row = {}
    for raw in rows(document['rows'], ROW_LIMIT):
        require(raw, 'organization total')
        org = ident(raw['organization'])
        if org in targets_row:
            raise ValueError('duplicate rounding row: ' + org)
        targets_row[org] = _nonneg_int(raw['total'], TOTAL_LIMIT,
                                       'row total')
    if set(targets_row) != row_set:
        raise ValueError('rounding rows must cover every source organization')

    targets_col = {}
    for raw in rows(document['columns'], COLUMN_LIMIT):
        require(raw, 'scope category total')
        scope = number(raw['scope'], 1, 2)
        category = ident(raw['category'])
        key = (scope, category)
        if key in targets_col:
            raise ValueError(
                'duplicate rounding column: %d/%s' % (scope, category))
        targets_col[key] = _nonneg_int(raw['total'], TOTAL_LIMIT,
                                       'column total')
    if set(targets_col) != col_set:
        raise ValueError(
            'rounding columns must cover every source scope/category')

    locks = {}
    for raw in rows(document['locks'], LOCK_LIMIT):
        require(raw, 'organization scope category rounding')
        org = ident(raw['organization'])
        scope = number(raw['scope'], 1, 2)
        category = ident(raw['category'])
        direction = raw['rounding']
        if direction not in ('floor', 'ceil'):
            raise ValueError("lock rounding must be 'floor' or 'ceil'")
        if org not in row_set:
            raise ValueError('lock references an unknown row: ' + org)
        key = (scope, category)
        if key not in col_set:
            raise ValueError(
                'lock references an unknown column: %d/%s'
                % (scope, category))
        cell = (org, key)
        if cell in locks:
            raise ValueError(
                'duplicate lock for cell %s/%d/%s'
                % (org, scope, category))
        locks[cell] = direction
    return {'row_targets': targets_row, 'col_targets': targets_col,
            'locks': locks}


def _floor_ceil(value):
    """Mathematical floor/ceil for a non-negative Fraction."""
    floor = value.numerator // value.denominator
    ceil = floor + (1 if value.numerator % value.denominator else 0)
    return floor, ceil


def min_cost_flow(row_residual, col_residual, free_edges, edge_cost,
                  required):
    """Exact min-cost flow on the bipartite residual network.

    ``free_edges`` maps a cell key ``k`` to its (row, column) indices, each
    with one unit of capacity.  All edge costs must be non-negative; they are
    arbitrary ordered objects supporting +, - and < (int or (int, int) tuple).
    Successive shortest paths use Dijkstra with Johnson potentials and a
    blocking flow per round, so the iteration count depends on the graph, not
    on the kg magnitudes.  Returns (unit flow per free edge, final node
    potentials, node index map, node list); the potentials let an independent
    caller inspect reduced costs, while ``run`` only consumes the flow.
    """
    import heapq
    from collections import deque

    n_rows = len(row_residual)
    n_cols = len(col_residual)
    nodes = ['S', 'T'] + ['R%d' % i for i in range(n_rows)] \
        + ['C%d' % j for j in range(n_cols)]
    index_of = {node: i for i, node in enumerate(nodes)}
    edges = []  # [to, reverse, capacity, cost, flow]
    adjacency = [[] for _ in nodes]
    if edge_cost:
        sample_cost = next(iter(edge_cost.values()))
        zero = (0, 0) if isinstance(sample_cost, tuple) else 0
    else:
        zero = 0

    def add_edge(u, v, capacity, cost):
        reverse_cost = (-cost[0], -cost[1]) if isinstance(cost, tuple) \
            else -cost
        forward = [index_of[v], len(edges) + 1, capacity, cost, 0]
        reverse = [index_of[u], len(edges), 0, reverse_cost, 0]
        edges.append(forward)
        edges.append(reverse)
        adjacency[index_of[u]].append(len(edges) - 2)
        adjacency[index_of[v]].append(len(edges) - 1)
        return len(edges) - 2

    free_edge = {}
    for i, residual in enumerate(row_residual):
        if residual:
            add_edge('S', 'R%d' % i, residual, zero)
    for j, residual in enumerate(col_residual):
        if residual:
            add_edge('C%d' % j, 'T', residual, zero)
    for k, (i, j) in free_edges.items():
        free_edge[k] = add_edge('R%d' % i, 'C%d' % j, 1, edge_cost[k])

    source, sink = index_of['S'], index_of['T']
    tuple_costs = isinstance(zero, tuple)

    def add_cost(a, b):
        return (a[0] + b[0], a[1] + b[1]) if tuple_costs else a + b

    def sub_cost(a, b):
        return (a[0] - b[0], a[1] - b[1]) if tuple_costs else a - b

    if required == 0:
        return ({k: edges[edge][4] for k, edge in free_edge.items()},
                [zero] * len(nodes), index_of, nodes)
    potential = [zero] * len(nodes)
    pushed = 0
    while pushed < required:
        distance = [None] * len(nodes)
        distance[source] = zero
        heap = [(zero, source)]
        while heap:
            base, u = heapq.heappop(heap)
            if base != distance[u]:
                continue
            for edge_index in adjacency[u]:
                edge = edges[edge_index]
                if edge[2] <= edge[4]:
                    continue
                v = edge[0]
                reduced = sub_cost(add_cost(edge[3], potential[u]),
                                   potential[v])
                candidate = add_cost(base, reduced)
                if distance[v] is None or candidate < distance[v]:
                    distance[v] = candidate
                    heapq.heappush(heap, (candidate, v))
        if distance[sink] is None:
            raise AssertionError('feasible rounding flow became unroutable')
        for node, value in enumerate(distance):
            if value is not None:
                potential[node] = add_cost(potential[node], value)
        # Blocking flow over the zero-reduced-cost admissible DAG.
        level = [-1] * len(nodes)
        level[source] = 0
        bfs = deque([source])
        while bfs:
            u = bfs.popleft()
            for edge_index in adjacency[u]:
                edge = edges[edge_index]
                v = edge[0]
                if (edge[2] > edge[4] and level[v] < 0
                        and add_cost(edge[3], potential[u])
                        == potential[v]):
                    level[v] = level[u] + 1
                    bfs.append(v)
        if level[sink] < 0:
            raise AssertionError('rounding flow lost its shortest path')
        current_arc = [0] * len(nodes)

        def send(node, budget):
            if node == sink or budget == 0:
                return budget
            while current_arc[node] < len(adjacency[node]):
                edge_index = adjacency[node][current_arc[node]]
                edge = edges[edge_index]
                v = edge[0]
                room = edge[2] - edge[4]
                if (room > 0 and level[v] == level[node] + 1
                        and add_cost(edge[3], potential[node])
                        == potential[v]):
                    amount = send(v, min(budget, room))
                    if amount:
                        edge[4] += amount
                        edges[edge[1]][4] -= amount
                        return amount
                current_arc[node] += 1
            return 0

        while pushed < required:
            amount = send(source, required - pushed)
            if not amount:
                break
            pushed += amount
    if pushed != required:
        raise AssertionError('min-cost rounding flow missed the residual')
    return ({k: edges[edge][4] for k, edge in free_edge.items()},
            potential, index_of, nodes)


def run(source_data, source_digest, spec_data, spec_digest):
    """Validate inputs, solve and build the complete schema-5 report."""
    factors, activities = parse_inventory(source_data)
    seen_ids = set()
    for activity in activities:
        if activity['id'] in seen_ids:
            raise ValueError('duplicate activity id: ' + activity['id'])
        seen_ids.add(activity['id'])

    exact_cells = {}
    for activity in activities:
        cell = (activity['organization'],
                (activity['scope'], activity['category']))
        exact_cells[cell] = (
            exact_cells.get(cell, Fraction(0))
            + activity['quantity'] * activity['rate'])

    organizations = sorted({cell[0] for cell in exact_cells})
    columns = sorted({cell[1] for cell in exact_cells})
    if len(organizations) > ROW_LIMIT or len(columns) > COLUMN_LIMIT:
        raise ValueError('rounded table exceeds 100 rows or 100 columns')
    spec = parse_rounding(spec_data, organizations, columns)
    locks = spec['locks']

    cell_keys = [(org, col) for org in organizations for col in columns]
    exact = []
    lower = []
    upper = []
    lock_dir = []
    for cell in cell_keys:
        value = exact_cells.get(cell, Fraction(0))
        floor, ceil = _floor_ceil(value)
        direction = locks.get(cell)
        if direction == 'ceil':
            low = high = ceil
        elif direction == 'floor':
            low = high = floor
        else:
            low, high = floor, ceil
        exact.append(value)
        lower.append(low)
        upper.append(high)
        lock_dir.append(direction)

    row_count, col_count = len(organizations), len(columns)

    def cell_index(i, j):
        return i * col_count + j

    row_exact = [Fraction(0)] * row_count
    col_exact = [Fraction(0)] * col_count
    for i in range(row_count):
        for j in range(col_count):
            k = cell_index(i, j)
            row_exact[i] += exact[k]
            col_exact[j] += exact[k]

    row_targets = [spec['row_targets'][org] for org in organizations]
    col_targets = [spec['col_targets'][col] for col in columns]
    for i, org in enumerate(organizations):
        floor, ceil = _floor_ceil(row_exact[i])
        if row_targets[i] not in (floor, ceil):
            raise ValueError(
                'row total for %s must be the floor or ceiling of its exact '
                'marginal sum' % org)
    for j, col in enumerate(columns):
        floor, ceil = _floor_ceil(col_exact[j])
        if col_targets[j] not in (floor, ceil):
            raise ValueError(
                'column total for %d/%s must be the floor or ceiling of its '
                'exact marginal sum' % (col[0], col[1]))

    row_residual = [row_targets[i] - sum(lower[cell_index(i, j)]
                                         for j in range(col_count))
                    for i in range(row_count)]
    col_residual = [col_targets[j] - sum(lower[cell_index(i, j)]
                                         for i in range(row_count))
                    for j in range(col_count)]
    row_capacity = [sum(upper[cell_index(i, j)] - lower[cell_index(i, j)]
                        for j in range(col_count))
                    for i in range(row_count)]
    col_capacity = [sum(upper[cell_index(i, j)] - lower[cell_index(i, j)]
                        for i in range(row_count))
                    for j in range(col_count)]

    # Bounds first, rows in row order and only then columns in column order.
    for i in range(row_count):
        if not 0 <= row_residual[i] <= row_capacity[i]:
            obstruction = {
                'kind': 'bounds', 'axis': 'row',
                'coordinate': {'organization': organizations[i]},
                'residual': row_residual[i], 'capacity': row_capacity[i]}
            return _infeasible_report(
                source_data, spec_data, source_digest, spec_digest, cell_keys, exact, lower, upper,
                organizations, columns, row_exact, col_exact, row_targets,
                col_targets, obstruction)
    for j in range(col_count):
        if not 0 <= col_residual[j] <= col_capacity[j]:
            obstruction = {
                'kind': 'bounds', 'axis': 'column',
                'coordinate': {'scope': columns[j][0],
                               'category': columns[j][1]},
                'residual': col_residual[j], 'capacity': col_capacity[j]}
            return _infeasible_report(
                source_data, spec_data, source_digest, spec_digest, cell_keys, exact, lower, upper,
                organizations, columns, row_exact, col_exact, row_targets,
                col_targets, obstruction)

    required = sum(row_residual)
    if required != sum(col_residual):
        obstruction = {'kind': 'balance', 'row_required': required,
                       'column_required': sum(col_residual)}
        return _infeasible_report(
            source_data, spec_data, source_digest, spec_digest, cell_keys, exact, lower, upper,
            organizations, columns, row_exact, col_exact, row_targets,
            col_targets, obstruction)

    # Flexible cells are exactly the non-integer cells without a lock; each
    # has exactly one unit of freedom between its floor and its ceiling.
    fractions = [exact[k] - lower[k] for k in range(len(cell_keys))]
    free_cells = [k for k in range(len(cell_keys)) if upper[k] > lower[k]]
    for k in free_cells:
        if upper[k] - lower[k] != 1:
            raise AssertionError('a free cell must span exactly one integer')

    # The residual problem is a bipartite flow: source -> rows (r_i),
    # row -> column via each free cell (capacity 1), columns -> sink (c_j).
    # It is rebuilt with plain integer BFS augmentations, so the number of
    # iterations is the flow value, never the kg magnitude.
    def build_graph():
        adjacency = {SOURCE: {}, SINK: {}}
        for i in range(row_count):
            adjacency['R%d' % i] = {}
        for j in range(col_count):
            adjacency['C%d' % j] = {}

        def add(u, v, capacity):
            adjacency[u][v] = adjacency[u].get(v, 0) + capacity
            adjacency[v].setdefault(u, 0)

        for i in range(row_count):
            if row_residual[i]:
                add(SOURCE, 'R%d' % i, row_residual[i])
        for j in range(col_count):
            if col_residual[j]:
                add('C%d' % j, SINK, col_residual[j])
        for k in free_cells:
            i, j = divmod(k, col_count)
            add('R%d' % i, 'C%d' % j, 1)
        return adjacency

    def augment(adjacency, limit):
        from collections import deque as _deque
        total = 0
        while total < limit:
            previous = {SOURCE: None}
            queue = _deque([SOURCE])
            while queue and SINK not in previous:
                u = queue.popleft()
                for v, room in adjacency[u].items():
                    if room > 0 and v not in previous:
                        previous[v] = u
                        queue.append(v)
            if SINK not in previous:
                break
            path = []
            node = SINK
            while node != SOURCE:
                parent = previous[node]
                path.append((parent, node))
                node = parent
            bottleneck = min(adjacency[u][v] for u, v in path)
            for u, v in path:
                adjacency[u][v] -= bottleneck
                adjacency[v][u] = adjacency[v].get(u, 0) + bottleneck
            total += bottleneck
        return total

    def reachable_nodes(adjacency):
        from collections import deque as _deque
        seen = {SOURCE}
        queue = _deque([SOURCE])
        while queue:
            u = queue.popleft()
            for v, room in adjacency[u].items():
                if room > 0 and v not in seen:
                    seen.add(v)
                    queue.append(v)
        return seen

    feasibility_graph = build_graph()
    flow_total = augment(feasibility_graph, required)
    if flow_total < required:
        reachable = reachable_nodes(feasibility_graph)
        row_in_a = {i for i in range(row_count)
                    if 'R%d' % i in reachable}
        col_in_b = {j for j in range(col_count)
                    if 'C%d' % j in reachable}
        cut_capacity = (
            sum(row_residual[i] for i in range(row_count)
                if i not in row_in_a)
            + sum(upper[cell_index(i, j)] - lower[cell_index(i, j)]
                  for i in row_in_a for j in range(col_count)
                  if j not in col_in_b)
            + sum(col_residual[j] for j in col_in_b))
        obstruction = {
            'kind': 'cut',
            'rows': [organizations[i] for i in sorted(row_in_a)],
            'columns': [{'scope': columns[j][0], 'category': columns[j][1]}
                        for j in sorted(col_in_b)],
            'required': required,
            'cut_capacity': cut_capacity,
            'deficit': required - cut_capacity}
        return _infeasible_report(
            source_data, spec_data, source_digest, spec_digest, cell_keys,
            exact, lower, upper, organizations, columns, row_exact,
            col_exact, row_targets, col_targets, obstruction)

    # One exact min-cost flow over the ordered composite per-edge cost
    # (shifted marginal error, positional unit vector).  Every source-to-sink
    # path uses exactly one cell edge, so summing path costs compares the
    # total absolute error first and, among ties, the lexicographic rounding
    # vector.  The vector is one bit per free cell (at most 10 000 bits), a
    # width fixed by the table size rather than the kg magnitudes.  The error
    # term is shifted by the constant 2D, identical on every one-cell path,
    # so every edge cost stays a non-negative integer and plain Dijkstra with
    # Johnson potentials is exact; blocking augmentations tie the iteration
    # count to the graph instead of 2^n combinations or per-unit expansion.
    from math import lcm
    common_denominator = 1
    for fraction in fractions:
        if fraction.denominator != 1:
            common_denominator = lcm(common_denominator, fraction.denominator)
    n_free = len(free_cells)
    free_edges = {k: divmod(k, col_count) for k in free_cells}
    composite_cost = {}
    for position, k in enumerate(free_cells):
        scaled = ((1 - 2 * fractions[k]) * common_denominator).numerator
        error_term = scaled + 2 * common_denominator
        composite_cost[k] = (error_term, 1 << (n_free - 1 - position))
    chosen, _, _, _ = min_cost_flow(row_residual, col_residual, free_edges,
                                    composite_cost, required)

    rounded = list(lower)
    for k, amount in chosen.items():
        rounded[k] += amount




    row_actual = [sum(rounded[cell_index(i, j)] for j in range(col_count))
                  for i in range(row_count)]
    col_actual = [sum(rounded[cell_index(i, j)] for i in range(row_count))
                  for j in range(col_count)]
    absolute_error = sum(
        (abs(Fraction(rounded[k]) - exact[k])
         for k in range(len(cell_keys))), Fraction(0))

    cells_block = [{
        'organization': cell_keys[k][0],
        'scope': cell_keys[k][1][0],
        'category': cell_keys[k][1][1],
        'exact': str(exact[k]), 'lower': lower[k], 'upper': upper[k],
        'rounded': rounded[k]} for k in range(len(cell_keys))]
    rows_block = [{
        'organization': organizations[i], 'exact': str(row_exact[i]),
        'target': row_targets[i], 'actual': row_actual[i]}
        for i in range(row_count)]
    columns_block = [{
        'scope': columns[j][0], 'category': columns[j][1],
        'exact': str(col_exact[j]), 'target': col_targets[j],
        'actual': col_actual[j]} for j in range(col_count)]

    report = {
        'schema_version': 5,
        'source_sha256': source_digest,
        'rounding_sha256': spec_digest,
        'status': 'feasible',
        'cells': cells_block,
        'rows': rows_block,
        'columns': columns_block,
        'objective': {'absolute_error': str(absolute_error),
                      'vector': list(rounded)},
        'obstruction': None,
    }
    from .round_verify import verify_rounding
    verify_rounding(source_data, spec_data, report)
    return report


def _infeasible_report(source_data, spec_data, source_digest, spec_digest,
                       cell_keys, exact, lower, upper, organizations,
                       columns, row_exact, col_exact, row_targets,
                       col_targets, obstruction):
    cells_block = [{
        'organization': cell_keys[k][0],
        'scope': cell_keys[k][1][0],
        'category': cell_keys[k][1][1],
        'exact': str(exact[k]), 'lower': lower[k], 'upper': upper[k],
        'rounded': None} for k in range(len(cell_keys))]
    rows_block = [{
        'organization': organizations[i], 'exact': str(row_exact[i]),
        'target': row_targets[i], 'actual': None}
        for i in range(len(organizations))]
    columns_block = [{
        'scope': columns[j][0], 'category': columns[j][1],
        'exact': str(col_exact[j]), 'target': col_targets[j],
        'actual': None} for j in range(len(columns))]
    report = {
        'schema_version': 5,
        'source_sha256': source_digest,
        'rounding_sha256': spec_digest,
        'status': 'infeasible',
        'cells': cells_block,
        'rows': rows_block,
        'columns': columns_block,
        'objective': None,
        'obstruction': obstruction,
    }
    from .round_verify import verify_rounding
    verify_rounding(source_data, spec_data, report)
    return report
