"""Acceptance tests for the shared-risk adaptive audit (schema version 4).

The oracle below is a direct, independent implementation: it rebuilds Omega
from scratch with Fractions, scans every state for each posterior, and solves
the minimax recursion without masks, bit indices or report code.  It is used
to check domain legality, posterior endpoints and witnesses, each reachable
sub-policy's worst cost and its smallest-ID tie choice.  The ``Budget`` design
(decide a bound, then extract) is compared with the direct posterior cost
function on the same instances.
"""
import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from carbon_inventory.audit import run as run_audit
from carbon_inventory.common import read
from carbon_inventory.strategy import Budget, Policy
from carbon_inventory.verify import verify
from carbon_inventory.uncertainty import Scenario, build_scenario
from carbon_inventory.model import parse_inventory

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# helpers to build small documents
# ---------------------------------------------------------------------------
def factor(fid='f', rate='1/1', category='c', unit='u', start='2026-01-01',
           end=None):
    return {'id': fid, 'category': category, 'unit': unit,
            'kgco2e_per_unit': rate, 'valid_from': start, 'valid_to': end}


def activity(aid, quantity=0, fid='f', organization='o', scope=1,
             category='c', unit='u', day='2026-06-01'):
    return {'id': aid, 'organization': organization, 'date': day,
            'scope': scope, 'category': category, 'quantity': quantity,
            'unit': unit, 'factor_id': fid}


def source(activities, factors=None):
    if factors is None:
        factors = [factor()]
    return {'factors': factors, 'activities': activities}


def spec(variables=(), constraints=(), quantities=(), rates=(), costs=None,
         groups=None, overall='0/1', activities=None):
    if costs is None:
        costs = {name: None for name in variables}
    if groups is None:
        groups = []
    return {
        'variables': list(variables),
        'constraints': list(constraints),
        'quantities': list(quantities),
        'rates': list(rates),
        'audit_costs': dict(costs),
        'precision': {
            'overall': overall,
            'groups': [{'organization': o, 'scope': s, 'tolerance': t}
                       for o, s, t in groups],
        },
    }


def constraint(cid, terms, lower, upper):
    return {'id': cid, 'terms': dict(terms), 'lower': lower, 'upper': upper}


def quantity(aid, terms):
    return {'activity_id': aid, 'terms': dict(terms)}


def rate(fid, terms):
    return {'factor_id': fid, 'terms': dict(terms)}


def build(src, document):
    """Scenario through the public builder (no report)."""
    factors, activities = parse_inventory(src)
    return build_scenario((factors, activities), document)


def report(src, document):
    result = run_audit(src, '0' * 64, document, '1' * 64)
    # Every published result must survive independent back-substitution.
    verify(src, document, result)
    return result


# ---------------------------------------------------------------------------
# independent oracle
# ---------------------------------------------------------------------------
class Oracle:
    def __init__(self, src, document):
        self.names = sorted(document['variables'])
        self.index = {name: i for i, name in enumerate(self.names)}
        self.costs = dict(document['audit_costs'])
        self.tolerances = {
            (g['organization'], g['scope']): Fraction(g['tolerance'])
            for g in document['precision']['groups']}
        self.overall_tolerance = Fraction(document['precision']['overall'])
        self.groups = sorted({(a['organization'], a['scope'])
                              for a in src['activities']})
        self.targets = [('group', key) for key in self.groups]
        self.targets.append(('overall', None))

        self.base_rate = {f['id']: Fraction(f['kgco2e_per_unit'])
                          for f in src['factors']}
        self.base_quantity = {a['id']: a['quantity']
                              for a in src['activities']}
        self.activity_factor = {a['id']: a['factor_id']
                                for a in src['activities']}
        self.group_of = {a['id']: (a['organization'], a['scope'])
                         for a in src['activities']}
        self.quantity_terms = {
            q['activity_id']: {name: Fraction(value)
                               for name, value in q['terms'].items()}
            for q in document['quantities']}
        self.rate_terms = {
            r['factor_id']: {name: Fraction(value)
                             for name, value in r['terms'].items()}
            for r in document['rates']}
        self.constraints = [
            ({name: int(value) for name, value in c['terms'].items()},
             c['lower'], c['upper'])
            for c in document['constraints']]

        self.states = []
        for state in itertools.product((0, 1), repeat=len(self.names)):
            assignment = dict(zip(self.names, state))
            if all(lower <= sum(c * assignment[v] for v, c in terms.items())
                   <= upper
                   for terms, lower, upper in self.constraints):
                self.states.append(state)
        self.values = {}
        for k, state in enumerate(self.states):
            assignment = dict(zip(self.names, state))
            per_activity = {}
            per_group = {key: Fraction(0) for key in self.groups}
            overall = Fraction(0)
            for aid, base in self.base_quantity.items():
                fid = self.activity_factor[aid]
                quantity_value = base + sum(
                    c * assignment[v]
                    for v, c in self.quantity_terms.get(aid, {}).items())
                rate_value = self.base_rate[fid] + sum(
                    c * assignment[v]
                    for v, c in self.rate_terms.get(fid, {}).items())
                emission = quantity_value * rate_value
                per_activity[aid] = emission
                per_group[self.group_of[aid]] += emission
                overall += emission
            self.values[k] = (per_activity, per_group, overall)

    def legality_errors(self):
        errors = []
        for k, state in enumerate(self.states):
            assignment = dict(zip(self.names, state))
            for aid, base in self.base_quantity.items():
                value = base + sum(
                    c * assignment[v]
                    for v, c in self.quantity_terms.get(aid, {}).items())
                if value < 0:
                    errors.append(('quantity', aid, value))
            for fid, base in self.base_rate.items():
                value = base + sum(
                    c * assignment[v]
                    for v, c in self.rate_terms.get(fid, {}).items())
                if value < 0:
                    errors.append(('rate', fid, value))
        return errors

    def matching(self, observations):
        return [k for k, state in enumerate(self.states)
                if all(state[self.index[name]] == value
                       for name, value in observations.items())]

    def target_values(self, k):
        per_activity, per_group, overall = self.values[k]
        return per_activity, per_group, overall

    def endpoints(self, observations):
        matching = self.matching(observations)
        if not matching:
            return None
        result = {}
        for kind, key in self.targets:
            def value(k):
                _, per_group, overall = self.values[k]
                return overall if kind == 'overall' else per_group[key]

            low = min(matching, key=lambda k: (value(k),
                                               self.states[k]))
            high = min(matching, key=lambda k: (-value(k),
                                                self.states[k]))
            result[key] = (value(low), value(high), self.states[low],
                           self.states[high])
        return result

    def within(self, observations):
        endpoints = self.endpoints(observations)
        if endpoints is None:
            return False
        for kind, key in self.targets:
            low, high, _, _ = endpoints[key]
            tolerance = (self.overall_tolerance if kind == 'overall'
                         else self.tolerances[key])
            if high - low > tolerance:
                return False
        return True

    def policy_cost(self, observations=(), memo=None):
        """(minimax cost or None, chosen variable or None)."""
        if memo is None:
            memo = {}
        key = tuple(sorted(observations))
        if key in memo:
            return memo[key]
        if self.within(dict(observations)):
            memo[key] = (0, None)
            return memo[key]
        best = None
        chosen = None
        observed = dict(observations)
        for name in self.names:
            if self.costs.get(name) is None or name in observed:
                continue
            branches = []
            feasible = True
            for value in (0, 1):
                extended = dict(observed)
                extended[name] = value
                if not self.matching(extended):
                    feasible = False
                    break
                cost, _ = self.policy_cost(tuple(sorted(extended.items())),
                                           memo)
                if cost is None:
                    feasible = False
                    break
                branches.append(cost)
            if not feasible:
                continue
            total = self.costs[name] + max(branches)
            if best is None or total < best:
                best = total
                chosen = name
        memo[key] = (best, chosen)
        return memo[key]


# ---------------------------------------------------------------------------
# domain acceptance
# ---------------------------------------------------------------------------
class DomainAcceptance(unittest.TestCase):
    def test_contradictory_constraints_are_infeasible(self):
        document = spec(
            variables=('x', 'y'),
            constraints=[constraint('one', {'x': 1, 'y': 1}, 1, 1),
                         constraint('none', {'x': 1, 'y': 1}, 0, 0)],
            costs={'x': 1, 'y': 1},
            groups=[('o', 1, '0/1')])
        result = report(source([activity('a')]), document)
        self.assertEqual(result['status'], 'infeasible')
        self.assertEqual(result['activities'], [])
        self.assertEqual(result['groups'], [])
        self.assertIsNone(result['overall'])
        self.assertIsNone(result['audit'])
        self.assertEqual(result['nominal']['totals'][0]['kgco2e'], '0')

    def test_feasible_negative_quantity_is_rejected(self):
        document = spec(
            variables=('x',),
            quantities=[quantity('a', {'x': -2})],
            constraints=[constraint('force', {'x': 1}, 1, 1)],
            costs={'x': 1},
            groups=[('o', 1, '0/1')])
        with self.assertRaises(ValueError) as caught:
            build(source([activity('a', quantity=1)]), document)
        self.assertIn("negative quantity", str(caught.exception))
        self.assertIn("'a'", str(caught.exception))
        self.assertIn('x=1', str(caught.exception))

    def test_constraint_removing_negative_state_avoids_false_rejection(self):
        document = spec(
            variables=('x',),
            quantities=[quantity('a', {'x': -2})],
            constraints=[constraint('off', {'x': 1}, 0, 0)],
            costs={'x': 1},
            groups=[('o', 1, '0/1')])
        result = report(source([activity('a', quantity=1)]), document)
        self.assertEqual(result['status'], 'feasible')
        self.assertEqual(result['overall']['minimum'], '1')
        self.assertEqual(result['overall']['maximum'], '1')

    def test_unreferenced_factor_rate_is_checked(self):
        document = spec(
            variables=('x',),
            rates=[rate('spare', {'x': '-1/1'})],
            costs={'x': 1},
            groups=[('o', 1, '0/1')])
        factors = [factor('f', '1/1'), factor('spare', '0/1')]
        with self.assertRaises(ValueError) as caught:
            build(source([activity('a', quantity=1)], factors), document)
        self.assertIn("'spare'", str(caught.exception))
        document['rates'] = []
        result = report(source([activity('a', quantity=1)], factors),
                        document)
        self.assertEqual(result['status'], 'feasible')

    def test_cancelling_activities(self):
        document = spec(
            variables=('x',),
            quantities=[quantity('a', {'x': 1}), quantity('b', {'x': -1})],
            costs={'x': None},
            groups=[('o', 1, '0/1')])
        result = report(source([activity('a', quantity=0),
                                activity('b', quantity=1)]), document)
        self.assertEqual([(a['minimum'], a['maximum'])
                          for a in result['activities']],
                         [('0', '1'), ('0', '1')],
                         msg=result['activities'])
        self.assertEqual((result['groups'][0]['minimum'],
                          result['groups'][0]['maximum']), ('1', '1'))
        self.assertEqual((result['overall']['minimum'],
                          result['overall']['maximum']), ('1', '1'))

    def test_product_endpoints_and_tie_witness(self):
        document = spec(
            variables=('x',),
            quantities=[quantity('a', {'x': 1})],
            rates=[rate('f', {'x': '-1/1'})],
            costs={'x': None},
            groups=[('o', 1, '0/1')])
        result = report(
            source([activity('a', quantity=1)], [factor('f', '2/1')]),
            document)
        self.assertEqual((result['overall']['minimum'],
                          result['overall']['maximum']), ('2', '2'))
        self.assertEqual(result['overall']['min_witness'], [0])
        self.assertEqual(result['overall']['max_witness'], [0])

    def test_empty_activities_still_review_every_factor(self):
        factors = [factor('spare-a', '3/2'), factor('spare-b', '0/1')]
        document = spec(variables=(), costs={}, groups=[], overall='0/1')
        result = report(source([], factors), document)
        self.assertEqual(result['status'], 'feasible')
        self.assertEqual(result['activities'], [])
        self.assertEqual(result['groups'], [])
        self.assertEqual(result['overall']['minimum'], '0')
        self.assertEqual(result['overall']['min_witness'], [])
        self.assertEqual(result['audit']['worst_cost'], 0)
        self.assertNotIn('variable', result['audit']['policy'])
        # An unreferenced factor with a feasible negative rate is still a
        # physical violation, even though no activity uses it.
        document = spec(variables=('x',),
                        rates=[rate('spare-b', {'x': '-1/1'})],
                        costs={'x': None}, groups=[])
        with self.assertRaises(ValueError) as caught:
            build(source([], factors), document)
        self.assertIn("'spare-b'", str(caught.exception))

    def test_units_and_dates_come_from_source(self):
        # A scenario that shifts quantity and rate keeps the source category,
        # unit and date in the nominal catalogue, and the source validation
        # still rejects a mismatched date.
        src = source([activity('a', quantity=1)])
        document = spec(variables=('x',),
                        quantities=[quantity('a', {'x': 1})],
                        costs={'x': None}, groups=[('o', 1, '0/1')])
        result = report(src, document)
        self.assertEqual(result['nominal']['activities'][0]['date'],
                         '2026-06-01')
        self.assertEqual(result['nominal']['activities'][0]['unit'], 'u')
        bad = source([activity('a', quantity=1, day='2025-12-31')])
        with self.assertRaises(ValueError):
            build(bad, document)


# ---------------------------------------------------------------------------
# specification validation
# ---------------------------------------------------------------------------
class SpecificationValidation(unittest.TestCase):
    def base(self):
        return spec(variables=('x',), costs={'x': 1},
                    groups=[('o', 1, '0/1')])

    def reject(self, document, src=None):
        with self.assertRaises((ValueError, TypeError)):
            build(src or source([activity('a')]), document)

    def test_top_level_fields_are_exact(self):
        document = self.base()
        document['extra'] = 1
        self.reject(document)
        document = self.base()
        del document['rates']
        self.reject(document)

    def test_variables(self):
        self.reject(spec(variables=('x', 'x'), costs={'x': 1},
                         groups=[('o', 1, '0/1')]))
        self.reject(spec(variables=('',), costs={'': 1},
                         groups=[('o', 1, '0/1')]))
        names = tuple('v%d' % i for i in range(11))
        document = spec(variables=names, costs={n: None for n in names},
                        groups=[('o', 1, '0/1')])
        self.reject(document)
        # Zero variables and zero activities remain a valid one-state domain.
        document = spec(variables=(), costs={}, groups=[])
        result = report(source([]), document)
        self.assertEqual(result['status'], 'feasible')
        self.assertEqual(result['overall']['minimum'], '0')
        self.assertEqual(result['overall']['min_witness'], [])
        self.assertEqual(result['audit']['status'], 'possible')

    def test_constraints(self):
        for terms, lower, upper in (
                ({'missing': 1}, 0, 1),
                ({'x': True}, 0, 1),
                ({'x': 1}, 2, 1),
                ({'x': 10 ** 6 + 1}, 0, 1),
                ({'x': '1'}, 0, 1),
                ({'x': 1}, -10 ** 12 - 1, 1),
                ({'x': 1}, 0, 1.5)):
            document = self.base()
            document['constraints'] = [
                constraint('c', terms, lower, upper)]
            self.reject(document)
        document = self.base()
        document['constraints'] = [constraint('c', {'x': 1}, 0, 1)] * 2
        self.reject(document)
        document = self.base()
        document['constraints'] = [
            {'id': 'c', 'terms': {'x': 1}, 'lower': 0, 'upper': 1, 'x': 2}]
        self.reject(document)

    def test_quantities_and_rates(self):
        document = self.base()
        document['quantities'] = [quantity('a', {'x': True})]
        self.reject(document)
        document = self.base()
        document['quantities'] = [quantity('missing', {'x': 1})]
        self.reject(document)
        document = self.base()
        document['quantities'] = [quantity('a', {'x': 1}),
                                  quantity('a', {})]
        self.reject(document)
        document = self.base()
        document['quantities'] = [quantity('a', {'y': 1})]
        self.reject(document)
        document = self.base()
        document['quantities'] = [
            {'activity_id': 'a', 'terms': {'x': 1}, 'extra': 0}]
        self.reject(document)
        document = self.base()
        document['quantities'] = [quantity('a', {'x': 10 ** 12 + 1})]
        self.reject(document)
        document = self.base()
        document['rates'] = [rate('missing', {'x': '1/1'})]
        self.reject(document)
        document = self.base()
        document['rates'] = [rate('f', {'x': '1/0'})]
        self.reject(document)
        document = self.base()
        document['rates'] = [rate('f', {'x': '1'})]
        self.reject(document)
        document = self.base()
        document['rates'] = [rate('f', {'x': '1/2'}), rate('f', {})]
        self.reject(document)
        # Empty terms and a signed fraction are accepted.
        document = self.base()
        document['rates'] = [rate('f', {'x': '-3/4'})]
        scenario = build(source([activity('a')]), document)
        self.assertEqual(
            Fraction(scenario.rate_scaled((1,), 'f'), scenario.denominator),
            Fraction(1, 4))

    def test_audit_costs(self):
        for value in (True, -1, 10 ** 6 + 1, 1.5, '1'):
            document = self.base()
            document['audit_costs'] = {'x': value}
            self.reject(document)
        document = self.base()
        document['audit_costs'] = {}
        self.reject(document)
        document = self.base()
        document['audit_costs'] = {'x': 1, 'y': 1}
        self.reject(document)

    def test_precision(self):
        document = self.base()
        document['precision']['groups'] = []
        self.reject(document)
        document = self.base()
        document['precision']['groups'] = [
            {'organization': 'o', 'scope': 1, 'tolerance': '0/1'},
            {'organization': 'o', 'scope': 1, 'tolerance': '0/1'}]
        self.reject(document)
        document = self.base()
        document['precision']['groups'] = [
            {'organization': 'o', 'scope': 2, 'tolerance': '0/1'}]
        self.reject(document)
        document = self.base()
        document['precision']['overall'] = '-1/2'
        self.reject(document)
        document = self.base()
        document['precision']['groups'][0]['tolerance'] = '1/0'
        self.reject(document)
        document = self.base()
        document['precision']['groups'][0]['extra'] = 1
        self.reject(document)
        document = self.base()
        document['precision']['extra'] = []
        self.reject(document)

    def test_limits_are_mode_specific(self):
        activities = [activity('a%d' % i) for i in range(41)]
        groups = [('o', 1, '0/1')]
        document = spec(variables=('x',), costs={'x': None}, groups=groups)
        self.reject(document, source(activities))
        factors = [factor('f%d' % i) for i in range(17)]
        document = spec(variables=('x',), costs={'x': None}, groups=groups)
        self.reject(document, source([activity('a')], factors))
        # v1 itself rejects duplicate factor ids; the audit adds activity ids.
        with self.assertRaises(ValueError):
            build(source([activity('a'), activity('a')]), document)


# ---------------------------------------------------------------------------
# strategy acceptance
# ---------------------------------------------------------------------------
class StrategyAcceptance(unittest.TestCase):
    def test_single_variable_cost(self):
        src = source([activity('a')])
        document = spec(variables=('x',),
                        quantities=[quantity('a', {'x': 1})],
                        costs={'x': 3}, groups=[('o', 1, '0/1')])
        result = report(src, document)
        audit = result['audit']
        self.assertEqual(audit['status'], 'possible')
        self.assertEqual(audit['worst_cost'], 3)
        root = audit['policy']
        self.assertEqual((root['variable'], root['cost'], root['worst_cost']),
                         ('x', 3, 3))
        self.assertIn('groups', root['zero'])
        self.assertIn('overall', root['one'])

    def test_unauditable_variable_is_impossible(self):
        src = source([activity('a')])
        document = spec(variables=('x',),
                        quantities=[quantity('a', {'x': 1})],
                        costs={'x': None}, groups=[('o', 1, '0/1')])
        audit = report(src, document)['audit']
        self.assertEqual(audit['status'], 'impossible')
        self.assertIsNone(audit['worst_cost'])
        self.assertIsNone(audit['policy'])
        witness = audit['witness']
        self.assertEqual(witness['observations'], {})
        self.assertEqual(witness['target']['kind'], 'group')
        self.assertEqual(witness['min_witness'], [0])
        self.assertEqual(witness['max_witness'], [1])
        self.assertEqual(Fraction(witness['maximum'])
                         - Fraction(witness['minimum']), 1)

    def test_free_variables_terminate(self):
        src = source([activity('a', quantity=1)], [factor('f', '1/1')])
        document = spec(variables=('x', 'y'),
                        quantities=[quantity('a', {'x': 1, 'y': 1})],
                        costs={'x': 0, 'y': 0}, groups=[('o', 1, '0/1')])
        result = report(src, document)
        audit = result['audit']
        self.assertEqual(audit['worst_cost'], 0)
        self.assertEqual(audit['policy']['variable'], 'x')
        scenario = build(src, document)
        direct = Policy(scenario)
        budget = Budget(scenario)
        self.assertEqual(direct.root()['worst_cost'], 0)
        self.assertEqual(budget.root()['worst_cost'], 0)
        self.assertEqual(budget.root()['variable'], 'x')

    def test_already_precise_stops_without_a_query(self):
        src = source([activity('a')])
        document = spec(variables=('x',),
                        quantities=[quantity('a', {'x': 1})],
                        costs={'x': 0}, groups=[('o', 1, '5/1')],
                        overall='5/1')
        audit = report(src, document)['audit']
        self.assertEqual(audit['worst_cost'], 0)
        self.assertNotIn('variable', audit['policy'])
        self.assertIn('groups', audit['policy'])

    def test_revealed_correlation_stops_the_policy(self):
        # Activity emissions are x and 1 - x.  Each activity stays uncertain,
        # but the group total is exactly 1, so no query may be charged.
        src = source([activity('a', quantity=0), activity('b', quantity=1)],
                     [factor('f', '1/1')])
        document = spec(
            variables=('x',),
            quantities=[quantity('a', {'x': 1}),
                        quantity('b', {'x': -1})],
            costs={'x': 5}, groups=[('o', 1, '0/1')])
        result = report(src, document)
        self.assertEqual([(a['minimum'], a['maximum'])
                          for a in result['activities']],
                         [('0', '1'), ('0', '1')],
                         msg=result['activities'])
        self.assertEqual(result['audit']['worst_cost'], 0)
        self.assertNotIn('variable', result['audit']['policy'])

    def test_fractional_micro_difference_is_exact(self):
        # A rate of 1/3 with a quantity of x gives a width of exactly 1/3.
        src = source([activity('a', quantity=0)], [factor('f', '1/3')])
        document = spec(variables=('x',),
                        quantities=[quantity('a', {'x': 1})],
                        costs={'x': 1}, groups=[('o', 1, '1/3')],
                        overall='1/3')
        self.assertEqual(report(src, document)['audit']['worst_cost'], 0)
        # 333333/1000000 < 1/3 by 1/3000000: still wider, so x must be bought.
        document['precision']['groups'][0]['tolerance'] = '333333/1000000'
        shrunk = report(src, document)
        self.assertEqual(shrunk['audit']['worst_cost'], 1)
        self.assertEqual(shrunk['overall']['maximum'], '1/3')
        # A tolerance of exactly the width is enough; one unit less is not.
        document['precision']['groups'][0]['tolerance'] = '1/3'
        self.assertEqual(report(src, document)['audit']['worst_cost'], 0)
        document['precision']['groups'][0]['tolerance'] = '1/3'
        document['precision']['overall'] = '0/1'
        self.assertEqual(report(src, document)['audit']['worst_cost'], 1)


# ---------------------------------------------------------------------------
# linked acceptance: shared risk across activities and factors
# ---------------------------------------------------------------------------
def linked_source():
    return source(
        [activity('line-a', quantity=1, fid='grid', category='electricity',
                  unit='kwh'),
         activity('line-b', quantity=0, fid='gas', category='natural-gas',
                  unit='m3')],
        [factor('grid', '0/1', 'electricity', 'kwh'),
         factor('gas', '0/1', 'natural-gas', 'm3')])


def linked_spec(costs):
    return spec(
        variables=('x', 'y', 'z'),
        quantities=[quantity('line-a', {'x': -1}),
                    quantity('line-b', {'x': 1})],
        rates=[rate('grid', {'y': '1/1'}), rate('gas', {'z': '1/1'})],
        costs=costs, groups=[('o', 1, '0/1')])


class LinkedAcceptance(unittest.TestCase):
    def test_initial_interval_and_adaptive_branches(self):
        result = report(linked_source(),
                        linked_spec({'x': 1, 'y': 2, 'z': 2}))
        self.assertEqual((result['overall']['minimum'],
                          result['overall']['maximum']), ('0', '1'))
        audit = result['audit']
        self.assertEqual(audit['status'], 'possible')
        self.assertEqual(audit['worst_cost'], 3)
        root = audit['policy']
        self.assertEqual((root['variable'], root['worst_cost']), ('x', 3))
        self.assertEqual(root['zero']['variable'], 'y')
        self.assertEqual(root['one']['variable'], 'z')
        # Answers are followed by exact stopping intervals, not initial ones.
        for branch in (root['zero'], root['one']):
            for answer in ('zero', 'one'):
                leaf = branch[answer]
                self.assertIn('groups', leaf)
                self.assertEqual(leaf['overall']['minimum'],
                                 leaf['overall']['maximum'])
        self.assertEqual(root['zero']['zero']['overall']['minimum'], '0')
        self.assertEqual(root['zero']['one']['overall']['minimum'], '1')
        self.assertEqual(root['one']['zero']['overall']['minimum'], '0')
        self.assertEqual(root['one']['one']['overall']['minimum'], '1')

    def test_unauditable_y_makes_the_whole_tree_impossible(self):
        audit = report(linked_source(),
                       linked_spec({'x': 1, 'y': None, 'z': 2}))['audit']
        self.assertEqual(audit['status'], 'impossible')
        self.assertIsNone(audit['policy'])
        witness = audit['witness']
        self.assertEqual(sorted(witness['observations']), ['x', 'z'])
        self.assertEqual(witness['observations']['x'], 0)
        self.assertGreater(Fraction(witness['maximum']),
                           Fraction(witness['minimum'])
                           + Fraction(witness['tolerance']))
        for witness_state in (witness['min_witness'],
                              witness['max_witness']):
            self.assertEqual(witness_state[0],
                             witness['observations']['x'])
            self.assertEqual(witness_state[2],
                             witness['observations']['z'])

    def test_posterior_narrowing_changes_the_next_question(self):
        src = linked_source()
        document = linked_spec({'x': 1, 'y': 2, 'z': 2})
        scenario = build(src, document)
        direct = Policy(scenario)
        budget = Budget(scenario)
        ids = scenario.variable_ids
        x_zero = scenario.mask_for({ids.index('x'): 0})
        x_one = scenario.mask_for({ids.index('x'): 1})
        self.assertEqual(direct.nodes[x_zero]['variable'], 'y')
        self.assertEqual(direct.nodes[x_one]['variable'], 'z')
        self.assertEqual(direct.nodes[x_zero]['worst_cost'], 2)
        self.assertEqual(direct.nodes[x_one]['worst_cost'], 2)
        self.assertEqual(budget.nodes[x_zero]['variable'], 'y')
        self.assertEqual(budget.nodes[x_one]['variable'], 'z')


# ---------------------------------------------------------------------------
# report shape, ordering and determinism
# ---------------------------------------------------------------------------
class ReportShape(unittest.TestCase):
    def test_sorted_blocks_and_nominal(self):
        src = source([activity('b', organization='z', scope=2),
                      activity('a', organization='a', scope=1)])
        document = spec(variables=('b-var', 'a-var'),
                        costs={'a-var': None, 'b-var': None},
                        groups=[('z', 2, '0/1'), ('a', 1, '0/1')])
        result = report(src, document)
        self.assertEqual(result['variable_ids'], ['a-var', 'b-var'])
        self.assertEqual([a['id'] for a in result['activities']],
                         ['a', 'b'])
        self.assertEqual([(g['organization'], g['scope'])
                          for g in result['groups']],
                         [('a', 1), ('z', 2)])
        self.assertEqual(result['nominal'], {
            'activities': [
                {'id': 'a', 'organization': 'a', 'date': '2026-06-01',
                 'scope': 1, 'category': 'c', 'quantity': 0, 'unit': 'u',
                 'factor_id': 'f', 'kgco2e': '0'},
                {'id': 'b', 'organization': 'z', 'date': '2026-06-01',
                 'scope': 2, 'category': 'c', 'quantity': 0, 'unit': 'u',
                 'factor_id': 'f', 'kgco2e': '0'}],
            'totals': [{'organization': 'a', 'scope': 1, 'kgco2e': '0'},
                       {'organization': 'z', 'scope': 2, 'kgco2e': '0'}],
        })
        self.assertEqual(result['schema_version'], 4)
        self.assertEqual(result['source_sha256'], '0' * 64)
        self.assertEqual(result['uncertainty_sha256'], '1' * 64)

    def test_variable_vector_order_follows_sorted_ids(self):
        document = spec(variables=('z', 'a'),
                        costs={'z': None, 'a': None},
                        groups=[('o', 1, '0/1')])
        result = report(source([activity('a')]), document)
        self.assertEqual(result['variable_ids'], ['a', 'z'])
        for block in [result['overall']] + result['groups']:
            self.assertEqual(len(block['min_witness']), 2)
            self.assertEqual(len(block['max_witness']), 2)


class ScaleAcceptance(unittest.TestCase):
    """The ten-variable limit is enumerable, not an excuse for wrong policy."""

    def test_full_ten_variable_domain_is_solved_and_verified(self):
        names = ['v%d' % i for i in range(10)]
        activities = [activity('a%d' % i, quantity=9)
                      for i in range(10)]
        quantities = [quantity('a%d' % i, {'v%d' % i: 1})
                      for i in range(10)]
        document = spec(variables=names, quantities=quantities,
                        costs={name: 1 for name in names},
                        groups=[('o', 1, '0/1')])
        result = report(source(activities), document)
        self.assertEqual(len(build(source(activities), document).states),
                         2 ** 10)
        self.assertEqual(result['audit']['worst_cost'], 10)
        # Every variable must be queried along every path, and no variable
        # may be paid twice.
        depths = set()

        def walk(node, seen):
            if 'variable' not in node:
                depths.add(len(seen))
                return
            self.assertNotIn(node['variable'], seen)
            walk(node['zero'], seen | {node['variable']})
            walk(node['one'], seen | {node['variable']})

        walk(result['audit']['policy'], frozenset())
        self.assertEqual(depths, {10})

    def test_shared_factor_across_groups(self):
        # One rate variable feeds two organizations' activities, so the two
        # group intervals must move together, not independently.
        factors = [factor('shared', '1/1')]
        activities = [activity('a', quantity=1, organization='x', fid='shared'),
                      activity('b', quantity=1, organization='y', fid='shared')]
        document = spec(
            variables=('y',),
            rates=[rate('shared', {'y': '-1/2'})],
            costs={'y': None},
            groups=[('y', 1, '0/1'), ('x', 1, '0/1')])
        result = report(source(activities, factors), document)
        self.assertEqual([(g['minimum'], g['maximum'])
                          for g in result['groups']],
                         [('1/2', '1'), ('1/2', '1')])
        self.assertEqual((result['overall']['minimum'],
                          result['overall']['maximum']), ('1', '2'))

    def test_cross_group_target_correlation(self):
        # Group x is x, group y is 1 - x: each group stays uncertain within
        # its own tolerance, but their sum is exactly 1.  The overall target
        # is already precise, so the adaptive policy must stop immediately.
        activities = [activity('a', quantity=0, organization='x'),
                      activity('b', quantity=1, organization='y')]
        document = spec(
            variables=('x',),
            quantities=[quantity('a', {'x': 1}),
                        quantity('b', {'x': -1})],
            costs={'x': 7},
            groups=[('y', 1, '1/1'), ('x', 1, '1/1')])
        result = report(source(activities), document)
        self.assertEqual(result['audit']['worst_cost'], 0)
        self.assertNotIn('variable', result['audit']['policy'])

    def test_constraint_fixed_variable_is_never_paid_for(self):
        # x is pinned to 0 by a constraint, so its 9-unit price is never
        # charged; y alone must be audited.
        document = spec(
            variables=('x', 'y'),
            constraints=[constraint('pin', {'x': 1}, 0, 0)],
            quantities=[quantity('a', {'x': 1, 'y': 1})],
            costs={'x': 9, 'y': 2},
            groups=[('o', 1, '0/1')])
        result = report(source([activity('a')]), document)
        policy = result['audit']['policy']
        self.assertEqual((policy['variable'], policy['worst_cost']),
                         ('y', 2))
        self.assertEqual(policy['cost'], 2)

    def test_answers_that_make_other_answers_unreachable_are_not_queried(self):
        # x + y = 1: once x is answered, y is known and must not be paid.
        document = spec(
            variables=('x', 'y'),
            constraints=[constraint('sum', {'x': 1, 'y': 1}, 1, 1)],
            quantities=[quantity('a', {'x': 1, 'y': -1})],
            costs={'x': 1, 'y': 4},
            groups=[('o', 1, '0/1')])
        result = report(source([activity('a', quantity=10)]), document)
        policy = result['audit']['policy']
        self.assertEqual(policy['variable'], 'x')
        for branch in ('zero', 'one'):
            child = policy[branch]
            self.assertNotIn('variable', child)
            self.assertEqual(child['overall']['minimum'],
                             child['overall']['maximum'])
        self.assertEqual(result['audit']['worst_cost'], 1)

    def test_irrelevant_paid_variable_is_never_bought(self):
        # y changes nothing; paying for it cannot improve the worst case.
        document = spec(
            variables=('x', 'y'),
            quantities=[quantity('a', {'x': 1})],
            costs={'x': 1, 'y': 5},
            groups=[('o', 1, '0/1')])
        result = report(source([activity('a')]), document)
        self.assertEqual(result['audit']['policy']['variable'], 'x')
        self.assertEqual(result['audit']['worst_cost'], 1)

    def test_constraint_makes_one_answer_unreachable(self):
        # x = y.  Answering x determines y, so y is never queried, and the
        # infeasible combination x=0,y=1 is not a failure branch.
        document = spec(
            variables=('x', 'y'),
            constraints=[constraint('same', {'x': 1, 'y': -1}, 0, 0)],
            quantities=[quantity('a', {'x': 1, 'y': 1})],
            costs={'x': 1, 'y': 6},
            groups=[('o', 1, '0/1')])
        result = report(source([activity('a', quantity=3)]), document)
        policy = result['audit']['policy']
        self.assertEqual((policy['variable'], policy['worst_cost']), ('x', 1))
        self.assertNotIn('variable', policy['zero'])
        self.assertNotIn('variable', policy['one'])
        # Emission is 3 + x + y: x=y=0 gives 3, x=y=1 gives 5.
        self.assertEqual(policy['zero']['overall']['minimum'], '3')
        self.assertEqual(policy['one']['overall']['minimum'], '5')

    def test_limits_are_accepted_at_the_boundary(self):
        factors = [factor('f%02d' % i) for i in range(16)]
        activities = [activity('a%02d' % i, quantity=9, fid='f00')
                      for i in range(40)]
        # Unreferenced variables are legal, so the catalogue is precise even
        # though ten variables are declared and none is auditable.
        document = spec(
            variables=tuple('v%d' % i for i in range(10)),
            costs={('v%d' % i): None for i in range(10)},
            groups=[('o', 1, '0/1')])
        result = report(source(activities, factors), document)
        self.assertEqual(len(result['activities']), 40)
        self.assertEqual(len(result['variable_ids']), 10)
        self.assertEqual(result['audit']['status'], 'possible')

    def test_zero_cost_variable_still_moves_the_policy(self):
        # y is free and irrelevant; x costs 1 and changes the answer.  The
        # smallest-ID tie-break applies only among equal-cost queries.
        document = spec(
            variables=('x', 'y'),
            quantities=[quantity('a', {'x': 1})],
            rates=[rate('f', {'y': '1/2'})],
            costs={'x': 1, 'y': 0},
            groups=[('o', 1, '0/1')])
        result = report(source([activity('a', quantity=2)], [factor('f', '1/1')]),
                        document)
        policy = result['audit']['policy']
        self.assertEqual(policy['variable'], 'x')
        self.assertEqual(policy['worst_cost'], 1)


# ---------------------------------------------------------------------------
# independent oracle comparison
# ---------------------------------------------------------------------------
class FeasibilityCrossCheck(unittest.TestCase):
    """A brute-force 'reveal every auditable variable' baseline must agree
    with the recursive policy on feasibility, even though it is not optimal
    in cost."""

    def test_feasibility_matches_reveal_everything(self):
        import random
        generator = random.Random(424242)
        for case in range(30):
            names = ['v%d' % i for i in range(generator.randint(0, 4))]
            factors = [factor('f0', '%d/5' % generator.randint(0, 4)),
                       factor('f1', '%d/7' % generator.randint(0, 4))]
            activities = [activity('a0', quantity=generator.randint(1, 6),
                                   fid='f0'),
                          activity('a1', quantity=generator.randint(0, 6),
                                   fid='f1')]
            quantities = []
            for aid in ('a0', 'a1'):
                terms = {name: generator.choice((-1, 0, 1))
                         for name in names}
                quantities.append(quantity(
                    aid, {k: v for k, v in terms.items() if v}))
            rates = []
            for fid in ('f0', 'f1'):
                terms = {}
                for name in names:
                    if generator.random() < 0.4:
                        terms[name] = '%d/%d' % (
                            generator.choice((-1, 1)),
                            generator.choice((1, 2, 3)))
                rates.append(rate(fid, terms))
            constraints = []
            for cid in range(generator.randint(0, 2)):
                terms = {name: generator.choice((-1, 0, 1))
                         for name in names}
                lower = generator.randint(-2, 1)
                constraints.append(constraint(
                    'c%d' % cid, {k: v for k, v in terms.items() if v},
                    lower, lower + generator.randint(0, 2)))
            groups = sorted({(a['organization'], a['scope'])
                             for a in activities})
            document = spec(
                variables=names, constraints=constraints,
                quantities=quantities, rates=rates,
                costs={name: generator.choice((None, 0, 1, 2))
                       for name in names},
                groups=[(o, s, generator.choice(('0/1', '1/2', '1/1')))
                        for o, s in groups],
                overall=generator.choice(('0/1', '1/2', '3/1')))
            src = source(activities, factors)
            try:
                scenario = build(src, document)
            except ValueError:
                continue
            if not scenario.states:
                continue
            oracle = Oracle(src, document)
            auditable = [name for name, cost in document['audit_costs'].items()
                         if cost is not None]
            # "Reveal everything": if any complete auditable answer set still
            # misses precision, no adaptive policy can succeed at any cost.
            resolvable = True
            for values in itertools.product((0, 1), repeat=len(auditable)):
                observations = dict(zip(auditable, values))
                if oracle.matching(observations) and not oracle.within(
                        observations):
                    resolvable = False
                    break
            policy = Policy(scenario)
            self.assertEqual(policy.root() is not None, resolvable, case)
            # Every constrained answer set has at least one state.
            for values in itertools.product((0, 1), repeat=len(names)):
                assignment = dict(zip(names, values))
                compiled = all(lower <= sum(c * assignment[v]
                                            for v, c in terms.items()) <= upper
                               for terms, lower, upper in oracle.constraints)
                self.assertEqual(
                    bool(oracle.matching(assignment)), compiled)


class OracleComparison(unittest.TestCase):
    def compare(self, src, document):
        oracle = Oracle(src, document)
        try:
            scenario = build(src, document)
        except ValueError as error:
            # The only acceptable domain rejection is a genuinely feasible
            # negative state, which the oracle must also see.
            self.assertTrue(oracle.legality_errors(), str(error))
            return
        self.assertEqual(oracle.legality_errors(), [])
        if not scenario.states:
            self.assertFalse(oracle.states)
            return
        self.assertEqual(
            [tuple(state) for state in scenario.states],
            [tuple(state) for state in oracle.states])
        direct = Policy(scenario)
        budget = Budget(scenario)

        # Every answer pattern over every auditable variable is a posterior
        # the policy may have to face.
        auditable = sorted(name for name, cost in document['audit_costs'].items()
                           if cost is not None)
        for size in range(len(auditable) + 1):
            for subset in itertools.combinations(auditable, size):
                for values in itertools.product((0, 1), repeat=len(subset)):
                    observations = dict(zip(subset, values))
                    matching = oracle.matching(observations)
                    if not matching:
                        continue
                    mask = scenario.mask_for(
                        {scenario.variable_ids.index(name): value
                         for name, value in observations.items()})
                    self.assertTrue(mask)
                    expected = oracle.endpoints(observations)
                    lows, highs = scenario.endpoints(mask)
                    for t, (kind, key) in enumerate(oracle.targets):
                        low, high, low_state, high_state = expected[key]
                        self.assertEqual(
                            scenario.target_values[t][lows[t]],
                            low * scenario.denominator)
                        self.assertEqual(
                            scenario.target_values[t][highs[t]],
                            high * scenario.denominator)
                        self.assertEqual(
                            tuple(scenario.states[lows[t]]), low_state)
                        self.assertEqual(
                            tuple(scenario.states[highs[t]]), high_state)
                    for name, cost in document['audit_costs'].items():
                        if name in observations:
                            continue
                        index = scenario.variable_ids.index(name)
                        self.assertEqual(
                            bool(mask & scenario.masks[index][0]),
                            bool(oracle.matching({**observations, name: 0})))
                    oracle_cost, oracle_choice = oracle.policy_cost(
                        tuple(sorted(observations.items())))
                    # Force the exact posterior cost function for this answer
                    # pattern even when no optimal policy reaches it.
                    node = direct._solve(mask)
                    self.assertIs(node, direct.nodes[mask])
                    if oracle_cost is None:
                        self.assertIsNone(node)
                        self.assertIsNone(budget.build(mask))
                    else:
                        self.assertEqual(node['worst_cost'], oracle_cost)
                        self.assertEqual(budget.build(mask)['worst_cost'],
                                         oracle_cost)
                        if oracle_choice is not None:
                            self.assertEqual(node['variable'], oracle_choice)
                            self.assertEqual(budget.build(mask)['variable'],
                                             oracle_choice)
                        else:
                            self.assertEqual(node['kind'], 'stop')
                            self.assertEqual(node['worst_cost'], 0)

    def test_prescribed_shared_risk_instance(self):
        self.compare(linked_source(), linked_spec({'x': 1, 'y': 2, 'z': 2}))
        self.compare(linked_source(),
                     linked_spec({'x': 1, 'y': None, 'z': 2}))

    def test_random_instances(self):
        import random
        generator = random.Random(20261117)
        for case in range(25):
            names = ['v%d' % i for i in range(generator.randint(0, 3))]
            groups = [('o', 1, '0/1')]
            factors = [factor('f0', '%d/4' % generator.randint(0, 3)),
                       factor('f1', '%d/3' % generator.randint(0, 3))]
            activities = [
                activity('a0', quantity=generator.randint(1, 4), fid='f0'),
                activity('a1', quantity=generator.randint(1, 4), fid='f1'),
            ]
            quantities = []
            for aid in ('a0', 'a1'):
                terms = {name: generator.choice((-1, 0, 1))
                         for name in names}
                terms = {k: v for k, v in terms.items() if v}
                quantities.append(quantity(aid, terms))
            rates = []
            for fid in ('f0', 'f1'):
                terms = {}
                for name in names:
                    numerator = generator.choice((-1, 0, 1))
                    if numerator:
                        terms[name] = '%d/%d' % (
                            numerator, generator.choice((1, 2)))
                rates.append(rate(fid, terms))
            constraints = []
            for cid in range(generator.randint(0, 2)):
                terms = {name: generator.choice((-1, 0, 1, 2))
                         for name in names}
                terms = {k: v for k, v in terms.items() if v}
                lower = generator.randint(-2, 1)
                constraints.append(
                    constraint('c%d' % cid, terms, lower,
                               lower + generator.randint(0, 3)))
            costs = {name: generator.choice((None, 0, 1, 2, 3))
                     for name in names}
            overall = generator.choice(('0/1', '1/2', '1/1', '3/2'))
            group_tolerance = generator.choice(('0/1', '1/4', '1/1', '4/1'))
            document = spec(
                variables=names, constraints=constraints,
                quantities=quantities, rates=rates, costs=costs,
                groups=[('o', 1, group_tolerance)], overall=overall)
            self.compare(source(activities, factors), document)


# ---------------------------------------------------------------------------
# command line behaviour
# ---------------------------------------------------------------------------
class CommandLine(unittest.TestCase):
    def call(self, *arguments):
        return subprocess.run(
            [sys.executable, '-m', 'carbon_inventory.cli',
             *map(str, arguments)],
            capture_output=True, text=True,
            env={'PYTHONPATH': str(ROOT / 'src'), 'PATH': '/usr/bin:/bin'})

    def test_demo_and_byte_determinism(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'audit.json'
            args = ('--source', ROOT / 'fixtures/audit/input.json',
                    '--uncertainty', ROOT / 'fixtures/audit/uncertainty.json',
                    '--output', out)
            first = self.call(*args)
            self.assertEqual(first.returncode, 0, first.stderr)
            payload = out.read_bytes()
            again = self.call(*args)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(payload, out.read_bytes())
            document = json.loads(payload)
            self.assertEqual(document['schema_version'], 4)
            self.assertEqual(document['audit']['worst_cost'], 3)
            self.assertEqual(document['audit']['policy']['variable'], 'x')

    def test_impossible_witness_tampering_is_refused(self):
        src = source([activity('a', quantity=0)])
        document = spec(
            variables=('x', 'u'),
            quantities=[quantity('a', {'x': 1, 'u': 1})],
            costs={'x': 1, 'u': None}, groups=[('o', 1, '0/1')])
        clean = run_audit(src, '0' * 64, document, '1' * 64)
        verify(src, document, clean)
        self.assertEqual(clean['audit']['status'], 'impossible')
        for mutate in (
                lambda r: r['audit']['witness'].__setitem__(
                    'observations', {}),
                lambda r: r['audit']['witness'].__setitem__(
                    'max_witness', [0, 0]),
                lambda r: r['audit']['witness'].__setitem__(
                    'tolerance', '5/1'),
                lambda r: r['audit'].__setitem__('status', 'possible')):
            import copy
            tampered = copy.deepcopy(clean)
            mutate(tampered)
            with self.assertRaises(AssertionError):
                verify(src, document, tampered)

    def test_impossible_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'audit.json'
            result = self.call(
                '--source', ROOT / 'fixtures/audit/input.json',
                '--uncertainty',
                ROOT / 'fixtures/audit/uncertainty-impossible.json',
                '--output', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            audit = json.loads(out.read_bytes())['audit']
            self.assertEqual(audit['status'], 'impossible')
            self.assertIsNone(audit['worst_cost'])
            self.assertIsNone(audit['policy'])
            self.assertEqual(audit['witness']['observations']['x'], 0)

    def test_cancel_demo_stops_without_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'audit.json'
            result = self.call(
                '--source', ROOT / 'fixtures/audit/cancel-input.json',
                '--uncertainty',
                ROOT / 'fixtures/audit/cancel-uncertainty.json',
                '--output', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(out.read_bytes())
            self.assertEqual(document['audit']['worst_cost'], 0)
            self.assertNotIn('variable', document['audit']['policy'])

    def test_mode_mutual_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'out.json'
            spec_path = ROOT / 'fixtures/audit/uncertainty.json'
            src = ROOT / 'fixtures/audit/input.json'
            boundary = ROOT / 'fixtures/group-boundary.json'
            batch = ROOT / 'fixtures/registry/batch-01-foundation.json'
            registry = Path(directory) / 'ledger.db'
            for arguments in (
                    ('--uncertainty', spec_path, '--output', out),
                    ('--source', src, '--uncertainty', spec_path,
                     '--registry', registry, '--output', out),
                    ('--source', src, '--uncertainty', spec_path,
                     '--batch', batch, '--output', out),
                    ('--source', src, '--uncertainty', spec_path,
                     '--boundary', boundary, '--root', 'group-hq',
                     '--output', out),
                    ('--source', src, '--uncertainty', spec_path,
                     '--root', 'group-hq', '--output', out)):
                result = self.call(*arguments)
                self.assertEqual(result.returncode, 2, arguments)
                self.assertFalse(out.exists())

    def test_distinct_paths_and_safe_output(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            out = directory / 'out.json'
            src = directory / 'in.json'
            src.write_bytes((ROOT / 'fixtures/audit/input.json').read_bytes())
            spec_path = directory / 'spec.json'
            spec_path.write_bytes(
                (ROOT / 'fixtures/audit/uncertainty.json').read_bytes())
            self.assertEqual(self.call('--source', src, '--uncertainty', src,
                                       '--output', out).returncode, 2)
            self.assertEqual(self.call('--source', src, '--uncertainty',
                                       spec_path, '--output', src).returncode,
                             2)
            self.assertEqual(self.call('--source', src, '--uncertainty',
                                       spec_path, '--output',
                                       spec_path).returncode, 2)
            link = directory / 'link.json'
            link.symlink_to(src)
            self.assertEqual(self.call('--source', src, '--uncertainty',
                                       spec_path, '--output',
                                       link).returncode, 2)
            self.assertTrue(src.exists())

    def test_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            out = directory / 'out.json'
            out.write_text('prior')
            src = directory / 'in.json'
            src.write_bytes((ROOT / 'fixtures/audit/input.json').read_bytes())
            for body in ('{}', '{"variables":[]}', 'NaN',
                         json.dumps(spec(variables=('x',), costs={},
                                         groups=[]))):
                bad = directory / 'bad.json'
                bad.write_text(body)
                result = self.call('--source', src, '--uncertainty', bad,
                                   '--output', out)
                self.assertEqual(result.returncode, 2, body)
                self.assertEqual(out.read_text(), 'prior')

    def test_tampered_report_is_refused(self):
        src = source([activity('a')])
        document = spec(variables=('x',),
                        quantities=[quantity('a', {'x': 1})],
                        costs={'x': 3}, groups=[('o', 1, '0/1')])
        clean = run_audit(src, '0' * 64, document, '1' * 64)
        verify(src, document, clean)
        for mutate in (
                lambda r: r['overall'].__setitem__('maximum', '0'),
                lambda r: r['audit'].__setitem__('worst_cost', 2),
                lambda r: r['audit']['policy'].__setitem__('variable', 'y'),
                lambda r: r['activities'][0].__setitem__('minimum', '-1'),
                lambda r: r['audit']['policy']['zero'][
                    'overall'].__setitem__('maximum', '1')):
            import copy
            tampered = copy.deepcopy(clean)
            mutate(tampered)
            with self.assertRaises(AssertionError):
                verify(src, document, tampered)

    def test_legacy_source_output_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'report.json'
            result = self.call('--source', ROOT / 'fixtures/input.json',
                               '--output', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(out.read_bytes())
            self.assertEqual(document['schema_version'], 1)
            self.assertEqual([(t['scope'], t['kgco2e'])
                              for t in document['result']['totals']],
                             [(1, '190'), (2, '400')])
            self.assertEqual(
                document['source_sha256'],
                __import__('hashlib').sha256(
                    (ROOT / 'fixtures/input.json').read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
