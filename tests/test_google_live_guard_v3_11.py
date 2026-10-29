import csv
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix='aste-liveguard-test-'))

paths = types.ModuleType('aste_paths')
paths.REPORTS_DIR = TMP / 'reports'; paths.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
paths.ensure_runtime_dirs = lambda: None
sys.modules['aste_paths'] = paths

off = types.ModuleType('geocode_pending_v2')
off.HERE = TMP
off.SELECT = ()
sys.modules['geocode_pending_v2'] = off

# Load real budget module with stubbed deps.
elig = types.ModuleType('google_eligibility_v3_2'); elig.address_parts=lambda r:{'civic':''}
sys.modules['google_eligibility_v3_2']=elig
v39=types.ModuleType('google_policy_v3_9')
sys.modules['google_policy_v3_9']=v39
sys.path.insert(0, str(ROOT / 'src' / 'geocoding'))
import google_budget_v3_10 as bud
sys.modules['google_budget_v3_10'] = bud
prev = types.ModuleType('google_preview_v3')
sys.modules['google_preview_v3'] = prev
import google_live_guard_v3_11 as live

class LiveGuardTests(unittest.TestCase):
    def setUp(self):
        for p in paths.REPORTS_DIR.glob('*'): p.unlink()
        for p in (TMP/'state',):
            if p.exists():
                for q in p.glob('*'): q.unlink()
        cp=TMP/'google_api_usage_v9_2.json'
        if cp.exists(): cp.unlink()

    def _csv_rows(self, name, n):
        p=paths.REPORTS_DIR/name
        with p.open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.writer(f,delimiter=';'); w.writerow(['pvp_id','query','assessment'])
            for i in range(n): w.writerow([i,'q'+str(i),'A'])
        return p

    def test_core_roundtrip_canonical(self):
        got=live.write_core_month_total(2510, path=TMP/'google_api_usage_v9_2.json', month='2026-10')
        self.assertEqual(got['effective'],2510); self.assertEqual(got['external_offset'],0)
        rd=live.read_core_month_usage(TMP/'google_api_usage_v9_2.json', month='2026-10')
        self.assertEqual(rd['local'],2510); self.assertEqual(rd['effective'],2510)

    def test_read_core_corrupt_fails_closed(self):
        p=TMP/'bad.json'; p.write_text('{broken',encoding='utf-8')
        with self.assertRaises(RuntimeError): live.read_core_month_usage(p,month='2026-10')

    def _summary(self, name, network_attempts, cache_hits=0, version='google_live_guard_v3_11_5'):
        p=paths.REPORTS_DIR/name
        p.write_text(json.dumps({
            'geocode_preview': {'google_live_guard': {
                'version': version,
                'run_network_attempts': network_attempts,
                'run_cache_hits': cache_hits,
            }}
        }), encoding='utf-8')
        return p

    def test_live_history_uses_summary_network_attempts_not_csv_rows(self):
        self._csv_rows('google_live_guard_v3_11_3_20261027-x.csv',10)
        self._summary('pvp_import_atomic_v1_20261027-x.json',0,10,'google_live_guard_v3_11_3')
        rows=live.detect_live_guard_report_rows(paths.REPORTS_DIR,month='2026-10')
        exact=live.detect_live_guard_network_attempts(paths.REPORTS_DIR,month='2026-10')
        self.assertEqual(rows['rows'],10)
        self.assertEqual(exact['attempts'],0)

    def test_repair_ledger_exact_only(self):
        lp=bud.ledger_path()
        bud.seed_ledger_floor(floor_used=5010,cap=9000,path=lp,month='2026-10')
        got=bud.repair_ledger_known_double_count(expected_inflated=5010,corrected_used=2510,cap=9000,path=lp,month='2026-10')
        self.assertEqual(got['used'],2510)
        with self.assertRaises(RuntimeError):
            bud.repair_ledger_known_double_count(expected_inflated=5010,corrected_used=2500,cap=9000,path=lp,month='2026-10')


    def test_reconcile_uses_configured_core_path_and_repairs_partial_crash(self):
        old = os.environ.get('GOOGLE_API_USAGE_FILE')
        old_month = bud._month_key
        try:
            os.environ['GOOGLE_API_USAGE_FILE'] = 'custom_usage.json'
            bud._month_key = lambda now=None: '2026-10'
            self._csv_rows('google_comparison_v3_20261027-a.csv', 2500)
            self._csv_rows('google_live_guard_v3_11_20261027-b.csv', 10)
            self._summary('pvp_import_atomic_v1_20261027-b.json',10,0,'google_live_guard_v3_11')
            custom = TMP / 'custom_usage.json'
            live.write_core_month_total(2510, path=custom, month='2026-10')
            # Deliberately create a conflicting default file: v3.11.1 could read this
            # while the Google core used custom_usage.json.
            live.write_core_month_total(5010, path=TMP/'google_api_usage_v9_2.json', month='2026-10')
            bud.seed_ledger_floor(floor_used=5010, cap=9000, month='2026-10')
            got = live.reconcile_usage_state(monthly_cap=9000)
            self.assertTrue(got['bug_signature_repaired'])
            self.assertEqual(got['repair_signature'], 'V3_11_1_PARTIAL_CRASH_LEDGER_ONLY')
            self.assertEqual(got['canonical_used_before'], 2510)
            self.assertEqual(Path(got['core_usage_path']), custom)
            self.assertEqual(live.read_core_month_usage(custom, month='2026-10')['effective'], 2510)
            self.assertEqual(bud.read_ledger(month='2026-10')['used'], 2510)
            # The unrelated default file must not be used or rewritten.
            self.assertEqual(live.read_core_month_usage(TMP/'google_api_usage_v9_2.json', month='2026-10')['effective'], 5010)
        finally:
            bud._month_key = old_month
            if old is None:
                os.environ.pop('GOOGLE_API_USAGE_FILE', None)
            else:
                os.environ['GOOGLE_API_USAGE_FILE'] = old

    def test_reconcile_explicit_loaded_core_path_wins_over_env_guess(self):
        old = os.environ.get('GOOGLE_API_USAGE_FILE')
        old_month = bud._month_key
        try:
            # Simulate the real installation: our guessed/default path differs from
            # the path exposed by the already-loaded Google core.
            os.environ['GOOGLE_API_USAGE_FILE'] = 'wrong_guess.json'
            bud._month_key = lambda now=None: '2026-10'
            self._csv_rows('google_comparison_v3_20261027-a.csv', 2500)
            self._csv_rows('google_live_guard_v3_11_20261027-b.csv', 10)
            self._summary('pvp_import_atomic_v1_20261027-b.json',10,0,'google_live_guard_v3_11')
            actual = TMP / 'runtime_cache_usage.json'
            wrong = TMP / 'wrong_guess.json'
            live.write_core_month_total(2510, path=actual, month='2026-10')
            live.write_core_month_total(7777, path=wrong, month='2026-10')
            bud.seed_ledger_floor(floor_used=5010, cap=9000, month='2026-10')
            got = live.reconcile_usage_state(monthly_cap=9000, usage_path=actual)
            self.assertEqual(Path(got['core_usage_path']), actual)
            self.assertEqual(got['canonical_used_before'], 2510)
            self.assertEqual(live.read_core_month_usage(actual, month='2026-10')['effective'], 2510)
            self.assertEqual(live.read_core_month_usage(wrong, month='2026-10')['effective'], 7777)
            self.assertEqual(bud.read_ledger(month='2026-10')['used'], 2510)
        finally:
            bud._month_key = old_month
            if old is None:
                os.environ.pop('GOOGLE_API_USAGE_FILE', None)
            else:
                os.environ['GOOGLE_API_USAGE_FILE'] = old

    def test_reconcile_repairs_original_v311_signature(self):
        old = os.environ.get('GOOGLE_API_USAGE_FILE')
        old_month = bud._month_key
        try:
            os.environ['GOOGLE_API_USAGE_FILE'] = 'custom_usage.json'
            bud._month_key = lambda now=None: '2026-10'
            self._csv_rows('google_comparison_v3_20261027-a.csv', 2500)
            self._csv_rows('google_live_guard_v3_11_20261027-b.csv', 10)
            self._summary('pvp_import_atomic_v1_20261027-b.json',10,0,'google_live_guard_v3_11')
            p = TMP/'custom_usage.json'
            live.write_core_month_total(2510, path=p, month='2026-10')
            obj=json.loads(p.read_text(encoding='utf-8'))
            obj['months']['2026-10']['geocoding']['external_offset']=2500
            p.write_text(json.dumps(obj),encoding='utf-8')
            bud.seed_ledger_floor(floor_used=5010,cap=9000,month='2026-10')
            got=live.reconcile_usage_state(monthly_cap=9000)
            self.assertTrue(got['bug_signature_repaired'])
            self.assertEqual(got['repair_signature'],'V3_11_ORIGINAL_DOUBLE_COUNT')
            self.assertEqual(got['canonical_used_before'],2510)
            rd=live.read_core_month_usage(p,month='2026-10')
            self.assertEqual((rd['local'],rd['external_offset'],rd['effective']),(2510,0,2510))
            self.assertEqual(bud.read_ledger(month='2026-10')['used'],2510)
        finally:
            bud._month_key=old_month
            if old is None: os.environ.pop('GOOGLE_API_USAGE_FILE',None)
            else: os.environ['GOOGLE_API_USAGE_FILE']=old


    def test_reconcile_repairs_v3114_cache_hit_csv_overcount(self):
        old_month = bud._month_key
        try:
            bud._month_key=lambda now=None:'2026-10'
            self._csv_rows('google_comparison_v3_20261027-a.csv',2500)
            # Three live CSVs, 30 selected rows total; middle run was cache-only.
            self._csv_rows('google_live_guard_v3_11_20261027-a.csv',10)
            self._csv_rows('google_live_guard_v3_11_3_20261027-b.csv',10)
            self._csv_rows('google_live_guard_v3_11_4_20261027-c.csv',10)
            self._summary('pvp_import_atomic_v1_20261027-a.json',10,0,'google_live_guard_v3_11')
            self._summary('pvp_import_atomic_v1_20261027-b.json',0,10,'google_live_guard_v3_11_3')
            self._summary('pvp_import_atomic_v1_20261027-c.json',10,0,'google_live_guard_v3_11_4')
            usage=TMP/'google_api_usage_v9_2.json'
            live.write_core_month_total(2530,path=usage,month='2026-10')
            bud.seed_ledger_floor(floor_used=2530,cap=9000,month='2026-10')
            got=live.reconcile_usage_state(monthly_cap=9000,usage_path=usage)
            self.assertTrue(got['bug_signature_repaired'])
            self.assertEqual(got['repair_signature'],'V3_11_4_LIVE_CSV_CACHE_HIT_OVERCOUNT')
            self.assertEqual(got['prior_live_guard_network_attempts_exact'],20)
            self.assertEqual(got['legacy_live_guard_csv_rows'],30)
            self.assertEqual(got['phantom_live_rows_detected'],10)
            self.assertEqual(got['canonical_used_before'],2520)
            self.assertEqual(live.read_core_month_usage(usage,month='2026-10')['effective'],2520)
            self.assertEqual(bud.read_ledger(month='2026-10')['used'],2520)
        finally:
            bud._month_key=old_month

    def test_forward_constrained_cache_key_matches_core_shape(self):
        class G:
            @staticmethod
            def clean(v):
                return '' if v is None else str(v).strip()
        row={'cap':'00100','comune':'Roma'}
        key=live._forward_constrained_cache_key(G,'Via Test 12',row)
        self.assertEqual(key,'FC|Via Test 12|country:IT|postal_code:00100|locality:Roma')

    def test_pick_network_misses_skips_cached_and_pages(self):
        old_pick=getattr(prev,'pick_fresh_cases',None)
        old_norm=getattr(prev,'normalize_query',None)
        old_max=getattr(prev,'MAX_CASES',None)
        try:
            prev.MAX_CASES=2
            prev.normalize_query=lambda q:q.lower()
            all_items=[
                ({'pvp_id':'1','cap':'','comune':'A'}, {}, 'G', 'Q1', 1, 'P1'),
                ({'pvp_id':'2','cap':'','comune':'A'}, {}, 'G', 'Q2', 1, 'P1'),
                ({'pvp_id':'3','cap':'','comune':'A'}, {}, 'G', 'Q3', 2, 'P2'),
                ({'pvp_id':'4','cap':'','comune':'A'}, {}, 'G', 'Q4', 2, 'P2'),
            ]
            def pick(planned,g,prior_ids,prior_queries,max_cases,priority_only=None):
                out=[]
                for it in all_items:
                    row,_,_,q,_,_=it
                    if row['pvp_id'] in prior_ids or q.lower() in prior_queries:
                        continue
                    out.append(it)
                    if len(out)>=max_cases: break
                return out, {'P1':2,'P2':2}
            prev.pick_fresh_cases=pick
            class G:
                @staticmethod
                def clean(v): return '' if v is None else str(v).strip()
            class C:
                data={'FC|Q1|country:IT|locality:A':{'ok':True},'FC|Q2|country:IT|locality:A':{'ok':False}}
                def get(self,k): return self.data.get(k)
            selected,available,skipped,scanned=live._pick_network_misses([],G,set(),set(),C(),2)
            self.assertEqual([x[3] for x in selected],['Q3','Q4'])
            self.assertEqual(skipped,2)
            self.assertEqual(scanned,4)
        finally:
            if old_pick is None: delattr(prev,'pick_fresh_cases')
            else: prev.pick_fresh_cases=old_pick
            if old_norm is None: delattr(prev,'normalize_query')
            else: prev.normalize_query=old_norm
            if old_max is None: delattr(prev,'MAX_CASES')
            else: prev.MAX_CASES=old_max

    def test_smoke_cap_validation(self):
        with self.assertRaises(ValueError):
            live.preview_new_rows([],max_rows=1,monthly_cap=9000,max_requests=51)

if __name__=='__main__': unittest.main(verbosity=2)
