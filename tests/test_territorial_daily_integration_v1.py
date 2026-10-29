#!/usr/bin/env python3
"""Offline deterministic tests. No Google, no network, no real Supabase or data files."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'src' / 'geocoding'))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


t = load(ROOT / 'src/geocoding/territorial_touched_daily_v1.py', 'test_territorial_touched_daily_v1')
p = load(ROOT / 'src/pipeline/daily_pipeline.py', 'test_pipeline_with_territorial_v1')
s = load(ROOT / 'src/geocoding/geocode_territorial_offline_v1.py', 'test_source_territorial_v1')


class Context:
    def resolve_admin(self, row):
        return {'status': 'RESOLVED', 'canonical_code': '113025', 'canonical_comune': 'Tempio Pausania'}

    def test(self, row, lat=None, lon=None, admin_meta=None):
        if lat is None:
            lat = row['latitudine']
        if lon is None:
            lon = row['longitudine']
        if lat is None or lon is None:
            return {'position': 'NO_COORDS'}
        return {'position': 'INSIDE' if float(lon) < 9.3 else 'OUTSIDE'}

    def representative_point(self, row, admin_meta=None):
        return {'ok': True, 'lat': 40.90244947414183, 'lon': 9.102690514989092}


ORIGINAL = {'pvp_id': '4608215', 'indirizzo': 'Strada Statale 127 Settentrionale Sarda, Tempio Pausania',
            'comune': 'Tempio Pausania', 'sigla_provincia': 'SS', 'cap': '07029',
            'latitudine': 40.9185787, 'longitudine': 9.4917711, 'maps_link': None,
            'geocode_source': None, 'geocode_location_type': 'PVP_SUSPECT',
            'geocode_query_used': None, 'geocode_formatted_address': None}


def manifest(path, ids=('4608215',)):
    path.write_text(''.join(json.dumps({'pvp_id': pid, 'fresh_inserted': 'YES',
                       'handoff_source': 'pvp_import_incremental_v12_3'}) + '\n' for pid in ids), encoding='utf-8')


class ManifestAndFilterTests(unittest.TestCase):
    def test_manifest_accepts_only_fresh_inserts(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / 'x.jsonl'
            manifest(f)
            self.assertEqual(t.read_manifest(f, 5000), ['4608215'])
            manifest(f, ('4608215', '4608215'))
            with self.assertRaisesRegex(RuntimeError, 'duplicati'):
                t.read_manifest(f, 5000)
            manifest(f, ('4608215', '4608216'))
            with self.assertRaisesRegex(RuntimeError, 'oltre'):
                t.read_manifest(f, 1)
            f.write_text('{"pvp_id":"4608215"}\n', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'handoff'):
                t.read_manifest(f, 5000)

    def test_empty_manifest_and_forged_authorization_fail_safe(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / 'empty.jsonl'; manifest(f, ())
            self.assertEqual(t.read_manifest(f, 5000), [])
            with self.assertRaises(SystemExit):
                t.main(['--manifest', str(f), '--apply'])
            with self.assertRaises(SystemExit):
                t.main(['--manifest', str(f), '--apply', '--pipeline-authorized'])

    def test_numeric_guard_and_special_address(self):
        row = dict(ORIGINAL, indirizzo="Via d'Annunzio, 5")
        q = parse_qs(urlsplit(t.guarded_url('https://example.invalid', '4608215', row, select=True)).query)
        self.assertEqual(q['indirizzo'], ["eq.Via d'Annunzio, 5"])
        self.assertEqual(q['latitudine'][0].split('.')[0], 'gte')
        self.assertEqual(q['latitudine'][1].split('.')[0], 'lte')
        self.assertEqual(q['longitudine'][0].split('.')[0], 'gte')
        self.assertEqual(q['longitudine'][1].split('.')[0], 'lte')
        self.assertEqual(q['maps_link'], ['is.null'])
        self.assertEqual(q['pvp_id'], ['eq.4608215'])

    def test_numeric_precision_roundtrip_and_change_detection(self):
        self.assertTrue(t.unchanged(dict(ORIGINAL, latitudine=40.9185787001), ORIGINAL))
        self.assertFalse(t.unchanged(dict(ORIGINAL, longitudine=9.40), ORIGINAL))

    def test_default_local_fallback_inside_municipality(self):
        r = s.evaluate(ORIGINAL, Context(), None, None, {})
        self.assertEqual(r['decision'], 'PROPOSE_ISTAT_COMUNE_INDICATIVE')
        self.assertEqual(t.allowed(r, ORIGINAL, Context(), s, {}), 'READY_COMUNE_INDICATIVE')
        pay = t.payload_for(r)
        self.assertEqual(pay['geocode_location_type'], 'ISTAT_COMUNE_FALLBACK')
        self.assertNotIn('comune', pay)
        self.assertNotIn('indirizzo', pay)

    def test_keep_inside_precise_and_other_municipality(self):
        row = dict(ORIGINAL, longitudine=9.10)
        self.assertEqual(s.evaluate(row, Context(), None, None, {})['decision'], 'KEEP_PVP_IN_COMUNE')
        proposal = s.evaluate(ORIGINAL, Context(), None, None, {})
        precise = dict(ORIGINAL, geocode_location_type='ROOFTOP')
        proposal['old_geocode_location_type'] = 'ROOFTOP'
        self.assertEqual(t.allowed(proposal, precise, Context(), s, {}), 'HOLD_PRECISE_SOURCE')
        with patch.object(s, 'alternative_comune_for_pin', return_value='Nuraminis'):
            self.assertEqual(s.evaluate(ORIGINAL, Context(), None, None, {})['decision'],
                             'KEEP_ALTERNATIVE_COMUNE_REVIEW')

    def test_street_needs_strict_evidence(self):
        proposal = s.evaluate(ORIGINAL, Context(), None, None, {})
        proposal.update(decision='PROPOSE_ANNCSU_STRADA',
                        anncsu_odonimo='SS 127', anncsu_street_score='0.98',
                        anncsu_strict_street_similarity='0.40', reason='simple_no_civic')
        self.assertEqual(t.allowed(proposal, ORIGINAL, Context(), s, {}), 'HOLD_WEAK_ROAD')
        proposal['anncsu_strict_street_similarity'] = '0.95'
        self.assertEqual(t.allowed(proposal, ORIGINAL, Context(), s, {}), 'READY_STRADA_INDICATIVE')

    def test_civic_not_applied_even_if_proposed(self):
        proposal = s.evaluate(ORIGINAL, Context(), None, None, {})
        proposal['decision'] = 'PROPOSE_ANNCSU_CIVICO'
        self.assertEqual(t.allowed(proposal, ORIGINAL, Context(), s, {}), 'HOLD_NOT_ALLOWED_DECISION')


class EndToEndOfflineTests(unittest.TestCase):
    def test_preview_then_apply_single_id_with_get_verified(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_file = root / 'ids.jsonl'
            manifest(manifest_file)
            ref = root / 'ref'
            ref.mkdir()
            (ref / 'Elenco-comuni-italiani.xlsx').write_bytes(b'fixture')
            (ref / 'Limiti01012026.zip').write_bytes(b'fixture')
            db = {'4608215': dict(ORIGINAL)}
            writes = []

            def fake_fetch(_base, _key, ids):
                return {pid: dict(db[pid]) for pid in ids}

            def fake_patch(_base, _key, pid, before, pay):
                self.assertEqual(db[pid]['latitudine'], before['latitudine'])
                writes.append(pid)
                db[pid].update(pay)
                return True

            boundary = SimpleNamespace(ISTAT_ZIP=ref / 'Limiti01012026.zip', get_context=lambda: Context())
            with (patch.object(t, 'REFERENCE_DIR', ref), patch.object(t, 'REPORTS_DIR', root),
                  patch.object(t, 'load_project_env', return_value={}),
                  patch.object(t, 'fetch_rows', side_effect=fake_fetch),
                  patch.object(t, 'guarded_get', return_value=True),
                  patch.object(t, 'guarded_patch', side_effect=fake_patch),
                  patch.object(s, 'mod', return_value=boundary),
                  patch.object(t, 'load_territorial', return_value=s),
                  patch.dict(os.environ, {'SUPABASE_URL': 'https://example.invalid',
                                          'SUPABASE_SERVICE_ROLE': 'not-a-real-secret',
                                          'ASTE_PIPELINE_TERRITORIAL_AUTHORIZED': '1'})):
                self.assertEqual(t.main(['--manifest', str(manifest_file)]), 0)
                self.assertEqual(writes, [])
                self.assertEqual(t.main(['--manifest', str(manifest_file), '--apply',
                                         '--pipeline-authorized']), 0)
                self.assertEqual(writes, ['4608215'])
                self.assertLess(db['4608215']['longitudine'], 9.3)
                summaries = [json.loads(f.read_text(encoding='utf-8')) for f in root.glob('territorial_touched_daily_v1_apply_*.json')]
                self.assertEqual(len(summaries), 1)
                self.assertEqual(summaries[0]['apply_counts'], {'PATCHED_GET_VERIFIED': 1})
                self.assertEqual(summaries[0]['google_calls'], 0)
                # A repeat preview must not propose another patch.
                self.assertEqual(t.main(['--manifest', str(manifest_file)]), 0)
                self.assertEqual(writes, ['4608215'])

    def test_does_not_patch_precise_or_inside(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / 'manifest.jsonl'; manifest(f, ('4608215', '4608216'))
            ref = root / 'ref'; ref.mkdir()
            (ref / 'Elenco-comuni-italiani.xlsx').write_bytes(b'fixture')
            (ref / 'Limiti01012026.zip').write_bytes(b'fixture')
            precise = dict(ORIGINAL, geocode_location_type='ROOFTOP')
            inside = dict(ORIGINAL, pvp_id='4608216', longitudine=9.1)
            db = {'4608215': precise, '4608216': inside}
            boundary = SimpleNamespace(ISTAT_ZIP=ref / 'Limiti01012026.zip', get_context=lambda: Context())
            with (patch.object(t, 'REFERENCE_DIR', ref), patch.object(t, 'REPORTS_DIR', root),
                  patch.object(t, 'load_project_env', return_value={}),
                  patch.object(t, 'fetch_rows', side_effect=lambda b,k,ids: {pid: dict(db[pid]) for pid in ids}),
                  patch.object(t, 'guarded_get', return_value=True),
                  patch.object(t, 'guarded_patch', side_effect=AssertionError('no patch allowed')),
                  patch.object(s, 'mod', return_value=boundary),
                  patch.object(t, 'load_territorial', return_value=s),
                  patch.dict(os.environ, {'SUPABASE_URL': 'https://example.invalid',
                                          'SUPABASE_SERVICE_ROLE': 'test',
                                          'ASTE_PIPELINE_TERRITORIAL_AUTHORIZED': '1'})):
                self.assertEqual(t.main(['--manifest', str(f), '--apply', '--pipeline-authorized']), 0)
                summaries = [json.loads(x.read_text()) for x in root.glob('territorial_touched_daily_v1_apply_*.json')]
                self.assertEqual(summaries[0]['ready'], 0)
                self.assertEqual(summaries[0]['db_patches'], 0)


class PipelineTests(unittest.TestCase):
    def capture(self, argv):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)/'handoff.jsonl'
            steps = []
            def fake_run(title, script, *, args=None, env=None):
                steps.append((str(script), list(args or []), dict(env or {})))
                if script == p.IMPORTER and args and '--apply' in args:
                    manifest(target, ())
            with (patch.object(p, 'ensure_runtime_dirs'), patch.object(p, 'load_project_env'),
                  patch.object(p, 'handoff_path', return_value=target),
                  patch.object(p, 'run_step', side_effect=fake_run),
                  patch.dict(os.environ, {'SUPABASE_URL':'https://invalid.local','SUPABASE_SERVICE_ROLE':'test'})):
                code = p.main(argv)
                return code, steps

    def test_default_apply_uses_only_offline_geocode(self):
        code, steps = self.capture(['--apply', '--skip-build'])
        self.assertEqual(code, 0)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0][0], str(p.IMPORTER))
        self.assertEqual(steps[1][0], str(p.TERRITORIAL))
        self.assertEqual(steps[1][2]['ASTE_PIPELINE_TERRITORIAL_AUTHORIZED'], '1')
        self.assertNotIn(str(p.GEOCODER), [a[0] for a in steps])

    def test_legacy_skip_geocode_skips_both(self):
        _, steps = self.capture(['--apply', '--skip-build', '--skip-geocode'])
        self.assertEqual([step[0] for step in steps], [str(p.IMPORTER)])

    def test_google_requires_explicit_flag(self):
        _, steps = self.capture(['--apply', '--skip-build', '--with-google'])
        self.assertEqual([step[0] for step in steps], [str(p.IMPORTER), str(p.TERRITORIAL), str(p.GEOCODER)])

    def test_dry_run_never_starts_geocoders(self):
        _, steps = self.capture(['--skip-build'])
        self.assertEqual([step[0] for step in steps], [str(p.IMPORTER)])
        self.assertEqual(steps[0][1], [])

    def test_google_and_skip_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as e:
            p.main(['--apply', '--with-google', '--skip-geocode'])
        self.assertEqual(e.exception.code, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
