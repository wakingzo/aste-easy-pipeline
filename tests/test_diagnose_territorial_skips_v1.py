"""Offline unit tests, no credentials, no network."""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / 'src' / 'geocoding' / 'diagnose_territorial_skips_v1.py'
# The actual repair module exists on the user's PC. Inject a tiny safe stub for
# testing this independent GET diagnostic without requiring project credentials.
repair_stub = types.ModuleType('repair_territorial_safe_v1')
repair_stub.FIELDS = ('pvp_id', 'indirizzo', 'comune', 'sigla_provincia', 'cap',
                      'latitudine', 'longitudine', 'maps_link', 'geocode_source',
                      'geocode_location_type', 'geocode_query_used',
                      'geocode_formatted_address')
repair_stub.same_num = lambda a, b: (a == b)
paths_stub = types.ModuleType('aste_paths')
paths_stub.REPORTS_DIR = Path(tempfile.gettempdir())
env_stub = types.ModuleType('project_env')
env_stub.load_project_env = lambda required: None
with patch.dict(sys.modules, {
    'repair_territorial_safe_v1': repair_stub,
    'aste_paths': paths_stub,
    'project_env': env_stub
}):
    spec = importlib.util.spec_from_file_location('diagnostic_under_test', SCRIPT)
    diag = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diag)

class Tests(unittest.TestCase):
    def test_single_failure(self):
        calls = []
        def matches(fields):
            calls.append(fields)
            return 'indirizzo' not in fields
        singles, coupled = diag.diagnostic_partition(('latitudine', 'indirizzo', 'maps_link'), matches)
        self.assertEqual(singles, ['indirizzo'])
        self.assertEqual(coupled, [])
        self.assertLessEqual(len(calls), 8)

    def test_two_failures(self):
        singles, coupled = diag.diagnostic_partition(('latitudine', 'indirizzo', 'maps_link'),
            lambda fields: not any(f in fields for f in ('latitudine', 'maps_link')))
        self.assertEqual(set(singles), {'latitudine', 'maps_link'})
        self.assertEqual(coupled, [])

    def test_interaction_only(self):
        singles, coupled = diag.diagnostic_partition(('indirizzo', 'maps_link'),
            lambda fields: len(fields) != 2)
        self.assertEqual(singles, [])
        self.assertEqual(coupled, ['indirizzo + maps_link'])

    def test_no_issue(self):
        self.assertEqual(diag.diagnostic_partition(('indirizzo',), lambda fields: True), ([], []))

    def test_snapshot_changes_not_ignored(self):
        old = {'pvp_id': '1', 'indirizzo': 'A', 'latitudine': 1.2, 'longitudine': None}
        now = {'pvp_id': '1', 'indirizzo': 'B', 'latitudine': 1.2, 'longitudine': None}
        self.assertEqual(diag.original_equal(now, old), ['indirizzo'])

    def test_get_only_probe(self):
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'[{"pvp_id":"123"}]'
        with patch.object(diag, 'urlopen', return_value=Reply()) as fetch:
            tally = __import__('collections').Counter()
            self.assertTrue(diag.probe_get('https://example.invalid', 'test-only-key', '123',
                                          {'indirizzo': "Via d'Abruzzo, 5"}, ('indirizzo',), tally))
            request = fetch.call_args[0][0]
            self.assertEqual(request.get_method(), 'GET')
            self.assertIn('indirizzo=eq.Via+d%27Abruzzo%2C+5', request.full_url)
            self.assertEqual(tally['get_requests'], 1)

if __name__ == '__main__':
    unittest.main()
