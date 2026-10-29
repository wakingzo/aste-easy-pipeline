"""Budget/cap tests: no network, no DB."""
import csv, json, sys, tempfile, types, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
TMP=Path(tempfile.mkdtemp(prefix='aste-budget-test-'))
paths=types.ModuleType('aste_paths')
paths.REPORTS_DIR=TMP/'reports'; paths.REPORTS_DIR.mkdir(parents=True,exist_ok=True)
paths.ensure_runtime_dirs=lambda: paths.REPORTS_DIR.mkdir(parents=True,exist_ok=True)
sys.modules['aste_paths']=paths
# This module is not used by the unit-tested pure helpers.
v39=types.ModuleType('google_policy_v3_9')
v39.preview_new_rows=lambda *a,**k: (_ for _ in ()).throw(AssertionError('not expected'))
sys.modules['google_policy_v3_9']=v39
sys.path.insert(0,str(ROOT/'src'/'geocoding'))
import google_budget_v3_10 as m

class BudgetTests(unittest.TestCase):
    def row(self, group, civic=True, eligible=True, pid='1'):
        return {
            'pvp_id':pid,'comune':'Test','indirizzo':('Via Roma 5' if civic else 'Via Roma'),
            'future_google_eligible':str(bool(eligible)), 'future_google_group':group,
            'map_lat':'40.0','map_lon':'9.0'
        }

    def test_priority_order(self):
        self.assertEqual(m.budget_priority(self.row('REVIEW_DISCORDANZA'))[0],1)
        self.assertEqual(m.budget_priority(self.row('SENZA_COORDINATE_PVP',True))[0],2)
        self.assertEqual(m.budget_priority(self.row('PVP_NON_CONFERMATO',True))[0],3)
        self.assertEqual(m.budget_priority(self.row('SENZA_COORDINATE_PVP',False))[0],4)
        self.assertEqual(m.budget_priority(self.row('PVP_NON_CONFERMATO',False))[0],5)

    def test_cap_defers_low_priority_first(self):
        rows=[self.row('PVP_NON_CONFERMATO',False,pid='p5'),
              self.row('SENZA_COORDINATE_PVP',True,pid='p2'),
              self.row('REVIEW_DISCORDANZA',False,pid='p1')]
        out,summary=m.schedule_rows(rows,cap=2,used=0)
        selected={r['pvp_id'] for r in out if r['budget_selected']}
        self.assertEqual(selected,{'p1','p2'})
        self.assertEqual(summary['deferred_priority_counts'],{'P5_PVP_NON_CONFERMATO_TOPONIMO':1})

    def test_existing_usage_reduces_budget(self):
        rows=[self.row('SENZA_COORDINATE_PVP',True,pid=str(i)) for i in range(4)]
        _,s=m.schedule_rows(rows,cap=10,used=8)
        self.assertEqual(s['scheduled_rows'],2)
        self.assertEqual(s['deferred_rows'],2)

    def test_local_report_attempt_counter_counts_reruns(self):
        d=TMP/'count'; d.mkdir(exist_ok=True)
        for name,n in [('google_comparison_v3_20261001-a.csv',2),('google_comparison_v3_20261002-b.csv',3),('google_comparison_v3_20260930.csv',9)]:
            with (d/name).open('w',encoding='utf-8-sig',newline='') as f:
                w=csv.writer(f,delimiter=';'); w.writerow(['pvp_id']);
                for i in range(n): w.writerow([i])
        z=m.detect_local_report_attempts(d,month='2026-10')
        self.assertEqual(z['attempts'],5)
        self.assertEqual(len(z['files']),2)

    def test_seed_floor_never_decreases_existing_usage(self):
        p=TMP/'seed.json'
        m.seed_ledger_floor(floor_used=2500,cap=9000,path=p,month='2026-10')
        m.reserve_before_http(cap=9000,count=10,path=p,month='2026-10')
        m.seed_ledger_floor(floor_used=1000,cap=9000,path=p,month='2026-10')
        self.assertEqual(m.read_ledger(p,month='2026-10')['used'],2510)

    def test_reserve_is_counted_before_http_and_hard_stops(self):
        p=TMP/'ledger.json'
        a=m.reserve_before_http(cap=3,count=2,path=p,month='2026-10')
        b=m.reserve_before_http(cap=3,count=1,path=p,month='2026-10')
        c=m.reserve_before_http(cap=3,count=1,path=p,month='2026-10')
        self.assertTrue(a['ok']); self.assertTrue(b['ok']); self.assertFalse(c['ok'])
        self.assertEqual(m.read_ledger(p,month='2026-10')['used'],3)

    def test_new_month_rolls_to_zero(self):
        p=TMP/'roll.json'; p.write_text(json.dumps({'month':'2026-10','used':8999}),encoding='utf-8')
        self.assertEqual(m.read_ledger(p,month='2026-11')['used'],0)

if __name__=='__main__': unittest.main(verbosity=2)
