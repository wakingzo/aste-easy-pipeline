#!/usr/bin/env python3
"""Offline tests: no imports of actual geocoders, no Supabase/network/Google."""
import importlib.util
import io
import json
import pathlib
import sys
import types
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

SRC=pathlib.Path(__file__).resolve().parents[1]/'src'/'geocoding'/'repair_territorial_safe_v1.py'
# These two filesystem helpers are not needed for pure unit tests.
sys.modules.setdefault('aste_paths',types.SimpleNamespace(REPORTS_DIR=pathlib.Path('/nonexistent'),REFERENCE_DIR=pathlib.Path('/nonexistent')))
sys.modules.setdefault('project_env',types.SimpleNamespace(load_project_env=lambda **kw:None))
spec=importlib.util.spec_from_file_location('territorial_safe_test',SRC)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class FakeCtx:
    def resolve_admin(self,row):
        return {'status':'RESOLVED','canonical_code':'113025'}
    def test(self,row,lat=None,lon=None,admin_meta=None):
        lat = m.val(row.get('latitudine')) if lat is None else lat
        lon = m.val(row.get('longitudine')) if lon is None else lon
        return {'position':'OUTSIDE' if abs(lon-9.4917711)<1e-6 else 'INSIDE'}
    def representative_point(self,row,admin_meta=None):
        return {'ok':True,'lat':40.90244947414183,'lon':9.102690514989092}

class Tests(unittest.TestCase):
    def setUp(self):
        self.p={'pvp_id':'4608215','decision':'PROPOSE_ISTAT_COMUNE_INDICATIVE',
                'precision':'COMUNE','reason':'no reliable local civic/street/locality',
                'comune':'Tempio Pausania','sigla_provincia':'SS',
                'indirizzo':'Strada Statale 127 Settentrionale Sarda, 07029 Tempio Pausania SS',
                'istat_code':'113025','original_lat':'40.9185787','original_lon':'9.4917711',
                'proposed_lat':'40.90244947414183','proposed_lon':'9.102690514989092',
                'old_geocode_source':'','old_geocode_location_type':'PVP_SUSPECT'}
        self.now={'pvp_id':self.p['pvp_id'],'indirizzo':self.p['indirizzo'],
                  'comune':self.p['comune'],'sigla_provincia':'SS','cap':'07029',
                  'latitudine':40.9185787,'longitudine':9.4917711,
                  'geocode_source':None,'geocode_location_type':'PVP_SUSPECT',
                  'maps_link':None,'geocode_query_used':None,'geocode_formatted_address':None}
        self.ctx=FakeCtx()
    def evaluate(self,alt=lambda *a:''):
        return m.proposal_check(self.p,self.now,self.ctx,alt,{})
    def test_ss127_generic_centroid_ready(self):
        self.assertEqual(self.evaluate(),'READY_COMUNE_INDICATIVE')
        payload=m.patch_payload(self.p)
        self.assertEqual(payload['geocode_location_type'],'ISTAT_COMUNE_FALLBACK')
        self.assertNotIn('comune',payload)
    def test_source_changed_holds(self):
        self.now['indirizzo']='something else'
        self.assertEqual(self.evaluate(),'HOLD_SOURCE_CHANGED')
    def test_coordinates_changed_holds(self):
        self.now['longitudine']=9.4
        self.assertEqual(self.evaluate(),'HOLD_COORDS_CHANGED')
    def test_alternative_municipality_holds(self):
        self.assertEqual(self.evaluate(lambda *a:'Nuraminis'),'HOLD_ALTERNATIVE_COMUNE')
    def test_proposed_outside_holds(self):
        self.p['proposed_lon']='9.4917711'
        self.assertEqual(self.evaluate(),'HOLD_PROPOSED_OUTSIDE')
    def test_representative_changed_holds(self):
        self.p['proposed_lon']='9.1027000'
        self.assertEqual(self.evaluate(),'HOLD_REPRESENTATIVE_CHANGED')
    def test_keep_precision_and_ambiguous_civic(self):
        self.p['old_geocode_location_type']='ROOFTOP'
        self.assertEqual(self.evaluate(),'HOLD_PRECISE_SOURCE')
        self.p['old_geocode_location_type']='PVP_SUSPECT'
        self.p['decision']='PROPOSE_ANNCSU_CIVICO'
        self.assertEqual(self.evaluate(),'HOLD_CIVICO_SEMANTICS')
    def test_road_evidence_requires_strict(self):
        self.p['decision']='PROPOSE_ANNCSU_STRADA';self.p['reason']='simple_no_civic'
        self.p['anncsu_odonimo']='STRADA STATALE 127';self.p['anncsu_street_score']='1.0'
        self.p['anncsu_strict_street_similarity']='0.35'
        self.assertEqual(self.evaluate(),'HOLD_STREET_EVIDENCE')
        self.p['anncsu_strict_street_similarity']='1.0'
        self.assertEqual(self.evaluate(),'READY_STRADA_INDICATIVE')
    def test_conditional_filter_raw_eq_escaped_and_null(self):
        self.now['indirizzo']="Via d'Annunzio, 7"
        seen=[]
        class Resp:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self):return json.dumps([{'pvp_id':'4608215'}]).encode('utf8')
        def fake_urlopen(req, timeout=None):
            seen.append(req)
            return Resp()
        with patch.object(m,'urlopen',fake_urlopen):
            self.assertTrue(m.conditional_patch('https://supabase.invalid','secret','4608215',self.now,
                  {'latitudine':40.9,'longitudine':9.1}))
        q=parse_qs(urlsplit(seen[0].full_url).query)
        self.assertEqual(q['indirizzo'][0],"eq.Via d'Annunzio, 7")
        self.assertEqual(q['maps_link'][0],'is.null')
        self.assertEqual(seen[0].get_method(),'PATCH')
    def test_exact_report_candidates_count(self):
        # In user's project, this is available; no DB needed. Skip in isolated package test.
        if not (m.REPORTS_DIR/m.PLAN_NAME).is_file():return
        plan=m.plan_rows()
        c=__import__('collections').Counter(m.preapproved_from_plan(p) for p in plan.values())
        self.assertEqual(c['CANDIDATE'],316)

if __name__=='__main__':unittest.main(verbosity=2)
