"""Shared-risk adaptive inventory audit (v4): specification and state domain.

The domain binds every declared 0/1 variable to every source activity and
factor.  A complete state is one assignment of all variables; Omega is the
finite set of states satisfying every linear constraint.  Quantities stay
exact integers and rates stay exact Fractions.  For efficient exact endpoint
comparison all emissions are additionally kept as integers over one common
denominator; that is a pure scaling, never a floating-point approximation.

Physical legality belongs to the domain, not to the nominal state: every
feasible state must give a non-negative quantity to all activities and a
non-negative rate to all source factors, including factors no activity
references.  One feasible negative state rejects the whole run; states that
the constraints exclude can never cause a false rejection.
"""
import itertools
from fractions import Fraction
from math import lcm

from .common import ident, number, require, rows

VARIABLE_LIMIT = 10
CONSTRAINT_LIMIT = 100
COEFFICIENT_LIMIT = 10 ** 6
BOUND_LIMIT = 10 ** 12
QUANTITY_COEFFICIENT_LIMIT = 10 ** 12
RATE_COEFFICIENT_LIMIT = 10 ** 12
AUDIT_COST_LIMIT = 10 ** 6
ACTIVITY_LIMIT = 40
FACTOR_LIMIT = 16


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('%s must be an integer in [%d, %d]'
                         % (label, minimum, maximum))
    return value


def _rational(value, label):
    """Strict non-negative ``n/d`` string with bounded written numerator."""
    text = ident(value)
    if text.count('/') != 1:
        raise ValueError('%s must be a rational n/d string' % label)
    numerator, denominator = text.split('/')
    if not numerator or not numerator.isdigit():
        raise ValueError('%s has a malformed numerator' % label)
    if not denominator or not denominator.isdigit():
        raise ValueError('%s has a malformed denominator' % label)
    n = int(numerator)
    d = int(denominator)
    if not 0 <= n <= RATE_COEFFICIENT_LIMIT:
        raise ValueError('%s numerator out of bounds' % label)
    if not 1 <= d <= RATE_COEFFICIENT_LIMIT:
        raise ValueError('%s denominator out of bounds' % label)
    return Fraction(n, d)


def _signed_rational(value, label):
    """Strict signed ``n/d`` string; used for rate coefficients."""
    text = ident(value)
    if text.count('/') != 1:
        raise ValueError('%s must be a rational n/d string' % label)
    numerator, denominator = text.split('/')
    sign = ''
    body = numerator
    if body.startswith('-'):
        sign, body = '-', body[1:]
    if not body or not body.isdigit():
        raise ValueError('%s has a malformed numerator' % label)
    if not denominator or not denominator.isdigit():
        raise ValueError('%s has a malformed denominator' % label)
    n = int(sign + body)
    d = int(denominator)
    if abs(n) > RATE_COEFFICIENT_LIMIT or not 1 <= d <= RATE_COEFFICIENT_LIMIT:
        raise ValueError('%s numerator/denominator out of bounds' % label)
    return Fraction(n, d)


def _object(value, keys, label):
    try:
        require(value, keys)
    except ValueError as error:
        raise ValueError('%s: %s' % (label, error))
    return value


def _terms(value, index_of, label, coefficient):
    if not isinstance(value, dict):
        raise ValueError('%s terms must be an object' % label)
    parsed = []
    seen = set()
    for name, raw in value.items():
        if not isinstance(name, str) or name not in index_of:
            raise ValueError('%s terms reference an unknown variable' % label)
        if index_of[name] in seen:
            raise ValueError('%s repeats a variable' % label)
        seen.add(index_of[name])
        parsed.append((index_of[name], coefficient(raw, label)))
    parsed.sort()
    return tuple(parsed)


def _constraint_coefficient(raw, label):
    return _integer(raw, -COEFFICIENT_LIMIT, COEFFICIENT_LIMIT,
                    label + ' coefficient')


def _quantity_coefficient(raw, label):
    return _integer(raw, -QUANTITY_COEFFICIENT_LIMIT,
                    QUANTITY_COEFFICIENT_LIMIT, label + ' coefficient')


def _rate_coefficient(raw, label):
    return _signed_rational(raw, label + ' coefficient')


def parse_uncertainty(document, factors, activities):
    """Validate the specification against already parsed source records."""
    _object(document,
            'variables constraints quantities rates audit_costs precision',
            'uncertainty')

    if len(activities) > ACTIVITY_LIMIT:
        raise ValueError('uncertainty mode allows at most %d activities'
                         % ACTIVITY_LIMIT)
    if len(factors) > FACTOR_LIMIT:
        raise ValueError('uncertainty mode allows at most %d factors'
                         % FACTOR_LIMIT)
    seen_activities = set()
    for activity in activities:
        if activity['id'] in seen_activities:
            raise ValueError('duplicate activity id: ' + activity['id'])
        seen_activities.add(activity['id'])

    variable_ids = []
    for raw in rows(document['variables'], VARIABLE_LIMIT):
        name = ident(raw)
        if name in variable_ids:
            raise ValueError('duplicate variable id: ' + name)
        variable_ids.append(name)
    # Sorted string order is the vector order of every witness.
    variable_ids.sort()
    index_of = {name: i for i, name in enumerate(variable_ids)}

    activity_ids = {a['id'] for a in activities}
    constraints = []
    seen_constraints = set()
    for raw in rows(document['constraints'], CONSTRAINT_LIMIT):
        _object(raw, 'id terms lower upper', 'constraint')
        cid = ident(raw['id'])
        if cid in seen_constraints:
            raise ValueError('duplicate constraint id: ' + cid)
        seen_constraints.add(cid)
        lower = _integer(raw['lower'], -BOUND_LIMIT, BOUND_LIMIT,
                         'constraint lower')
        upper = _integer(raw['upper'], -BOUND_LIMIT, BOUND_LIMIT,
                         'constraint upper')
        if lower > upper:
            raise ValueError('constraint lower exceeds upper')
        constraints.append({
            'id': cid,
            'terms': _terms(raw['terms'], index_of, 'constraint',
                            _constraint_coefficient),
            'lower': lower,
            'upper': upper,
        })

    quantity_terms = {}
    for raw in rows(document['quantities'], ACTIVITY_LIMIT):
        _object(raw, 'activity_id terms', 'quantity')
        aid = ident(raw['activity_id'])
        if aid not in activity_ids:
            raise ValueError('quantity references unknown activity: ' + aid)
        if aid in quantity_terms:
            raise ValueError('duplicate quantity for activity: ' + aid)
        quantity_terms[aid] = _terms(raw['terms'], index_of, 'quantity',
                                     _quantity_coefficient)

    rate_terms = {}
    for raw in rows(document['rates'], FACTOR_LIMIT):
        _object(raw, 'factor_id terms', 'rate')
        fid = ident(raw['factor_id'])
        if fid not in factors:
            raise ValueError('rate references unknown factor: ' + fid)
        if fid in rate_terms:
            raise ValueError('duplicate rate for factor: ' + fid)
        rate_terms[fid] = _terms(raw['terms'], index_of, 'rate',
                                 _rate_coefficient)

    raw_costs = document['audit_costs']
    if not isinstance(raw_costs, dict) or set(raw_costs) != set(variable_ids):
        raise ValueError(
            'audit_costs must cover exactly the declared variable IDs')
    audit_costs = {}
    for name in variable_ids:
        raw = raw_costs[name]
        if raw is None:
            audit_costs[name] = None
        else:
            audit_costs[name] = _integer(raw, 0, AUDIT_COST_LIMIT,
                                         'audit cost')

    precision = _object(document['precision'], 'overall groups', 'precision')
    overall_tolerance = _rational(precision['overall'], 'overall precision')
    groups = sorted({(a['organization'], a['scope']) for a in activities})
    tolerances = {}
    for raw in rows(precision['groups'], len(groups)):
        _object(raw, 'organization scope tolerance', 'precision group')
        key = (ident(raw['organization']), number(raw['scope'], 1, 2))
        if key in tolerances:
            raise ValueError('duplicate precision group')
        tolerances[key] = _rational(raw['tolerance'], 'group tolerance')
    if set(tolerances) != set(groups):
        raise ValueError(
            'precision groups must cover every source organization/scope '
            'exactly once')

    return {
        'variable_ids': variable_ids,
        'constraints': constraints,
        'quantity_terms': quantity_terms,
        'rate_terms': rate_terms,
        'audit_costs': audit_costs,
        'overall_tolerance': overall_tolerance,
        'group_tolerances': tolerances,
        'groups': groups,
    }


class Scenario:
    """Omega, its exact emissions, and exact endpoint queries over masks."""

    def __init__(self, spec, factors, activities):
        self.variable_ids = spec['variable_ids']
        self.constraints = spec['constraints']
        self.quantity_terms = spec['quantity_terms']
        self.rate_terms = spec['rate_terms']
        self.audit_costs = spec['audit_costs']
        self.overall_tolerance = spec['overall_tolerance']
        self.group_tolerances = spec['group_tolerances']
        self.groups = spec['groups']
        self.factors = factors
        self.activities = sorted(activities, key=lambda a: a['id'])

        denominators = [f['rate'].denominator for f in factors.values()]
        for terms in self.rate_terms.values():
            denominators.extend(coeff.denominator for _, coeff in terms)
        self.denominator = 1
        for d in denominators:
            self.denominator = lcm(self.denominator, d)

        self.quantity_nominal = {a['id']: a['quantity']
                                 for a in self.activities}
        self.rate_nominal = {fid: f['rate'] for fid, f in factors.items()}
        self.rate_nominal_scaled = {
            fid: self._scale(value) for fid, value in self.rate_nominal.items()}
        self.rate_terms_scaled = {
            fid: tuple((i, self._scale(coeff)) for i, coeff in terms)
            for fid, terms in self.rate_terms.items()}

        self.states = [
            state for state in itertools.product(
                (0, 1), repeat=len(self.variable_ids))
            if self._satisfies(state)]
        self.masks = [[0, 0] for _ in self.variable_ids]
        for k, state in enumerate(self.states):
            bit = 1 << k
            for i, value in enumerate(state):
                self.masks[i][value] |= bit
        self.all_mask = (1 << len(self.states)) - 1

        # Targets: sorted source groups, then the whole inventory.
        self.target_keys = list(self.groups) + [None]
        self.activity_values = {a['id']: [] for a in self.activities}
        self.target_values = [[] for _ in self.target_keys]
        self._evaluate()
        if self.states:
            self._check_physical()

        self._endpoint_cache = {}

    # -- domain ---------------------------------------------------------
    def _scale(self, value):
        scaled = value * self.denominator
        if scaled.denominator != 1:
            raise AssertionError('common denominator does not divide value')
        return scaled.numerator

    def _satisfies(self, state):
        for constraint in self.constraints:
            total = 0
            for i, coeff in constraint['terms']:
                total += coeff * state[i]
            if not constraint['lower'] <= total <= constraint['upper']:
                return False
        return True

    def quantity(self, state, activity_id):
        total = self.quantity_nominal[activity_id]
        for i, coeff in self.quantity_terms.get(activity_id, ()):
            total += coeff * state[i]
        return total

    def rate_scaled(self, state, factor_id):
        total = self.rate_nominal_scaled[factor_id]
        for i, coeff in self.rate_terms_scaled.get(factor_id, ()):
            total += coeff * state[i]
        return total

    def describe(self, state):
        return '{' + ', '.join('%s=%d' % pair
                               for pair in zip(self.variable_ids,
                                               state)) + '}'

    def _check_physical(self):
        for state in self.states:
            for activity in self.activities:
                value = self.quantity(state, activity['id'])
                if value < 0:
                    raise ValueError(
                        'feasible state has negative quantity for activity '
                        '%r: %d at %s' % (activity['id'], value,
                                          self.describe(state)))
            for factor_id in self.factors:
                value = self.rate_scaled(state, factor_id)
                if value < 0:
                    raise ValueError(
                        'feasible state has negative rate for factor %r: %s '
                        'at %s' % (factor_id,
                                   Fraction(value, self.denominator),
                                   self.describe(state)))

    # -- evaluated emissions -------------------------------------------
    def _evaluate(self):
        for state in self.states:
            per_group = {key: 0 for key in self.groups}
            overall = 0
            for activity in self.activities:
                value = (self.quantity(state, activity['id'])
                         * self.rate_scaled(state, activity['factor_id']))
                self.activity_values[activity['id']].append(value)
                per_group[(activity['organization'],
                           activity['scope'])] += value
                overall += value
            for t, key in enumerate(self.target_keys):
                self.target_values[t].append(
                    overall if key is None else per_group[key])

    # -- masks ----------------------------------------------------------
    def mask_for(self, observations):
        mask = self.all_mask
        for index, value in observations.items():
            mask &= self.masks[index][value]
        return mask

    # -- exact endpoints over a posterior mask --------------------------
    def endpoints(self, mask):
        """Return (min_indices, max_indices), one per target, over ``mask``.

        Recursive splitting on the first variable that still varies shares
        the computation across all posterior masks.  Indices follow the
        enumeration order, which is lexicographic in the complete 0/1
        vector, so choosing the smaller index on a tie is exactly the
        required lexicographically smallest witness.
        """
        cache = self._endpoint_cache
        cached = cache.get(mask)
        if cached is not None:
            return cached
        if not mask:
            raise ValueError('empty posterior has no endpoint')
        split = None
        for i in range(len(self.variable_ids)):
            if (mask & self.masks[i][0]) and (mask & self.masks[i][1]):
                split = i
                break
        if split is None:
            k = (mask & -mask).bit_length() - 1
            lows = (k,) * len(self.target_keys)
            highs = lows
        else:
            zero = self.endpoints(mask & self.masks[split][0])
            one = self.endpoints(mask & self.masks[split][1])
            lows = []
            highs = []
            for t, values in enumerate(self.target_values):
                a, b = zero[0][t], one[0][t]
                if values[a] <= values[b]:
                    lows.append(a)
                else:
                    lows.append(b)
                a, b = zero[1][t], one[1][t]
                if values[a] >= values[b]:
                    highs.append(a)
                else:
                    highs.append(b)
            lows = tuple(lows)
            highs = tuple(highs)
        cache[mask] = (lows, highs)
        return cache[mask]

    def _interval(self, values, low_state, high_state):
        return {
            'minimum': str(Fraction(values[low_state], self.denominator)),
            'maximum': str(Fraction(values[high_state], self.denominator)),
            'min_witness': list(self.states[low_state]),
            'max_witness': list(self.states[high_state]),
        }

    def target_interval(self, mask, target):
        lows, highs = self.endpoints(mask)
        return self._interval(self.target_values[target],
                              lows[target], highs[target])

    def group_interval(self, mask, key):
        return self.target_interval(mask, self.groups.index(key))

    def overall_interval(self, mask):
        return self.target_interval(mask, len(self.groups))

    def interval_block(self, mask):
        """Sorted group intervals plus the overall interval for a posterior."""
        groups = []
        for t, key in enumerate(self.groups):
            interval = {'organization': key[0], 'scope': key[1]}
            interval.update(self.target_interval(mask, t))
            groups.append(interval)
        return {'groups': groups, 'overall': self.overall_interval(mask)}

    def activity_interval_initial(self, activity_id):
        values = self.activity_values[activity_id]
        low_state = high_state = 0
        for k in range(1, len(values)):
            if values[k] < values[low_state]:
                low_state = k
            if values[k] > values[high_state]:
                high_state = k
        return self._interval(values, low_state, high_state)

    def targets(self):
        """(label, target index, tolerance) in deterministic order."""
        for index, key in enumerate(self.groups):
            yield ({'kind': 'group', 'organization': key[0],
                    'scope': key[1]}, index, self.group_tolerances[key])
        yield {'kind': 'overall'}, len(self.groups), self.overall_tolerance

    def within_precision(self, mask):
        lows, highs = self.endpoints(mask)
        for t, values in enumerate(self.target_values):
            width = values[highs[t]] - values[lows[t]]
            tolerance = (self.overall_tolerance if t == len(self.groups)
                         else self.group_tolerances[self.groups[t]])
            if width * tolerance.denominator > tolerance.numerator * self.denominator:
                return False
        return True


def build_scenario(source, spec):
    """Validate source and specification, then construct Omega."""
    factors, activities = source
    parsed = parse_uncertainty(spec, factors, activities)
    return Scenario(parsed, factors, activities)
