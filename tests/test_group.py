"""Acceptance tests for dated ownership consolidation (group mode)."""
import itertools
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path

from carbon_inventory.boundary import parse_boundary
from carbon_inventory.consolidation import attribute, consolidate

ROOT = Path(__file__).resolve().parents[1]


def rel(parent, child, share='1/2', control=False, start='2026-01-01',
        end=None):
    return {'parent': parent, 'child': child, 'share': share,
            'operational_control': control, 'valid_from': start,
            'valid_to': end}


def boundary(*relationships):
    return {'relationships': list(relationships)}


def act(aid, org, day='2026-06-01', scope=1, kg='100'):
    return {'id': aid, 'organization': org, 'date': day,
            'scope': scope, 'kgco2e': kg}


class BoundaryValidationTests(unittest.TestCase):
    def reject(self, document):
        with self.assertRaises((ValueError, TypeError)):
            parse_boundary(document)

    def test_document_is_strictly_relationships(self):
        self.reject({})
        self.reject({'relationships': [], 'extra': 1})
        self.reject([])
        self.reject({'relationships': {}})
        self.reject({'relationships': [rel('A', 'B')], 'extra': []})
        self.assertEqual(parse_boundary({'relationships': []}).endpoints, ())

    def test_relationship_fields_are_strict(self):
        for bad in (
                {'parent': 'A', 'child': 'B', 'share': '1/2',
                 'operational_control': True, 'valid_from': '2026-01-01'},
                dict(rel('A', 'B'), extra=1),
                dict(rel('A', 'B'), parent=''),
                dict(rel('A', 'B'), child=' B'),
                dict(rel('A', 'B'), share='1/2', operational_control=1),
                dict(rel('A', 'B'), share='1/2', operational_control=0),
                dict(rel('A', 'B'), share='1/2', operational_control='true'),
                dict(rel('A', 'B'), share='1/2', operational_control=None),
                dict(rel('A', 'B'), valid_from='2026-02-30'),
                dict(rel('A', 'B'), valid_from=None),
                dict(rel('A', 'B'), valid_to='not-a-date'),
        ):
            self.reject({'relationships': [bad]})

    def test_share_is_nonnegative_rational_not_greater_than_one(self):
        for good in ('0/1', '1/2', '2/4', '1/1', '000/7'):
            parse_boundary(boundary(rel('A', 'B', share=good)))
        for bad in ('1', '1/0', '-1/2', '2/1', '7/3', '0.5', '1 / 2', '',
                    '1/-2', 'abc/2'):
            self.reject(boundary(rel('A', 'B', share=bad)))
        self.reject(boundary(rel('A', 'B', share=3)))

    def test_interval_is_half_open_and_nonempty(self):
        self.reject(boundary(rel('A', 'B', start='2026-05-01',
                                end='2026-05-01')))
        self.reject(boundary(rel('A', 'B', start='2026-05-02',
                                end='2026-05-01')))
        index = parse_boundary(boundary(rel('A', 'B', start='2026-05-01',
                                          end='2026-05-02')))
        self.assertEqual([d.isoformat() for d in index.endpoints],
                         ['2026-05-01', '2026-05-02'])

    def test_limits(self):
        # More than 10000 organizations (5001 disjoint relationships).
        self.reject(boundary(*[rel('p%d' % i, 'c%d' % i, share='1/1')
                               for i in range(5001)]))
        # Organization count at the limit is accepted.
        parse_boundary(boundary(*[rel('p%d' % i, 'c%d' % i, share='1/1')
                                  for i in range(5000)]))
        # The relationship array itself is bounded at 50000.
        self.reject({'relationships': [rel('A', 'B')] * 50001})

    def test_same_pair_may_not_overlap(self):
        self.reject(boundary(
            rel('A', 'B', start='2026-01-01', end='2026-03-01'),
            rel('A', 'B', start='2026-02-01', end='2026-04-01')))
        self.reject(boundary(rel('A', 'B'), rel('A', 'B')))
        self.reject(boundary(
            rel('A', 'B', start='2026-01-01', end=None),
            rel('A', 'B', start='2027-01-01', end=None)))
        # Touching half-open intervals are not an overlap.
        parse_boundary(boundary(
            rel('A', 'B', start='2026-01-01', end='2026-03-01'),
            rel('A', 'B', start='2026-03-01', end='2026-04-01')))

    def test_share_sum_checked_on_every_date(self):
        self.reject(boundary(rel('A', 'C', '2/3'), rel('B', 'C', '2/3')))
        parse_boundary(boundary(rel('A', 'C', '2/3'), rel('B', 'C', '1/3')))
        # The violation exists only in an interval without any activity.
        self.reject(boundary(
            rel('A', 'C', '3/5', start='2026-01-01', end='2026-02-01'),
            rel('B', 'C', '1/2', start='2026-01-15', end='2026-03-01')))
        # Expiry on the same date restores a legal graph that day.
        parse_boundary(boundary(
            rel('A', 'C', '3/5', start='2026-01-01', end='2026-02-01'),
            rel('B', 'C', '1/2', start='2026-02-01', end='2026-03-01')))

    def test_cycles_rejected_on_the_complete_timeline(self):
        self.reject(boundary(rel('A', 'B'), rel('B', 'A')))
        self.reject(boundary(rel('A', 'A', '0/1')))
        self.reject(boundary(rel('A', 'A')))
        self.reject(boundary(rel('A', 'B'), rel('B', 'C'), rel('C', 'A')))
        # No activity ever occurs in the short cycle interval, but the
        # timeline still contains a valid graph with a cycle.
        self.reject(boundary(
            rel('A', 'B', start='2026-01-01', end='2026-01-10'),
            rel('B', 'C', start='2026-01-01', end='2026-01-10'),
            rel('C', 'A', start='2026-01-03', end='2026-01-04')))
        # Cycles in a long chain inserted in reverse order.
        n = 500
        chain = [rel('o%d' % (i + 1), 'o%d' % i, share='1/3')
                 for i in range(n)]
        parse_boundary(boundary(*chain))
        chain.append(rel('o0', 'o%d' % n, share='1/3'))
        self.reject(boundary(*chain))
        # Overlapping in time is what matters, not mere adjacency.
        parse_boundary(boundary(
            rel('A', 'B', start='2026-01-01', end='2026-01-05'),
            rel('B', 'A', start='2026-01-05', end='2026-01-06')))

    def test_random_small_graphs_match_brute_force(self):
        random.seed(7)
        names = ['A', 'B', 'C', 'D']
        for _ in range(120):
            rows = []
            for a, b in itertools.permutations(names, 2):
                if random.random() < 0.30:
                    start = date(2026, 1, 1) + timedelta(
                        days=random.randint(0, 5))
                    end = (None if random.random() < 0.35 else
                           start + timedelta(days=random.randint(1, 4)))
                    rows.append(rel(a, b, share='1/%d' % random.randint(2, 4),
                                    start=start.isoformat(),
                                    end=None if end is None else end.isoformat()))
            try:
                index = parse_boundary(boundary(*rows))
            except ValueError:
                continue
            for shift in range(8):
                day = date(2026, 1, 1) + timedelta(days=shift)
                active = [r for r in rows
                          if (date.fromisoformat(r['valid_from']) <= day
                              and (r['valid_to'] is None
                                   or day < date.fromisoformat(r['valid_to'])))]
                shares = {}
                for r in active:
                    shares[r['child']] = shares.get(r['child'], 0) + Fraction(
                        r['share'])
                self.assertTrue(all(v <= 1 for v in shares.values()))
                graph = {}
                for r in active:
                    graph.setdefault(r['parent'], []).append(r['child'])
                # brute-force acyclicity
                state = {}

                def visit(node):
                    state[node] = 1
                    for nxt in graph.get(node, ()):
                        if state.get(nxt) == 1:
                            return False
                        if state.get(nxt) is None and not visit(nxt):
                            return False
                    state[node] = 2
                    return True

                self.assertTrue(all(state.get(n) is None and visit(n)
                                    or state.get(n) == 2 for n in names))
                got = attribute([act('x', 'D', day.isoformat())], index, 'A')
                expected = brute_attribute(active, 'A', 'D')
                self.assertEqual(Fraction(got[0][0]['equity_share']),
                                 expected[0])
                self.assertEqual(got[0][0]['control_share'],
                                 '1' if expected[1] else '0')


def brute_attribute(rows, root, org):
    """Independent all-simple-path enumeration for tiny graphs."""
    graph = {}
    for r in rows:
        graph.setdefault(r['parent'], []).append(
            (r['child'], Fraction(r['share']),
             bool(r['operational_control'])))
    equity = Fraction(0)
    controlled = False

    def walk(node, product, all_controlled, seen):
        nonlocal equity, controlled
        if node == org:
            equity += product
            if all_controlled:
                controlled = True
        for child, share, flag in graph.get(node, ()):
            if child in seen:
                continue
            walk(child, product * share, all_controlled and flag,
                 seen | {child})

    walk(root, Fraction(1), True, {root})
    return equity, controlled


class ConsolidationTests(unittest.TestCase):
    def weights(self, rows, org='C', day='2026-06-01'):
        index = parse_boundary(boundary(*rows))
        out, totals, unattributed = attribute([act('x', org, day)], index, 'A')
        return (Fraction(out[0]['equity_share']),
                out[0]['control_share'], out[0]['equity_kgco2e'],
                out[0]['control_kgco2e'], unattributed)

    def test_prescribed_equity_and_control_case(self):
        rows = [rel('A', 'B', '1/2', True), rel('A', 'C', '1/4', False),
                rel('B', 'C', '1/2', True)]
        equity, control, eq_amount, ctl_amount, _ = self.weights(rows)
        self.assertEqual(equity, Fraction(1, 2))   # 1/4 + 1/2*1/2
        self.assertEqual(control, '1')             # A->B->C all controlled
        self.assertEqual(eq_amount, '50')
        self.assertEqual(ctl_amount, '100')
        # With only A->B controlled, C is not in the control scope.
        rows = [rel('A', 'B', '1/2', True), rel('A', 'C', '1/4', False),
                rel('B', 'C', '1/2', False)]
        self.assertEqual(self.weights(rows)[:2],
                         (Fraction(1, 2), '0'))

    def test_root_counts_itself_once(self):
        index = parse_boundary(boundary(rel('A', 'B', '1/2', True)))
        out, totals, unattributed = attribute(
            [act('x', 'A')], index, 'A')
        self.assertEqual((out[0]['equity_share'], out[0]['control_share']),
                         ('1', '1'))
        self.assertEqual(out[0]['equity_kgco2e'], '100')
        self.assertEqual(unattributed, [])

    def test_converging_paths_add_but_control_is_deduplicated(self):
        # A -> B(1/2) -> D(1/2); A -> C(2/3) -> D(1/3):
        # equity = 1/4 + 2/9 = 17/36, control = 1 (counted once).
        rows = [rel('A', 'B', '1/2', True), rel('B', 'D', '1/2', True),
                rel('A', 'C', '2/3', True), rel('C', 'D', '1/3', True)]
        equity, control, _, ctl_amount, _ = self.weights(rows, 'D')
        self.assertEqual(equity, Fraction(17, 36))
        self.assertEqual(control, '1')
        self.assertEqual(ctl_amount, '100')
        # One of the two paths loses control: D still controlled by the other.
        rows[1] = rel('B', 'D', '1/2', False)
        equity, control, _, _, _ = self.weights(rows, 'D')
        self.assertEqual((equity, control), (Fraction(17, 36), '1'))
        rows[3] = rel('C', 'D', '1/3', False)
        equity, control, _, _, _ = self.weights(rows, 'D')
        self.assertEqual((equity, control), (Fraction(17, 36), '0'))

    def test_expiry_day_uses_the_new_slice(self):
        # A->B expires exactly when A->C begins; B->C stays active.
        rows = [rel('A', 'B', '1/2', True, '2026-01-01', '2026-06-01'),
                rel('B', 'C', '1/2', True),
                rel('A', 'C', '1/4', False, '2026-06-01', None)]
        # Before the change only the A->B->C path exists.
        self.assertEqual(self.weights(rows, 'C', '2026-05-31')[:2],
                         (Fraction(1, 4), '1'))
        # On the endpoint date only the new A->C edge is effective: the
        # weight is 1/4 (not 1/2) and the controlled path is gone.
        self.assertEqual(self.weights(rows, 'C', '2026-06-01'),
                         (Fraction(1, 4), '0', '25', '0', []))
        # An expiring edge alone yields no attribution on its end date.
        rows = [rel('A', 'B', '1/2', True, '2026-01-01', '2026-06-01')]
        self.assertEqual(self.weights(rows, 'B', '2026-05-31')[:2],
                         (Fraction(1, 2), '1'))
        self.assertEqual(self.weights(rows, 'B', '2026-06-01')[:2],
                         (Fraction(0), '0'))

    def test_expiry_and_start_on_the_same_day_use_only_the_new_edge(self):
        # A->B ends exactly when B->C starts.  On that day A->B is already
        # gone, so B->C is active but unreachable from A: no weight, no
        # control.  The day before, B->C does not exist yet.
        rows = [rel('A', 'B', '1/2', True, '2026-01-01', '2026-06-01'),
                rel('B', 'C', '1/2', True, '2026-06-01', None)]
        self.assertEqual(self.weights(rows, 'B', '2026-05-31')[:2],
                         (Fraction(1, 2), '1'))
        self.assertEqual(self.weights(rows, 'B', '2026-06-01')[:2],
                         (Fraction(0), '0'))
        self.assertEqual(self.weights(rows, 'C', '2026-06-01'),
                         (Fraction(0), '0', '0', '0', ['x']))

    def test_stale_slice_regression_would_be_detected(self):
        """A closed-interval index makes the expiry-day activity wrong."""
        rows = [rel('A', 'B', '1/2', True, '2026-01-01', '2026-06-01'),
                rel('B', 'C', '1/2', True),
                rel('A', 'C', '1/4', False, '2026-06-01', None)]
        stale = []
        for r in rows:
            item = dict(r)
            # Simulate the bug: expire only *after* valid_to.
            if item['valid_to'] is not None:
                item['valid_to'] = (date.fromisoformat(item['valid_to'])
                                    + timedelta(days=1)).isoformat()
            stale.append(item)
        fresh_index = parse_boundary(boundary(*rows))
        stale_index = parse_boundary(boundary(*stale))
        fresh = attribute([act('x', 'C', '2026-06-01')], fresh_index, 'A')[0]
        wrong = attribute([act('x', 'C', '2026-06-01')], stale_index, 'A')[0]
        self.assertEqual(Fraction(fresh[0]['equity_share']), Fraction(1, 4))
        # The stale slice keeps the expired edge, doubling the weight and
        # wrongly restoring control.
        self.assertEqual(Fraction(wrong[0]['equity_share']), Fraction(1, 2))
        self.assertEqual(fresh[0]['control_share'], '0')
        self.assertEqual(wrong[0]['control_share'], '1')

    def test_unattributed_activity_list(self):
        rows = [rel('A', 'B', '1/2', True)]
        index = parse_boundary(boundary(*rows))
        out, totals, unattributed = attribute(
            [act('z', 'C'), act('y', 'B')], index, 'A')
        self.assertEqual(unattributed, ['z'])
        self.assertEqual(Fraction(out[1]['equity_share']), 0)
        self.assertEqual(Fraction(out[0]['equity_share']), Fraction(1, 2))

    def test_report_is_independent_of_input_order(self):
        rows = [rel('A', 'B', '1/3', True), rel('A', 'C', '2/5', False),
                rel('B', 'D', '1/7', True), rel('C', 'D', '3/11', True)]
        activities = [act('a', 'D', kg='77'), act('b', 'B', kg='11'),
                      act('c', 'C', day='2026-07-01', kg='13'),
                      act('d', 'C', day='2026-07-01', kg='17')]
        one = consolidate({'activities': activities},
                          parse_boundary(boundary(*rows)), 'A')
        two = consolidate({'activities': list(reversed(activities))},
                          parse_boundary(boundary(*list(reversed(rows)))), 'A')
        self.assertEqual(one, two)
        # This exact value follows from adding both converging paths once.
        by_id = {row['id']: row for row in one['activities']}
        share = Fraction(1, 3) * Fraction(1, 7) + Fraction(2, 5) * Fraction(3, 11)
        self.assertEqual(Fraction(by_id['a']['equity_share']), share)
        self.assertEqual(Fraction(by_id['a']['equity_kgco2e']), 77 * share)

    def test_empty_activities(self):
        index = parse_boundary(boundary(rel('A', 'B', '1/2')))
        self.assertEqual(consolidate({'activities': []}, index, 'A'),
                         {'root': 'A', 'activities': [], 'totals': [],
                          'unattributed_activities': []})

    def test_random_dag_against_path_enumeration(self):
        random.seed(19)
        names = ['A', 'B', 'C', 'D', 'E']
        for trial in range(80):
            rows = []
            remaining = {name: Fraction(1) for name in names}
            pairs = [(names[i], names[j]) for i in range(len(names))
                     for j in range(i + 1, len(names))]
            for a, b in pairs:
                if random.random() < 0.45 and remaining[b] > 0:
                    share = Fraction(random.randint(0, 4), 4)
                    if share > remaining[b]:
                        share = remaining[b]
                    remaining[b] -= share
                    rows.append(rel(a, b, '%d/%d' % (share.numerator,
                                                     share.denominator),
                                    random.random() < 0.5))
            index = parse_boundary(boundary(*rows))
            activities = [act('x%d' % k, org, '2026-06-01')
                          for k, org in enumerate(names)]
            out, totals, _ = attribute(activities, index, 'A')
            for row in out:
                expected = brute_attribute(rows, 'A', row['organization'])
                self.assertEqual(Fraction(row['equity_share']), expected[0])
                self.assertEqual(row['control_share'],
                                 '1' if expected[1] else '0')
            expected_totals = sum((brute_attribute(rows, 'A', org)[0]
                                   for org in names), Fraction(0)) * 100
            self.assertEqual(Fraction(totals[1][0]), expected_totals)

    def test_date_switch_changes_attribution_and_totals(self):
        # The same activity set is run before and after a boundary change.
        before = [rel('A', 'B', '1/2', True, '2026-01-01', '2026-06-01')]
        after = [rel('A', 'B', '1/2', True, '2026-01-01', '2026-06-01'),
                 rel('A', 'B', '2/3', True, '2026-06-01', None)]
        activities = [act('may', 'B', '2026-05-31', kg='90'),
                      act('jun', 'B', '2026-06-01', kg='100')]
        out_before, totals_before, _ = attribute(
            activities, parse_boundary(boundary(*before)), 'A')
        out_after, totals_after, _ = attribute(
            activities, parse_boundary(boundary(*after)), 'A')
        self.assertEqual([r['equity_kgco2e'] for r in out_after],
                         ['200/3', '45'])
        self.assertEqual(totals_after[1][0], Fraction(45) + Fraction(200, 3))
        self.assertEqual([r['kgco2e'] for r in out_before],
                         ['100', '90'])
        self.assertEqual(totals_before[1][0], Fraction(45))
        # Changing activity emissions moves both totals proportionally.
        changed = [dict(a, kgco2e='200') if a['id'] == 'jun' else a
                   for a in activities]
        out2, totals2, _ = attribute(
            changed, parse_boundary(boundary(*after)), 'A')
        self.assertEqual(totals2[1][0] - totals_after[1][0],
                         Fraction(2, 3) * 100)
        self.assertEqual(totals2[1][1] - totals_after[1][1], 100)

    def test_large_volume_exact_fractions(self):
        # 20000 activities of 10^12 kg under a converging diamond.
        rows = [rel('A', 'B', '1/3', True), rel('B', 'D', '1/7', True),
                rel('A', 'C', '2/5', False), rel('C', 'D', '3/11', True)]
        index = parse_boundary(boundary(*rows))
        activities = [act('a%05d' % i, 'D', '2026-06-01', 2, '1000000000000')
                      for i in range(20000)]
        out, totals, unattributed = attribute(activities, index, 'A')
        share = Fraction(1, 3) * Fraction(1, 7) + Fraction(2, 5) * Fraction(3, 11)
        expected = 20000 * 10 ** 12 * share
        self.assertEqual(Fraction(totals[2][0]), expected)
        self.assertEqual(Fraction(totals[2][1]), 20000 * 10 ** 12)
        self.assertEqual(len(out), 20000)
        self.assertEqual(unattributed, [])
        self.assertEqual(Fraction(out[0]['equity_kgco2e']),
                         Fraction(10 ** 12) * share)

    def test_wide_random_interval_graph_is_processed_exactly(self):
        random.seed(23)
        rels = []
        incoming = {}
        for i in range(40):
            parent = 'n%d' % i
            targets = list(range(i + 1, min(i + 4, 41)))
            random.shuffle(targets)
            for j in targets:
                # Keep the direct share sum per child at or below one.
                if incoming.get(j, 0) >= 5 or random.random() < 0.4:
                    continue
                incoming[j] = incoming.get(j, 0) + 1
                rels.append(rel(parent, 'n%d' % j, '1/5',
                                random.random() < 0.5))
        index = parse_boundary(boundary(*rels))
        activities = [act('x%d' % i, 'n%d' % i, '2026-06-01')
                      for i in range(1, 41)]
        out, totals, _ = attribute(activities, index, 'n0')
        for row in out:
            expected = brute_attribute(rels, 'n0', row['organization'])
            self.assertEqual(Fraction(row['equity_share']), expected[0])


class PathConvergenceTests(unittest.TestCase):
    def test_maximum_inputs_stay_exact_and_bounded(self):
        # 10000 organizations / 20000 activities at the allowed ceilings.
        # Layer i nodes feed layer i+1 with share 1/2 so every root-to-last
        # weight is exactly representable.
        orgs = ['g0'] + ['L%05d' % i for i in range(1, 10000)]
        rows = []
        for i in range(1, 10000):
            rows.append(rel(orgs[i - 1], orgs[i], '1/2', i % 2 == 0))
        index = parse_boundary(boundary(*rows))
        self.assertEqual(len(index.organizations), 10000)
        activities = [act('a%05d' % i, orgs[1 + i % 2],
                          '2026-06-01', 1, '1000000000000')
                      for i in range(19999)]
        activities.append(act('a-deep', orgs[9999], '2026-06-01', 2,
                              '1000000000000'))
        out, totals, unattributed = attribute(activities, index, 'g0')
        self.assertEqual(len(out), 20000)
        self.assertEqual(unattributed, [])
        # Deepest weight is 2^-9999; the report must stay an exact fraction.
        deepest = [r for r in out if r['id'] == 'a-deep'][0]
        self.assertEqual(Fraction(deepest['equity_share']),
                         Fraction(1, 2 ** 9999))
        self.assertEqual(Fraction(deepest['equity_kgco2e']),
                         Fraction(10 ** 12, 2 ** 9999))
        self.assertEqual(len(deepest['equity_share']),
                         2 + len(str(2 ** 9999)))
        # 10000 activities on orgs[1] (1/2) and 9999 on orgs[2] (1/4).
        self.assertEqual(Fraction(totals[1][0]),
                         (10000 * Fraction(1, 2)
                          + 9999 * Fraction(1, 4)) * 10 ** 12)

    def test_exponential_path_count_is_collapsed(self):
        # 2^25 distinct root-to-Z paths: layers of two nodes.
        layers = 25
        rows = [rel('A', 'a0', '1/2', True), rel('A', 'b0', '1/2', True)]
        for layer in range(layers - 1):
            for source in ('a', 'b'):
                for target in ('a', 'b'):
                    rows.append(rel('%s%d' % (source, layer),
                                    '%s%d' % (target, layer + 1),
                                    '1/2', True))
        for source in ('a', 'b'):
            rows.append(rel('%s%d' % (source, layers - 1), 'Z', '1/2', True))
        index = parse_boundary(boundary(*rows))
        out, totals, _ = attribute([act('x', 'Z')], index, 'A')
        # 2^25 distinct root-to-Z paths, each with product 2^-26.  The exact
        # share is 2^25 / 2^26 = 1/2, obtained without listing any path.
        self.assertEqual(Fraction(out[0]['equity_share']), Fraction(1, 2))
        self.assertEqual(out[0]['control_share'], '1')
        self.assertEqual(out[0]['equity_kgco2e'], '50')


class GroupReportTests(unittest.TestCase):
    def run_cli(self, *args):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'src')
        return subprocess.run([sys.executable, '-m', 'carbon_inventory.cli',
                               *map(str, args)], capture_output=True,
                              text=True, env=env)

    def test_fixture_report_and_determinism(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'group.json'
            result = self.run_cli('--source', ROOT / 'fixtures/group-input.json',
                                  '--boundary', ROOT / 'fixtures/group-boundary.json',
                                  '--root', 'group-hq', '--output', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out.read_bytes())
            self.assertEqual(report['schema_version'], 3)
            body = report['consolidation']
            self.assertEqual(body['root'], 'group-hq')
            by_id = {row['id']: row for row in body['activities']}
            # Winter: HQ holds 1/2 of plant-a; plant-b is only 50% and
            # uncontrolled, plant-c is outside the group.
            self.assertEqual(by_id['a-winter-power']['equity_share'], '1/2')
            self.assertEqual(by_id['a-winter-power']['control_share'], '1')
            # Summer: HQ->a 3/4 plus HQ->b 1/3 * b->a 1/4 = 5/6.
            self.assertEqual(by_id['a-summer-power']['equity_share'], '5/6')
            self.assertEqual(by_id['a-summer-power']['equity_kgco2e'], '200')
            # plant-b is held at 1/3 and not controlled (HQ->b false).
            self.assertEqual(by_id['b-summer-gas']['equity_share'], '1/3')
            self.assertEqual(by_id['b-summer-gas']['control_share'], '0')
            self.assertIn('c-summer-gas', body['unattributed_activities'])
            self.assertEqual(body['totals'], [
                {'scope': 1, 'equity_kgco2e': '190/3',
                 'control_kgco2e': '0'},
                {'scope': 2, 'equity_kgco2e': '400',
                 'control_kgco2e': '640'},
            ])
            first = out.read_bytes()
            again = Path(td) / 'again.json'
            result = self.run_cli(
                '--source', ROOT / 'fixtures/group-input.json',
                '--boundary', ROOT / 'fixtures/group-boundary.json',
                '--root', 'group-hq', '--output', again)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(first, again.read_bytes())

    def test_cli_mode_pairing(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'out.json'
            src = ROOT / 'fixtures/group-input.json'
            bnd = ROOT / 'fixtures/group-boundary.json'
            self.assertEqual(self.run_cli(
                '--source', src, '--boundary', bnd, '--output', out
            ).returncode, 2)
            self.assertEqual(self.run_cli(
                '--source', src, '--root', 'group-hq', '--output', out
            ).returncode, 2)
            self.assertEqual(self.run_cli(
                '--registry', Path(td) / 'reg.db', '--batch', bnd,
                '--source', src, '--output', out).returncode, 2)
            self.assertEqual(self.run_cli(
                '--registry', Path(td) / 'reg.db', '--batch', bnd,
                '--root', 'group-hq', '--output', out).returncode, 2)
            self.assertEqual(self.run_cli(
                '--source', src, '--batch', bnd, '--boundary', bnd,
                '--root', 'group-hq', '--output', out).returncode, 2)

    def test_path_safety_and_failure_keeps_old_report(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out = td / 'out.json'
            out.write_text('prior')
            bnd = td / 'boundary.json'
            bnd.write_text((ROOT / 'fixtures/group-boundary.json').read_text())
            # Boundary must not alias the source or the output.
            self.assertEqual(self.run_cli(
                '--source', ROOT / 'fixtures/group-input.json',
                '--boundary', bnd, '--root', 'group-hq',
                '--output', bnd).returncode, 2)
            self.assertEqual(self.run_cli(
                '--source', bnd, '--boundary', bnd,
                '--root', 'group-hq', '--output', out).returncode, 2)
            self.assertEqual(out.read_text(), 'prior')
            # An invalid boundary leaves a prior report untouched.
            bnd.write_text(json.dumps(boundary(
                rel('A', 'B'), rel('B', 'A'))))
            result = self.run_cli(
                '--source', ROOT / 'fixtures/group-input.json',
                '--boundary', bnd, '--root', 'A', '--output', out)
            self.assertEqual(result.returncode, 2)
            self.assertIn('cycle', result.stderr)
            self.assertEqual(out.read_text(), 'prior')
            # Duplicate keys and non-ISO dates fail at the boundary reader.
            bnd.write_text('{"relationships":[],"relationships":[]}')
            self.assertEqual(self.run_cli(
                '--source', ROOT / 'fixtures/group-input.json',
                '--boundary', bnd, '--root', 'A',
                '--output', out).returncode, 2)
            self.assertEqual(out.read_text(), 'prior')

    def test_share_sum_violation_on_activity_free_dates_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            bnd = td / 'boundary.json'
            out = td / 'out.json'
            bnd.write_text(json.dumps(boundary(
                rel('A', 'C', '3/5', True, '2026-01-01', '2026-02-01'),
                rel('B', 'C', '1/2', True, '2026-01-15', '2026-03-01'))))
            # The fixture's activities are in June, outside the bad interval.
            result = self.run_cli(
                '--source', ROOT / 'fixtures/group-input.json',
                '--boundary', bnd, '--root', 'A', '--output', out)
            self.assertEqual(result.returncode, 2)
            self.assertIn('share sum', result.stderr)
            self.assertFalse(out.exists())

    def test_hidden_cycle_rejected_even_without_activity(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out = td / 'out.json'
            bnd = td / 'boundary.json'
            bnd.write_text(json.dumps(boundary(
                rel('A', 'B', '1/2', True, '2026-01-01', '2026-01-10'),
                rel('B', 'C', '1/2', True, '2026-01-01', '2026-01-10'),
                rel('C', 'A', '1/2', True, '2026-01-03', '2026-01-04'))))
            # Activities are on dates where the graph is acyclic.
            result = self.run_cli(
                '--source', ROOT / 'fixtures/group-input.json',
                '--boundary', bnd, '--root', 'A', '--output', out)
            self.assertEqual(result.returncode, 2)
            self.assertIn('cycle', result.stderr)
            self.assertFalse(out.exists())

    def test_exact_endpoint_activity_through_cli(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / 'source.json'
            bnd = td / 'boundary.json'
            out = td / 'out.json'
            fixture = json.loads((ROOT / 'fixtures/input.json').read_text())
            fixture['activities'][0]['date'] = '2026-03-01'
            fixture['activities'][1]['date'] = '2026-02-28'
            src.write_text(json.dumps(fixture))
            bnd.write_text(json.dumps(boundary(
                rel('group-hq', 'plant-a', '1/2', True,
                    '2026-01-01', '2026-03-01'),
                rel('group-hq', 'plant-a', '2/3', True,
                    '2026-03-01', None))))
            result = self.run_cli('--source', src, '--boundary', bnd,
                                  '--root', 'group-hq', '--output', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = {r['id']: r for r in
                    json.loads(out.read_bytes())['consolidation']['activities']}
            self.assertEqual(rows['plant-power']['equity_share'], '2/3')
            self.assertEqual(rows['plant-power']['equity_kgco2e'], '800/3')
            self.assertEqual(rows['boiler-gas']['equity_share'], '1/2')
            self.assertEqual(rows['boiler-gas']['equity_kgco2e'], '95')

    def test_legacy_source_mode_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'legacy.json'
            result = self.run_cli('--source', ROOT / 'fixtures/input.json',
                                  '--output', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            # Byte-for-byte golden report from the pre-group-mode v1 path.
            expected = (
                '{\n'
                '  "result": {\n'
                '    "activities": [\n'
                '      {\n'
                '        "category": "natural-gas",\n'
                '        "date": "2026-06-01",\n'
                '        "factor_id": "gas-2026",\n'
                '        "id": "boiler-gas",\n'
                '        "kgco2e": "190",\n'
                '        "organization": "plant-a",\n'
                '        "quantity": 100,\n'
                '        "scope": 1,\n'
                '        "unit": "m3"\n'
                '      },\n'
                '      {\n'
                '        "category": "electricity",\n'
                '        "date": "2026-06-01",\n'
                '        "factor_id": "grid-2026",\n'
                '        "id": "plant-power",\n'
                '        "kgco2e": "400",\n'
                '        "organization": "plant-a",\n'
                '        "quantity": 1000,\n'
                '        "scope": 2,\n'
                '        "unit": "kwh"\n'
                '      }\n'
                '    ],\n'
                '    "totals": [\n'
                '      {\n'
                '        "kgco2e": "190",\n'
                '        "organization": "plant-a",\n'
                '        "scope": 1\n'
                '      },\n'
                '      {\n'
                '        "kgco2e": "400",\n'
                '        "organization": "plant-a",\n'
                '        "scope": 2\n'
                '      }\n'
                '    ]\n'
                '  },\n'
                '  "schema_version": 1,\n'
                '  "source_sha256": "9a626bfbe51b6b9ee92ba90ea4ef617461'
                '0797d52206842ac62e27f3c40eb767"\n'
                '}\n').encode()
            self.assertEqual(out.read_bytes(), expected)

    def test_run_group_digests(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'out.json'
            result = self.run_cli('--source', ROOT / 'fixtures/group-input.json',
                                  '--boundary', ROOT / 'fixtures/group-boundary.json',
                                  '--root', 'group-hq', '--output', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out.read_bytes())
            import hashlib
            self.assertEqual(
                report['source_sha256'],
                hashlib.sha256(
                    (ROOT / 'fixtures/group-input.json').read_bytes()
                ).hexdigest())
            self.assertEqual(
                report['boundary_sha256'],
                hashlib.sha256(
                    (ROOT / 'fixtures/group-boundary.json').read_bytes()
                ).hexdigest())
            self.assertEqual(json.loads(result.stdout), report)


if __name__ == '__main__':
    unittest.main()
