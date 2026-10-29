"""Offline regression tests: no network or Supabase state required."""
import importlib.util
import json
import sys
import unittest
import types
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

FILE=Path(__file__).resolve().parents[1]/'src'/'geocoding'/'repair_2_pvp_outside_v1.py'
spec=importlib.util.spec_from_file_location('repair_2_pvp_outside_v1_under_test',FILE)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class FakeBoundary:
    def test(self,row,*,lat,lon):
        name=row['comune']
        # synthetic but deterministic polygon outcome, not real ISTAT data
        if name=='Rodi Garganico':
            return {'position':'INSIDE' if (lat,lon)==(41.924866,15.886721) else 'OUTSIDE',
                    'canonical_code':'071043'}
        if name=='Frosinone':
            return {'position':'INSIDE' if (lat,lon)==(41.641538,13.3579665) else 'OUTSIDE',
                    'canonical_code':'060038'}
        if name=='Muravera': return {'position':'OUTSIDE','canonical_code':'118029'}
        if name=='Nuraminis': return {'position':'INSIDE','canonical_code':'092047'}
        if name=='Gorla Minore': return {'position':'OUTSIDE','canonical_code':'012079'}
        if name=='Gorla Maggiore': return {'position':'INSIDE','canonical_code':'012078'}
        raise AssertionError(name)

class TestSafety(unittest.TestCase):
    def test_fixed_allowlist(self):
        self.assertEqual(set(mod.TARGETS),{'4625019','4630701'})
        self.assertEqual(set(mod.ALT_MUNICIPALITIES),{'4629847','4625939'})
        self.assertTrue(set(mod.TARGETS).isdisjoint(mod.ALT_MUNICIPALITIES))
    def test_input_sha_and_expected_cases(self):
        plan,audit,istat=mod.source_data()
        self.assertEqual(plan['4630701']['anncsu_civic'],'5')
        self.assertEqual(istat['4625019']['istat_candidato_anncsu'],'INSIDE')
        self.assertEqual(audit['4625939']['comune_db_oggi'],'Gorla Minore')
    def test_preflight_two_target_rows(self):
        plan,audit,istat=mod.source_data()
        boundary=FakeBoundary()
        for pid in mod.TARGETS:
            p,a=plan[pid],audit[pid]
            now={
                'pvp_id':pid,'indirizzo':p['source_indirizzo'],
                'comune':p['source_comune'],'sigla_provincia':p['source_sigla_provincia'],
                'cap':p['source_cap'],'latitudine':float(a['lat_db_oggi']),
                'longitudine':float(a['lon_db_oggi']),
                'geocode_source':None,'geocode_location_type':None,
                'geocode_query_used':None,'geocode_formatted_address':None,
                'maps_link':'https://www.google.com/maps?q=old'
            }
            out=mod.make_record(pid,plan,audit,istat,boundary,now)
            self.assertEqual(out['esito'],'PRONTO_SOLO_DOPO_CONFERMA_MANUALE')
            self.assertEqual(out['istat_anncsu_comune_db'],'INSIDE')
            altered=dict(now,latitudine=45.)
            self.assertEqual(mod.make_record(pid,plan,audit,istat,boundary,altered)['esito'],
                             'BLOCCATO_STATO_DB_CAMBIATO')
            altered=dict(now,geocode_source='google_corrected_pvp')
            self.assertEqual(mod.make_record(pid,plan,audit,istat,boundary,altered)['esito'],
                             'BLOCCATO_STATO_DB_CAMBIATO')
    def test_admin_only_no_patch_and_alt_containment(self):
        plan,audit,istat=mod.source_data()
        for pid in mod.ALT_MUNICIPALITIES:
            p,a=plan[pid],audit[pid]
            now={'pvp_id':pid,'indirizzo':p['source_indirizzo'],
                 'comune':p['source_comune'],'sigla_provincia':p['source_sigla_provincia'],
                 'cap':p['source_cap'],'latitudine':float(a['lat_db_oggi']),
                 'longitudine':float(a['lon_db_oggi']),'geocode_source':None,
                 'geocode_location_type':None,'geocode_query_used':None,'geocode_formatted_address':None}
            r=mod.make_record(pid,plan,audit,istat,FakeBoundary(),now)
            self.assertEqual(r['istat_pin_comune_alternativo'],'INSIDE')
            self.assertEqual(r['proposta'],'NESSUN_PATCH_COMUNE')
            with self.assertRaises(RuntimeError): mod.patch_one('https://example.invalid','dummy',pid,now,{})
    def test_filter_exactly_uses_before_state_and_no_literal_quotes(self):
        before={f:None for f in mod.FIELDS}
        before.update(pvp_id='4625019',indirizzo="Via dell'Abisso 26, Rodi Garganico",
                      comune='Rodi Garganico',maps_link='https://www.google.com/maps?q=41,15')
        class Resp:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b'[]'
        urls=[]
        def fake(req,timeout):
            urls.append(req.full_url)
            self.assertEqual(req.get_method(),'PATCH')
            self.assertEqual(json.loads(req.data)['latitudine'],41.924866)
            return Resp()
        with patch.object(mod,'urlopen',side_effect=fake):
            got=mod.patch_one('https://example.supabase.co','never-print-key','4625019',before,
                              {'latitudine':41.924866})
        self.assertIsNone(got)
        qs=parse_qs(urlsplit(urls[0]).query)
        self.assertEqual(qs['indirizzo'],["eq.Via dell'Abisso 26, Rodi Garganico"])
        self.assertEqual(qs['latitudine'],['is.null'])
        self.assertEqual(qs['maps_link'],['eq.https://www.google.com/maps?q=41,15'])
        self.assertNotIn('"',qs['indirizzo'][0])
    def test_full_apply_simulation_touches_only_two_allowlisted_ids(self):
        plan,audit,territorial=mod.source_data()
        snapshots={}
        for pid in mod.ALLOWED_IDS:
            p,a=plan[pid],audit[pid]
            snapshots[pid]={
                'pvp_id':pid,'indirizzo':p['source_indirizzo'],
                'comune':p['source_comune'],'sigla_provincia':p['source_sigla_provincia'],
                'cap':p['source_cap'],'latitudine':float(a['lat_db_oggi']),
                'longitudine':float(a['lon_db_oggi']),
                'geocode_source':None,'geocode_location_type':None,
                'geocode_query_used':None,'geocode_formatted_address':None,
                'maps_link':'https://www.google.com/maps?q=old'
            }
        writes=[]
        summaries=[]
        def fake_get(base,key,pid): return dict(snapshots[pid])
        def fake_patch(base,key,pid,before,payload):
            self.assertIn(pid,mod.TARGETS)
            self.assertEqual(before,snapshots[pid])
            writes.append(pid)
            snapshots[pid].update(payload)
            return dict(snapshots[pid])
        fake_env=types.ModuleType('project_env')
        fake_env.load_project_env=lambda required:None
        fake_args=['repair_2_pvp_outside_v1.py','--apply']
        with tempfile.TemporaryDirectory() as tempdir, \
             patch.object(mod,'REPORTS_DIR',Path(tempdir)), \
             patch.object(mod,'source_data',return_value=(plan,audit,territorial)), \
             patch.object(mod,'get_one',side_effect=fake_get), \
             patch.object(mod,'patch_one',side_effect=fake_patch), \
             patch.object(mod,'boundary_context',return_value=FakeBoundary()), \
             patch.object(mod,'write_reports',side_effect=lambda rows,summary,mode:summaries.append((rows,dict(summary),mode))), \
             patch.object(mod,'input',return_value='CORREGGI_2_PIN_ANNCSU'), \
             patch.dict(sys.modules,{'project_env':fake_env}), \
             patch.dict(mod.os.environ,{'SUPABASE_URL':'https://example.invalid','SUPABASE_SERVICE_ROLE':'testing-fake-secret'}), \
             patch.object(sys,'argv',fake_args):
            mod.main()
        self.assertEqual(set(writes),set(mod.TARGETS))
        self.assertEqual(len(writes),2)
        self.assertEqual(summaries[-1][1]['supabase_patches'],2)
        self.assertEqual(summaries[-1][1]['apply_counts']['PATCHED_GET_VERIFIED'],2)
        self.assertTrue(all(snapshots[pid]['geocode_source'] is None for pid in mod.ALT_MUNICIPALITIES))

    def test_payload_preserves_admin_and_labels_representative_civic(self):
        plan,audit,_=mod.source_data()
        p=mod.payload(plan['4630701'],mod.TARGETS['4630701'],audit['4630701'])
        self.assertEqual(p['geocode_source'],'anncsu_corrected_boundary_civico')
        self.assertEqual(p['geocode_location_type'],'ANNCSU_METHOD_3')
        self.assertIn('PIAZZA RISORGIMENTO 5',p['geocode_formatted_address'])
        self.assertNotIn('comune',p)

if __name__=='__main__': unittest.main(verbosity=2)
