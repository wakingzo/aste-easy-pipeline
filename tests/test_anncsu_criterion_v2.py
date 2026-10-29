"""Pure offline criterion regression tests; no HTTP / Supabase / Google."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
extra = os.environ.get('ASTE_TEST_EXISTING_SRC')
if extra: sys.path.insert(0, extra)
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC/'importer'))
sys.path.insert(0, str(SRC/'geocoding'))

class TestCriterionV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.old_ref=os.environ.get('ASTE_REFERENCE_DIR')
        os.environ['ASTE_REFERENCE_DIR']=cls.tmp.name
        (Path(cls.tmp.name)/'comuni_cap.csv').write_text('comune;provincia;sigla_provincia;regione;cap\nTempio Pausania;Sassari;SS;Sardegna;07029\n',encoding='utf-8')
        import geocode_pending_v2 as geo
        import anncsu_runtime_consolidated_v2 as ann
        cls.geo=geo; cls.ann=ann

    @classmethod
    def tearDownClass(cls):
        if cls.old_ref is None: os.environ.pop('ASTE_REFERENCE_DIR',None)
        else: os.environ['ASTE_REFERENCE_DIR']=cls.old_ref
        cls.tmp.cleanup()

    def row(self, **changes):
        r={'pvp_id':'42','comune':'Tempio Pausania','sigla_provincia':'SS','indirizzo':'Via Roma, Tempio Pausania',
           'pvp_latitudine':40.90,'pvp_longitudine':9.1,'pvp_geo_source':'bene:1/1',
           'latitudine':None,'longitudine':None,'geocode_status':'PENDING','geocode_input_hash':'a'*64}
        r.update(changes)
        return r

    def ctx(self):
        class Context:
            def resolve_admin(self,r):return {'status':'RESOLVED','canonical_code':'090070'}
            def test(self,r,lat=None,lon=None,admin_meta=None):
                return {'position':'INSIDE' if lat is not None else 'NO_COORDS'}
            def representative_point(self,r,admin_meta=None):return {'ok':True,'lat':40.9,'lon':9.1}
        return Context()

    def terr(self, choice=None):
        class Territorial:
            def local_option(self,row,ev,local,ctx,meta):return choice
        return Territorial()

    def ev(self, **updates):
        out={'province_relation':'MATCH','ambiguity':'NO','direct_locality_guard':'NO_HINT',
             'street_score':.99,'strict_street_similarity':.98,'matching_street_candidates':1,
             'street_representative_status':'UNIQUE','anncsu_odonimo':'Via Roma',
             'street_representative':{'lat':40.89,'lon':9.05,'max_from_rep_m':350,'unique_coord_count':40},
             'selected_street_high_accuracy_accesses':20,
             'selected_street_nearest_high_accuracy_access_m':40,
             'runtime_locality_match_mode':'','runtime_locality_source_specificity':'HAS_SPECIFIC'}
        out.update(updates);return out

    def decide(self, r=None, ev=None, local=None, terr=None):
        class Local:
            @staticmethod
            def locality_for_missing(row, evidence, allow_fallback=True):
                return (evidence.get('runtime_locality_match_mode') == 'EXACT' and
                        evidence.get('runtime_locality_source_specificity') == 'LOCALITY_ONLY' and
                        evidence.get('runtime_locality_accesses',0) >= 5 and
                        evidence.get('runtime_locality_p90_m',float('inf')) <= 250, 'test')
        return self.geo.decision(r or self.row(),self.ctx(),terr or self.terr(),
                                 local if local is not None else Local(), ev if ev is not None else self.ev())

    def test_road_access_not_distant_representative(self):
        p=self.decide()
        self.assertEqual(p['geocode_precision'],'PVP_STRADA_COMPATIBILE')
        self.assertEqual(p['latitudine'],40.9)
        self.assertIn('40 m',p['geocode_decision_reason'])
        self.assertEqual(p['geocode_google_outcome'],'NOT_REQUESTED')

    def test_road_uses_nearest_actual_access_not_centroid(self):
        p=self.decide(ev=self.ev(selected_street_nearest_high_accuracy_access_m=720,
                                  street_representative={'lat':40.9,'lon':9.1,'max_from_rep_m':320}))
        self.assertEqual(p['geocode_precision'],'PVP_IN_COMUNE')
        self.assertIn('720 m',p['geocode_decision_reason'])

    def test_street_beyond_150_not_false_verified(self):
        p=self.decide(ev=self.ev(selected_street_nearest_high_accuracy_access_m=210))
        self.assertEqual(p['geocode_precision'],'PVP_IN_COMUNE')
        self.assertEqual(p['geocode_status'],'DONE')

    def test_compact_distant_road_requires_review_without_pin(self):
        p=self.decide(ev=self.ev(selected_street_nearest_high_accuracy_access_m=1700))
        self.assertEqual(p['geocode_status'],'REVIEW')
        self.assertIsNone(p['latitudine'])
        self.assertEqual(p['geocode_source'],'review_pvp_strada_discordante')

    def test_long_road_never_review_solely_for_distant_access(self):
        p=self.decide(ev=self.ev(selected_street_nearest_high_accuracy_access_m=2500,
                street_representative={'max_from_rep_m':3500,'unique_coord_count':50}))
        self.assertEqual(p['geocode_precision'],'PVP_IN_COMUNE')
        self.assertIsNotNone(p['latitudine'])

    def test_multi_bene_never_review_from_street_geometry(self):
        p=self.decide(self.row(pvp_geo_source='bene_multi:1/2'),
                      ev=self.ev(selected_street_nearest_high_accuracy_access_m=2500))
        self.assertEqual(p['geocode_precision'],'PVP_IN_COMUNE')

    def test_rural_km_never_review_from_street_geometry(self):
        p=self.decide(self.row(indirizzo='Strada statale SS 127 km 31, Tempio Pausania'),
                      ev=self.ev(selected_street_nearest_high_accuracy_access_m=2500))
        self.assertEqual(p['geocode_precision'],'PVP_IN_COMUNE')

    def test_street_low_similarity_fails_closed(self):
        p=self.decide(ev=self.ev(strict_street_similarity=.84))
        self.assertEqual(p['geocode_precision'],'PVP_IN_COMUNE')
        self.assertEqual(p['geocode_source'],'pvp_comune_compatibile')

    def test_street_ambiguous_progressivo_fails_closed(self):
        p=self.decide(ev=self.ev(street_representative_status='SELECTED_ODONIMO_PROGRESSIVI_2'))
        self.assertEqual(p['geocode_precision'],'PVP_IN_COMUNE')

    def test_street_insufficient_high_accuracy_fails_closed(self):
        p=self.decide(ev=self.ev(selected_street_high_accuracy_accesses=0,
                                  selected_street_nearest_high_accuracy_access_m=''))
        self.assertEqual(p['geocode_precision'],'PVP_IN_COMUNE')

    def test_civic_wins_over_street(self):
        p=self.decide(ev=self.ev(), terr=self.terr({'precision':'CIVICO','lat':40.9,'lon':9.1,'reason':'unique'}))
        self.assertEqual(p['geocode_precision'],'PVP_CIVICO_COMPATIBILE')

    def test_middle_civic_distance_is_not_road_confirmed(self):
        civic={'precision':'CIVICO','lat':40.9015,'lon':9.1,'reason':'unique'}
        p=self.decide(ev=self.ev(),terr=self.terr(civic))
        self.assertEqual(p['geocode_precision'],'PVP_IN_COMUNE')
        self.assertEqual(p['geocode_source'],'pvp_civico_non_conclusivo')

    def test_distant_civic_never_auto_replaces_pvp(self):
        civic={'precision':'CIVICO','lat':40.95,'lon':9.1,'reason':'unique'}
        p=self.decide(ev=self.ev(),terr=self.terr(civic))
        self.assertEqual(p['geocode_status'],'REVIEW')
        self.assertIsNone(p['latitudine'])
        self.assertEqual(p['geocode_source'],'review_pvp_civico_discordante')
        self.assertIn('nessuna sostituzione automatica',p['geocode_decision_reason'])

    def test_locality_exact_compact_near(self):
        evidence=self.ev(street_score=.30, runtime_locality_match_mode='EXACT',
                         runtime_locality_source_specificity='LOCALITY_ONLY',
                         runtime_anncsu_locality='Monti', runtime_locality_accesses=15,
                         runtime_locality_p90_m=120,runtime_locality_max_m=220,
                         matched_locality_high_accuracy_accesses=12,
                         matched_locality_nearest_high_accuracy_access_m=85)
        p=self.decide(self.row(indirizzo='Località Monti, Tempio Pausania'),ev=evidence)
        self.assertEqual(p['geocode_precision'],'PVP_LOCALITA_COMPATIBILE')
        self.assertIn('85 m',p['geocode_decision_reason'])

    def test_locality_distant_compact_requires_review(self):
        evidence=self.ev(street_score=.30, runtime_locality_match_mode='EXACT',
                         runtime_locality_source_specificity='LOCALITY_ONLY',
                         runtime_anncsu_locality='Monti', runtime_locality_accesses=15,
                         runtime_locality_p90_m=120,runtime_locality_max_m=220,
                         matched_locality_high_accuracy_accesses=12,
                         matched_locality_nearest_high_accuracy_access_m=1800)
        p=self.decide(self.row(indirizzo='Località Monti, Tempio Pausania'),ev=evidence)
        self.assertEqual(p['geocode_status'],'REVIEW')
        self.assertIsNone(p['latitudine'])

    def test_locality_explicit_unmatched_never_confirmed(self):
        evidence=self.ev(street_score=.30,runtime_locality_match_mode='EXPLICIT_UNMATCHED',
                         runtime_locality_source_specificity='LOCALITY_ONLY',
                         matched_locality_high_accuracy_accesses=12,
                         matched_locality_nearest_high_accuracy_access_m=20)
        p=self.decide(self.row(indirizzo='Località Mocaiana'),ev=evidence)
        self.assertEqual(p['geocode_precision'],'PVP_IN_COMUNE')

    def test_locality_with_specific_road_not_confirmed(self):
        evidence=self.ev(street_score=.30,runtime_locality_match_mode='EXACT',
                         runtime_locality_source_specificity='HAS_SPECIFIC',
                         runtime_locality_accesses=15,runtime_locality_p90_m=100,
                         matched_locality_high_accuracy_accesses=12,
                         matched_locality_nearest_high_accuracy_access_m=20)
        p=self.decide(self.row(indirizzo='Località Monti, Via Roma 9'),ev=evidence)
        self.assertEqual(p['geocode_precision'],'PVP_IN_COMUNE')

    def test_no_pvp_uses_existing_local_option(self):
        r=self.row(pvp_latitudine=None,pvp_longitudine=None)
        p=self.decide(r,terr=self.terr({'precision':'STRADA','lat':40.91,'lon':9.11,'reason':'simple_no_civic'}))
        self.assertEqual(p['geocode_precision'],'STRADA')

    def test_nearest_access_filter_dedup_and_methods(self):
        class Ann:
            @staticmethod
            def haversine_m(a,b,c,d):return abs(a-c)*100000+abs(b-d)*100000
        aa=[('1',40.9,9.1,'1'),('2',40.9,9.1,'1'),('3',40.9005,9.1,'3.0'),
            ('4',40.9,9.10001,'2'),('5',0,0,'1')]
        m=self.ann._nearest_high_accuracy_access(Ann(),aa,40.9,9.1)
        self.assertEqual(m['high_accuracy_accesses'],2)
        self.assertEqual(m['nearest_m'],0)

    def test_no_nearest_when_only_low_quality(self):
        class Ann:
            @staticmethod
            def haversine_m(*a):return 0
        self.assertEqual(self.ann._nearest_high_accuracy_access(Ann(),[('1',40.9,9.1,'2')],40.9,9.1)['nearest_m'],'')

    def test_runtime_evidence_uses_selected_progressivo_not_other_street(self):
        """Stubbed ANNCSU scan verifies the geometry feeds the v2 decision."""
        from types import SimpleNamespace
        r=self.ann
        t=Path(self.tmp.name)
        (t/'istat.xlsx').touch()
        class Ann:
            MUNICIPALITY_ALIASES={}
            @staticmethod
            def norm_name(s):return str(s).casefold().strip()
            @staticmethod
            def haversine_m(a,b,c,d):return abs(a-c)*100000+abs(b-d)*100000
            @staticmethod
            def scan_strad(*args,**kwargs):return {'42':[(.99,'P1','Via Roma',''),(.4,'P2','Via Milano','')]}
            @staticmethod
            def evaluate_record_v5(*args,**kwargs):return {
                'anncsu_odonimo':'Via Roma', 'street_score':.99,
                'matching_street_candidates':1,'province_relation':'MATCH',
                'ambiguity':'NO','direct_locality_guard':'NO_HINT'}
        audit=SimpleNamespace(
            ANNCSU_STRAD_FILE='STRAD', ANNCSU_INDIR_FILE='INDIR', ISTAT_COMUNI_FILE='istat.xlsx',
            pick_input=lambda *args:Path('not-read.csv'),
            safe_float=lambda val:float(val) if val is not None else None,
            load_istat_comuni_plus=lambda *args:{},
            assert_istat_sardinia_2026=lambda *args:None,
            build_anncsu_catasto_crosswalk=lambda *args:{},
            resolve_municipalities_v2_2=lambda *args:({'42':'090070'},{'42':{}},[]),
            scan_indir_plus=lambda *args:({'P1': [('4',40.9005,9.1,'1'),('5',40.901,9.1,'3')],
                                            'P2': [('8',40.9,9.1,'1') ]},{}),
            nearest_plausible_street_distance=lambda *args:0,
            stored_reverse_evidence=lambda *args:{},
            distance_outside_bbox_m=lambda *args:0,
            strict_street_similarity=lambda *args:.98,
            classify_v2_2=lambda *args:('MATCH',0),
            correction_tier_v2_2=lambda *args:('TIER','ok'))
        core=SimpleNamespace(expected_house_numbers=lambda row:set())
        def loader(path,name):
            filename=Path(path).name
            if 'audit' in filename:return audit
            if 'anncsu_local' in filename:return Ann
            return core
        row=self.row()
        with mock.patch.object(r,'_load',side_effect=loader), \
             mock.patch.object(r,'REFERENCE_DIR',t), \
             mock.patch.object(r,'_scan_strad_localities',return_value={
                 '42':{'match_mode':'EXACT','progressivi':['P2'],
                       'anncsu_locality':'Milano','streets_count':1}}):
            evidence,summary=r.build_local_evidence([self.geo.pvp_row(row)])
        e=evidence[0]
        self.assertEqual(e['street_progressivo'],'P1')
        # Via Milano has an access AT the PVP point but may not corroborate Via Roma.
        self.assertEqual(e['selected_street_nearest_high_accuracy_access_m'],50)
        self.assertEqual(e['matched_locality_nearest_high_accuracy_access_m'],0)
        p=self.decide(row,ev=e)
        self.assertEqual(p['geocode_precision'],'PVP_STRADA_COMPATIBILE')

    def test_v2_entrypoint_imports_v2_geocoder_without_side_effects(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location(
            'consolidated_v2', ROOT/'src'/'pipeline'/'daily_pipeline_consolidated_v2.py')
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.geocoder.VERSION,'geocode_pending_offline_v2')
        with mock.patch.object(module.geocoder,'prepare_context',return_value=(self.ctx(),self.terr(),None)), \
             mock.patch.object(module.importer,'run',return_value={}) as imp, \
             mock.patch.object(module.subprocess,'run',side_effect=AssertionError('builder called')):
            self.assertEqual(module.main([]),0)
        self.assertFalse(imp.call_args.kwargs['apply'])

    def test_v2_apply_blocked_before_import_or_network(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location(
            'consolidated_v2_apply_guard',ROOT/'src'/'pipeline'/'daily_pipeline_consolidated_v2.py')
        mod=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with mock.patch.object(mod.importer,'run',side_effect=AssertionError('import attempted')), \
             mock.patch.object(mod.geocoder,'prepare_context',side_effect=AssertionError('geo preflight attempted')):
            with self.assertRaises(SystemExit) as cm:
                mod.main(['--apply','--expected-new','616','--expected-raw-sha256','f'*64])
        self.assertEqual(cm.exception.code,2)
        with mock.patch.object(self.geo,'load_project_env',side_effect=AssertionError('network attempted')):
            with self.assertRaisesRegex(RuntimeError,'--apply non disponibile'):
                self.geo.run(apply=True,expected_pending=1)

    def test_no_db_or_google_in_preview(self):
        row=self.row()
        with mock.patch.object(self.geo,'prepare_context',return_value=(self.ctx(),self.terr(),None)), \
             mock.patch.object(self.geo,'_json_request',side_effect=AssertionError('DB called')):
            result=self.geo.preview_new_rows([row],max_rows=1,no_anncsu=True)
        self.assertEqual(result['google_calls'],0)
        self.assertEqual(result['decision_counts'],{'DONE:PVP_IN_COMUNE':1})

if __name__=='__main__':unittest.main(verbosity=2)
