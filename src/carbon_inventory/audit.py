"""Report assembly for the shared-risk adaptive audit (schema version 4).

Validation order is fixed: the source document is parsed with the untouched
v1 rules, the catalogue is computed exactly as the legacy mode would, the
uncertainty specification is validated against it, and only then is Omega
enumerated and the policy solved.  Nothing is published unless every result
has been reproduced through its own witness states.
"""
import itertools
from fractions import Fraction

from .inventory import calculate
from .model import parse_inventory
from .strategy import Policy, render_node
from .uncertainty import build_scenario
from .verify import verify


def _exceeds(scenario, mask, target, tolerance):
    lows, highs = scenario.endpoints(mask)
    values = scenario.target_values[target]
    width = values[highs[target]] - values[lows[target]]
    return width * tolerance.denominator > (
        tolerance.numerator * scenario.denominator)


def find_impossible_witness(scenario):
    """A complete auditable answer set whose posterior still misses precision.

    Every auditable variable is fixed, so both endpoint witnesses carry the
    same auditable answers while differing in unauditable ones.  Ties in the
    state search follow the specification's vector order, making the reported
    counterexample deterministic.
    """
    auditable = [i for i, name in enumerate(scenario.variable_ids)
                 if scenario.audit_costs[name] is not None]
    for values in itertools.product((0, 1), repeat=len(auditable)):
        mask = scenario.all_mask
        for index, value in zip(auditable, values):
            mask &= scenario.masks[index][value]
        if not mask or scenario.within_precision(mask):
            continue
        for label, target, tolerance in scenario.targets():
            if not _exceeds(scenario, mask, target, tolerance):
                continue
            lows, highs = scenario.endpoints(mask)
            low_state, high_state = lows[target], highs[target]
            observations = {
                scenario.variable_ids[index]: value
                for index, value in zip(auditable, values)}
            # Both witnesses must agree with the reported answers; this is
            # asserted rather than assumed so a domain bug cannot leak a bad
            # counterexample into the report.
            for state in (scenario.states[low_state],
                          scenario.states[high_state]):
                for index in auditable:
                    if state[index] != observations[scenario.variable_ids[index]]:
                        raise AssertionError(
                            'impossible witness contradicts observations')
            values_by_target = scenario.target_values[target]
            return {
                'observations': observations,
                'target': label,
                'tolerance': str(tolerance),
                'minimum': str(Fraction(values_by_target[low_state],
                                        scenario.denominator)),
                'maximum': str(Fraction(values_by_target[high_state],
                                        scenario.denominator)),
                'min_witness': list(scenario.states[low_state]),
                'max_witness': list(scenario.states[high_state]),
            }
    raise AssertionError('impossible audit without a counterexample')


def run(source_data, source_digest, spec_data, spec_digest):
    """Validate inputs and build the complete schema-4 report."""
    factors, activities = parse_inventory(source_data)
    nominal = calculate(activities)
    scenario = build_scenario((factors, activities), spec_data)
    report = {
        'schema_version': 4,
        'source_sha256': source_digest,
        'uncertainty_sha256': spec_digest,
        'variable_ids': list(scenario.variable_ids),
        'nominal': nominal,
    }
    if not scenario.states:
        report.update({
            'status': 'infeasible',
            'activities': [],
            'groups': [],
            'overall': None,
            'audit': None,
        })
        verify(source_data, spec_data, report)
        return report

    activities_block = []
    for activity in scenario.activities:
        interval = scenario.activity_interval_initial(activity['id'])
        activities_block.append({'id': activity['id'], **interval})
    groups_block = []
    for key in scenario.groups:
        interval = scenario.group_interval(scenario.all_mask, key)
        groups_block.append({'organization': key[0], 'scope': key[1],
                             **interval})
    report.update({
        'status': 'feasible',
        'activities': activities_block,
        'groups': groups_block,
        'overall': scenario.overall_interval(scenario.all_mask),
    })

    policy = Policy(scenario)
    root = policy.root()
    if root is None:
        report['audit'] = {
            'status': 'impossible',
            'worst_cost': None,
            'policy': None,
            'witness': find_impossible_witness(scenario),
        }
    else:
        report['audit'] = {
            'status': 'possible',
            'worst_cost': root['worst_cost'],
            'policy': render_node(root),
        }
    try:
        verify(source_data, spec_data, report)
    except AssertionError as error:
        raise ValueError('audit report failed back-substitution: %s' % error)
    return report
