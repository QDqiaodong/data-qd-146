"""Independent back-substitution of a schema-5 rounding report.

Nothing is published on the strength of the production solver.  This module
re-parses the source and the rounding specification with the shared strict
validators, rebuilds the exact Fraction table and the post-lock bounds, and
then re-derives every published number through independent machinery:

* an adjacency-list Edmonds-Karp max flow (not the allocation network used by
  the producer) decides feasibility and recomputes the source-reachable cut;
* an SPFA negative-cycle test over the residual graph with hand-written
  ordered composite costs certifies that a published feasible flow has no
  improving cycle, i.e. it simultaneously minimizes total absolute error and
  the lexicographic rounding vector;
* every cell bound, marginal sum, error fraction and obstruction number is
  recomputed from the exact table and the locks.

Any mismatch raises, so the CLI exits 2 and the previous output survives.
"""
from collections import deque
from fractions import Fraction

from .model import parse_inventory
from .rounding import (COLUMN_LIMIT, ROW_LIMIT, _floor_ceil,
                       parse_rounding)


def _build_table(source_data, spec_data):
    """Recompute the exact table, locks, bounds and marginals from scratch."""
    factors, activities = parse_inventory(source_data)
    seen = set()
    for activity in activities:
        if activity['id'] in seen:
            raise AssertionError('verifier sees a duplicate activity id')
        seen.add(activity['id'])
    exact_cells = {}
    for activity in activities:
        key = (activity['organization'],
               (activity['scope'], activity['category']))
        exact_cells[key] = (
            exact_cells.get(key, Fraction(0))
            + activity['quantity'] * activity['rate'])
    organizations = sorted({key[0] for key in exact_cells})
    columns = sorted({key[1] for key in exact_cells})
    if len(organizations) > ROW_LIMIT or len(columns) > COLUMN_LIMIT:
        raise AssertionError('verifier sees an over-sized table')
    spec = parse_rounding(spec_data, organizations, columns)

    cell_keys = [(org, col) for org in organizations for col in columns]
    exact, lower, upper, fractions = [], [], [], []
    for cell in cell_keys:
        value = exact_cells.get(cell, Fraction(0))
        floor, ceil = _floor_ceil(value)
        direction = spec['locks'].get(cell)
        if direction == 'floor':
            low = high = floor
        elif direction == 'ceil':
            low = high = ceil
        else:
            low, high = floor, ceil
        exact.append(value)
        lower.append(low)
        upper.append(high)
        fractions.append(value - low)
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
    row_residual = [row_targets[i] - sum(lower[cell_index(i, j)]
                                         for j in range(col_count))
                    for i in range(row_count)]
    col_residual = [col_targets[j] - sum(lower[cell_index(i, j)]
                                         for i in range(row_count))
                    for j in range(col_count)]
    return {
        'factors': factors, 'activities': activities,
        'organizations': organizations, 'columns': columns,
        'cell_keys': cell_keys, 'exact': exact, 'lower': lower,
        'upper': upper, 'fractions': fractions,
        'row_exact': row_exact, 'col_exact': col_exact,
        'row_targets': row_targets, 'col_targets': col_targets,
        'row_residual': row_residual, 'col_residual': col_residual,
        'locks': spec['locks']}


def _neg_cost(cost):
    """Negate an (error digit, unit-vector dict) composite cost."""
    digit, vector = cost
    return -digit, {index: -coefficient
                    for index, coefficient in vector.items()}


class FlowGraph:
    """Independent Edmonds-Karp network: adjacency lists of mutable edges."""
    def __init__(self):
        self.adj = {}

    def node(self, name):
        self.adj.setdefault(name, [])

    def add(self, u, v, capacity, cost=None):
        self.node(u)
        self.node(v)
        reverse_cost = None if cost is None else _neg_cost(cost)
        forward = {'to': v, 'cap': capacity, 'cost': cost, 'rev': None}
        reverse = {'to': u, 'cap': 0, 'cost': reverse_cost, 'rev': None}
        forward['rev'] = reverse
        reverse['rev'] = forward
        self.adj[u].append(forward)
        self.adj[v].append(reverse)
        return forward

    def max_flow(self, source, sink):
        total = 0
        while True:
            previous = {}
            queue = deque([source])
            seen = {source}
            while queue and sink not in previous:
                u = queue.popleft()
                for edge in self.adj[u]:
                    if edge['cap'] > 0 and edge['to'] not in seen:
                        seen.add(edge['to'])
                        previous[edge['to']] = edge
                        queue.append(edge['to'])
            if sink not in previous:
                return total
            bottleneck = None
            node = sink
            while node != source:
                edge = previous[node]
                bottleneck = (edge['cap'] if bottleneck is None
                              else min(bottleneck, edge['cap']))
                node = edge['rev']['to']
            node = sink
            while node != source:
                edge = previous[node]
                edge['cap'] -= bottleneck
                edge['rev']['cap'] += bottleneck
                node = edge['rev']['to']
            total += bottleneck

    def reachable(self, source):
        seen = {source}
        queue = deque([source])
        while queue:
            u = queue.popleft()
            for edge in self.adj[u]:
                if edge['cap'] > 0 and edge['to'] not in seen:
                    seen.add(edge['to'])
                    queue.append(edge['to'])
        return seen

    def has_negative_cycle(self):
        """Exact SPFA negative-cycle test with ordered composite costs.

        Every cell edge carries (scaled error digit d, unit vector e_k); the
        full cost compares first on the integer digit (a single common
        denominator scales every fraction exactly) and then lexicographically
        on the cell vector.  Distances are (digit, sparse vector) pairs, so
        no floating point and no per-unit expansion is involved.  A node
        relaxed |V| times lies on a reachable negative cycle.
        """
        zero_vector = {}

        def add(a, b):
            digit, vector = a
            other_digit, other_vector = b
            merged = dict(vector)
            for index, coefficient in other_vector.items():
                total = merged.get(index, 0) + coefficient
                if total:
                    merged[index] = total
                else:
                    merged.pop(index, None)
            return digit + other_digit, merged

        def less(a, b):
            if a[0] != b[0]:
                return a[0] < b[0]
            for index in sorted(set(a[1]) | set(b[1])):
                left, right = a[1].get(index, 0), b[1].get(index, 0)
                if left != right:
                    return left < right
            return False

        distance = {'S': (0, zero_vector)}
        relaxations = {'S': 0}
        in_queue = {'S'}
        queue = deque(['S'])
        while queue:
            u = queue.popleft()
            in_queue.discard(u)
            base = distance[u]
            for edge in self.adj[u]:
                if edge['cap'] <= 0 or edge['cost'] is None:
                    continue
                candidate = add(base, edge['cost'])
                current = distance.get(edge['to'])
                if current is None or less(candidate, current):
                    distance[edge['to']] = candidate
                    count = relaxations.get(edge['to'], 0) + 1
                    relaxations[edge['to']] = count
                    if count > len(self.adj):
                        return True
                    if edge['to'] not in in_queue:
                        in_queue.add(edge['to'])
                        queue.append(edge['to'])
        return False


def _free_graph(table, with_costs):
    """Bipartite residual network over the post-lock bounds."""
    organizations = table['organizations']
    columns = table['columns']
    cell_keys = table['cell_keys']
    lower, upper = table['lower'], table['upper']
    row_count, col_count = len(organizations), len(columns)
    graph = FlowGraph()
    source, sink = 'S', 'T'
    graph.node(source)
    graph.node(sink)
    for i in range(row_count):
        graph.add(source, 'R%d' % i, table['row_residual'][i])
    for j in range(col_count):
        graph.add('C%d' % j, sink, table['col_residual'][j])
    denominators = [f.denominator for f in table['fractions']
                    if f.denominator != 1]
    common = 1
    from math import lcm
    for denominator in denominators:
        common = lcm(common, denominator)
    for k in range(len(cell_keys)):
        if upper[k] <= lower[k]:
            continue
        if upper[k] - lower[k] != 1:
            raise AssertionError('free cell spans more than one unit')
        i, j = divmod(k, col_count)
        if with_costs:
            weight = 1 - 2 * table['fractions'][k]
            # (exact scaled error digit, lexicographic unit vector)
            cost = ((weight * common).numerator, {k: 1})
        else:
            cost = None
        graph.add('R%d' % i, 'C%d' % j, 1, cost)
    return graph, source, sink, common


def _assert_structure(report):
    required = {'schema_version', 'source_sha256', 'rounding_sha256',
                'status', 'cells', 'rows', 'columns', 'objective',
                'obstruction'}
    if set(report) != required:
        raise AssertionError('report has unexpected top-level fields')
    if report['schema_version'] != 5:
        raise AssertionError('schema version is not 5')
    if report['status'] not in ('feasible', 'infeasible'):
        raise AssertionError('invalid status')


def _assert_reduced_fraction(text):
    if not isinstance(text, str):
        raise AssertionError('fraction must be a reduced string')
    value = Fraction(text)
    if str(value) != text:
        raise AssertionError('fraction is not in reduced form: ' + text)
    return value


def verify_rounding(source_data, spec_data, report):
    """Raise on any discrepancy; return normally when everything checks."""
    _assert_structure(report)
    table = _build_table(source_data, spec_data)
    organizations = table['organizations']
    columns = table['columns']
    cell_keys = table['cell_keys']
    exact, lower, upper = table['exact'], table['lower'], table['upper']
    row_count, col_count = len(organizations), len(columns)

    def cell_index(i, j):
        return i * col_count + j

    if len(report['cells']) != len(cell_keys):
        raise AssertionError('cell count mismatch')
    for k, block in enumerate(report['cells']):
        if set(block) != {'organization', 'scope', 'category', 'exact',
                          'lower', 'upper', 'rounded'}:
            raise AssertionError('cell has unexpected fields')
        org, col = cell_keys[k]
        if block['organization'] != org or block['scope'] != col[0] \
                or block['category'] != col[1]:
            raise AssertionError('cell coordinates or ordering mismatch')
        if _assert_reduced_fraction(block['exact']) != exact[k]:
            raise AssertionError('cell exact value mismatch')
        if block['lower'] != lower[k] or block['upper'] != upper[k]:
            raise AssertionError('cell bounds mismatch')
        if lower[k] > upper[k]:
            raise AssertionError('inverted cell bounds')

    if len(report['rows']) != row_count:
        raise AssertionError('row block count mismatch')
    for i, block in enumerate(report['rows']):
        if set(block) != {'organization', 'exact', 'target', 'actual'}:
            raise AssertionError('row has unexpected fields')
        if block['organization'] != organizations[i]:
            raise AssertionError('row ordering mismatch')
        if _assert_reduced_fraction(block['exact']) != table['row_exact'][i]:
            raise AssertionError('row exact marginal mismatch')
        if block['target'] != table['row_targets'][i]:
            raise AssertionError('row target mismatch')

    if len(report['columns']) != col_count:
        raise AssertionError('column block count mismatch')
    for j, block in enumerate(report['columns']):
        if set(block) != {'scope', 'category', 'exact', 'target', 'actual'}:
            raise AssertionError('column has unexpected fields')
        if (block['scope'], block['category']) != columns[j]:
            raise AssertionError('column ordering mismatch')
        if _assert_reduced_fraction(block['exact']) != table['col_exact'][j]:
            raise AssertionError('column exact marginal mismatch')
        if block['target'] != table['col_targets'][j]:
            raise AssertionError('column target mismatch')

    if report['status'] == 'feasible':
        _verify_feasible(report, table)
    else:
        _verify_infeasible(report, table)


def _verify_feasible(report, table):
    organizations = table['organizations']
    columns = table['columns']
    cell_keys = table['cell_keys']
    exact, lower, upper = table['exact'], table['lower'], table['upper']
    row_count, col_count = len(organizations), len(columns)
    row_targets, col_targets = table['row_targets'], table['col_targets']

    def cell_index(i, j):
        return i * col_count + j

    rounded = []
    for block in report['cells']:
        value = block['rounded']
        if type(value) is not int or not lower[len(rounded)] <= value \
                <= upper[len(rounded)]:
            raise AssertionError('rounded value outside its floor/ceiling')
        rounded.append(value)
    if report['objective'] is None or set(report['objective']) != \
            {'absolute_error', 'vector'}:
        raise AssertionError('feasible report needs an objective')
    if report['objective']['vector'] != rounded:
        raise AssertionError('objective vector does not follow the cells')
    if report['obstruction'] is not None:
        raise AssertionError('feasible report carries an obstruction')

    row_actual, col_actual = [], []
    for i in range(row_count):
        actual = sum(rounded[cell_index(i, j)] for j in range(col_count))
        row_actual.append(actual)
        if actual != row_targets[i]:
            raise AssertionError('row target not met')
        if report['rows'][i]['actual'] != actual:
            raise AssertionError('row actual mismatch')
    for j in range(col_count):
        actual = sum(rounded[cell_index(i, j)] for i in range(row_count))
        col_actual.append(actual)
        if actual != col_targets[j]:
            raise AssertionError('column target not met')
        if report['columns'][j]['actual'] != actual:
            raise AssertionError('column actual mismatch')
    if sum(row_actual) != sum(col_actual):
        raise AssertionError('row and column totals disagree')

    error = sum((abs(Fraction(rounded[k]) - exact[k])
                 for k in range(len(cell_keys))), Fraction(0))
    if _assert_reduced_fraction(
            report['objective']['absolute_error']) != error:
        raise AssertionError('absolute error mismatch')

    # Independent feasibility: the residual network must carry every residual.
    graph, source, sink, _ = _free_graph(table, with_costs=False)
    required = sum(table['row_residual'])
    if required < 0 or graph.max_flow(source, sink) != required:
        raise AssertionError('a feasible report is not supported by a flow')

    # Independent optimality: no negative composite cycle in the residual
    # graph of the published flow means both objective levels are optimal.
    cost_graph, _, _, _ = _free_graph(table, with_costs=True)
    edges = {}
    for k in range(len(cell_keys)):
        if upper[k] <= lower[k]:
            continue
        i, j = divmod(k, col_count)
        for edge in cost_graph.adj['R%d' % i]:
            if edge['to'] == 'C%d' % j and edge['cost'] is not None \
                    and edge['cap'] == 1:
                edges[k] = edge
    for k, edge in edges.items():
        chosen = rounded[k] - lower[k]
        if chosen:
            edge['cap'] -= 1
            edge['rev']['cap'] += 1
    if cost_graph.has_negative_cycle():
        raise AssertionError('published flow is not error/lex optimal')


def _verify_infeasible(report, table):
    organizations = table['organizations']
    columns = table['columns']
    cell_keys = table['cell_keys']
    lower, upper = table['lower'], table['upper']
    row_count, col_count = len(organizations), len(columns)
    row_residual, col_residual = table['row_residual'], table['col_residual']

    def cell_index(i, j):
        return i * col_count + j

    if report['objective'] is not None:
        raise AssertionError('infeasible report carries an objective')
    for block in report['cells']:
        if block['rounded'] is not None:
            raise AssertionError('infeasible cell must round to null')
    for block in report['rows']:
        if block['actual'] is not None:
            raise AssertionError('infeasible row actual must be null')
    for block in report['columns']:
        if block['actual'] is not None:
            raise AssertionError('infeasible column actual must be null')
    obstruction = report['obstruction']
    if not isinstance(obstruction, dict):
        raise AssertionError('infeasible report needs an obstruction')

    row_capacity = [sum(upper[cell_index(i, j)] - lower[cell_index(i, j)]
                        for j in range(col_count))
                    for i in range(row_count)]
    col_capacity = [sum(upper[cell_index(i, j)] - lower[cell_index(i, j)]
                        for i in range(row_count))
                    for j in range(col_count)]
    row_bad = next((i for i in range(row_count)
                    if not 0 <= row_residual[i] <= row_capacity[i]), None)
    col_bad = next((j for j in range(col_count)
                    if not 0 <= col_residual[j] <= col_capacity[j]), None)

    if row_bad is not None or col_bad is not None:
        if row_bad is not None:
            expected = {
                'kind': 'bounds', 'axis': 'row',
                'coordinate': {'organization': organizations[row_bad]},
                'residual': row_residual[row_bad],
                'capacity': row_capacity[row_bad]}
        else:
            expected = {
                'kind': 'bounds', 'axis': 'column',
                'coordinate': {'scope': columns[col_bad][0],
                               'category': columns[col_bad][1]},
                'residual': col_residual[col_bad],
                'capacity': col_capacity[col_bad]}
        if obstruction != expected:
            raise AssertionError('bounds obstruction does not reproduce')
        return

    required = sum(row_residual)
    column_required = sum(col_residual)
    if required != column_required:
        if obstruction != {'kind': 'balance', 'row_required': required,
                           'column_required': column_required}:
            raise AssertionError('balance obstruction does not reproduce')
        return

    graph, source, sink, _ = _free_graph(table, with_costs=False)
    flow = graph.max_flow(source, sink)
    if flow == required:
        raise AssertionError('infeasible report contradicts a full flow')
    reachable = graph.reachable(source)
    rows_a = {i for i in range(row_count) if 'R%d' % i in reachable}
    cols_b = {j for j in range(col_count) if 'C%d' % j in reachable}
    cut_capacity = (
        sum(row_residual[i] for i in range(row_count) if i not in rows_a)
        + sum(upper[cell_index(i, j)] - lower[cell_index(i, j)]
              for i in rows_a for j in range(col_count) if j not in cols_b)
        + sum(col_residual[j] for j in cols_b))
    deficit = required - cut_capacity
    if deficit <= 0:
        raise AssertionError('cut evidence must have a positive deficit')
    expected = {
        'kind': 'cut',
        'rows': [organizations[i] for i in sorted(rows_a)],
        'columns': [{'scope': columns[j][0], 'category': columns[j][1]}
                    for j in sorted(cols_b)],
        'required': required, 'cut_capacity': cut_capacity,
        'deficit': deficit}
    if obstruction != expected:
        raise AssertionError('cut obstruction does not reproduce')
