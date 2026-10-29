import importlib.util
import json
from unittest import mock
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'src'
HERE = SRC / 'importer' / 'pvp_import_consolidated_preview.py'
# In normal installation aste_paths is already in src; tests can also run in a
# temporary unpacked release when our already-inspected project layout is supplied.
PROJECT_SRC = Path(os.environ.get('ASTE_TEST_PROJECT_SRC', str(SRC)))
if str(PROJECT_SRC) not in sys.path: sys.path.insert(0, str(PROJECT_SRC))

class ImporterPreviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root/'comuni_cap.csv').write_text('comune;provincia;sigla_provincia;regione;cap\nTempio Pausania;Sassari;SS;Sardegna;07029\nOlbia;Sassari;SS;Sardegna;07026\n',encoding='utf-8')
        cls.old_ref = os.environ.get('ASTE_REFERENCE_DIR')
        os.environ['ASTE_REFERENCE_DIR'] = str(root)
        sp=importlib.util.spec_from_file_location('consolidated_test',HERE)
        cls.mod=importlib.util.module_from_spec(sp);sp.loader.exec_module(cls.mod)
    @classmethod
    def tearDownClass(cls):
        if cls.old_ref is None: os.environ.pop('ASTE_REFERENCE_DIR',None)
        else: os.environ['ASTE_REFERENCE_DIR']=cls.old_ref
        cls.tmp.cleanup()

    def fixture(self,pid='4608215',first_coords=False):
        m=self.mod
        a={'descComune':'Tempio Pausania','descProvincia':'Sassari','siglaProvincia':'SS','cap':'07029','via':'Strada Statale 127'}
        b={'descComune':'Olbia','descProvincia':'Sassari','siglaProvincia':'SS','cap':'07026','via':'Via di Olbia','latitudine':'40.9201','longitudine':'9.5001'}
        if first_coords:
            a.update({'latitudine':'40.919','longitudine':'9.15'})
        return {'pvp_id':pid,'link_pvp':'https://pvp.giustizia.it','detail_body':{'beni':[{'indirizzo':a},{'indirizzo':b}]},
                'search_item':{'indirizzo':b,'dataVendita':'2026-10-01'},'raw_text':''}

    def test_same_bene_no_olbia_coords(self):
        geo=self.mod.primary_geo(self.fixture())
        self.assertEqual(geo['comune'],'Tempio Pausania')
        self.assertIn('127',geo['indirizzo'])
        self.assertIsNone(geo['latitudine'])
        self.assertIsNone(geo['longitudine'])
        self.assertEqual(geo['source'],'bene')

    def test_keep_same_bene_valid_pair(self):
        row,source=self.mod.derive_new_consistent(self.fixture(first_coords=True),{})
        self.assertEqual(source,'bene')
        self.assertEqual(row['comune'],'Tempio Pausania')
        self.assertEqual(row['latitudine'],40.919)
        self.assertEqual(row['longitudine'],9.15)
        self.assertNotIn('9.5001',row['maps_link'])

    def test_incomplete_coordinate_pair_fails_closed(self):
        a=self.fixture()['detail_body']['beni'][0]['indirizzo']
        a['latitudine']='40.9'
        self.assertEqual(self.mod.coordinate_pair(a),(None,None))

    def test_avoid_unsafe_merge_upsert_and_deleting_existing_soggetti(self):
        self.assertFalse(hasattr(self.mod,'upsert_listings'))
        self.assertFalse(hasattr(self.mod,'delete_soggetti_for_pvp_ids'))

    def test_existing_pvp_not_derived_or_written(self):
        m=self.mod
        existing=self.fixture('4608215')
        def fail_derive(*a,**k): raise AssertionError('existing ID must never be derived')
        old=m.derive_new_consistent;m.derive_new_consistent=fail_derive
        try:
            p=m.plan_new_only([existing],[],[],{'4608215'},{'ok':True,'by_comune':{}},date(2026,9,19))
            self.assertEqual(p['new_candidates'],0)
            self.assertEqual(len(p['eligible']),0)
        finally: m.derive_new_consistent=old

    def test_date_and_admin_filter_reused(self):
        m=self.mod
        admins={'ok':True,'by_comune':{'tempio pausania':[{'comune':'Tempio Pausania','provincia':'Sassari','sigla_provincia':'SS','regione':'Sardegna'}]}}
        today=date(2026,9,19)
        r={'comune':'Tempio Pausania','provincia':'Sassari','sigla_provincia':'SS','regione':'Sardegna','data_asta':today.strftime('%d/%m/%Y')}
        self.assertEqual(m.classify_new_candidate(r,today,admins)[0],'ELIGIBLE_ACTIVE')
        r['data_asta']=(today-timedelta(days=15)).strftime('%d/%m/%Y')
        self.assertEqual(m.classify_new_candidate(r,today,admins)[0],'SKIP_EXPIRED_BEYOND_RETENTION')
        r['data_asta']=(today+timedelta(days=731)).strftime('%d/%m/%Y')
        self.assertEqual(m.classify_new_candidate(r,today,admins)[0],'SKIP_DATE_FAR_FUTURE_REVIEW')

    def test_conflicting_duplicate_input_rejected(self):
        a=self.fixture();b=self.fixture();b['raw_text']='conflicting'
        with self.assertRaisesRegex(ValueError,'dati diversi'):
            self.mod.plan_new_only([a,b],[],[],set(),{'ok':True,'by_comune':{}},date(2026,9,19))

    def test_full_preview_is_get_only_and_counts_new(self):
        m=self.mod
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)
            raw=d/'listings_api_raw.jsonl'; att=d/'allegati_links.jsonl'; sog=d/'listing_soggetti.jsonl'; ist=d/'Elenco-comuni-italiani.xlsx'
            existing=self.fixture('4608215')
            new=self.fixture('9001234',first_coords=True)
            new['search_item']['dataVendita']=(date.today()+timedelta(days=20)).strftime('%Y-%m-%d')
            for obj in (existing,new):
                with raw.open('a',encoding='utf-8') as f: f.write(json.dumps(obj)+'\n')
            att.write_text(json.dumps({'pvp_id':'9001234','filename':'avviso.pdf','resource_url':'https://example.test/avviso.pdf'})+'\n',encoding='utf-8')
            sog.write_text(json.dumps({'pvp_id':'9001234','nome':'Mario','cognome':'Rossi'})+'\n',encoding='utf-8')
            ist.write_text('fixture',encoding='utf-8')
            admins={'ok':True,'by_comune':{'tempio pausania':[{'comune':'Tempio Pausania','provincia':'Sassari','sigla_provincia':'SS','regione':'Sardegna'}]}}
            with mock.patch.multiple(m,IN_RAW=raw,IN_ALLEGATI=att,IN_SOGGETTI=sog,ISTAT_XLSX=ist,REPORTS_DIR=d), \
                 mock.patch.object(m,'load_project_env',return_value={}), \
                 mock.patch.object(m,'fetch_existing_ids',return_value={'4608215'}) as get_only, \
                 mock.patch.object(m,'load_istat_admin',return_value=admins), \
                 mock.patch.dict(os.environ,{'SUPABASE_URL':'https://fake.invalid','SUPABASE_SERVICE_ROLE':'test_key'}):
                self.assertEqual(m.consolidated_main([]),0)
                get_only.assert_called_once()
            outputs=list(d.glob('pvp_single_importer_preview_v1_*.json'))
            self.assertEqual(len(outputs),1)
            result=json.loads(outputs[0].read_text(encoding='utf-8'))
            self.assertEqual((result['existing'],result['eligible_new']), (1,1))
            self.assertEqual((result['new_soggetti'],result['new_allegati']),(1,1))
            self.assertEqual(result['eligible_with_pvp_coords'],1)
            self.assertEqual(result['db_patches'],0)
            self.assertEqual(result['google_calls'],0)

    def test_apply_rejected_before_database_access(self):
        with self.assertRaises(SystemExit) as ctx:
            self.mod.consolidated_main(['--apply'])
        self.assertEqual(ctx.exception.code,2)

if __name__=='__main__': unittest.main(verbosity=2)
