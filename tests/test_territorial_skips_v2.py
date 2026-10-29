"""Offline unit tests for the isolated 29-skip recovery. No network/DB/Google."""
import csv
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / 'src' / 'geocoding'
SRC_ZIP = ROOT / 'tests' / '_original_for_test.zip'  # not included; tests mock original via copy in temp tree


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        (base / 'src' / 'geocoding').mkdir(parents=True)
        fake = base / 'src' / 'aste_paths.py'
        fake.write_text('from pathlib import Path\nREPORTS_DIR=Path("/tmp/safe-test")\nREFERENCE_DIR=Path("/tmp/safe-test")\n')
        (base / 'src' / 'project_env.py').write_text('def load_project_env(**kwargs): raise RuntimeError("No network in tests")\n')
        # Import the exact original repair bundled in this test only, not a possibly-modified installed version.
        archive = PKG / 'repair_territorial_safe_v1.py'
        assert archive.is_file(), 'Place original repair_territorial_safe_v1.py alongside v2 on Windows'
        (base / 'src' / 'geocoding' / 'repair_territorial_safe_v1.py').write_bytes(archive.read_bytes())
        (base / 'src' / 'geocoding' / 'repair_territorial_skips_v2.py').write_bytes((PKG / 'repair_territorial_skips_v2.py').read_bytes())
        sys.path.insert(0, str(base / 'src' / 'geocoding'))
        sys.path.insert(0, str(base / 'src'))
        spec = importlib.util.spec_from_file_location('territorial_skips_v2_under_test', base / 'src' / 'geocoding' / 'repair_territorial_skips_v2.py')
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_29_exact_fingerprints_embedded(self):
        self.assertEqual(self.m.EXPECTED_SKIPS, 29)
        self.assertEqual(len(self.m.INPUTS), 3)
        self.assertTrue(all(len(v) == 64 for v in self.m.INPUTS.values()))

    def test_exact_filters_preserved(self):
        row = {k: None for k in self.m.repair.FIELDS}
        row.update(pvp_id='4608215', indirizzo='Via d\'Abisso, 56', comune='Tempio Pausania',
                   latitudine=40.9185787, longitudine=9.4917711)
        q = parse_qs(urlsplit(self.m.guarded_url('https://example.invalid', '4608215', row, True)).query)
        self.assertEqual(q['pvp_id'], ['eq.4608215'])
        self.assertEqual(q['indirizzo'], ["eq.Via d'Abisso, 56"])
        self.assertEqual(q['geocode_source'], ['is.null'])
        self.assertEqual(q['select'], ['pvp_id'])
        for k in ('latitudine', 'longitudine'):
            self.assertEqual(len(q[k]), 2)
            self.assertTrue(q[k][0].startswith('gte.'))
            self.assertTrue(q[k][1].startswith('lte.'))
            self.assertFalse(any(s.startswith('eq.') for s in q[k]))

    def test_bounded_interval_sub_meter_and_contains_old(self):
        row = {k: None for k in self.m.repair.FIELDS}
        row.update(pvp_id='1', latitudine=45.6151384, longitudine=9.5894021)
        q = parse_qs(urlsplit(self.m.guarded_url('https://example.invalid', '1', row)).query)
        for field in ('latitudine', 'longitudine'):
            a, b = q[field]
            x = row[field]
            self.assertTrue(float(a[4:]) < x < float(b[4:]))
            self.assertLess(float(b[4:]) - float(a[4:]), .000011)

    def test_changes_rejected_and_float_roundoff_allowed(self):
        before = {k: None for k in self.m.repair.FIELDS}
        before.update(pvp_id='1', indirizzo='Strada A', latitudine=44.5, longitudine=9.7)
        self.assertTrue(self.m.same_original(dict(before, latitudine=44.5000000001), before))
        self.assertFalse(self.m.same_original(dict(before, latitudine=44.50001), before))
        self.assertFalse(self.m.same_original(dict(before, indirizzo='Strada B'), before))
        self.assertFalse(self.m.same_original(dict(before, geocode_source='pvp_verified'), before))

    def test_fail_closed_non_numeric_coordinates(self):
        row = {k: None for k in self.m.repair.FIELDS}
        row.update(pvp_id='1', latitudine=None, longitudine=8.5)
        with self.assertRaisesRegex(RuntimeError, 'Coordinate originali'):
            self.m.guarded_pairs('1', row)
        with self.assertRaisesRegex(RuntimeError, 'Guard ID'):
            self.m.guarded_pairs('2', dict(row, latitudine=44.0))

    def test_read_only_preview_no_patch_invocation(self):
        source = (PKG / 'repair_territorial_skips_v2.py').read_text()
        self.assertIn('if not args.apply:', source)
        self.assertIn("write_reports(report, summary, 'get_only')", source)
        self.assertIn("'TERRITORIAL_SKIPS_APPLY_29'", source)
        self.assertIn("summary['db_patches'] += 1", source)
        self.assertNotIn('google_geocode_core', source)


if __name__ == '__main__':
    unittest.main(verbosity=2)
