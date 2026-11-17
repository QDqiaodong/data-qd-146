"""Independent back-substitution of a schema-4 audit report before publication.

The report is never published on the strength of the code that built it.
This module re-parses the source and the uncertainty document, re-enumerates
Omega and recomputes every emission with plain Fraction arithmetic, then
checks the exact bytes of the report against that independent computation:

* ``nominal`` is exactly the legacy ``analyze(source)`` result;
* every published endpoint, witness and interval is reproduced by scanning
  the posterior states directly, and witnesses really are feasible states;
* a stop node is only published where the posterior already meets every
  tolerance, and a query node names an unpaid, unrevealed auditable variable
  whose both answers are reachable, with
  ``worst_cost == cost + max(child costs)``;
* the whole reported tree equals the bound-then-extract ``Budget`` tree, so
  the two independent strategy designs agree at every node;
* an ``impossible`` audit carries an auditable-complete answer set whose two
  witnesses agree on every auditable variable and still exceed precision.

Any mismatch raises, so the caller exits 2 and the previous output survives.
"""
import itertools
from fractions import Fraction

from .inventory import calculate
from .model import parse_inventory
from .strategy import Budget, Policy
from .uncertainty import ACTIVITY_LIMIT, FACTOR_LIMIT, Scenario, parse_uncertainty


def _rows(factors, activities, spec):
    """All feasible states with Fractions: (state, activities, groups, all)."""
    names = spec['variable_ids']
    quantity_terms = spec['quantity_terms']
    rate_terms = spec['rate_terms']
    ordered = sorted(activities, key=lambda a: a['id'])
    for state in itertools.product((0, 1), repeat=len(names)):
        if any(not c['lower'] <= sum(coeff * state[i]
                                     for i, coeff in c['terms'])
               <= c['upper'] for c in spec['constraints']):
            continue
        per_activity = {}
        per_group = {}
        overall = Fraction(0)
        for activity in ordered:
            aid = activity['id']
            fid = activity['factor_id']
            quantity = activity['quantity'] + sum(
                coeff * state[i] for i, coeff in quantity_terms.get(aid, ()))
            value = factors[fid]['rate'] + sum(
                coeff * state[i] for i, coeff in rate_terms.get(fid, ()))
            if quantity < 0:
                raise AssertionError(
                    'verifier found negative quantity: %r at %s'
                    % (aid, state))
            if value < 0:
                raise AssertionError(
                    'verifier found negative rate: %r at %s' % (fid, state))
            emission = quantity * value
            per_activity[aid] = emission
            key = (activity['organization'], activity['scope'])
            per_group[key] = per_group.get(key, Fraction(0)) + emission
            overall += emission
        for fid, factor in factors.items():
            value = factor['rate'] + sum(
                coeff * state[i] for i, coeff in rate_terms.get(fid, ()))
            if value < 0:
                raise AssertionError(
                    'verifier found negative rate: %r at %s' % (fid, state))
        yield state, per_activity, per_group, overall


def _assert_interval(block, values, states):
    low = min(values)
    high = max(values)
    low_state = states[values.index(low)]
    high_state = states[values.index(high)]
    if block['minimum'] != str(low) or block['maximum'] != str(high):
        raise AssertionError('interval endpoints do not reproduce: %r'
                             % (block,))
    if block['min_witness'] != list(low_state):
        raise AssertionError('minimum witness does not reproduce: %r'
                             % (block,))
    if block['max_witness'] != list(high_state):
        raise AssertionError('maximum witness does not reproduce: %r'
                             % (block,))


def _assert_within(rows, spec):
    by_group = {key: [row[2][key] for row in rows] for key in spec['groups']}
    for key, values in by_group.items():
        if max(values) - min(values) > spec['group_tolerances'][key]:
            raise AssertionError(
                'published stop posterior exceeds group precision: %r' % (key,))
    overall = [row[3] for row in rows]
    if max(overall) - min(overall) > spec['overall_tolerance']:
        raise AssertionError('published stop posterior exceeds overall '
                             'precision')


def _assert_still_needed(rows, spec):
    for key in spec['groups']:
        values = [row[2][key] for row in rows]
        if max(values) - min(values) > spec['group_tolerances'][key]:
            return
    overall = [row[3] for row in rows]
    if max(overall) - min(overall) > spec['overall_tolerance']:
        return
    raise AssertionError('query at a posterior that already meets precision')


def _render(node):
    if node is None:
        return None
    if node['kind'] == 'stop':
        return {'groups': node['groups'], 'overall': node['overall']}
    return {'variable': node['variable'], 'cost': node['cost'],
            'worst_cost': node['worst_cost'],
            'zero': _render(node['zero']), 'one': _render(node['one'])}


def _walk(rows, spec, node, observations):
    index_of = {name: i for i, name in enumerate(spec['variable_ids'])}
    posterior = [row for row in rows
                 if all(row[0][index_of[name]] == value
                        for name, value in observations.items())]
    if not posterior:
        raise AssertionError('published node has an empty posterior')
    if 'variable' not in node:
        if not isinstance(node, dict) or not isinstance(
                node.get('groups'), list) or not isinstance(
                node.get('overall'), dict):
            raise AssertionError('malformed stop node')
        _assert_interval(node['overall'], [row[3] for row in posterior],
                         [row[0] for row in posterior])
        if len(node['groups']) != len(spec['groups']):
            raise AssertionError('stop node has the wrong group list')
        for key in spec['groups']:
            block = next((b for b in node['groups']
                          if (b.get('organization'), b.get('scope')) == key),
                         None)
            if block is None:
                raise AssertionError('stop node is missing group: %r' % (key,))
            _assert_interval(block, [row[2][key] for row in posterior],
                             [row[0] for row in posterior])
        _assert_within(posterior, spec)
        return
    if not isinstance(node, dict) or not isinstance(node.get('variable'), str):
        raise AssertionError('malformed query node')
    for key in ('cost', 'worst_cost', 'zero', 'one'):
        if key not in node:
            raise AssertionError('query node is missing: ' + key)
    name = node['variable']
    if name not in spec['variable_ids']:
        raise AssertionError('query names an undeclared variable: ' + name)
    if name in observations:
        raise AssertionError('query re-observes variable: ' + name)
    # A query node may only appear where more precision is still needed;
    # adding a zero-cost query after the tolerance was met is not optimal.
    _assert_still_needed(posterior, spec)
    cost = spec['audit_costs'][name]
    if cost is None:
        raise AssertionError('query on an unauditable variable: ' + name)
    if node['cost'] != cost:
        raise AssertionError('query cost mismatch: ' + name)
    index = index_of[name]
    children = []
    for value, child in ((0, node['zero']), (1, node['one'])):
        branch = [row for row in posterior if row[0][index] == value]
        if not branch:
            raise AssertionError(
                'query branch is unreachable: %s=%d' % (name, value))
        _walk(rows, spec, child, {**observations, name: value})
        children.append(0 if 'variable' not in child else child['worst_cost'])
    if node['worst_cost'] != cost + max(children):
        raise AssertionError(
            'worst_cost is not cost plus the larger branch: ' + name)


def _verify_impossible(rows, spec, audit):
    witness = audit['witness']
    index_of = {name: i for i, name in enumerate(spec['variable_ids'])}
    observations = witness['observations']
    expected_auditable = {name for name, cost in spec['audit_costs'].items()
                          if cost is not None}
    if set(observations) != expected_auditable:
        raise AssertionError('impossible witness does not fix every '
                             'auditable variable')
    posterior = [row for row in rows
                 if all(row[0][index_of[name]] == value
                        for name, value in observations.items())]
    if not posterior:
        raise AssertionError('impossible witness observations are infeasible')
    states = [list(row[0]) for row in posterior]
    target = witness['target']
    if not isinstance(target, dict) or target.get('kind') not in (
            'overall', 'group'):
        raise AssertionError('impossible witness has no valid target')
    if target['kind'] == 'overall':
        values = [row[3] for row in posterior]
        tolerance = spec['overall_tolerance']
    else:
        key = (target['organization'], target['scope'])
        if key not in spec['group_tolerances']:
            raise AssertionError('impossible witness names an unknown group')
        values = [row[2][key] for row in posterior]
        tolerance = spec['group_tolerances'][key]
    if witness['min_witness'] not in states:
        raise AssertionError('impossible minimum witness is not in posterior')
    if witness['max_witness'] not in states:
        raise AssertionError('impossible maximum witness is not in posterior')
    # A witness is only evidence if it actually attains the stated endpoint.
    by_state = {tuple(state): value for state, value in zip(states, values)}
    if by_state[tuple(witness['min_witness'])] != min(values):
        raise AssertionError('impossible minimum witness does not attain the '
                             'minimum')
    if by_state[tuple(witness['max_witness'])] != max(values):
        raise AssertionError('impossible maximum witness does not attain the '
                             'maximum')
    if Fraction(witness['minimum']) != min(values):
        raise AssertionError('impossible minimum does not reproduce')
    if Fraction(witness['maximum']) != max(values):
        raise AssertionError('impossible maximum does not reproduce')
    if Fraction(witness['tolerance']) != tolerance:
        raise AssertionError('impossible tolerance does not reproduce')
    if max(values) - min(values) <= tolerance:
        raise AssertionError('impossible witness is actually within '
                             'precision')
    for name, value in observations.items():
        for state in (witness['min_witness'], witness['max_witness']):
            if state[index_of[name]] != value:
                raise AssertionError(
                    'impossible witness contradicts auditable answers')


def verify(source_data, spec_data, report):
    """Raise on any discrepancy; return normally when everything checks."""
    factors, activities = parse_inventory(source_data)
    if len(activities) > ACTIVITY_LIMIT or len(factors) > FACTOR_LIMIT:
        raise AssertionError('verifier sees a source outside the mode limits')
    ids = [a['id'] for a in activities]
    if len(ids) != len(set(ids)):
        raise AssertionError('verifier sees duplicate activity ids')
    if report['nominal'] != calculate(activities):
        raise AssertionError('nominal catalogue is not the legacy result')
    spec = parse_uncertainty(spec_data, factors, activities)
    scenario = Scenario(spec, factors, activities)
    rows = list(_rows(factors, activities, spec))
    if [tuple(r[0]) for r in rows] != [tuple(s) for s in scenario.states]:
        raise AssertionError('verifier Omega differs from the report domain')
    if report['variable_ids'] != spec['variable_ids']:
        raise AssertionError('variable_ids do not match the sorted spec')

    if not rows:
        expected = {'status': 'infeasible', 'activities': [], 'groups': [],
                    'overall': None, 'audit': None}
        for key, value in expected.items():
            if report[key] != value:
                raise AssertionError('infeasible report field mismatch: '
                                     + key)
        return
    if report['status'] != 'feasible':
        raise AssertionError('non-empty Omega must be feasible')

    for activity in sorted(activities, key=lambda a: a['id']):
        block = next(b for b in report['activities']
                     if b['id'] == activity['id'])
        _assert_interval(block, [row[1][activity['id']] for row in rows],
                         [row[0] for row in rows])
    if len(report['activities']) != len(activities):
        raise AssertionError('activity block count mismatch')
    for key in spec['groups']:
        block = next(b for b in report['groups']
                     if (b['organization'], b['scope']) == key)
        _assert_interval(block, [row[2][key] for row in rows],
                         [row[0] for row in rows])
    if len(report['groups']) != len(spec['groups']):
        raise AssertionError('group block count mismatch')
    _assert_interval(report['overall'], [row[3] for row in rows],
                     [row[0] for row in rows])

    policy = Policy(scenario)
    budget = Budget(scenario)
    if _render(policy.root()) != _render(budget.root()):
        raise AssertionError('direct and budget policy designs disagree')
    if policy.root() is None:
        if report['audit']['status'] != 'impossible':
            raise AssertionError('impossible audit published as possible')
        if report['audit']['worst_cost'] is not None:
            raise AssertionError('impossible audit carries a worst cost')
        if report['audit']['policy'] is not None:
            raise AssertionError('impossible audit carries a policy')
        _verify_impossible(rows, spec, report['audit'])
        return
    if report['audit']['status'] != 'possible':
        raise AssertionError('attainable audit published as impossible')
    if report['audit']['worst_cost'] != policy.root()['worst_cost']:
        raise AssertionError('root worst cost does not reproduce')
    _walk(rows, spec, report['audit']['policy'], {})
    if report['audit']['policy'] != _render(policy.root()):
        raise AssertionError('published policy differs from the exact tree')
