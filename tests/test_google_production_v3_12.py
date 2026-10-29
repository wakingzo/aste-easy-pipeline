import sys, types, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'/'geocoding'))

paths=types.ModuleType('aste_paths'); paths.REPORTS_DIR=Path('/tmp'); paths.ensure_runtime_dirs=lambda:None
sys.modules['aste_paths']=paths
proj=types.ModuleType('project_env'); proj.load_project_env=lambda required=():None
sys.modules['project_env']=proj

off=types.ModuleType('geocode_pending_v2')
def valid_pair(a,b):
    try: return -90<=float(a)<=90 and -180<=float(b)<=180 and not(float(a)==0 and float(b)==0)
    except: return False
off.valid_pair=valid_pair; off.maps_link=lambda a,b:f'https://maps.test/{a:.8f},{b:.8f}'
off.SELECT=('pvp_id',); off.HERE=Path('/tmp')
sys.modules['geocode_pending_v2']=off

bud=types.ModuleType('google_budget_v3_10'); sys.modules['google_budget_v3_10']=bud
live=types.ModuleType('google_live_guard_v3_11'); sys.modules['google_live_guard_v3_11']=live
pol=types.ModuleType('google_policy_v3_9'); sys.modules['google_policy_v3_9']=pol
prev=types.ModuleType('google_preview_v3'); sys.modules['google_preview_v3']=prev
elig=types.ModuleType('google_eligibility_v3_2')
elig.eligibility=lambda r,b:{'google_reason':'X'}
elig.canonical=lambda s:' '.join(str(s or '').lower().replace(',',' ').split())
sys.modules['google_eligibility_v3_2']=elig

import google_production_v3_12 as m

class G:
    @staticmethod
    def constrained_address_variants(row): return [row.get('variant','')]

class ProductionTests(unittest.TestCase):
    def test_review_without_precise_pin_is_stored_as_comune_marker(self):
        row={'pvp_id':'1','indirizzo':'X'}
        base={'geocode_status':'REVIEW','geocode_source':'review_x','geocode_precision':'NESSUNA','geocode_decision_reason':'conflict'}
        final={'final_status':'REVIEW','final_source':'review_google_conflict','final_precision':'NESSUNA','final_reason':'discordanza'}
        mp={'map_lat':40.1,'map_lon':9.2,'map_source':'istat_comune_fallback_v39','map_precision':'COMUNE','map_is_fallback':True}
        p=m._payload(row,base,final,mp,google_outcome='DEFERRED')
        self.assertEqual(p['geocode_status'],'REVIEW')
        self.assertEqual(p['geocode_precision'],'COMUNE')
        self.assertEqual(p['geocode_source'],'istat_comune_fallback_v312')
        self.assertIn('underlying_source=review_google_conflict',p['geocode_decision_reason'])
        self.assertTrue(m.offline.valid_pair(p['latitudine'],p['longitudine']))

    def test_nonfallback_preserves_final_precision(self):
        row={'pvp_id':'1','indirizzo':'Via Roma 1'}
        base={'geocode_status':'DONE','geocode_source':'x','geocode_precision':'COMUNE','geocode_decision_reason':'x'}
        final={'final_status':'DONE','final_source':'google_civico','final_precision':'CIVICO','final_reason':'ok'}
        mp={'map_lat':40.1,'map_lon':9.2,'map_source':'google_civico','map_precision':'CIVICO','map_is_fallback':False}
        p=m._payload(row,base,final,mp,evidence={'google_location_type':'ROOFTOP'},query='Via Roma 1',google_outcome='OK')
        self.assertEqual((p['geocode_status'],p['geocode_precision'],p['geocode_source']),('DONE','CIVICO','google_civico'))
        self.assertEqual(p['geocode_location_type'],'ROOFTOP')

    def test_query_guard_requires_toponym_and_civic(self):
        try:
            prev.priority_bucket=lambda row,base:(2,'P2',{'address_name':'Via Giacomo Leopardi','address_civic':'26','address_kind':'STRADA'})
            self.assertIsNone(m._query_for({'variant':'Via Giacomo 26'}, {}, G()))
            self.assertIsNone(m._query_for({'variant':'Via Giacomo Leopardi'}, {}, G()))
            z=m._query_for({'variant':'Via Giacomo Leopardi 26'}, {}, G())
            self.assertEqual(z['label'],'P2')
        finally:
            delattr(prev,'priority_bucket')


    def test_apply_path_prepares_map_then_guarded_patch(self):
        import os, tempfile
        os.environ['SUPABASE_URL']='https://example.invalid'; os.environ['SUPABASE_SERVICE_ROLE']='k'; os.environ['GOOGLE_GEOCODING_API_KEY']='g'
        row={'pvp_id':'1','geocode_status':'PENDING','latitudine':None,'longitudine':None,'geocode_input_hash':'h','indirizzo':'Localita X'}
        baseline={'geocode_status':'REVIEW','geocode_source':'review_x','geocode_precision':'NESSUNA','geocode_decision_reason':'conflict','latitudine':None,'longitudine':None}
        old={}
        names=['fetch_rows_by_ids','_load_core_and_reconcile','_query_for']
        for n in names: old[n]=getattr(m,n)
        attrs=['prepare_context','plan_rows','guarded_patch']
        for n in attrs: old['off_'+n]=getattr(m.offline,n,None)
        old['bud_read']=getattr(m.budget,'read_ledger',None)
        old['pol_apply']=getattr(m.policy,'apply_policy',None); old['pol_map']=getattr(m.policy,'choose_map_point',None)
        old_report=m.REPORTS_DIR
        class AU:
            run={'geocoding':{'network_attempts':0,'cache_hits':0}}
            def month_usage(self,sku): return (0,0,0)
            def save(self): pass
        class Cache:
            def __init__(self,p): pass
            def save(self): pass
        class FG: API_USAGE=AU(); CACHE_FILE=Path('/tmp/c'); CacheStore=Cache
        try:
            m.REPORTS_DIR=Path(tempfile.mkdtemp())
            m.fetch_rows_by_ids=lambda base,key,ids:[dict(row)]
            m.offline.prepare_context=lambda no_anncsu=False:(object(),object(),object())
            m.offline.plan_rows=lambda rows,ctx,t,l:[(rows[0],dict(baseline))]
            calls=[]; m.offline.guarded_patch=lambda base,key,r,p:(calls.append((r,p)) or True)
            m._load_core_and_reconcile=lambda **kw:(FG(),{'month':'2026-10','canonical_used_before':0})
            m._query_for=lambda r,b,g:None
            m.budget.read_ledger=lambda month=None:{'used':0}
            m.policy.apply_policy=lambda r,b,e:{'final_status':'REVIEW','final_source':'review_x','final_precision':'NESSUNA','final_reason':'conflict','policy_action':'KEEP'}
            m.policy.choose_map_point=lambda r,f,ctx,c:{'map_lat':40.0,'map_lon':9.0,'map_source':'istat_comune_fallback_v39','map_precision':'COMUNE','map_is_fallback':True}
            z=m.run_for_ids(['1'],apply=True,max_rows=10,monthly_cap=9000,run_cap=10)
            self.assertEqual(z['patched_verified'],1); self.assertEqual(z['map_coverage']['unmapped'],0)
            self.assertEqual(calls[0][1]['geocode_precision'],'COMUNE')
        finally:
            m.REPORTS_DIR=old_report
            for n in names: setattr(m,n,old[n])
            for n in attrs:
                if old['off_'+n] is not None: setattr(m.offline,n,old['off_'+n])
            if old['bud_read'] is not None: m.budget.read_ledger=old['bud_read']
            if old['pol_apply'] is not None: m.policy.apply_policy=old['pol_apply']
            if old['pol_map'] is not None: m.policy.choose_map_point=old['pol_map']

if __name__=='__main__': unittest.main()
