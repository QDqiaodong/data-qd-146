"""Tests for the power certificate revision ledger and exact allocation."""
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from fractions import Fraction
from pathlib import Path

from carbon_inventory.allocation import optimize
from carbon_inventory.common import read, render
from carbon_inventory.registry import (Store, adjudicate, canonical_sha256,
                                       validate_batch, validate_state)

ROOT = Path(__file__).resolve().parents[1]


def factor(fid, rate='2/5', category='electricity', unit='kwh'):
    return {'id': fid, 'category': category, 'unit': unit,
            'kgco2e_per_unit': rate, 'valid_from': '2026-01-01',
            'valid_to': None}


def residual(fid='res', rate='1/1'):
    return factor(fid, rate)


def activity(aid, org='A', qty=1, scope=2, fid='grid', rid='res',
             day='2026-06-01'):
    base = {'id': aid, 'organization': org, 'date': day, 'scope': scope,
            'category': 'electricity', 'quantity': qty, 'unit': 'kwh',
            'factor_id': fid}
    if scope == 2:
        base['residual_factor_id'] = rid
    return base


def certificate(cid, orgs, qty=1, rate='0/1', start='2026-01-01', end=None):
    return {'id': cid, 'eligible_organizations': orgs, 'valid_from': start,
            'valid_to': end, 'quantity': qty, 'kgco2e_per_kwh': rate}


def change(kind, cid, value):
    return {'kind': kind, 'id': cid, 'value': value}


def batch(bid, base, changes):
    return {'batch_id': bid, 'base_generation': base, 'changes': changes}


def foundation_batches():
    g1 = batch('g1', 0, [
        change('factor', 'grid', factor('grid')),
        change('factor', 'res', residual()),
        change('activity', 'a1', activity('a1')),
        change('certificate', 'F', certificate('F', ['A', 'B'])),
    ])
    g2 = batch('g2', 1, [
        change('activity', 'b1', activity('b1', org='B')),
        change('certificate', 'G', certificate('G', ['A'])),
    ])
    return g1, g2


class BatchValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'reg.db')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def reject(self, payload):
        with self.assertRaises((ValueError, TypeError)):
            adjudicate(self.store, payload)

    def test_empty_database_starts_at_generation_zero(self):
        self.assertEqual(self.store.generation, 0)
        self.assertEqual(self.store.load_state(),
                         {'factor': {}, 'activity': {}, 'certificate': {}})

    def test_batch_strict_fields(self):
        self.reject({})
        self.reject({'batch_id': 'x', 'base_generation': 0, 'changes': [], 'extra': 1})
        self.reject(batch('x', 0, [{'kind': 'factor', 'id': 'f'}]))
        self.reject(batch('x', -1, []))
        self.reject(batch('x', '0', []))
        self.reject(batch('', 0, []))

    def test_change_kind_id_and_value_rules(self):
        self.reject(batch('x', 0, [change('nope', 'f', factor('f'))]))
        self.reject(batch('x', 0, [change('factor', 'f', None)]))
        mismatch = change('factor', 'f', factor('other'))
        self.reject(batch('x', 0, [mismatch]))
        dup = [change('factor', 'f', factor('f')),
               change('factor', 'f', factor('f'))]
        self.reject(batch('x', 0, dup))

    def test_unknown_fields_and_numeric_strictness(self):
        bad = factor('f')
        bad['extra'] = 1
        self.reject(batch('x', 0, [change('factor', 'f', bad)]))
        bad_rate = factor('f', rate='2')
        self.reject(batch('x', 0, [change('factor', 'f', bad_rate)]))
        bad_qty = activity('a', qty=10 ** 12 + 1)
        good = [change('factor', 'grid', factor('grid')),
                change('factor', 'res', residual()),
                change('activity', 'a', bad_qty)]
        self.reject(batch('x', 0, good))
        self.reject(batch('x', 0, [
            change('certificate', 'c', certificate('c', ['A'], qty=1.5))]))
        self.reject(batch('x', 0, [
            change('certificate', 'c', certificate('c', ['A'], rate='-1/2'))]))

    def test_scope2_requires_kwh_and_residual(self):
        no_res = activity('a')
        del no_res['residual_factor_id']
        self.reject(batch('x', 0, [
            change('factor', 'grid', factor('grid')),
            change('activity', 'a', no_res)]))
        wrong_unit = activity('a')
        wrong_unit['unit'] = 'mwh'
        self.reject(batch('x', 0, [
            change('factor', 'grid', factor('grid')),
            change('factor', 'res', residual()),
            change('activity', 'a', wrong_unit)]))
        scope1_with_res = activity('a', scope=1, rid=None)
        scope1_with_res['residual_factor_id'] = 'res'
        self.reject(batch('x', 0, [
            change('factor', 'gas', factor('gas', category='gas', unit='m3')),
            change('factor', 'res', residual()),
            change('activity', 'a', scope1_with_res)]))

    def test_certificate_organization_and_dates(self):
        self.reject(batch('x', 0, [
            change('certificate', 'c', certificate('c', []))]))
        self.reject(batch('x', 0, [
            change('certificate', 'c', certificate('c', ['A', 'A']))]))
        self.reject(batch('x', 0, [
            change('certificate', 'c',
                   certificate('c', ['A'], start='2026-06-01',
                               end='2026-01-01'))]))

    def test_final_state_validation_and_no_partial_change(self):
        g1, _ = foundation_batches()
        mode, report, gen = adjudicate(self.store, g1)
        self.assertEqual((mode, gen), ('accepted', 1))
        # Activity pointing at an unknown factor: whole batch rejected,
        # generation and entities stay at the prior state.
        broken = activity('a2', fid='missing')
        before = self.store.load_state()
        self.reject(batch('bad', 1, [
            change('factor', 'extra', factor('extra')),
            change('activity', 'a2', broken)]))
        self.assertEqual(self.store.generation, 1)
        self.assertEqual(self.store.load_state(), before)
        # Deleting a factor still referenced by an activity is rejected too.
        self.reject(batch('bad2', 1, [change('factor', 'grid', None)]))
        self.assertEqual(self.store.generation, 1)

    def test_limits(self):
        changes = [change('factor', 'grid', factor('grid')),
                   change('factor', 'res', residual())]
        changes += [change('certificate', 'c%03d' % i,
                           certificate('c%03d' % i, ['A']))
                    for i in range(101)]
        self.reject(batch('too-many-certs', 0, changes))
        changes = [change('factor', 'grid', factor('grid')),
                   change('factor', 'res', residual())]
        changes += [change('activity', 'a%03d' % i, activity('a%03d' % i))
                    for i in range(301)]
        self.reject(batch('too-many-scope2', 0, changes))
        # Activity ids must be unique across the final state.
        dup = batch('dup', 0, [
            change('factor', 'grid', factor('grid')),
            change('factor', 'res', residual()),
            change('activity', 'a', activity('a')),
            change('certificate', 'F', certificate('F', ['A'])),
        ])
        adjudicate(self.store, dup)
        self.reject(batch('dup2', 1, [
            change('activity', 'a2', activity('a2')),
            change('activity', 'a3', activity('a2'))]))


class AdjudicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'reg.db')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_replay_precedence_and_identity(self):
        g1, g2 = foundation_batches()
        mode, report1, gen1 = adjudicate(self.store, g1)
        self.assertEqual((mode, gen1), ('accepted', 1))
        mode, report2, gen2 = adjudicate(self.store, g2)
        self.assertEqual((mode, gen2), ('accepted', 2))
        # Same batch id and identical canonical content: replay wins over
        # the base_generation check (its base 0 is stale at generation 2)
        # and does not advance the generation.
        reordered = dict(g1)
        reordered['changes'] = list(reversed(g1['changes']))
        mode, replay, gen = adjudicate(self.store, reordered)
        self.assertEqual((mode, gen), ('replay', 2))
        self.assertEqual(render(replay), render(report1))
        # Same id but different content is always rejected.
        conflict = json.loads(json.dumps(g1))
        conflict['changes'][0]['value']['category'] = 'gas'
        with self.assertRaises(ValueError):
            adjudicate(self.store, conflict)
        self.assertEqual(self.store.generation, 2)

    def test_canonical_hash_is_order_insensitive(self):
        g1, _ = foundation_batches()
        _, _, changes1 = validate_batch(g1)
        reordered = {'changes': list(reversed(g1['changes'])),
                     'base_generation': g1['base_generation'],
                     'batch_id': g1['batch_id']}
        _, _, changes2 = validate_batch(reordered)
        self.assertEqual(canonical_sha256(g1, changes1),
                         canonical_sha256(reordered, changes2))
        # No equivalence normalization: 1/2 and 2/4 are distinct content.
        variant = json.loads(json.dumps(g1))
        variant['changes'][0]['value']['kgco2e_per_unit'] = '4/10'
        _, _, changes3 = validate_batch(variant)
        self.assertNotEqual(canonical_sha256(g1, changes1),
                            canonical_sha256(variant, changes3))

    def test_stale_base_rejected(self):
        g1, _ = foundation_batches()
        adjudicate(self.store, g1)
        with self.assertRaises(ValueError):
            adjudicate(self.store, batch('late', 0, []))
        self.assertEqual(self.store.generation, 1)

    def test_generation_advances_exactly_one(self):
        g1, g2 = foundation_batches()
        adjudicate(self.store, g1)
        self.assertEqual(self.store.generation, 1)
        adjudicate(self.store, g2)
        self.assertEqual(self.store.generation, 2)
        row = self.store.conn.execute(
            'SELECT generation FROM batches ORDER BY generation').fetchall()
        self.assertEqual(row, [(1,), (2,)])

    def test_persistence_survives_reopen(self):
        g1, g2 = foundation_batches()
        _, report1, _ = adjudicate(self.store, g1)
        _, report2, _ = adjudicate(self.store, g2)
        path = self.store.path
        self.store.close()
        with Store(path) as reopened:
            self.assertEqual(reopened.generation, 2)
            mode, replayed, gen = adjudicate(
                reopened, json.loads(json.dumps(g1)))
            self.assertEqual(mode, 'replay')
            self.assertEqual(render(replayed), render(report1))
            self.assertEqual(gen, 2)
            state = reopened.load_state()
            self.assertEqual(set(state['activity']), {'a1', 'b1'})


class AllocationTests(unittest.TestCase):
    def test_prescribed_ab_two_organization_case(self):
        grid = factor('grid', rate='1/1')
        res = residual(rate='1/1')
        certs = [
            {'id': 'F', 'organizations': ['A', 'B'], 'start': date(2026, 1, 1),
             'end': None, 'quantity': 1, 'rate': Fraction(0)},
        ]
        acts = [dict(activity('a1'), date=date(2026, 6, 1),
                     residual_rate=Fraction(1))]
        edges, amounts, obj = optimize(certs, acts, {})
        self.assertEqual(list(zip(edges, amounts)), [(('F', 'a1'), 1)])
        self.assertEqual(obj['total_absolute_change'], 1)
        certs.append({'id': 'G', 'organizations': ['A'],
                      'start': date(2026, 1, 1), 'end': None,
                      'quantity': 1, 'rate': Fraction(0)})
        acts.append(dict(activity('b1', org='B'), date=date(2026, 6, 1),
                         residual_rate=Fraction(1)))
        previous = {('F', 'a1'): 1}
        edges, amounts, obj = optimize(certs, acts, previous)
        flow = dict(zip(edges, amounts))
        self.assertEqual(flow, {('F', 'a1'): 0, ('F', 'b1'): 1,
                                ('G', 'a1'): 1})
        self.assertEqual(obj['covered_kwh'], 2)
        self.assertEqual(obj['market_kgco2e'], Fraction(0))
        self.assertEqual(obj['total_absolute_change'], 3)
        self.assertEqual(obj['allocation_vector'], [0, 1, 1])

    def test_equivalent_certificate_freezes_prior_allocation(self):
        certs = [{'id': 'F', 'organizations': ['A', 'B'],
                  'start': date(2026, 1, 1), 'end': None, 'quantity': 20,
                  'rate': Fraction(1, 10)}]
        acts = [dict(activity('a', org='A', qty=10), date=date(2026, 6, 1),
                     residual_rate=Fraction(7, 10)),
                dict(activity('b', org='B', qty=10), date=date(2026, 6, 1),
                     residual_rate=Fraction(7, 10))]
        edges, amounts, _ = optimize(certs, acts, {})
        previous = dict(zip(edges, amounts))
        certs.append({'id': 'G', 'organizations': ['A', 'B'],
                      'start': date(2026, 1, 1), 'end': None, 'quantity': 20,
                      'rate': Fraction(1, 10)})
        edges2, amounts2, obj2 = optimize(certs, acts, previous)
        self.assertEqual(obj2['total_absolute_change'], 0)
        self.assertEqual(dict(zip(edges2, amounts2)).get(('G', 'a'), 0), 0)
        # Capacity cut forces global re-optimization; dead edges count.
        certs[0]['quantity'] = 0
        edges3, amounts3, obj3 = optimize(certs, acts, previous)
        flow = dict(zip(edges3, amounts3))
        self.assertEqual(flow, {('F', 'a'): 0, ('F', 'b'): 0,
                                ('G', 'a'): 10, ('G', 'b'): 10})
        self.assertEqual(obj3['total_absolute_change'], 40)

    def test_conservation_and_brute_force(self):
        random.seed(11)
        orgs = ['A', 'B', 'C']
        rates = [(0, 1), (1, 10), (1, 2), (3, 7), (2, 1)]
        for trial in range(120):
            # Mix 2x2 and denser 3x3 graphs to exercise longer alternating
            # cycles and tighter capacity constraints.
            if trial % 2 == 0:
                nc, na = random.randint(1, 2), random.randint(1, 2)
                qmax = 3
            else:
                nc, na = 3, 3
                qmax = 2
            certs = [{'id': 'c%d' % i,
                      'organizations': random.sample(orgs, random.randint(1, 2)),
                      'start': date(2026, 1, 1), 'end': None,
                      'quantity': random.randint(0, qmax),
                      'rate': Fraction(*random.choice(rates))}
                     for i in range(nc)]
            acts = [{'id': 'a%d' % i, 'organization': random.choice(orgs),
                     'date': date(2026, 6, 1), 'scope': 2,
                     'category': 'electricity',
                     'quantity': random.randint(0, qmax), 'unit': 'kwh',
                     'factor_id': 'grid', 'residual_factor_id': 'res',
                     'residual_rate': Fraction(7, 10)} for i in range(na)]
            previous = {}
            if trial % 2 == 0:
                # Dead/expired pairs count as current value zero in deviation.
                previous[('dead-cert', 'a0')] = 2
                for c in certs:
                    for a in acts:
                        if random.random() < 0.4:
                            previous[(c['id'], a['id'])] = \
                                random.randint(0, qmax)
            edges, amounts, obj = optimize(certs, acts, previous)
            expected_edges, expected = _brute(certs, acts, previous)
            self.assertEqual(edges, expected_edges)
            self.assertEqual(obj['allocation_vector'], expected[1])
            self.assertEqual(obj['covered_kwh'], -expected[0][0])
            self.assertEqual(obj['market_kgco2e'], expected[0][1])
            self.assertEqual(obj['total_absolute_change'], expected[0][2])
            # Per-activity conservation: covered never exceeds demand.
            used_c, used_a = {}, {}
            for (cid, aid), value in zip(edges, amounts):
                used_c[cid] = used_c.get(cid, 0) + value
                used_a[aid] = used_a.get(aid, 0) + value
            for c in certs:
                self.assertLessEqual(used_c.get(c['id'], 0), c['quantity'])
            for a in acts:
                self.assertLessEqual(used_a.get(a['id'], 0), a['quantity'])
            self.assertEqual(obj['covered_kwh'], sum(amounts))


def _brute(certs, acts, previous):
    edges = sorted((c['id'], a['id']) for c in certs for a in acts
                   if a['organization'] in c['organizations'])
    cmap = {c['id']: c for c in certs}
    amap = {a['id']: a for a in acts}
    best = None

    def rec(i, used_c, used_a, alloc):
        nonlocal best
        if i == len(edges):
            covered = sum(alloc.values())
            market = sum(v * cmap[c]['rate'] for (c, a), v in alloc.items())
            for a in acts:
                market += (a['quantity'] - used_a[a['id']]) * a['residual_rate']
            deviation = sum(abs(alloc.get(e, 0) - previous.get(e, 0))
                            for e in set(previous) | set(edges))
            vector = tuple(alloc.get(e, 0) for e in edges)
            key = (-covered, market, deviation, vector)
            if best is None or key < best[0]:
                best = (key, list(vector))
            return
        cid, aid = edges[i]
        cap = min(cmap[cid]['quantity'] - used_c[cid],
                  amap[aid]['quantity'] - used_a[aid])
        for x in range(cap + 1):
            uc = dict(used_c); uc[cid] += x
            ua = dict(used_a); ua[aid] += x
            al = dict(alloc); al[(cid, aid)] = x
            rec(i + 1, uc, ua, al)

    rec(0, {c['id']: 0 for c in certs}, {a['id']: 0 for a in acts}, {})
    return edges, best


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.reg = self.td / 'reg.db'
        self.out = self.td / 'out.json'
        self.g1, self.g2 = foundation_batches()
        self.batch1 = self.td / 'g1.json'
        self.batch2 = self.td / 'g2.json'
        self.batch1.write_text(json.dumps(self.g1))
        self.batch2.write_text(json.dumps(self.g2))

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, *args):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'src')
        return subprocess.run([sys.executable, '-m', 'carbon_inventory.cli',
                               *map(str, args)], capture_output=True,
                              text=True, env=env)

    def accept(self, batch_path, output):
        result = self.call('--registry', self.reg, '--batch', batch_path,
                           '--output', output)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_modes_are_mutually_exclusive(self):
        src = self.td / 'src.json'
        src.write_text((ROOT / 'fixtures/input.json').read_text())
        result = self.call('--source', src, '--registry', self.reg,
                           '--batch', self.batch1, '--output', self.out)
        self.assertEqual(result.returncode, 2)
        result = self.call('--registry', self.reg, '--output', self.out)
        self.assertEqual(result.returncode, 2)

    def test_accept_replay_byte_identical_and_restart(self):
        r1 = self.accept(self.batch1, self.out)
        first_bytes = self.out.read_bytes()
        self.assertEqual(r1['generation'], 1)
        # Replay with reordered changes and different serialization bytes.
        reordered = self.td / 'g1-reorder.json'
        payload = dict(self.g1)
        payload['changes'] = list(reversed(payload['changes']))
        reordered.write_text(json.dumps(payload, indent=5))
        result = self.call('--registry', self.reg, '--batch', reordered,
                           '--output', self.td / 'replay.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.td / 'replay.json').read_bytes(), first_bytes)
        gen = sqlite3.connect(self.reg).execute(
            "SELECT value FROM meta WHERE key='generation'").fetchone()[0]
        self.assertEqual(gen, 1)
        # Second generation and fresh-process replay of the old batch.
        self.accept(self.batch2, self.out)
        result = self.call('--registry', self.reg, '--batch', self.batch1,
                           '--output', self.td / 'old.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.td / 'old.json').read_bytes(), first_bytes)
        gen = sqlite3.connect(self.reg).execute(
            "SELECT value FROM meta WHERE key='generation'").fetchone()[0]
        self.assertEqual(gen, 2)

    def test_failure_before_commit_keeps_old_report(self):
        self.accept(self.batch1, self.out)
        old = self.out.read_bytes()
        bad = self.td / 'bad.json'
        bad.write_text(json.dumps(batch('bad', 1, [
            change('activity', 'x', activity('x', fid='missing'))])))
        result = self.call('--registry', self.reg, '--batch', bad,
                           '--output', self.out)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.out.read_bytes(), old)
        # Duplicate JSON keys are rejected at parse time.
        bad.write_text('{"batch_id":"b","base_generation":1,'
                       '"changes":[],"changes":[]}')
        result = self.call('--registry', self.reg, '--batch', bad,
                           '--output', self.out)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.out.read_bytes(), old)

    def test_post_commit_write_failure_then_replay_repairs(self):
        r1 = self.accept(self.batch1, self.td / 'ok.json')
        stored = (self.td / 'ok.json').read_bytes()
        # Writing onto an existing directory raises OSError *after* the
        # database commit: simulates a post-commit report publication fault.
        broken = self.td / 'directory.json'
        broken.mkdir()
        result = self.call('--registry', self.reg, '--batch',
                           self.batch2, '--output', broken)
        self.assertEqual(result.returncode, 2)
        self.assertIn('accepted', result.stderr)
        # Generation 2 was committed despite the file failure.
        gen = sqlite3.connect(self.reg).execute(
            "SELECT value FROM meta WHERE key='generation'").fetchone()[0]
        self.assertEqual(gen, 2)
        # Replaying the same batch repairs the report without re-accepting.
        repaired = self.td / 'repaired.json'
        result = self.call('--registry', self.reg, '--batch', self.batch2,
                           '--output', repaired)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(repaired.read_text())
        self.assertEqual(report['generation'], 2)
        gen = sqlite3.connect(self.reg).execute(
            "SELECT value FROM meta WHERE key='generation'").fetchone()[0]
        self.assertEqual(gen, 2)
        # Old generation report still reproduces byte-for-byte.
        result = self.call('--registry', self.reg, '--batch', self.batch1,
                           '--output', self.td / 'old.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.td / 'old.json').read_bytes(), stored)

    def test_path_safety(self):
        self.accept(self.batch1, self.td / 'ok.json')
        # Output must differ from registry and batch.
        result = self.call('--registry', self.reg, '--batch', self.batch1,
                           '--output', self.reg)
        self.assertEqual(result.returncode, 2)
        result = self.call('--registry', self.reg, '--batch', self.batch1,
                           '--output', self.batch1)
        self.assertEqual(result.returncode, 2)
        # Symlink output is refused.
        link = self.td / 'link.json'
        link.symlink_to(self.td / 'ok.json')
        result = self.call('--registry', self.reg, '--batch', self.batch1,
                           '--output', link)
        self.assertEqual(result.returncode, 2)
        # Symlink registry path is refused.
        dblink = self.td / 'dbreg.db'
        dblink.symlink_to(self.reg)
        result = self.call('--registry', dblink, '--batch', self.batch1,
                           '--output', self.td / 'x.json')
        self.assertEqual(result.returncode, 2)

    def test_legacy_source_mode_unchanged(self):
        src = ROOT / 'fixtures/input.json'
        result = self.call('--source', src, '--output', self.td / 'legacy.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.td / 'legacy.json').read_text())
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual([(x['scope'], x['kgco2e'])
                          for x in report['result']['totals']],
                         [(1, '190'), (2, '400')])


if __name__ == '__main__':
    unittest.main()
