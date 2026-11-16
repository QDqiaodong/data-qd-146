
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random
from pathlib import Path
from fractions import Fraction
from carbon_inventory.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_field(self):
        d=sample();d['extra']=1;self.bad(d)

    def test_totals(self):
        r=analyze(sample());self.assertEqual([(x['scope'],x['kgco2e']) for x in r['totals']],[(1,'190'),(2,'400')])
    def test_activity_and_factor_determinism(self):
        d=sample();d['activities'].reverse();self.assertEqual(analyze(d),analyze(sample()))
    def test_date_interval_end_excluded(self):
        d=sample();d['factors'][0]['valid_to']='2026-06-01';self.bad(d)
    def test_category_mismatch(self):
        d=sample();d['activities'][0]['category']='other';self.bad(d)
    def test_unit_mismatch(self):
        d=sample();d['activities'][0]['unit']='mwh';self.bad(d)
    def test_unknown_factor(self):
        d=sample();d['activities'][0]['factor_id']='missing';self.bad(d)
    def test_duplicate_factor(self):
        d=sample();d['factors'].append(d['factors'][0]);self.bad(d)
    def test_empty(self):
        d=sample();d['activities']=[];self.assertEqual(analyze(d)['totals'],[])

class Command(unittest.TestCase):
    def call(self,source,output):
        return subprocess.run([sys.executable,'-m','carbon_inventory.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
