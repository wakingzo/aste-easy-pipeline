"""No network: Google comparison categorization, geographic guard, spending caps."""
from __future__ import annotations
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / 'src'
# Avoid importing any heavy local ANNCSU/ISTAT/Supabase libraries in isolated tests.
paths = types.ModuleType('aste_paths')
paths.REPORTS_DIR = Path(tempfile.gettempdir())
paths.ensure_runtime_dirs = lambda: None
sys.modules['aste_paths'] = paths
env = types.ModuleType('project_env')
env.load_project_env = lambda **kwargs: None
sys.modules['project_env'] = env
geo = types.ModuleType('geocode_pending_v2')
geo.HERE = SRC / 'geocoding'
geo.SELECT = ('pvp_id','comune','indirizzo','pvp_geo_source','pvp_latitudine',
              'pvp_longitudine','geocode_status','sigla_provincia')
geo.valid_pair = lambda la, lo: la is not None and lo is not None
geo.distance_m = lambda a,b,c,d: 180 if (a != c or b != d) else 0
geo.pvp_row = lambda row: dict(row)
geo.prepare_context = lambda **kwargs: (None, None, None)
geo.plan_rows = lambda *args: []
sys.modules['geocode_pending_v2'] = geo
sys.path.insert(0, str(SRC / 'geocoding'))
import google_preview_v3 as m

class Context:
    def resolve_admin(self, row):
        return {'status':'RESOLVED','canonical_code':'090070'}
    def test(self, row, lat=None, lon=None, admin_meta=None):
        return {'position':'INSIDE' if lat == 40.9 else 'OUTSIDE'}

class Core:
    @staticmethod
    def score_forward_candidate(row, candidate):
        return {'score':candidate.get('score',90), 'reasons':['comune_ok','indirizzo_ok']}

class GooglePreviewTests(unittest.TestCase):
    def row(self, **kw):
        row={'pvp_id':'1','indirizzo':'Via Roma 5, Tempio Pausania',
             'comune':'Tempio Pausania','pvp_geo_source':'bene:1/1',
             'pvp_latitudine':40.9,'pvp_longitudine':9.1}
        row.update(kw)
        return row
    def dec(self, source='pvp_comune_compatibile', precision='PVP_IN_COMUNE', status='DONE'):
        return {'geocode_source':source,'geocode_precision':precision,'geocode_status':status,
                'latitudine':40.9,'longitudine':9.1}
    def candidate(self, **kw):
        c={'lat':40.9,'lon':9.1,'ok':True,'partial_match':False,'location_type':'ROOFTOP',
           'formatted_address':'Via Roma 5, Tempio Pausania','score':90,
           'components':[{'long_name':'5','short_name':'5','types':['street_number']}]}
        c.update(kw)
        return c
    def test_read_real_previous_report_shape_without_network(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td)/'google_comparison_v3_example.csv').write_text(
                'pvp_id;query;assessment\n1;Via Roma 5, Roma;X\n2;  Via  Roma 5, Roma ;Y\n',
                encoding='utf-8-sig')
            ids,queries,files=m.previously_compared(Path(td))
        self.assertEqual(ids, {'1','2'})
        self.assertEqual(queries, {'via roma 5, roma'})
        self.assertEqual(len(files),1)

    def test_gate_prevents_paid_queries_for_vague_addresses(self):
        core=types.SimpleNamespace(address_specificity=lambda row: True,
            constrained_address_variants=lambda row: [row['indirizzo']])
        items=[(self.row(pvp_id='100',indirizzo='Via nd, 07029 Tempio Pausania'),self.dec(precision='COMUNE')),
               (self.row(pvp_id='101',indirizzo='Foglio 12 Mappale 1134'),self.dec(precision='COMUNE')),
               (self.row(pvp_id='102',indirizzo='Via Roma 5, Tempio Pausania'),self.dec(precision='COMUNE'))]
        selected,_=m.pick_fresh_cases(items,core,set(),set(),3)
        self.assertEqual([x[0]['pvp_id'] for x in selected],['102'])

    def test_anncsu_resolved_address_not_sent_to_google(self):
        self.assertIsNone(m.category(self.row(),self.dec(source='anncsu_civico',precision='CIVICO')))
        self.assertIsNone(m.category(self.row(),self.dec(source='pvp_anncsu_strada_compatibile',precision='PVP_STRADA_COMPATIBILE')))

    def test_prior_report_missing_fails_without_google(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(RuntimeError,'Previous Google CSV'):
                m.previously_compared(Path(td))

    def test_repeated_query_is_skipped_even_for_different_listing_id(self):
        core=types.SimpleNamespace(address_specificity=lambda row:True,
            constrained_address_variants=lambda row:[row['indirizzo']])
        planned=[(self.row(pvp_id='10',indirizzo='Via Nuova, 5'),self.dec(precision='COMUNE')),
                 (self.row(pvp_id='11',indirizzo='  via  nuova, 5 '),self.dec(precision='COMUNE')),
                 (self.row(pvp_id='12',indirizzo='Via Roma, 7'),self.dec(precision='PVP_IN_COMUNE'))]
        chosen,_=m.pick_fresh_cases(planned,core,set(),set(),3)
        self.assertEqual(len(chosen),2)
        self.assertEqual(len({m.normalize_query(x[3]) for x in chosen}),2)
        excluded,_=m.pick_fresh_cases(planned,core,{'10'}, {'via nuova, 5'},3)
        self.assertEqual([x[0]['pvp_id'] for x in excluded],['12'])

    def test_civic_address_excludes_cadastral_and_cap(self):
        self.assertTrue(m.has_civic_address('Via Roma, 5, 00100 Roma'))
        self.assertTrue(m.has_civic_address('Contrada Palombara, 7, 63833 Montegiorgio'))
        self.assertFalse(m.has_civic_address('Foglio 12 Mappale 1134-1133'))
        self.assertFalse(m.has_civic_address('Via Roma, 00100 Roma'))

    def test_route_number_is_not_civic_and_abbreviated_via_is(self):
        self.assertFalse(m.has_civic_address('SS 47 Alberobello n.45, Putignano'))
        self.assertFalse(m.has_civic_address('SS 538, Arielli'))
        self.assertTrue(m.has_civic_address('V. Giacomo Leopardi, 26, Pedaso'))
        self.assertFalse(m.has_civic_address('Via 4 Novembre, Nuoro'))


    def test_numeric_extraurban_query_must_keep_route_number(self):
        r=self.row(pvp_id='route',indirizzo='SS 538, Arielli',comune='Arielli')
        for query in ('Arielli','SS, Arielli','Via 538, Arielli'):
            with self.subTest(query=query):
                core=types.SimpleNamespace(constrained_address_variants=lambda row,q=query:[q])
                chosen,_=m.pick_fresh_cases([(r,self.dec())],core,set(),set(),1)
                self.assertEqual(chosen,[])
        core=types.SimpleNamespace(constrained_address_variants=lambda row:['SS 538, Arielli'])
        chosen,_=m.pick_fresh_cases([(r,self.dec())],core,set(),set(),1)
        self.assertEqual(len(chosen),1)

    def test_shortened_query_dropping_toponym_or_civic_is_rejected(self):
        r = self.row(pvp_id='x', indirizzo='V. Giacomo Leopardi, 26, Pedaso', comune='Pedaso')
        for query in ('Via Giacomo 26, Pedaso', 'Via Giacomo Leopardi, Pedaso',
                      'Pedaso', 'Via Roma 26, Pedaso'):
            with self.subTest(query=query):
                core = types.SimpleNamespace(constrained_address_variants=lambda row, q=query: [q])
                result, _ = m.pick_fresh_cases([(r,self.dec())], core, set(), set(), 1)
                self.assertEqual(result, [])
        core = types.SimpleNamespace(constrained_address_variants=lambda row: ['Via Giacomo Leopardi 26, Pedaso'])
        result, _ = m.pick_fresh_cases([(r,self.dec())], core, set(), set(), 1)
        self.assertEqual(len(result), 1)

    def test_exact_google_coordinate_reports_not_independent(self):
        result=m.compare_candidate(self.row(),self.dec(),[self.candidate()],Context(),Core())
        self.assertEqual(result['assessment'],'GOOGLE_COINCIDE_CON_PVP_ORIGINE_NON_INDIPENDENTE')

    def test_max_cases_one_keeps_highest_priority_only(self):
        core=types.SimpleNamespace(constrained_address_variants=lambda row:[row['indirizzo']])
        items=[(self.row(pvp_id='1',indirizzo='Via Uno, 3',pvp_latitudine=None,pvp_longitudine=None),
                self.dec(source='istat_comune_indicativo',precision='COMUNE')),
               (self.row(pvp_id='2',indirizzo='Via Due, 4'),self.dec(precision='PVP_IN_COMUNE'))]
        chosen,_=m.pick_fresh_cases(items,core,set(),set(),1)
        self.assertEqual(len(chosen),1)
        self.assertEqual(chosen[0][2],'SENZA_COORDINATE_PVP')
        self.assertEqual(chosen[0][5],'P2_SENZA_PVP_CON_CIVICO')

    def test_multi_bene_excluded(self):
        self.assertIsNone(m.category(self.row(pvp_geo_source='bene_multi:1/2'),self.dec()))
    def test_generic_municipality_excluded(self):
        self.assertIsNone(m.category(self.row(indirizzo='Comune di Tempio Pausania'),self.dec()))
    def test_civic_priority(self):
        self.assertEqual(m.category(self.row(), self.dec(source='pvp_civico_non_conclusivo')), 'PVP_CIVICO_NON_CONCLUSIVO')
    def test_municipality_priority(self):
        self.assertEqual(m.category(self.row(),self.dec(precision='COMUNE')), 'COMUNE_INDICATIVO')
    def test_select_max_and_excludes_precise(self):
        items=[]
        for i in range(35):
            items.append((self.row(pvp_id=str(i)),self.dec(precision='COMUNE' if i<15 else 'PVP_IN_COMUNE')))
        items.append((self.row(pvp_id='100'),self.dec(precision='CIVICO')))
        core=types.SimpleNamespace(address_specificity=lambda row: True,
            constrained_address_variants=lambda row: ['Via Roma 5, '+str(row['pvp_id'])+' Tempio Pausania'])
        selected, counts=m.pick_fresh_cases(items,core,set(),set(),25)
        self.assertEqual(len(selected),25)
        self.assertNotIn('100',{x[0]['pvp_id'] for x in selected})
    def test_max_cases_fails_closed(self):
        with self.assertRaises(ValueError):m.pick_fresh_cases([],object(),set(),set(),501)

    def test_priority_order_discrepancy_then_no_pvp_civic_then_pvp_civic_then_toponyms(self):
        core=types.SimpleNamespace(constrained_address_variants=lambda row:[row['indirizzo']])
        planned=[
            (self.row(pvp_id='5',indirizzo='Via Cinque, Tempio Pausania'),self.dec(precision='PVP_IN_COMUNE')),
            (self.row(pvp_id='4',indirizzo='Via Quattro, Tempio Pausania',pvp_latitudine=None,pvp_longitudine=None),self.dec(source='istat_comune_indicativo',precision='COMUNE')),
            (self.row(pvp_id='3',indirizzo='Via Tre 3, Tempio Pausania'),self.dec(precision='PVP_IN_COMUNE')),
            (self.row(pvp_id='2',indirizzo='Via Due 2, Tempio Pausania',pvp_latitudine=None,pvp_longitudine=None),self.dec(source='istat_comune_indicativo',precision='COMUNE')),
            (self.row(pvp_id='1',indirizzo='Via Uno 1, Tempio Pausania'),self.dec(source='review_pvp_civico_discordante',precision='NESSUNA',status='REVIEW')),
        ]
        chosen,_=m.pick_fresh_cases(planned,core,set(),set(),5)
        self.assertEqual([x[5] for x in chosen],[
            'P1_DISCORDANZA_PVP_ANNCSU',
            'P2_SENZA_PVP_CON_CIVICO',
            'P3_PVP_NON_CONFERMATO_CON_CIVICO',
            'P4_SENZA_PVP_TOPONIMO',
            'P5_PVP_NON_CONFERMATO_TOPONIMO'])

    def test_limit_allows_500_but_not_501(self):
        self.assertEqual(m.MAX_REQUESTS,500)
        self.assertEqual(m.MAX_CASES,500)
        # Empty inputs still validate the cap before selection.
        m.pick_fresh_cases([],object(),set(),set(),500)
        with self.assertRaises(ValueError):
            m.pick_fresh_cases([],object(),set(),set(),501)

    def test_outside_municipality_rejected_even_rooftop(self):
        z=m.compare_candidate(self.row(),self.dec(),[self.candidate(lat=41.9)],Context(),Core())
        self.assertEqual(z['assessment'],'NESSUN_CANDIDATO_COHERENTE')
    def test_partial_match_rejected_even_rooftop(self):
        z=m.compare_candidate(self.row(),self.dec(),[self.candidate(partial_match=True)],Context(),Core())
        self.assertEqual(z['assessment'],'NESSUN_CANDIDATO_COHERENTE')
    def test_approximate_not_promoted(self):
        z=m.compare_candidate(self.row(),self.dec(),[self.candidate(location_type='APPROXIMATE')],Context(),Core())
        self.assertEqual(z['assessment'],'INDIZIO_NON_PRECISO_NON_SOSTITUIRE')
    def test_rooftop_high_score_is_only_candidate(self):
        z=m.compare_candidate(self.row(pvp_latitudine=None,pvp_longitudine=None),self.dec(),[self.candidate()],Context(),Core())
        self.assertEqual(z['assessment'],'GOOGLE_CIVICO_ESATTO_DA_VALIDARE')
        self.assertEqual(z['google_civic_relation'],'exact')

    def test_review_discrepancy_compares_google_to_original_pvp_not_empty_baseline(self):
        baseline=self.dec(source='review_pvp_civico_discordante',precision='NESSUNA',status='REVIEW')
        baseline['latitudine']=None; baseline['longitudine']=None
        same=m.compare_candidate(self.row(),baseline,[self.candidate()],Context(),Core())
        self.assertEqual(same['assessment'],'GOOGLE_COINCIDE_CON_PVP_ORIGINE_NON_INDIPENDENTE')
        self.assertEqual(same['google_distance_from_pvp_m'],0)
        self.assertIsNone(same['google_distance_from_offline_m'])

    def test_existing_pin_distance_not_auto_replace(self):
        z=m.compare_candidate(self.row(),self.dec(),[self.candidate(lat=40.9,lon=9.2)],Context(),Core())
        self.assertEqual(z['assessment'],'GOOGLE_DIVERGE_DA_PVP_DA_VERIFICARE')
    def test_explicit_google_cap_validation_before_any_network(self):
        with self.assertRaises(ValueError):m.load_google_core(0)
        with self.assertRaises(ValueError):m.load_google_core(501)
    def test_no_key_fails_before_loading_google_module(self):
        with patch.dict(os.environ,{'GOOGLE_GEOCODING_API_KEY':''}):
            with self.assertRaisesRegex(RuntimeError,'missing'):
                m.load_google_core(5)
    def test_incomplete_sample_fails_before_google(self):
        with self.assertRaisesRegex(RuntimeError,'Incomplete offline batch'):
            m.preview_new_rows([self.row(),self.row(pvp_id='2')],max_rows=1,max_requests=2,max_cases=2)
    def test_zero_targets_does_not_import_google_or_need_key(self):
        row=self.row(pvp_geo_source='bene_multi:1/2',geocode_status='PENDING')
        with tempfile.TemporaryDirectory() as td:
            (Path(td)/'google_comparison_v3_old.csv').write_text('pvp_id;query;assessment\nold;Via Vecchia;X\n',encoding='utf-8')
            with patch.object(m,'REPORTS_DIR',Path(td)), patch.object(m,'ensure_runtime_dirs'), \
                 patch.object(m.offline,'prepare_context',return_value=(Context(),object(),object())), \
                 patch.object(m.offline,'plan_rows',return_value=[(row,self.dec())]), \
                 patch.object(m,'load_google_core',side_effect=AssertionError('Google unexpectedly loaded')):
                out=m.preview_new_rows([row],max_rows=1,max_requests=1,max_cases=1)
        self.assertEqual(out['google_calls'],0)
        self.assertEqual(out['google_preview']['selected_cases'],0)

    def test_mock_google_sample_one_call_and_no_db_write(self):
        row=self.row(geocode_status='PENDING',pvp_latitudine=None,pvp_longitudine=None)
        class Usage:
            def __init__(self):self.run={'geocoding':{'network_attempts':0,'cache_hits':0}};self.saved=False
            def save(self):self.saved=True
        class Cache:
            def __init__(self,path):self.path=path;self.saved=False
            def save(self):self.saved=True
        usage=Usage()
        calls=[]
        fake=types.SimpleNamespace(CACHE_FILE=Path('cache'),API_USAGE=usage,CacheStore=Cache,
            constrained_address_variants=lambda row:['Via Roma 5'],address_specificity=lambda row:True,
            google_forward_constrained=lambda query,row,cache:(calls.append((query,row['pvp_id'])),
                usage.run['geocoding'].__setitem__('network_attempts',1),[self.candidate()])[-1],
            score_forward_candidate=Core.score_forward_candidate,ApiBudgetExceeded=type('ApiBudgetExceeded',(RuntimeError,),{}))
        with tempfile.TemporaryDirectory() as td:
            (Path(td)/'google_comparison_v3_old.csv').write_text('pvp_id;query;assessment\nold;Via Vecchia;X\n',encoding='utf-8')
            with patch.object(m,'REPORTS_DIR',Path(td)),patch.object(m,'ensure_runtime_dirs'), \
                 patch.object(m.offline,'prepare_context',return_value=(Context(),object(),object())), \
                 patch.object(m.offline,'plan_rows',return_value=[(row,self.dec(precision='COMUNE'))]), \
                 patch.object(m,'load_google_core',return_value=fake):
                out=m.preview_new_rows([row],max_rows=1,max_requests=1,max_cases=1)
                assert Path(out['google_preview']['report_csv']).is_file()
        self.assertEqual(out['google_calls'],1)
        self.assertEqual(len(calls),1)
        self.assertEqual(out['google_preview']['assessment_counts'],{'GOOGLE_CIVICO_ESATTO_DA_VALIDARE':1})
        self.assertTrue(usage.saved)
        self.assertEqual(row['pvp_latitudine'],None)

    def test_policy_civic_exact_uses_v34_parser_even_if_core_reason_omits_civic(self):
        row=self.row(pvp_latitudine=None,pvp_longitudine=None,indirizzo='Via Marconi 158, Tempio Pausania')
        cand=self.candidate(components=[{'long_name':'158','short_name':'158','types':['street_number']}])
        z=m.compare_candidate(row,self.dec(),[cand],Context(),Core())
        self.assertEqual(z['google_civic_relation'],'exact')
        self.assertEqual(z['assessment'],'GOOGLE_CIVICO_ESATTO_DA_VALIDARE')

    def test_policy_civic_variant_not_promoted(self):
        row=self.row(pvp_latitudine=None,pvp_longitudine=None,indirizzo='Via Roma 5/B, Tempio Pausania')
        cand=self.candidate(components=[{'long_name':'5','short_name':'5','types':['street_number']}])
        z=m.compare_candidate(row,self.dec(),[cand],Context(),Core())
        self.assertEqual(z['google_civic_relation'],'variant')
        self.assertEqual(z['assessment'],'INDIZIO_NON_PRECISO_NON_SOSTITUIRE')

    def test_priority_only_can_sample_p3_directly(self):
        core=types.SimpleNamespace(constrained_address_variants=lambda row:[row['indirizzo']])
        planned=[
            (self.row(pvp_id='2',indirizzo='Via Due 2, Tempio Pausania',pvp_latitudine=None,pvp_longitudine=None),self.dec(source='istat_comune_indicativo',precision='COMUNE')),
            (self.row(pvp_id='3',indirizzo='Via Tre 3, Tempio Pausania'),self.dec(precision='PVP_IN_COMUNE')),
        ]
        chosen,_=m.pick_fresh_cases(planned,core,set(),set(),5,priority_only='P3_PVP_NON_CONFERMATO_CON_CIVICO')
        self.assertEqual(len(chosen),1)
        self.assertEqual(chosen[0][0]['pvp_id'],'3')
        self.assertEqual(chosen[0][5],'P3_PVP_NON_CONFERMATO_CON_CIVICO')

if __name__=='__main__':unittest.main(verbosity=2)
