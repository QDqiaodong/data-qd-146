"""Power certificate revision ledger.

The registry mode keeps an immutable batch history and the current entity
state in one SQLite database.  Every accepted batch advances the generation by
exactly one; a batch whose (batch id, canonical SHA256) was accepted before is
a replay and returns the original generation's stored report byte-for-byte
without advancing the generation.

Validation always runs on the final state after all changes of a batch are
applied, so operation order cannot change the meaning of a batch.
"""
from datetime import date
from fractions import Fraction
import hashlib
import json
import sqlite3

from .allocation import optimize
from .common import ident, number, rows

FACTOR_LIMIT = 1000
ACTIVITY_LIMIT = 20000
SCOPE2_LIMIT = 300
CERTIFICATE_LIMIT = 100
CHANGE_LIMIT = FACTOR_LIMIT + ACTIVITY_LIMIT + CERTIFICATE_LIMIT
KINDS = ('factor', 'activity', 'certificate')


def _day(value):
    if not isinstance(value, str):
        raise ValueError('expected ISO date')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError('invalid ISO date')


def _rate(value):
    """Non-negative rational 'n/d' with the same strictness as factor rates."""
    text = ident(value)
    if '/' not in text:
        raise ValueError('expected rational rate n/d')
    numerator, denominator = text.split('/')
    return Fraction(number(int(numerator), 0, 10 ** 12),
                    number(int(denominator), 1, 10 ** 12))


def validate_factors(raw_factors):
    factors = {}
    for f in rows(raw_factors, FACTOR_LIMIT):
        if not isinstance(f, dict) or set(f) != {
                'id', 'category', 'unit', 'kgco2e_per_unit',
                'valid_from', 'valid_to'}:
            raise ValueError('invalid factor object fields')
        fid = ident(f['id'])
        if fid in factors:
            raise ValueError('duplicate factor id')
        start = _day(f['valid_from'])
        end = None if f['valid_to'] is None else _day(f['valid_to'])
        if end is not None and end <= start:
            raise ValueError('factor interval must be nonempty')
        factors[fid] = {
            'id': fid,
            'category': ident(f['category']),
            'unit': ident(f['unit']),
            'rate': _rate(f['kgco2e_per_unit']),
            'start': start,
            'end': end,
        }
    return factors


def check_factor_entities(factors):
    """Validate factors already in parsed (stored) form."""
    if len(factors) > FACTOR_LIMIT:
        raise ValueError('too many factors')
    for f in factors.values():
        if (set(f) != {'id', 'category', 'unit', 'rate', 'start', 'end'}
                or not isinstance(f['rate'], Fraction) or f['rate'] < 0
                or not isinstance(f['start'], date)
                or (f['end'] is not None and
                    (not isinstance(f['end'], date) or f['end'] <= f['start']))):
            raise ValueError('invalid factor entity')
        ident(f['id']); ident(f['category']); ident(f['unit'])


def validate_activities(raw_activities, factors):
    activities = {}
    scope2 = 0
    for a in rows(raw_activities, ACTIVITY_LIMIT):
        base_keys = {'id', 'organization', 'date', 'scope', 'category',
                     'quantity', 'unit', 'factor_id'}
        if not isinstance(a, dict) or not (set(a) == base_keys or
                                           set(a) == base_keys | {'residual_factor_id'}):
            raise ValueError('invalid activity object fields')
        aid = ident(a['id'])
        scope = number(a['scope'], 1, 2)
        if scope == 2:
            if set(a) != base_keys | {'residual_factor_id'}:
                raise ValueError('scope 2 activity requires residual_factor_id')
            if ident(a['unit']) != 'kwh':
                raise ValueError('scope 2 activity must use kwh')
        elif set(a) != base_keys:
            raise ValueError('scope 1 activity must not carry residual_factor_id')
        entity = {
            'id': aid,
            'organization': ident(a['organization']),
            'date': _day(a['date']),
            'scope': scope,
            'category': ident(a['category']),
            'quantity': number(a['quantity'], 0, 10 ** 12),
            'unit': ident(a['unit']),
            'factor_id': ident(a['factor_id']),
        }
        if scope == 2:
            scope2 += 1
            entity['residual_factor_id'] = ident(a['residual_factor_id'])
        if scope2 > SCOPE2_LIMIT:
            raise ValueError('too many scope 2 activities')
        _check_activity_entity(entity, factors, activities)
        activities[aid] = entity
    return activities


def _check_activity_entity(a, factors, activities):
    """Cross-check one parsed activity against the factor table."""
    aid = a['id']
    if aid in activities:
        raise ValueError('duplicate activity id')
    ident(aid); ident(a['organization']); ident(a['category']); ident(a['unit'])
    if type(a['scope']) is not int or a['scope'] not in (1, 2):
        raise ValueError('invalid activity scope')
    if type(a['quantity']) is not int or not 0 <= a['quantity'] <= 10 ** 12:
        raise ValueError('integer outside allowed bounds')
    if not isinstance(a['date'], date):
        raise ValueError('invalid activity date')
    if a['factor_id'] not in factors:
        raise ValueError('unknown emission factor')
    factor = factors[a['factor_id']]
    if (factor['category'] != a['category'] or factor['unit'] != a['unit']
            or a['date'] < factor['start']
            or (factor['end'] is not None and a['date'] >= factor['end'])):
        raise ValueError('factor does not match activity')
    if a['scope'] == 2:
        if a['unit'] != 'kwh' or 'residual_factor_id' not in a:
            raise ValueError('scope 2 activity must use kwh with residual factor')
        rid = a['residual_factor_id']
        if rid not in factors:
            raise ValueError('unknown residual factor')
        residual = factors[rid]
        if (residual['category'] != a['category']
                or residual['unit'] != a['unit']
                or a['date'] < residual['start']
                or (residual['end'] is not None
                    and a['date'] >= residual['end'])):
            raise ValueError('residual factor does not match activity')
    elif 'residual_factor_id' in a:
        raise ValueError('scope 1 activity must not carry residual_factor_id')


def check_activity_entities(activities, factors):
    """Validate activities already in parsed (stored) form."""
    if len(activities) > ACTIVITY_LIMIT:
        raise ValueError('too many activities')
    scope2 = 0
    seen = {}
    for a in activities.values():
        _check_activity_entity(a, factors, seen)
        seen[a['id']] = a
        if a['scope'] == 2:
            scope2 += 1
    if scope2 > SCOPE2_LIMIT:
        raise ValueError('too many scope 2 activities')


def validate_certificates(raw_certificates):
    certificates = {}
    for c in rows(raw_certificates, CERTIFICATE_LIMIT):
        if not isinstance(c, dict) or set(c) != {
                'id', 'eligible_organizations', 'valid_from', 'valid_to',
                'quantity', 'kgco2e_per_kwh'}:
            raise ValueError('invalid certificate object fields')
        cid = ident(c['id'])
        orgs = c['eligible_organizations']
        if not isinstance(orgs, list) or not orgs:
            raise ValueError('eligible organizations must be a nonempty list')
        normalized = [ident(org) for org in orgs]
        if len(set(normalized)) != len(normalized):
            raise ValueError('duplicate eligible organization')
        start = _day(c['valid_from'])
        end = None if c['valid_to'] is None else _day(c['valid_to'])
        if end is not None and end <= start:
            raise ValueError('certificate interval must be nonempty')
        certificates[cid] = {
            'id': cid,
            'organizations': normalized,
            'start': start,
            'end': end,
            'quantity': number(c['quantity'], 0, 10 ** 12),
            'rate': _rate(c['kgco2e_per_kwh']),
        }
    return certificates


def check_certificate_entities(certificates):
    """Validate certificates already in parsed (stored) form."""
    if len(certificates) > CERTIFICATE_LIMIT:
        raise ValueError('too many certificates')
    seen = set()
    for c in certificates.values():
        if set(c) != {'id', 'organizations', 'start', 'end', 'quantity', 'rate'}:
            raise ValueError('invalid certificate entity')
        cid = ident(c['id'])
        if cid in seen:
            raise ValueError('duplicate certificate id')
        seen.add(cid)
        orgs = c['organizations']
        if not isinstance(orgs, list) or not orgs or any(
                not isinstance(o, str) for o in orgs) or len(set(orgs)) != len(orgs):
            raise ValueError('invalid eligible organizations')
        for org in orgs:
            ident(org)
        if not isinstance(c['start'], date):
            raise ValueError('invalid certificate date')
        if c['end'] is not None and (not isinstance(c['end'], date)
                                     or c['end'] <= c['start']):
            raise ValueError('certificate interval must be nonempty')
        if type(c['quantity']) is not int or not 0 <= c['quantity'] <= 10 ** 12:
            raise ValueError('integer outside allowed bounds')
        if not isinstance(c['rate'], Fraction) or c['rate'] < 0:
            raise ValueError('invalid certificate rate')


def validate_batch(batch):
    if not isinstance(batch, dict) or set(batch) != {
            'batch_id', 'base_generation', 'changes'}:
        raise ValueError('invalid batch object fields')
    batch_id = ident(batch['batch_id'])
    base_generation = batch['base_generation']
    if type(base_generation) is not int or base_generation < 0:
        raise ValueError('base_generation must be a nonnegative integer')
    changes = batch['changes']
    if not isinstance(changes, list) or len(changes) > CHANGE_LIMIT:
        raise ValueError('changes must be a bounded list')
    seen = set()
    normalized = []
    for change in changes:
        if not isinstance(change, dict) or set(change) != {'kind', 'id', 'value'}:
            raise ValueError('invalid change object fields')
        kind = change['kind']
        if kind not in KINDS:
            raise ValueError('unknown change kind')
        cid = ident(change['id'])
        if (kind, cid) in seen:
            raise ValueError('duplicate change target in batch')
        seen.add((kind, cid))
        value = change['value']
        if value is not None:
            if not isinstance(value, dict):
                raise ValueError('change value must be null or a full object')
            if 'id' not in value or value['id'] != cid:
                raise ValueError('change object id must match target id')
        normalized.append({'kind': kind, 'id': cid, 'value': value})
    return batch_id, base_generation, normalized


def canonical_sha256(batch, changes):
    """SHA256 over key-sorted JSON with changes ordered by (kind, id)."""
    ordered_changes = sorted(
        ({'kind': c['kind'], 'id': c['id'], 'value': c['value']}
         for c in changes),
        key=lambda c: (c['kind'], c['id']))
    canonical = {
        'batch_id': batch['batch_id'],
        'base_generation': batch['base_generation'],
        'changes': ordered_changes,
    }
    text = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def apply_changes(state, changes):
    """Apply every change onto copies; deleting a missing object is rejected."""
    next_state = {kind: dict(state[kind]) for kind in KINDS}
    for change in changes:
        kind, cid, value = change['kind'], change['id'], change['value']
        table = next_state[kind]
        if value is None:
            if cid not in table:
                raise ValueError('cannot delete missing %s' % kind)
            del table[cid]
        else:
            table[cid] = value
    return next_state


def validate_state(raw_state):
    """Validate the final state after all batch changes are applied.

    The state mixes entities already parsed from earlier generations with
    raw replacement objects carried by the new batch; each kind is normalized
    before cross-references are checked, so operation order cannot matter.
    """
    factor_entities = {}
    for fid, f in raw_state['factor'].items():
        if isinstance(f.get('rate'), Fraction):
            factor_entities[fid] = f
        else:
            factor_entities.update(validate_factors([f]))
    check_factor_entities(factor_entities)
    activity_entities = {}
    for aid, a in raw_state['activity'].items():
        if isinstance(a.get('date'), date):
            activity_entities[aid] = a
        else:
            activity_entities.update(validate_activities([a], factor_entities))
    check_activity_entities(activity_entities, factor_entities)
    certificate_entities = {}
    for cid, c in raw_state['certificate'].items():
        if isinstance(c.get('rate'), Fraction):
            certificate_entities[cid] = c
        else:
            certificate_entities.update(validate_certificates([c]))
    check_certificate_entities(certificate_entities)
    return factor_entities, activity_entities, certificate_entities


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS factors(
    id TEXT PRIMARY KEY, category TEXT NOT NULL, unit TEXT NOT NULL,
    rate TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT);
CREATE TABLE IF NOT EXISTS activities(
    id TEXT PRIMARY KEY, organization TEXT NOT NULL, date TEXT NOT NULL,
    scope INTEGER NOT NULL, category TEXT NOT NULL, quantity INTEGER NOT NULL,
    unit TEXT NOT NULL, factor_id TEXT NOT NULL, residual_factor_id TEXT);
CREATE TABLE IF NOT EXISTS certificates(
    id TEXT PRIMARY KEY, organizations TEXT NOT NULL, valid_from TEXT NOT NULL,
    valid_to TEXT, quantity INTEGER NOT NULL, rate TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batches(
    batch_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
    base_generation INTEGER NOT NULL, canonical_sha256 TEXT NOT NULL,
    content TEXT NOT NULL, report BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS allocations(
    generation INTEGER NOT NULL, certificate_id TEXT NOT NULL,
    activity_id TEXT NOT NULL, amount INTEGER NOT NULL,
    PRIMARY KEY(generation, certificate_id, activity_id));
"""


class Store:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.executescript(SCHEMA)
        row = self.conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
        if row is None:
            self.conn.execute("INSERT INTO meta(key,value) VALUES('generation',0)")
            self.conn.commit()
            self.generation = 0
        else:
            self.generation = row[0]

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def load_state(self):
        state = {kind: {} for kind in KINDS}
        for fid, category, unit, rate, start, end in self.conn.execute(
                'SELECT id,category,unit,rate,valid_from,valid_to FROM factors'):
            state['factor'][fid] = {
                'id': fid, 'category': category, 'unit': unit,
                'rate': Fraction(rate), 'start': date.fromisoformat(start),
                'end': None if end is None else date.fromisoformat(end)}
        for row in self.conn.execute(
                'SELECT id,organization,date,scope,category,quantity,unit,'
                'factor_id,residual_factor_id FROM activities'):
            aid, org, day, scope, category, quantity, unit, fid, rid = row
            entity = {
                'id': aid, 'organization': org, 'date': date.fromisoformat(day),
                'scope': scope, 'category': category, 'quantity': quantity,
                'unit': unit, 'factor_id': fid}
            if rid is not None:
                entity['residual_factor_id'] = rid
            state['activity'][aid] = entity
        for cid, orgs, start, end, quantity, rate in self.conn.execute(
                'SELECT id,organizations,valid_from,valid_to,quantity,rate '
                'FROM certificates'):
            state['certificate'][cid] = {
                'id': cid, 'organizations': json.loads(orgs),
                'start': date.fromisoformat(start),
                'end': None if end is None else date.fromisoformat(end),
                'quantity': quantity, 'rate': Fraction(rate)}
        return state

    def load_previous_allocation(self):
        previous = {}
        for cid, aid, amount in self.conn.execute(
                'SELECT certificate_id,activity_id,amount FROM allocations '
                'WHERE generation=?', (self.generation,)):
            previous[(cid, aid)] = amount
        return previous

    def lookup_batch(self, batch_id):
        row = self.conn.execute(
            'SELECT canonical_sha256,report FROM batches WHERE batch_id=?',
            (batch_id,)).fetchone()
        if row is None:
            return None
        return row[0], row[1]

    def commit_generation(self, generation, factors, activities, certificates,
                          batch_id, base_generation, digest, content, report,
                          edges, amounts):
        conn = self.conn
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute('DELETE FROM factors')
            conn.execute('DELETE FROM activities')
            conn.execute('DELETE FROM certificates')
            conn.executemany(
                'INSERT INTO factors(id,category,unit,rate,valid_from,valid_to)'
                ' VALUES(?,?,?,?,?,?)',
                [(f['id'], f['category'], f['unit'], str(f['rate']),
                  f['start'].isoformat(),
                  None if f['end'] is None else f['end'].isoformat())
                 for f in factors.values()])
            conn.executemany(
                'INSERT INTO activities(id,organization,date,scope,category,'
                'quantity,unit,factor_id,residual_factor_id)'
                ' VALUES(?,?,?,?,?,?,?,?,?)',
                [(a['id'], a['organization'], a['date'].isoformat(),
                  a['scope'], a['category'], a['quantity'], a['unit'],
                  a['factor_id'], a.get('residual_factor_id'))
                 for a in activities.values()])
            conn.executemany(
                'INSERT INTO certificates(id,organizations,valid_from,'
                'valid_to,quantity,rate) VALUES(?,?,?,?,?,?)',
                [(c['id'], json.dumps(c['organizations']),
                  c['start'].isoformat(),
                  None if c['end'] is None else c['end'].isoformat(),
                  c['quantity'], str(c['rate']))
                 for c in certificates.values()])
            conn.executemany(
                'INSERT INTO allocations(generation,certificate_id,'
                'activity_id,amount) VALUES(?,?,?,?)',
                [(generation, cid, aid, amount)
                 for (cid, aid), amount in zip(edges, amounts) if amount])
            conn.execute(
                'INSERT INTO batches(batch_id,generation,base_generation,'
                'canonical_sha256,content,report) VALUES(?,?,?,?,?,?)',
                (batch_id, generation, base_generation, digest, content, report))
            conn.execute("UPDATE meta SET value=? WHERE key='generation'",
                         (generation,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        self.generation = generation


def build_report(generation, batch_id, digest, factors, activities,
                 certificates, edges, amounts, objectives):
    cert_by_id = certificates
    by_activity = {a['id']: 0 for a in activities.values() if a['scope'] == 2}
    by_certificate = {c['id']: 0 for c in certificates.values()}
    positive_edges = []
    for (cid, aid), amount in zip(edges, amounts):
        if amount:
            positive_edges.append({
                'certificate_id': cid, 'activity_id': aid, 'amount': amount})
        by_activity[aid] += amount
        by_certificate[cid] += amount
    activity_rows = []
    totals = {}
    for a in sorted(activities.values(), key=lambda x: x['id']):
        location = a['quantity'] * factors[a['factor_id']]['rate']
        row = {
            'id': a['id'],
            'organization': a['organization'],
            'date': a['date'].isoformat(),
            'scope': a['scope'],
            'category': a['category'],
            'quantity': a['quantity'],
            'unit': a['unit'],
            'factor_id': a['factor_id'],
            'location_kgco2e': str(location),
        }
        if a['scope'] == 1:
            market = location
        else:
            covered = by_activity[a['id']]
            uncovered = a['quantity'] - covered
            residual = factors[a['residual_factor_id']]['rate']
            market = Fraction(0)
            for (cid, aid), amount in zip(edges, amounts):
                if aid == a['id'] and amount:
                    market += amount * cert_by_id[cid]['rate']
            market += uncovered * residual
            row['residual_factor_id'] = a['residual_factor_id']
            row['covered_kwh'] = covered
            row['uncovered_kwh'] = uncovered
        row['market_kgco2e'] = str(market)
        activity_rows.append(row)
        key = (a['organization'], a['scope'])
        loc_sum, mkt_sum = totals.get(key, (Fraction(0), Fraction(0)))
        totals[key] = (loc_sum + location, mkt_sum + market)
    total_rows = [
        {'organization': org, 'scope': scope,
         'location_kgco2e': str(loc_sum), 'market_kgco2e': str(mkt_sum)}
        for (org, scope), (loc_sum, mkt_sum) in sorted(totals.items())]
    remaining = [
        {'certificate_id': cid,
         'remaining': certificates[cid]['quantity'] - by_certificate[cid]}
        for cid in sorted(certificates)]
    return {
        'schema_version': 2,
        'generation': generation,
        'batch_id': batch_id,
        'canonical_sha256': digest,
        'allocation': {
            'edges': positive_edges,
            'certificate_remaining': remaining,
        },
        'activities': activity_rows,
        'totals': total_rows,
        'objectives': {
            'covered_kwh': objectives['covered_kwh'],
            'market_kgco2e': str(objectives['market_kgco2e']),
            'total_absolute_change': objectives['total_absolute_change'],
            'allocation_edges': [[cid, aid] for cid, aid in edges],
            'allocation_vector': objectives['allocation_vector'],
        },
    }


def adjudicate(store, batch_data):
    """Validate, optimize and commit one batch; replay takes precedence."""
    batch_id, base_generation, changes = validate_batch(batch_data)
    digest = canonical_sha256(batch_data, changes)
    existing = store.lookup_batch(batch_id)
    if existing is not None:
        stored_digest, stored_report = existing
        if stored_digest != digest:
            raise ValueError('batch id reused with different content')
        return 'replay', json.loads(stored_report.decode('utf-8')), store.generation
    if base_generation != store.generation:
        raise ValueError('stale base_generation; current generation is %d'
                         % store.generation)
    state = store.load_state()
    next_state = apply_changes(state, changes)
    # Full validation happens only after every change has been applied.
    factors, activities, certificates = validate_state(next_state)
    previous = store.load_previous_allocation()
    for a in activities.values():
        if a['scope'] == 2:
            a['residual_rate'] = factors[a['residual_factor_id']]['rate']
    edges, amounts, objectives = optimize(
        list(certificates.values()), list(activities.values()), previous)
    generation = store.generation + 1
    report = build_report(generation, batch_id, digest, factors, activities,
                          certificates, edges, amounts, objectives)
    from .common import render
    blob = render(report)
    content = json.dumps(batch_data, sort_keys=True, ensure_ascii=False)
    store.commit_generation(generation, factors, activities, certificates,
                            batch_id, base_generation, digest, content, blob,
                            edges, amounts)
    return 'accepted', report, generation
