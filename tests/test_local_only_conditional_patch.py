"""Offline unit tests; never opens the network or reads live Supabase."""
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlencode

root = Path(__file__).resolve().parents[1]
paths = types.ModuleType('aste_paths')
paths.PROJECT_ROOT = root
paths.REFERENCE_DIR = root / 'data' / 'reference'
paths.REPORTS_DIR = root / 'runtime' / 'reports'
sys.modules['aste_paths'] = paths
module_path = root / 'src' / 'geocoding' / 'local_only_repair_touched_v1.py'
spec = importlib.util.spec_from_file_location('aste_localonly_v1_1_test', module_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class FakeResponse:
    def __init__(self, value):
        self.value = value
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False
    def read(self):
        return json.dumps(self.value).encode('utf-8')


class ConditionalPatchTests(unittest.TestCase):
    def setUp(self):
        self.before = {
            'pvp_id': '4621048', 'indirizzo': 'Via del Sole, 12 (scala A)',
            'comune': 'Siracusa', 'sigla_provincia': 'SR', 'cap': '96100',
            'latitudine': None, 'longitudine': None, 'geocode_source': None,
            'geocode_location_type': '', 'geocode_query_used': None,
            'geocode_formatted_address': None,
        }
        self.payload = {'latitudine': 37.1019629, 'longitudine': 15.2905145,
                        'geocode_source': 'anncsu_missing_civico',
                        'geocode_location_type': 'ANNCSU_METHOD_4',
                        'geocode_formatted_address': 'VIA LUIGI CASSIA, 47, Siracusa',
                        'geocode_query_used': 'local_only_v1:test',
                        'maps_link': 'https://www.google.com/maps?q=37.1019629,15.2905145'}

    def test_ambiguous_null_empty_special_chars_are_conditionally_guarded(self):
        captured = {}
        def fake_urlopen(request, timeout=None):
            captured['request'] = request
            return FakeResponse([dict(self.before, **self.payload)])
        original = mod.urlopen
        mod.urlopen = fake_urlopen
        try:
            result = mod.db_patch_one('https://example.invalid', 'fake_key', '4621048', self.payload, self.before)
        finally:
            mod.urlopen = original
        self.assertEqual(result['pvp_id'], '4621048')
        req = captured['request']
        self.assertEqual(req.get_method(), 'PATCH')
        qs = parse_qs(urlsplit(req.full_url).query, keep_blank_values=True)
        self.assertEqual(qs['pvp_id'], ['eq.4621048'])
        self.assertEqual(qs['latitudine'], ['is.null'])
        self.assertEqual(qs['longitudine'], ['is.null'])
        self.assertEqual(qs['geocode_source'], ['is.null'])
        self.assertEqual(qs['geocode_location_type'], ['eq.'])
        self.assertEqual(qs['indirizzo'], ['eq.Via del Sole, 12 (scala A)'])
        self.assertEqual(qs['cap'], ['eq.96100'])
        self.assertEqual(mod.db_filter_value(None), 'is.null')
        self.assertEqual(mod.db_filter_value(''), 'eq.')
        self.assertEqual(json.loads(req.data), self.payload)

    def test_direct_eq_address_matches_without_literal_quotes(self):
        address = 'Via Santa Maria in Val d\'Abisso n. 56, Piobbico Pesaro e Urbino'
        encoded = urlencode({'indirizzo': mod.db_filter_value(address)})
        decoded = parse_qs(encoded)['indirizzo'][0]
        self.assertEqual(decoded, 'eq.' + address)
        # A direct eq filter must match the original value, not a string with quotes.
        self.assertEqual(decoded.removeprefix('eq.'), address)
        for special in ['Via É, 1', 'Via \"A\", 1', 'Piazza A&B (interno 4)', 'Via Roma, 2']:
            encoded = urlencode({'indirizzo': mod.db_filter_value(special)})
            self.assertEqual(parse_qs(encoded)['indirizzo'][0], 'eq.' + special)

    def test_race_returns_no_patch(self):
        original = mod.urlopen
        mod.urlopen = lambda request, timeout=None: FakeResponse([])
        try:
            result = mod.db_patch_one('https://example.invalid', 'fake_key', '4621048', self.payload, self.before)
        finally:
            mod.urlopen = original
        self.assertIsNone(result)

    def test_wrong_echo_stops(self):
        original = mod.urlopen
        mod.urlopen = lambda request, timeout=None: FakeResponse([dict(self.before, pvp_id='4621048', latitudine=1)])
        try:
            with self.assertRaisesRegex(RuntimeError, 'coordinate'):
                mod.db_patch_one('https://example.invalid', 'fake_key', '4621048', self.payload, self.before)
        finally:
            mod.urlopen = original


if __name__ == '__main__':
    unittest.main(verbosity=2)
