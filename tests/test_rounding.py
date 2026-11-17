"""Acceptance tests for the bidirectionally controlled rounding mode (v5).

The oracle below is deliberately independent of the production flow solver:
for small tables it enumerates every admissible floor/ceiling combination
(with locks fixing cells), checks the row and column targets directly and
selects the minimum exact Fraction error followed by the lexicographically
smallest rounding vector.  Every published number, including each kind of
infeasible evidence, is recomputed from the exact table and the locks.
"""
import copy
import hashlib
import itertools
import json
import random
import subprocess
import sys
import tempfile
import time
import unittest
from collections import deque
from fractions import Fraction
from pathlib import Path

from carbon_inventory.common import read
from carbon_inventory.model import parse_inventory
from carbon_inventory.round_verify import verify_rounding
from carbon_inventory.rounding import run as run_rounding

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# document builders
# ---------------------------------------------------------------------------
def factor(fid, category, unit, rate, start='2026-01-01', end=None):
    return {'id': fid, 'category': category, 'unit': unit,
            'kgco2e_per_unit': rate, 'valid_from': start, 'valid_to': end}


def activity(aid, organization, scope, category, quantity, unit, fid,
             day='2026-06-01'):
    return {'id': aid, 'organization': organization, 'date': day,
            'scope': scope, 'category': category, 'quantity': quantity,
            'unit': unit, 'factor_id': fid}


def rounding_doc(rows, columns, locks=()):
    return {'rows': [{'organization': org, 'total': total}
                     for org, total in rows],
            'columns': [{'scope': scope, 'category': category,
                         'total': total}
                        for (scope, category), total in columns],
            'locks': [dict(lock) for lock in locks]}


# ---------------------------------------------------------------------------
# independent exhaustive oracle
# ---------------------------------------------------------------------------
class Oracle:
    """Rebuilds the exact table and brute-forces every rounding vector."""

    def __init__(self, source, spec):
        factors, activities = parse_inventory(source)
        self.factors = factors
        exact = {}
        for a in activities:
            key = (a['organization'], (a['scope'], a['category']))
            exact[key] = exact.get(key, Fraction(0)) + a['quantity'] \
                * factors[a['factor_id']]['rate']
        self.organizations = sorted({k[0] for k in exact})
        self.columns = sorted({k[1] for k in exact})
        self.cell_keys = [(o, c) for o in self.organizations
                          for c in self.columns]
        self.exact = [exact.get(k, Fraction(0)) for k in self.cell_keys]
        self.floor = [e.numerator // e.denominator for e in self.exact]
        self.ceil = [f + (1 if e.numerator % e.denominator else 0)
                     for f, e in zip(self.floor, self.exact)]
        self.locks = {(l['organization'], (l['scope'], l['category'])):
                      l['rounding'] for l in spec['locks']}
        self.row_targets = {r['organization']: r['total']
                            for r in spec['rows']}
        self.col_targets = {(c['scope'], c['category']): c['total']
                            for c in spec['columns']}
        self.n_rows = len(self.organizations)
        self.n_cols = len(self.columns)

    def choices(self):
        options = []
        for k, key in enumerate(self.cell_keys):
            direction = self.locks.get(key)
            if direction == 'floor':
                options.append((self.floor[k],))
            elif direction == 'ceil':
                options.append((self.ceil[k],))
            elif self.floor[k] == self.ceil[k]:
                options.append((self.floor[k],))
            else:
                options.append((self.floor[k], self.ceil[k]))
        return options

    def solve(self):
        """Return (feasible, best_vector, best_error) by full enumeration."""
        options = self.choices()
        best_vector = None
        best_error = None
        for vector in itertools.product(*options):
            row_sums = [0] * self.n_rows
            col_sums = [0] * self.n_cols
            for k, value in enumerate(vector):
                i, j = divmod(k, self.n_cols)
                row_sums[i] += value
                col_sums[j] += value
            if any(row_sums[i] != self.row_targets[org]
                   for i, org in enumerate(self.organizations)):
                continue
            if any(col_sums[j] != self.col_targets[col]
                   for j, col in enumerate(self.columns)):
                continue
            error = sum((abs(Fraction(vector[k]) - self.exact[k])
                         for k in range(len(vector))), Fraction(0))
            if best_error is None or error < best_error or (
                    error == best_error and list(vector) < best_vector):
                best_error = error
                best_vector = list(vector)
        return best_vector is not None, best_vector, best_error

    def residual_evidence(self):
        """Recompute post-lock L/U, residuals and capacities independently."""
        lower, upper = [], []
        for k, key in enumerate(self.cell_keys):
            direction = self.locks.get(key)
            if direction == 'ceil':
                lower.append(self.ceil[k])
                upper.append(self.ceil[k])
            else:
                lower.append(self.floor[k])
                upper.append(self.ceil[k]
                             if direction is None else self.floor[k])
        row_residual, row_capacity = [], []
        for i in range(self.n_rows):
            low_sum = sum(lower[i * self.n_cols + j]
                          for j in range(self.n_cols))
            row_residual.append(
                self.row_targets[self.organizations[i]] - low_sum)
            row_capacity.append(
                sum(upper[i * self.n_cols + j]
                    - lower[i * self.n_cols + j]
                    for j in range(self.n_cols)))
        col_residual, col_capacity = [], []
        for j in range(self.n_cols):
            low_sum = sum(lower[i * self.n_cols + j]
                          for i in range(self.n_rows))
            col_residual.append(
                self.col_targets[self.columns[j]] - low_sum)
            col_capacity.append(
                sum(upper[i * self.n_cols + j]
                    - lower[i * self.n_cols + j]
                    for i in range(self.n_rows)))
        return (lower, upper, row_residual, row_capacity, col_residual,
                col_capacity)

    def flow_evidence(self):
        """Independent BFS augmenting-path flow and source-reachable cut."""
        (lower, upper, row_residual, row_capacity, col_residual,
         col_capacity) = self.residual_evidence()
        adjacency = {node: [] for node in ['S', 'T']}
        for i in range(self.n_rows):
            adjacency['R%d' % i] = []
        for j in range(self.n_cols):
            adjacency['C%d' % j] = []

        def add_edge(u, v, capacity):
            forward = {'to': v, 'cap': capacity, 'flow': 0, 'rev': None}
            reverse = {'to': u, 'cap': capacity, 'flow': 0, 'rev': None}
            forward['rev'] = reverse
            reverse['rev'] = forward
            adjacency[u].append(forward)
            adjacency[v].append(reverse)

        for i in range(self.n_rows):
            add_edge('S', 'R%d' % i, row_residual[i])
        for j in range(self.n_cols):
            add_edge('C%d' % j, 'T', col_residual[j])
        for k in range(len(self.cell_keys)):
            i, j = divmod(k, self.n_cols)
            add_edge('R%d' % i, 'C%d' % j, upper[k] - lower[k])

        total = 0
        while True:
            previous = {}
            queue = deque(['S'])
            seen = {'S'}
            while queue and 'T' not in previous:
                u = queue.popleft()
                for edge in adjacency[u]:
                    residual = edge['cap'] - edge['flow']
                    if residual > 0 and edge['to'] not in seen:
                        seen.add(edge['to'])
                        previous[edge['to']] = edge
                        queue.append(edge['to'])
            if 'T' not in previous:
                break
            path = []
            node = 'T'
            while node != 'S':
                edge = previous[node]
                path.append(edge)
                node = edge['rev']['to']
            bottleneck = min(edge['cap'] - edge['flow'] for edge in path)
            for edge in path:
                edge['flow'] += bottleneck
                edge['rev']['flow'] -= bottleneck
            total += bottleneck
        reachable = {'S'}
        queue = deque(['S'])
        while queue:
            u = queue.popleft()
            for edge in adjacency[u]:
                if edge['cap'] - edge['flow'] > 0 \
                        and edge['to'] not in reachable:
                    reachable.add(edge['to'])
                    queue.append(edge['to'])
        rows_a = {i for i in range(self.n_rows)
                  if 'R%d' % i in reachable}
        cols_b = {j for j in range(self.n_cols)
                  if 'C%d' % j in reachable}
        cut_capacity = (
            sum(row_residual[i] for i in range(self.n_rows)
                if i not in rows_a)
            + sum(upper[k] - lower[k]
                  for k in range(len(self.cell_keys))
                  if (k // self.n_cols) in rows_a
                  and (k % self.n_cols) not in cols_b)
            + sum(col_residual[j] for j in cols_b))
        return total, rows_a, cols_b, cut_capacity


# ---------------------------------------------------------------------------
# random small-table generation
# ---------------------------------------------------------------------------
RATES = ('1/1', '1/2', '1/3', '2/3', '1/7', '3/2', '0/1')


def random_instance(generator):
    n_rows = generator.randint(1, 3)
    n_cols = generator.randint(1, 3)
    organizations = ['org-%d' % i for i in range(n_rows)]
    # Distinct categories make every (scope, category) tuple unique.
    columns = sorted((generator.choice((1, 2)), 'cat-%d' % j)
                     for j in range(n_cols))
    factors = []
    for j, (scope, category) in enumerate(columns):
        factors.append(factor('f-%d' % j, category, 'u%d' % j,
                              generator.choice(RATES)))
    activities = []
    counter = itertools.count()
    exact = {(o, c): Fraction(0) for o in organizations for c in columns}
    # Guarantee a covering first: one distinct cell per column (rows cycle),
    # which also touches every row when n_cols >= n_rows; then add any of the
    # remaining pairs randomly so the Cartesian table still gets zero cells.
    required_pairs = set()
    for j in range(n_cols):
        required_pairs.add((j % n_rows, j))
    for i in range(n_rows):
        required_pairs.add((i, i % n_cols))
    for i in range(n_rows):
        for j in range(n_cols):
            if (i, j) not in required_pairs and generator.random() < 0.3:
                continue  # genuine zero cell in the Cartesian table
            org = organizations[i]
            col = columns[j]
            # quantity >= 1: rows and columns exist only where an activity
            # actually contributes.
            quantity = generator.randint(1, 3)
            activities.append(activity(
                'a-%03d' % next(counter), org, col[0], col[1], quantity,
                'u%d' % j, 'f-%d' % j))
            exact[(org, col)] += quantity * Fraction(
                factors[j]['kgco2e_per_unit'])
    row_marginals = [sum(exact[(o, c)] for c in columns)
                     for o in organizations]
    col_marginals = [sum(exact[(o, c)] for o in organizations)
                     for c in columns]

    def floor_ceil(v):
        f = v.numerator // v.denominator
        return f, f + (1 if v.numerator % v.denominator else 0)

    rows = []
    for org, marginal in zip(organizations, row_marginals):
        f, c = floor_ceil(marginal)
        rows.append((org, generator.choice((f, c))))
    cols = []
    for col, marginal in zip(columns, col_marginals):
        f, c = floor_ceil(marginal)
        cols.append((col, generator.choice((f, c))))
    locks = []
    for key in exact:
        if generator.random() < 0.25:
            locks.append({'organization': key[0], 'scope': key[1][0],
                          'category': key[1][1],
                          'rounding': generator.choice(('floor', 'ceil'))})
    source = {'factors': factors, 'activities': activities}
    return source, rounding_doc(rows, cols, locks), exact


class RoundingOracleTests(unittest.TestCase):
    def compare(self, source, spec, digest_prefix='x'):
        report = run_rounding(source, digest_prefix * 64, spec,
                              digest_prefix * 64)
        verify_rounding(source, spec, report)  # independent back-substitution
        oracle = Oracle(source, spec)
        feasible, vector, error = oracle.solve()
        if feasible:
            self.assertEqual(report['status'], 'feasible')
            self.assertEqual(report['objective']['vector'], vector)
            self.assertEqual(Fraction(report['objective']['absolute_error']),
                             error)
            self.assertEqual([c['rounded'] for c in report['cells']], vector)
            for i, org in enumerate(oracle.organizations):
                self.assertEqual(
                    report['rows'][i]['actual'],
                    sum(vector[i * oracle.n_cols + j]
                        for j in range(oracle.n_cols)))
            for j, col in enumerate(oracle.columns):
                self.assertEqual(
                    report['columns'][j]['actual'],
                    sum(vector[i * oracle.n_cols + j]
                        for i in range(oracle.n_rows)))
        else:
            self.assertEqual(report['status'], 'infeasible')
            self.assertIsNone(report['objective'])
            self.assertTrue(all(c['rounded'] is None
                                for c in report['cells']))
            self._check_evidence(oracle, report['obstruction'])
        return report

    def _check_evidence(self, oracle, obstruction):
        (lower, upper, row_residual, row_capacity, col_residual,
         col_capacity) = oracle.residual_evidence()
        row_bad = next((i for i in range(oracle.n_rows)
                        if not 0 <= row_residual[i] <= row_capacity[i]), None)
        col_bad = next((j for j in range(oracle.n_cols)
                        if not 0 <= col_residual[j] <= col_capacity[j]), None)
        if row_bad is not None:
            self.assertEqual(obstruction, {
                'kind': 'bounds', 'axis': 'row',
                'coordinate': {'organization':
                               oracle.organizations[row_bad]},
                'residual': row_residual[row_bad],
                'capacity': row_capacity[row_bad]})
            return
        if col_bad is not None:
            self.assertEqual(obstruction, {
                'kind': 'bounds', 'axis': 'column',
                'coordinate': {'scope': oracle.columns[col_bad][0],
                               'category': oracle.columns[col_bad][1]},
                'residual': col_residual[col_bad],
                'capacity': col_capacity[col_bad]})
            return
        required = sum(row_residual)
        if required != sum(col_residual):
            self.assertEqual(obstruction, {
                'kind': 'balance', 'row_required': required,
                'column_required': sum(col_residual)})
            return
        flow, rows_a, cols_b, cut_capacity = oracle.flow_evidence()
        self.assertLess(flow, required)
        self.assertEqual(obstruction['kind'], 'cut')
        self.assertEqual(obstruction['rows'],
                         [oracle.organizations[i] for i in sorted(rows_a)])
        self.assertEqual(
            obstruction['columns'],
            [{'scope': oracle.columns[j][0],
              'category': oracle.columns[j][1]}
             for j in sorted(cols_b)])
        self.assertEqual(obstruction['required'], required)
        self.assertEqual(obstruction['cut_capacity'], cut_capacity)
        self.assertEqual(obstruction['deficit'], required - cut_capacity)
        self.assertGreater(obstruction['deficit'], 0)

    def test_random_tables_against_enumeration(self):
        generator = random.Random(20261117)
        statuses = {'feasible': 0, 'infeasible': 0}
        for _ in range(300):
            source, spec, _ = random_instance(generator)
            report = self.compare(source, spec)
            statuses[report['status']] += 1
        # Both outcomes must occur across the randomized campaign.
        self.assertEqual(set(statuses), {'feasible', 'infeasible'})
        self.assertGreater(statuses['feasible'], 50)
        self.assertGreater(statuses['infeasible'], 5)

    def test_locked_random_tables_against_enumeration(self):
        generator = random.Random(987654321)
        for _ in range(120):
            source, spec, _ = random_instance(generator)
            # Force locks on every non-integer cell of a copy.
            oracle = Oracle(source, spec)
            extra = []
            for k, key in enumerate(oracle.cell_keys):
                if oracle.floor[k] != oracle.ceil[k]:
                    extra.append({'organization': key[0],
                                  'scope': key[1][0], 'category': key[1][1],
                                  'rounding': generator.choice(
                                      ('floor', 'ceil'))})
            locked_spec = copy.deepcopy(spec)
            locked_spec['locks'] = extra
            self.compare(source, locked_spec)


class SpecifiedAcceptanceTests(unittest.TestCase):
    def half_source(self):
        factors = [factor('f1', 'c1', 'u1', '1/2'),
                   factor('f2', 'c2', 'u2', '1/2')]
        activities = [
            activity('a1', 'oa', 1, 'c1', 1, 'u1', 'f1'),
            activity('a2', 'oa', 2, 'c2', 1, 'u2', 'f2'),
            activity('a3', 'ob', 1, 'c1', 1, 'u1', 'f1'),
            activity('a4', 'ob', 2, 'c2', 1, 'u2', 'f2')]
        return {'factors': factors, 'activities': activities}

    def half_spec(self, locks=()):
        return rounding_doc(
            [('oa', 1), ('ob', 1)],
            [((1, 'c1'), 1), ((2, 'c2'), 1)], locks)

    def test_all_halves_lexicographic_tie_break(self):
        report = run_rounding(self.half_source(), 's' * 64,
                              self.half_spec(), 'r' * 64)
        self.assertEqual(report['status'], 'feasible')
        self.assertEqual(report['objective']['vector'], [0, 1, 1, 0])
        self.assertEqual(report['objective']['absolute_error'], '2')

    def fraction_source(self):
        factors = [factor('f1', 'c1', 'u1', '1/10'),
                   factor('f3', 'c2', 'u2', '1/10')]
        activities = [
            activity('a1', 'oa', 1, 'c1', 9, 'u1', 'f1'),
            activity('a2', 'oa', 2, 'c2', 8, 'u2', 'f3'),
            activity('a3', 'ob', 1, 'c1', 8, 'u1', 'f1'),
            activity('a4', 'ob', 2, 'c2', 1, 'u2', 'f3')]
        return {'factors': factors, 'activities': activities}

    def fraction_spec(self, locks=()):
        return rounding_doc(
            [('oa', 1), ('ob', 1)],
            [((1, 'c1'), 1), ((2, 'c2'), 1)], locks)

    def test_fraction_matrix_beats_rowwise_greedy(self):
        report = run_rounding(self.fraction_source(), 's' * 64,
                              self.fraction_spec(), 'r' * 64)
        self.assertEqual(report['objective']['vector'], [0, 1, 1, 0])
        self.assertEqual(report['objective']['absolute_error'], '7/5')

    def test_fraction_lock_forces_diagonal_table(self):
        lock = [{'organization': 'oa', 'scope': 2, 'category': 'c2',
                 'rounding': 'floor'}]
        report = run_rounding(self.fraction_source(), 's' * 64,
                              self.fraction_spec(lock), 'r' * 64)
        self.assertEqual(report['status'], 'feasible')
        self.assertEqual(report['objective']['vector'], [1, 0, 0, 1])
        self.assertEqual(report['objective']['absolute_error'], '13/5')
        # The locked cell's bounds collapse to its floor.
        locked_cell = report['cells'][1]
        self.assertEqual(locked_cell['lower'], 0)
        self.assertEqual(locked_cell['upper'], 0)
        self.assertEqual(locked_cell['rounded'], 0)

    def test_half_table_row_floor_locks_report_bounds(self):
        locks = [
            {'organization': 'oa', 'scope': 1, 'category': 'c1',
             'rounding': 'floor'},
            {'organization': 'oa', 'scope': 2, 'category': 'c2',
             'rounding': 'floor'}]
        report = run_rounding(self.half_source(), 's' * 64,
                              self.half_spec(locks), 'r' * 64)
        self.assertEqual(report['status'], 'infeasible')
        self.assertEqual(report['obstruction'], {
            'kind': 'bounds', 'axis': 'row',
            'coordinate': {'organization': 'oa'},
            'residual': 1, 'capacity': 0})
        Oracle(self.half_source(), self.half_spec(locks)).solve()

    def test_three_by_three_cut_counterexample(self):
        factors = [factor('f1', 'c1', 'u1', '1/2'),
                   factor('f2', 'c2', 'u2', '1/2'),
                   factor('f3', 'c3', 'u3', '1/2')]
        activities = [
            activity('a1', 'r1', 1, 'c1', 1, 'u1', 'f1'),
            activity('a2', 'r2', 1, 'c1', 1, 'u1', 'f1'),
            activity('a3', 'r3', 1, 'c2', 1, 'u2', 'f2'),
            activity('a4', 'r3', 1, 'c3', 1, 'u3', 'f3')]
        source = {'factors': factors, 'activities': activities}
        spec = rounding_doc(
            [('r1', 1), ('r2', 1), ('r3', 1)],
            [((1, 'c1'), 1), ((1, 'c2'), 1), ((1, 'c3'), 1)])
        report = run_rounding(source, 's' * 64, spec, 'r' * 64)
        self.assertEqual(report['status'], 'infeasible')
        obstruction = report['obstruction']
        self.assertEqual(obstruction['kind'], 'cut')
        self.assertEqual(obstruction['rows'], ['r1', 'r2'])
        self.assertEqual(obstruction['columns'],
                         [{'scope': 1, 'category': 'c1'}])
        self.assertEqual(obstruction['required'], 3)
        self.assertEqual(obstruction['cut_capacity'], 2)
        self.assertEqual(obstruction['deficit'], 1)
        # Independently recompute every number of the published cut.
        oracle = Oracle(source, spec)
        feasible, _, _ = oracle.solve()
        self.assertFalse(feasible)
        flow, rows_a, cols_b, capacity = oracle.flow_evidence()
        self.assertEqual(flow, 2)
        self.assertEqual(rows_a, {0, 1})
        self.assertEqual(cols_b, {0})
        self.assertEqual(capacity, obstruction['cut_capacity'])
        # Zero cells must not have donated phantom capacity.
        zero_cell = next(c for c in report['cells']
                         if c['organization'] == 'r1'
                         and c['category'] == 'c2')
        self.assertEqual(zero_cell['exact'], '0')
        self.assertEqual((zero_cell['lower'], zero_cell['upper']), (0, 0))

    def test_balance_obstruction(self):
        # A single column with two half cells. Both row marginals are 1/2
        # and the column marginal is exactly 1. Taking both row ceilings
        # makes the row residual total 2 while the column (at its floor)
        # only needs 1: every single-line capacity is legal, but the sums
        # disagree, which is exactly the balance obstruction.
        factors = [factor('f1', 'c1', 'u1', '1/2')]
        activities = [
            activity('a1', 'oa', 1, 'c1', 1, 'u1', 'f1'),
            activity('a2', 'ob', 1, 'c1', 1, 'u1', 'f1')]
        source = {'factors': factors, 'activities': activities}
        spec = rounding_doc(
            [('oa', 1), ('ob', 1)],
            [((1, 'c1'), 1)])
        report = run_rounding(source, 's' * 64, spec, 'r' * 64)
        self.assertEqual(report['obstruction'], {
            'kind': 'balance', 'row_required': 2, 'column_required': 1})
        oracle = Oracle(source, spec)
        feasible, _, _ = oracle.solve()
        self.assertFalse(feasible)

    def test_column_bounds_obstruction(self):
        # Lock both c1 cells to floor: column c1 target 1 cannot be reached.
        locks = [
            {'organization': 'oa', 'scope': 1, 'category': 'c1',
             'rounding': 'floor'},
            {'organization': 'ob', 'scope': 1, 'category': 'c1',
             'rounding': 'floor'}]
        report = run_rounding(self.half_source(), 's' * 64,
                              self.half_spec(locks), 'r' * 64)
        self.assertEqual(report['status'], 'infeasible')
        self.assertEqual(report['obstruction']['kind'], 'bounds')
        self.assertEqual(report['obstruction']['axis'], 'column')
        self.assertEqual(report['obstruction']['coordinate'],
                         {'scope': 1, 'category': 'c1'})

    def test_integer_and_zero_cell_locks_are_legal(self):
        # Cell (oa, c1) is the integer 1; cell (oa, c2) is an exact zero
        # produced by a zero-quantity activity.  floor and ceil coincide for
        # both, so locking either direction is legal, including on the zero.
        factors = [factor('f1', 'c1', 'u1', '1/1'),
                   factor('f2', 'c2', 'u2', '1/2')]
        source = {'factors': factors, 'activities': [
            activity('a1', 'oa', 1, 'c1', 1, 'u1', 'f1'),
            activity('a2', 'oa', 2, 'c2', 0, 'u2', 'f2')]}
        for direction in ('floor', 'ceil'):
            spec = rounding_doc(
                [('oa', 1)], [((1, 'c1'), 1), ((2, 'c2'), 0)],
                [{'organization': 'oa', 'scope': 1, 'category': 'c1',
                  'rounding': direction},
                 {'organization': 'oa', 'scope': 2, 'category': 'c2',
                  'rounding': direction}])
            report = run_rounding(source, 's' * 64, spec, 'r' * 64)
            self.assertEqual(report['status'], 'feasible', direction)
            zero = next(c for c in report['cells']
                        if c['category'] == 'c2')
            self.assertEqual(zero['exact'], '0')
            self.assertEqual(zero['lower'], 0)
            self.assertEqual(zero['upper'], 0)
            self.assertEqual(zero['rounded'], 0)

    def test_lock_changes_feasible_outcome(self):
        # Same source, no lock vs floor lock: the optimum must be recomputed.
        unlocked = run_rounding(self.fraction_source(), 's' * 64,
                                self.fraction_spec(), 'r' * 64)
        lock = [{'organization': 'oa', 'scope': 2, 'category': 'c2',
                 'rounding': 'floor'}]
        locked = run_rounding(self.fraction_source(), 's' * 64,
                              self.fraction_spec(lock), 'r' * 64)
        self.assertNotEqual(unlocked['objective']['vector'],
                            locked['objective']['vector'])


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            'factors': [factor('f1', 'c1', 'u1', '1/2'),
                        factor('f2', 'c2', 'u2', '1/2')],
            'activities': [
                activity('a1', 'oa', 1, 'c1', 1, 'u1', 'f1'),
                activity('a2', 'ob', 2, 'c2', 1, 'u2', 'f2')]}
        self.spec = rounding_doc(
            [('oa', 1), ('ob', 0)], [((1, 'c1'), 1), ((2, 'c2'), 0)])

    def reject(self, source=None, spec=None):
        with self.assertRaises((ValueError, TypeError, KeyError)):
            run_rounding(self.source if source is None else source,
                         's' * 64,
                         self.spec if spec is None else spec, 'r' * 64)

    def test_duplicate_activity_id_rejected(self):
        source = copy.deepcopy(self.source)
        source['activities'][1]['id'] = 'a1'
        self.reject(source=source)

    def test_unreferenced_factor_still_validated(self):
        source = copy.deepcopy(self.source)
        source['factors'].append(factor('bad', 'cx', 'ux', '1/2',
                                        start='2026-02-01',
                                        end='2026-01-01'))
        self.reject(source=source)

    def test_spec_unknown_field(self):
        spec = copy.deepcopy(self.spec)
        spec['extra'] = 1
        self.reject(spec=spec)

    def test_row_entry_unknown_field(self):
        spec = copy.deepcopy(self.spec)
        spec['rows'][0]['note'] = 'x'
        self.reject(spec=spec)

    def test_boolean_totals_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec['rows'][0]['total'] = True
        self.reject(spec=spec)
        spec = copy.deepcopy(self.spec)
        spec['columns'][0]['total'] = False
        self.reject(spec=spec)

    def test_negative_and_oversized_totals_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec['rows'][0]['total'] = -1
        self.reject(spec=spec)
        spec = copy.deepcopy(self.spec)
        spec['rows'][0]['total'] = 10 ** 30 + 1
        self.reject(spec=spec)

    def test_total_boundary_10_to_30_enforced_exactly(self):
        from carbon_inventory.rounding import parse_rounding
        # Structural validation accepts the inclusive upper bound 10**30.
        spec = {'rows': [{'organization': 'oa', 'total': 10 ** 30}],
                'columns': [], 'locks': []}
        parse_rounding(spec, ['oa'], [])
        over = {'rows': [{'organization': 'oa', 'total': 10 ** 30 + 1}],
                'columns': [], 'locks': []}
        with self.assertRaises(ValueError):
            parse_rounding(over, ['oa'], [])
        # A real source cannot reach that marginal under the v1 caps, so the
        # floor/ceiling marginal check rejects it at run time.
        source = {'factors': [factor('f1', 'c1', 'u1', '1/1')],
                  'activities': [activity('a1', 'oa', 1, 'c1', 1,
                                          'u1', 'f1')]}
        bad = rounding_doc([('oa', 10 ** 30)], [((1, 'c1'), 1)])
        with self.assertRaises(ValueError):
            run_rounding(source, 's' * 64, bad, 'r' * 64)

    def test_total_outside_floor_ceiling_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec['rows'][0]['total'] = 5  # exact marginal is 1/2
        self.reject(spec=spec)

    def test_missing_and_extra_row_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec['rows'] = spec['rows'][:1]
        self.reject(spec=spec)
        spec = copy.deepcopy(self.spec)
        spec['rows'].append({'organization': 'ghost', 'total': 0})
        self.reject(spec=spec)

    def test_missing_and_extra_column_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec['columns'] = spec['columns'][:1]
        self.reject(spec=spec)
        spec = copy.deepcopy(self.spec)
        spec['columns'].append({'scope': 1, 'category': 'ghost',
                                'total': 0})
        self.reject(spec=spec)

    def test_duplicate_row_and_column_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec['rows'].append(dict(spec['rows'][0]))
        self.reject(spec=spec)
        spec = copy.deepcopy(self.spec)
        spec['columns'].append(dict(spec['columns'][0]))
        self.reject(spec=spec)

    def test_lock_unknown_coordinates_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec['locks'] = [{'organization': 'ghost', 'scope': 1,
                          'category': 'c1', 'rounding': 'floor'}]
        self.reject(spec=spec)
        spec = copy.deepcopy(self.spec)
        spec['locks'] = [{'organization': 'oa', 'scope': 1,
                          'category': 'ghost', 'rounding': 'floor'}]
        self.reject(spec=spec)

    def test_lock_bad_direction_and_duplicate_rejected(self):
        good = {'organization': 'oa', 'scope': 1, 'category': 'c1',
                'rounding': 'floor'}
        spec = copy.deepcopy(self.spec)
        spec['locks'] = [dict(good, rounding='nearest')]
        self.reject(spec=spec)
        spec = copy.deepcopy(self.spec)
        spec['locks'] = [good, dict(good, rounding='ceil')]
        self.reject(spec=spec)

    def test_empty_source_requires_empty_spec(self):
        empty = {'factors': [], 'activities': []}
        report = run_rounding(empty, 's' * 64,
                              {'rows': [], 'columns': [], 'locks': []},
                              'r' * 64)
        self.assertEqual(report['cells'], [])
        self.assertEqual(report['rows'], [])
        self.assertEqual(report['columns'], [])
        self.assertEqual(report['objective'],
                         {'absolute_error': '0', 'vector': []})
        self.reject(source=empty,
                    spec={'rows': [{'organization': 'oa', 'total': 0}],
                          'columns': [], 'locks': []})

    def test_table_size_limits(self):
        # 101 organizations exceed the 100-row cap.
        factors = [factor('f0', 'c0', 'u0', '1/2')]
        activities = [activity('a%d' % i, 'org-%03d' % i, 1, 'c0', 1,
                               'u0', 'f0') for i in range(101)]
        source = {'factors': factors, 'activities': activities}
        spec = rounding_doc(
            [('org-%03d' % i, 1) for i in range(101)],
            [((1, 'c0'), 101)])
        self.reject(source=source, spec=spec)

    def test_duplicate_json_key_rejected_at_cli_level(self):
        # The shared strict parser refuses duplicate keys before validation.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.json'
            path.write_text('{"rows": [],"rows": [],"columns": [],"locks": []}')
            with self.assertRaises(ValueError):
                read(path)

    def test_aggregation_accepts_two_activities_in_one_cell(self):
        source = {
            'factors': [factor('f1', 'c1', 'u1', '1/2')],
            'activities': [
                activity('a1', 'oa', 1, 'c1', 1, 'u1', 'f1'),
                activity('a2', 'oa', 1, 'c1', 1, 'u1', 'f1')]}
        spec = rounding_doc([('oa', 1)], [((1, 'c1'), 1)])
        report = run_rounding(source, 's' * 64, spec, 'r' * 64)
        self.assertEqual(report['cells'][0]['exact'], '1')
        self.assertEqual(report['cells'][0]['rounded'], 1)


class OrderingAndFractionTests(unittest.TestCase):
    def test_input_reordering_byte_identical(self):
        factors = [factor('f1', 'c1', 'u1', '1/3'),
                   factor('f2', 'c2', 'u2', '2/7')]
        activities = [
            activity('a1', 'oa', 1, 'c1', 2, 'u1', 'f1'),
            activity('a2', 'ob', 1, 'c1', 1, 'u1', 'f1'),
            activity('a3', 'oa', 2, 'c2', 3, 'u2', 'f2'),
            activity('a4', 'ob', 2, 'c2', 2, 'u2', 'f2')]
        spec = rounding_doc(
            [('oa', 2), ('ob', 1)],
            [((1, 'c1'), 1), ((2, 'c2'), 2)])
        first = run_rounding({'factors': factors, 'activities': activities},
                             's' * 64, spec, 'r' * 64)
        shuffled = list(reversed(activities))
        second = run_rounding(
            {'factors': list(reversed(factors)), 'activities': shuffled},
            's' * 64, spec, 'r' * 64)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))
        # Cells follow (organization, scope, category), not input order.
        self.assertEqual([(c['organization'], c['scope'], c['category'])
                          for c in first['cells']],
                         [('oa', 1, 'c1'), ('oa', 2, 'c2'),
                          ('ob', 1, 'c1'), ('ob', 2, 'c2')])

    def test_fraction_micro_differences_keep_exact_ordering(self):
        # Fractions 4999/10000 and 1/2 differ by less than 10^-4 but must be
        # distinguished exactly; the cheaper ceiling is chosen first.
        factors = [factor('f1', 'c1', 'u1', '4999/10000'),
                   factor('f2', 'c2', 'u2', '1/2')]
        activities = [
            activity('a1', 'oa', 1, 'c1', 1, 'u1', 'f1'),
            activity('a2', 'oa', 2, 'c2', 1, 'u2', 'f2'),
            activity('a3', 'ob', 1, 'c1', 1, 'u1', 'f1'),
            activity('a4', 'ob', 2, 'c2', 1, 'u2', 'f2')]
        spec = rounding_doc(
            [('oa', 1), ('ob', 1)],
            [((1, 'c1'), 1), ((2, 'c2'), 1)])
        report = run_rounding(
            {'factors': factors, 'activities': activities},
            's' * 64, spec, 'r' * 64)
        oracle = Oracle({'factors': factors, 'activities': activities}, spec)
        feasible, vector, error = oracle.solve()
        self.assertTrue(feasible)
        self.assertEqual(report['objective']['vector'], vector)
        self.assertEqual(Fraction(report['objective']['absolute_error']),
                         error)

    def test_zero_and_integer_cells_have_equal_bounds(self):
        source = {
            'factors': [factor('f1', 'c1', 'u1', '2/1'),
                        factor('f2', 'c2', 'u2', '1/2')],
            'activities': [
                activity('a1', 'oa', 1, 'c1', 2, 'u1', 'f1'),
                activity('a2', 'ob', 2, 'c2', 1, 'u2', 'f2')]}
        spec = rounding_doc(
            [('oa', 4), ('ob', 1)],
            [((1, 'c1'), 4), ((2, 'c2'), 1)])
        report = run_rounding(source, 's' * 64, spec, 'r' * 64)
        integer_cell = report['cells'][0]
        zero_cell = report['cells'][1]
        self.assertEqual(integer_cell['lower'], integer_cell['upper'])
        self.assertEqual(integer_cell['rounded'], 4)
        self.assertEqual(zero_cell['exact'], '0')
        self.assertEqual(zero_cell['lower'], zero_cell['upper'])
        self.assertEqual(zero_cell['rounded'], 0)


class LargeScaleTests(unittest.TestCase):
    def test_ten_thousand_cells_with_trillion_integer_parts(self):
        n_rows = n_cols = 100
        # Each column has two factors: a third-valued one and an integer one.
        # 10**12 is divisible by 2 but not by 3, so quantity * 1/3 keeps a
        # non-trivial fraction while carrying a 10**12-scale integer part.
        # The fractional factor fills a checkerboard; integer factors fill
        # the rest so every row and column has a routable one-unit residual.
        factors = []
        for j in range(n_cols):
            scope = 1 if j < 50 else 2
            category = 'cat-%03d' % j
            factors.append(factor('h-%03d' % j, category, 'h%03d' % j,
                                  '1/3'))
            factors.append(factor('w-%03d' % j, category, 'w%03d' % j,
                                  '1/1'))
        rate_by_factor = {f['id']: Fraction(f['kgco2e_per_unit'])
                          for f in factors}
        activities = []
        aid = 0
        for i in range(n_rows):
            org = 'org-%03d' % i
            for j in range(n_cols):
                scope, category = 1 if j < 50 else 2, 'cat-%03d' % j
                if (i + j) % 2 == 0:
                    fid, unit = 'h-%03d' % j, 'h%03d' % j
                else:
                    fid, unit = 'w-%03d' % j, 'w%03d' % j
                activities.append(activity(
                    'a-%05d' % aid, org, scope, category, 10 ** 12,
                    unit, fid))
                aid += 1
        self.assertEqual(aid, 10_000)
        source = {'factors': factors, 'activities': activities}
        exact = {}
        for a in activities:
            key = (a['organization'], (a['scope'], a['category']))
            exact[key] = exact.get(key, Fraction(0)) + a['quantity'] \
                * rate_by_factor[a['factor_id']]
        fractional = sum(1 for value in exact.values()
                         if value.denominator != 1)
        self.assertEqual(fractional, 5000)
        orgs = sorted({k[0] for k in exact})
        cols = sorted({k[1] for k in exact})
        rows, columns = [], []
        for i, org in enumerate(orgs):
            marginal = sum(exact[(org, c)] for c in cols)
            floor = marginal.numerator // marginal.denominator
            rows.append((org, floor + (1 if i < 50 else 0)))
        for j, col in enumerate(cols):
            marginal = sum(exact[(o, col)] for o in orgs)
            floor = marginal.numerator // marginal.denominator
            columns.append((col, floor + (1 if j < 50 else 0)))
        spec = rounding_doc(rows, columns)

        started = time.monotonic()
        report = run_rounding(source, 's' * 64, spec, 'r' * 64)
        elapsed = time.monotonic() - started
        # The 10 000-cell exact solve (plus independent back-substitution)
        # must finish in seconds: flow iterations follow the graph, never the
        # 10**12 kg magnitudes or a 2**5000 combination space.
        self.assertLess(elapsed, 45)
        self.assertEqual(len(report['cells']), 10_000)
        self.assertEqual(report['status'], 'feasible')

        # Independent recomputation of every bound, marginal and the error.
        vector = report['objective']['vector']
        self.assertEqual(len(vector), 10_000)
        error = Fraction(0)
        for k, cell in enumerate(report['cells']):
            value = exact[(cell['organization'],
                           (cell['scope'], cell['category']))]
            self.assertEqual(Fraction(cell['exact']), value)
            floor = value.numerator // value.denominator
            ceil = floor + (1 if value.numerator % value.denominator else 0)
            self.assertEqual(cell['lower'], floor)
            self.assertEqual(cell['upper'], ceil)
            self.assertIn(vector[k], (floor, ceil))
            self.assertEqual(cell['rounded'], vector[k])
            error += abs(Fraction(vector[k]) - value)
        self.assertEqual(Fraction(report['objective']['absolute_error']),
                         error)
        for i, org in enumerate(orgs):
            self.assertEqual(sum(vector[i * n_cols + j]
                                 for j in range(n_cols)),
                             dict(rows)[org])
        for j, col in enumerate(cols):
            self.assertEqual(sum(vector[i * n_cols + j]
                                 for i in range(n_rows)),
                             dict(columns)[col])
        verify_rounding(source, spec, report)


class CommandLineTests(unittest.TestCase):
    def call(self, *arguments):
        return subprocess.run(
            [sys.executable, '-m', 'carbon_inventory.cli',
             *map(str, arguments)],
            capture_output=True, text=True,
            env={'PYTHONPATH': str(ROOT / 'src'), 'PATH': '/usr/bin:/bin'})

    def test_demo_fixtures_and_byte_determinism(self):
        cases = [
            ('half-input.json', 'half-rounding.json', 'feasible',
             [0, 1, 1, 0]),
            ('fraction-input.json', 'fraction-rounding.json', 'feasible',
             [0, 1, 1, 0]),
            ('fraction-input.json', 'fraction-locked.json', 'feasible',
             [1, 0, 0, 1]),
            ('cut-input.json', 'cut-rounding.json', 'infeasible', None),
            ('half-input.json', 'half-locked-bounds.json', 'infeasible',
             None),
            ('grid-input.json', 'grid-rounding.json', 'feasible', None)]
        with tempfile.TemporaryDirectory() as directory:
            for source_name, spec_name, status, vector in cases:
                out = Path(directory) / (spec_name + '.json')
                args = ('--source',
                        ROOT / 'fixtures/rounding' / source_name,
                        '--rounding',
                        ROOT / 'fixtures/rounding' / spec_name,
                        '--output', out)
                first = self.call(*args)
                self.assertEqual(first.returncode, 0, first.stderr)
                payload = out.read_bytes()
                document = json.loads(payload)
                self.assertEqual(document['schema_version'], 5)
                self.assertEqual(document['status'], status)
                if vector is not None:
                    self.assertEqual(document['objective']['vector'], vector)
                second = self.call(*args)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(out.read_bytes(), payload)
                source_digest = hashlib.sha256(
                    (ROOT / 'fixtures/rounding' / source_name).read_bytes()
                ).hexdigest()
                spec_digest = hashlib.sha256(
                    (ROOT / 'fixtures/rounding' / spec_name).read_bytes()
                ).hexdigest()
                self.assertEqual(document['source_sha256'], source_digest)
                self.assertEqual(document['rounding_sha256'], spec_digest)

    def test_infeasible_exits_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'out.json'
            result = self.call(
                '--source', ROOT / 'fixtures/rounding/cut-input.json',
                '--rounding', ROOT / 'fixtures/rounding/cut-rounding.json',
                '--output', out)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(out.read_bytes())['status'],
                             'infeasible')

    def test_mode_mutual_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'out.json'
            src = ROOT / 'fixtures/rounding/half-input.json'
            spec = ROOT / 'fixtures/rounding/half-rounding.json'
            boundary = ROOT / 'fixtures/group-boundary.json'
            batch = ROOT / 'fixtures/registry/batch-01-foundation.json'
            registry = Path(directory) / 'ledger.db'
            uncertainty = ROOT / 'fixtures/audit/uncertainty.json'
            for arguments in (
                    ('--rounding', spec, '--output', out),
                    ('--source', src, '--rounding', spec,
                     '--registry', registry, '--output', out),
                    ('--source', src, '--rounding', spec,
                     '--batch', batch, '--output', out),
                    ('--source', src, '--rounding', spec,
                     '--boundary', boundary, '--root', 'group-hq',
                     '--output', out),
                    ('--source', src, '--rounding', spec,
                     '--uncertainty', uncertainty, '--output', out),
                    ('--registry', registry, '--rounding', spec,
                     '--output', out)):
                result = self.call(*arguments)
                self.assertEqual(result.returncode, 2, arguments)
                self.assertFalse(out.exists())

    def test_distinct_paths_and_safe_output(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            out = directory / 'out.json'
            src = directory / 'in.json'
            src.write_bytes(
                (ROOT / 'fixtures/rounding/half-input.json').read_bytes())
            spec = directory / 'spec.json'
            spec.write_bytes(
                (ROOT / 'fixtures/rounding/half-rounding.json').read_bytes())
            self.assertEqual(self.call('--source', src, '--rounding', src,
                                       '--output', out).returncode, 2)
            self.assertEqual(self.call('--source', src, '--rounding', spec,
                                       '--output', src).returncode, 2)
            self.assertEqual(self.call('--source', src, '--rounding', spec,
                                       '--output', spec).returncode, 2)
            link = directory / 'link.json'
            link.symlink_to(src)
            self.assertEqual(self.call('--source', src, '--rounding', spec,
                                       '--output', link).returncode, 2)
            self.assertTrue(src.exists())

    def test_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            out = directory / 'out.json'
            out.write_text('prior')
            src = directory / 'in.json'
            src.write_bytes(
                (ROOT / 'fixtures/rounding/half-input.json').read_bytes())
            for body in ('{}', '{"rows": [],"rows": [],"columns": [],"locks": []}',
                         'NaN',
                         json.dumps({'rows': [{'organization': 'oa',
                                               'total': True}],
                                     'columns': [], 'locks': []})):
                bad = directory / 'bad.json'
                bad.write_text(body)
                result = self.call('--source', src, '--rounding', bad,
                                   '--output', out)
                self.assertEqual(result.returncode, 2, body)
                self.assertEqual(out.read_text(), 'prior')

    def test_legacy_modes_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'legacy.json'
            result = self.call('--source', ROOT / 'fixtures/input.json',
                               '--output', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(out.read_bytes())
            self.assertEqual(document['schema_version'], 1)
            self.assertEqual([(t['scope'], t['kgco2e'])
                              for t in document['result']['totals']],
                             [(1, '190'), (2, '400')])


if __name__ == '__main__':
    unittest.main()
