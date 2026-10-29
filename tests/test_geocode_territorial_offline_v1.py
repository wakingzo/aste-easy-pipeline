#!/usr/bin/env python3
"""Deterministic offline tests: no Google keys, DB or ISTAT shapefile needed."""
import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'src' / 'geocoding' / 'geocode_territorial_offline_v1.py'
spec = importlib.util.spec_from_file_location('territorial_offline_test', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeCtx:
    def resolve_admin(self, row):
        name = row.get('comune', '')
        d = {'Tempio Pausania': '113025', 'Nuraminis': '092048',
             'Muravera': '111042', 'Gorla Maggiore': '012079', 'Gorla Minore': '012080'}
        return {'status': 'RESOLVED', 'canonical_code': d[name]} if name in d else {'status': 'UNRESOLVED'}

    def test(self, row, lat=None, lon=None, admin_meta=None):
        if lat is None: lat = row.get('latitudine')
        if lon is None: lon = row.get('longitudine')
        if lat is None or lon is None:
            return {'position': 'NO_COORDS'}
        code = (admin_meta or self.resolve_admin(row)).get('canonical_code')
        in_tempio = 40.8 <= float(lat) <= 41.05 and 9.0 <= float(lon) <= 9.3
        in_nuraminis = abs(float(lat)-39.43) < .15 and abs(float(lon)-9.02) < .15
        in_muravera = abs(float(lat)-39.42) < .15 and abs(float(lon)-9.57) < .15
        in_gorla_mag = abs(float(lat)-45.66) < .03 and abs(float(lon)-8.89) < .03
        in_gorla_min = abs(float(lat)-45.64) < .03 and abs(float(lon)-8.87) < .03
        hits = {'113025':in_tempio, '092048':in_nuraminis, '111042':in_muravera,
                '012079':in_gorla_mag, '012080':in_gorla_min}
        return {'position':'INSIDE' if hits.get(code,False) else 'OUTSIDE'}

    def representative_point(self, row, admin_meta=None):
        if row.get('comune') == 'Tempio Pausania':
            return {'ok': True, 'lat': 40.92, 'lon': 9.14}
        return {'ok': False}


class FakeLocal:
    @staticmethod
    def direct_civic_for_missing(ev): return bool(ev.get('exact'))
    @staticmethod
    def street_for_missing(row, ev): return bool(ev.get('street')), 'strict_street'
    @staticmethod
    def locality_for_missing(row, ev, allow_fallback=False):
        assert allow_fallback is False
        return bool(ev.get('locality')), 'exact_locality'


class OfflineTerritorialTests(unittest.TestCase):
    def setUp(self):
        self.ctx = FakeCtx()
        self.local = FakeLocal()
        self.ss = {'pvp_id':'4608215', 'comune':'Tempio Pausania', 'sigla_provincia':'SS',
                   'indirizzo':'Strada Statale 127 Settentrionale Sarda',
                   'latitudine':40.9185787, 'longitudine':9.4917711}

    def test_ss127_generic_municipality_fallback_without_external_lookup(self):
        result = m.evaluate(self.ss, self.ctx, self.local, {}, {})
        self.assertEqual(result['decision'], 'PROPOSE_ISTAT_COMUNE_INDICATIVE')
        self.assertEqual((result['proposed_lat'],result['proposed_lon']), (40.92,9.14))
        self.assertEqual(result['precision'],'COMUNE')
        self.assertEqual(self.ctx.test(self.ss, result['proposed_lat'],result['proposed_lon'])['position'], 'INSIDE')

    def test_existing_inside_pin_is_untouched(self):
        r = dict(self.ss, latitudine=40.92, longitudine=9.14)
        result = m.evaluate(r, self.ctx,self.local,{}, {})
        self.assertEqual(result['decision'],'KEEP_PVP_IN_COMUNE')
        self.assertEqual(result['proposed_lat'],'')

    def test_civic_only_when_inside_target_municipality(self):
        ev = {'exact':True,'anncsu_lat':40.945,'anncsu_lon':9.18}
        res = m.evaluate(self.ss,self.ctx,self.local,ev,{})
        self.assertEqual(res['decision'],'PROPOSE_ANNCSU_CIVICO')
        ev['anncsu_lon']=9.55
        res=m.evaluate(self.ss,self.ctx,self.local,ev,{})
        self.assertEqual(res['decision'],'PROPOSE_ISTAT_COMUNE_INDICATIVE')

    def test_no_incorrect_street_civic(self):
        ev = {'street':True,'street_representative':{'lat':40.94,'lon':9.15}}
        res=m.evaluate(self.ss,self.ctx,self.local,ev,{})
        self.assertEqual(res['decision'],'PROPOSE_ANNCSU_STRADA')
        self.assertEqual(res['precision'],'STRADA')

    def test_multimunicipality_pvp_protection(self):
        r={'pvp_id':'4629847','comune':'Muravera','indirizzo':'Via Nazionale, 233, 09024 Nuraminis CA, Muravera Sud Sardegna',
           'latitudine':39.43,'longitudine':9.02}
        result=m.evaluate(r,self.ctx,self.local,{}, {})
        self.assertEqual(result['decision'],'KEEP_ALTERNATIVE_COMUNE_REVIEW')
        self.assertEqual(result['alternative_comune'],'Nuraminis')
        self.assertEqual(result['proposed_lat'],'')

    def test_missing_requires_opt_in(self):
        r=dict(self.ss,latitudine=None,longitudine=None)
        self.assertEqual(m.evaluate(r,self.ctx,self.local,{}, {})['decision'],'NOT_TARGETED_MISSING')
        self.assertEqual(m.evaluate(r,self.ctx,self.local,{}, {},include_missing=True)['decision'],
                         'PROPOSE_ISTAT_COMUNE_INDICATIVE')

    def test_unresolved_municipality_fail_closed(self):
        r=dict(self.ss, comune='Nonexistent')
        self.assertEqual(m.evaluate(r,self.ctx,self.local,{}, {})['decision'],'REVIEW_ADMIN')

    def test_no_safe_interior_point_fail_closed(self):
        r=dict(self.ss,comune='Muravera',latitudine=40.9185787,longitudine=9.4917711)
        self.assertEqual(m.evaluate(r,self.ctx,self.local,{}, {})['decision'],'REVIEW_NO_SAFE_REP')

    def test_alt_ignored_when_pin_not_inside_alt(self):
        r = dict(self.ss, indirizzo='Strada Statale 127, 09024 Nuraminis CA',latitudine=40.9185787,longitudine=9.4917711)
        self.assertEqual(m.evaluate(r,self.ctx,self.local,{}, {})['decision'],'PROPOSE_ISTAT_COMUNE_INDICATIVE')

if __name__=='__main__': unittest.main(verbosity=2)
