#!/usr/bin/env python3
"""Deterministic unit tests. No actual Supabase/Google/PVP requests."""
import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from datetime import date, timedelta
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/'src'
# Local development only: installed tools already has these modules.
extra=os.environ.get('ASTE_TEST_EXISTING_SRC')
if extra: sys.path.insert(0,extra)
sys.path.insert(0,str(SRC))
sys.path.insert(0,str(SRC/'importer'))
sys.path.insert(0,str(SRC/'geocoding'))

class Setup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.prior=os.environ.get('ASTE_REFERENCE_DIR')
        os.environ['ASTE_REFERENCE_DIR']=cls.tmp.name
        (Path(cls.tmp.name)/'comuni_cap.csv').write_text('comune;provincia;sigla_provincia;regione;cap\nTempio Pausania;Sassari;SS;Sardegna;07029\nOlbia;Sassari;SS;Sardegna;07026\n',encoding='utf-8')
        import pvp_import_atomic_v1 as importer
        import geocode_pending_v1 as geocoder
        cls.imp,cls.geo=importer,geocoder

    @classmethod
    def tearDownClass(cls):
        if cls.prior is None: os.environ.pop('ASTE_REFERENCE_DIR',None)
        else: os.environ['ASTE_REFERENCE_DIR']=cls.prior
        cls.tmp.cleanup()

    def raw(self, multi=False, first_coord=False):
        b={'descComune':'Tempio Pausania','descProvincia':'Sassari','siglaProvincia':'SS',
           'cap':'07029','via':'SS 127'}
        if first_coord: b.update(latitudine='40.901',longitudine='9.101')
        beni=[{'indirizzo':b}]
        if multi: beni.append({'indirizzo':{'descComune':'Olbia','latitudine':'40.93','longitudine':'9.5'}})
        return {'pvp_id':'4608215','search_item':{'indirizzo':beni[-1]['indirizzo'],
                'dataVendita':(date.today()+timedelta(days=30)).isoformat()},
                'detail_body':{'beni':beni},'raw_text':''}

    def pending(self, **changes):
        row={'pvp_id':'4608215','comune':'Tempio Pausania','sigla_provincia':'SS',
             'indirizzo':'SS 127, Tempio Pausania','pvp_latitudine':None,'pvp_longitudine':None,
             'pvp_geo_source':'bene','latitudine':None,'longitudine':None,
             'geocode_status':'PENDING','geocode_input_hash':'a'*64}
        row.update(changes);return row

    def fake_ctx(self, status='RESOLVED',position='INSIDE',rep=True):
        class Ctx:
            def resolve_admin(self,row): return {'status':status,'canonical_code':'090070'}
            def test(self,row,lat=None,lon=None,admin_meta=None):
                if lat is None or lon is None: return {'position':'NO_COORDS'}
                if lat==40.9 and lon==9.1: return {'position':'INSIDE'}
                if lat==40.901 and lon==9.101: return {'position':position}
                return {'position':position}
            def representative_point(self,row,admin_meta=None):
                return {'ok':rep,'lat':40.9,'lon':9.1}
        return Ctx()

    def fake_terr(self,choice=None,alt=''):
        class Territorial:
            def local_option(self,row,ev,local,ctx,meta): return choice
            def alternative_comune_for_pin(self,row,ctx,lat,lon): return alt
        return Territorial()

    def test_primary_geo_never_splices_second_bene(self):
        raw=self.raw(multi=True)
        x=self.imp.prepare_listing(self.imp.previous.derive_new_consistent(raw,{})[0],raw)
        self.assertIsNone(x['pvp_latitudine']);self.assertIsNone(x['pvp_longitudine'])
        self.assertEqual(x['pvp_geo_source'],'bene_multi:1/2')
        self.assertNotIn('latitudine',x);self.assertNotIn('maps_link',x)

    def test_prepare_listing_raw_original_kept_and_no_publication(self):
        raw=self.raw(first_coord=True)
        p=self.imp.prepare_listing(self.imp.previous.derive_new_consistent(raw,{})[0],raw)
        self.assertEqual((p['pvp_latitudine'],p['pvp_longitudine']),(40.901,9.101))
        self.assertEqual(p['geocode_status'],'PENDING')
        self.assertEqual(len(p['geocode_input_hash']),64)
        self.assertNotIn('longitudine',p)

    def test_child_url_collision_fails_not_silently_deduped(self):
        aa=[{'pvp_id':'1','filename':'A','resource_url':'https://a'},
            {'pvp_id':'1','filename':'B','resource_url':'https://a'}]
        with self.assertRaisesRegex(ValueError,'Conflicting attachment'):
            self.imp.prepare_children('1',[],aa)

    def test_subject_key_collision_fails_not_silently_deduped(self):
        ss=[{'pvp_id':'1','role_code':'X','display_name':'Mario','telefono':'123'},
            {'pvp_id':'1','role_code':'X','display_name':'Mario','telefono':'456'}]
        with self.assertRaisesRegex(ValueError,'Conflicting subject'):
            self.imp.prepare_children('1',ss,[])

    def test_child_same_id_duplicate_deduped(self):
        aa=[{'pvp_id':'1','filename':'A','resource_url':'https://a'}]*2
        ss=[{'pvp_id':'1','role_code':'X','display_name':'Mario'}]*2
        s,a=self.imp.prepare_children('1',ss,aa)
        self.assertEqual((len(s),len(a)),(1,1))

    def test_google_not_called_when_pvp_in_municipality(self):
        row=self.pending(pvp_latitudine=40.901,pvp_longitudine=9.101)
        pay=self.geo.decision(row,self.fake_ctx(),self.fake_terr())
        self.assertEqual(pay['geocode_status'],'DONE')
        self.assertEqual(pay['geocode_precision'],'PVP_IN_COMUNE')
        self.assertEqual(pay['geocode_google_outcome'],'NOT_REQUESTED')

    def test_local_civic_takes_priority_over_commune(self):
        choice={'precision':'CIVICO','lat':40.9,'lon':9.1,'reason':'unique'}
        pay=self.geo.decision(self.pending(),self.fake_ctx(),self.fake_terr(choice),object(),{})
        self.assertEqual(pay['geocode_source'],'anncsu_civico')
        self.assertEqual(pay['geocode_precision'],'CIVICO')

    def test_safe_municipal_point_explicitly_indicative(self):
        pay=self.geo.decision(self.pending(),self.fake_ctx(),self.fake_terr())
        self.assertEqual(pay['geocode_status'],'DONE')
        self.assertEqual(pay['geocode_precision'],'COMUNE')
        self.assertIn('INDICATIVO',pay['geocode_decision_reason'])

    def test_unresolved_administration_never_invents_pin(self):
        pay=self.geo.decision(self.pending(),self.fake_ctx(status='UNRESOLVED'),self.fake_terr())
        self.assertEqual(pay['geocode_status'],'REVIEW')
        self.assertIsNone(pay['latitudine'])

    def test_multibene_pvp_outside_must_not_repoint(self):
        r=self.pending(pvp_latitudine=40.901,pvp_longitudine=9.101,pvp_geo_source='bene_multi')
        pay=self.geo.decision(r,self.fake_ctx(position='OUTSIDE'),self.fake_terr())
        self.assertEqual(pay['geocode_status'],'REVIEW')
        self.assertIsNone(pay['latitudine'])

    def test_alt_commune_retains_original_marked_review(self):
        r=self.pending(pvp_latitudine=40.901,pvp_longitudine=9.101)
        pay=self.geo.decision(r,self.fake_ctx(position='OUTSIDE'),self.fake_terr(alt='Olbia'))
        self.assertEqual(pay['geocode_status'],'REVIEW')
        self.assertEqual(pay['latitudine'],40.901)
        self.assertIn('Olbia',pay['geocode_decision_reason'])

    def test_pending_coordinates_cannot_be_overwritten(self):
        with self.assertRaisesRegex(RuntimeError,'published coordinates'):
            self.geo.decision(self.pending(latitudine=40.9),self.fake_ctx(),self.fake_terr())

    def test_preview_new_rows_simulates_pending_without_db(self):
        r=self.pending(pvp_latitudine=40.901,pvp_longitudine=9.101)
        with mock.patch.object(self.geo,'prepare_context',return_value=(self.fake_ctx(),self.fake_terr(),None)), \
             mock.patch.object(self.geo,'_json_request',side_effect=AssertionError('DB request')):
            result=self.geo.preview_new_rows([r],max_rows=2,no_anncsu=True)
        self.assertEqual(result['reviewed_without_db_writes'],1)
        self.assertEqual(result['decision_counts'],{'DONE:PVP_IN_COMUNE':1})
        self.assertEqual(result['google_calls'],0)

    def test_patch_guard_contains_pending_hash_and_null_final(self):
        r=self.pending()
        with mock.patch.object(self.geo,'_json_request',side_effect=[[],]) as req:
            self.assertFalse(self.geo.guarded_patch('https://example.test','fake',r,{'geocode_status':'DONE'}))
        params=req.call_args.kwargs['params']
        self.assertIn(('geocode_status','eq.PENDING'),params)
        self.assertIn(('geocode_input_hash','eq.'+'a'*64),params)
        self.assertIn(('latitudine','is.null'),params)
        self.assertIn(('longitudine','is.null'),params)

    def test_preview_no_rpc_called(self):
        fake={'records':[(self.pending(),[],[])], 'incoming_unique':1,'existing':0,
              'new_candidates':1,'subjects':0,'attachments':0,'decisions':{},
              'primary_sources':{},'raw_sha256':'f'*64}
        fake['records'][0][0]['pvp_latitudine']=None
        with mock.patch.object(self.imp,'build_plan',return_value=fake), \
             mock.patch.object(self.imp,'rpc_insert',side_effect=AssertionError('rpc called')), \
             mock.patch.object(self.imp,'REPORTS_DIR',new=Path(self.tmp.name)):
            result=self.imp.run(apply=False)
        self.assertEqual(result['inserted'],0)

    def test_import_apply_requires_preview_sha_before_rpc(self):
        fake={'records':[(self.pending(),[],[])], 'incoming_unique':1,'existing':0,
              'new_candidates':1,'subjects':0,'attachments':0,'decisions':{},
              'primary_sources':{},'raw_sha256':'f'*64}
        with mock.patch.object(self.imp,'build_plan',return_value=fake), \
             mock.patch.object(self.imp,'rpc_insert',side_effect=AssertionError('rpc called')):
            with self.assertRaisesRegex(RuntimeError,'checksum changed'):
                self.imp.run(apply=True,expected_new=1,expected_sha='a'*64)

    def test_rpc_payload_contains_all_children_and_no_merge_upsert(self):
        row={'pvp_id':'1'}
        ss=[{'pvp_id':'1','display_name':'Mario'}]
        aa=[{'pvp_id':'1','filename':'avviso.pdf','resource_url':'https://a'}]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*x):pass
            def read(self): return b'{"inserted":true,"pvp_id":"1","soggetti":1,"allegati":1}'
        with mock.patch.object(self.imp,'urlopen',return_value=Response()) as open_:
            x=self.imp.rpc_insert('https://example.test','fake',row,ss,aa)
        request=open_.call_args.args[0]
        payload=json.loads(request.data.decode())
        self.assertEqual(payload['p_soggetti'],ss)
        self.assertEqual(payload['p_allegati'],aa)
        self.assertIn('/rest/v1/rpc/aste_insert_new_listing_v1',request.full_url)
        self.assertTrue(x['inserted'])

    def test_rpc_sql_defines_one_transaction_and_restricts_public(self):
        sql=(ROOT/'sql/01_atomic_import_rpc.sql').read_text(encoding='utf-8')
        self.assertIn('ON CONFLICT (pvp_id) DO NOTHING RETURNING listing_id',sql)
        self.assertIn('REVOKE ALL ON FUNCTION',sql)
        self.assertIn('TO service_role',sql)
        self.assertNotIn('resolution=merge-duplicates',sql)
        self.assertIn('RAISE EXCEPTION',sql)

if __name__=='__main__':unittest.main(verbosity=2)
