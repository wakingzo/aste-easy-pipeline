import sys, tempfile, types, unittest, importlib.util
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
tmp=Path(tempfile.mkdtemp())
paths=types.ModuleType('aste_paths')
paths.RAW_DIR=tmp/'raw'; paths.REFERENCE_DIR=tmp/'reference'; paths.REPORTS_DIR=tmp/'reports'
def ensure():
    for p in (paths.RAW_DIR,paths.REFERENCE_DIR,paths.REPORTS_DIR): p.mkdir(parents=True,exist_ok=True)
paths.ensure_runtime_dirs=ensure
sys.modules['aste_paths']=paths
ensure()

spec=importlib.util.spec_from_file_location('builder314',ROOT/'src'/'importer'/'build_listings_daily_v3_14.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class FakeSession:
    def __init__(self): self.kw=None
    def request(self, method, url, timeout=0, **kwargs):
        self.kw=(method,url,kwargs)
        class R:
            def raise_for_status(self): pass
            def json(self): return {'body':{'content':[]}}
        return R()

class Builder314Tests(unittest.TestCase):
    def test_api_sort_is_publication_desc_then_city(self):
        s=FakeSession(); m.fetch_search_page(s,0,200)
        params=s.kw[2]['params']
        self.assertIn(('sort','dataPubblicazione,desc'),params)
        self.assertIn(('sort','citta,asc'),params)
        self.assertNotIn(('sort','dataOraVendita,desc'),params)

    def test_strict_dates_requires_every_row_parseable(self):
        good=[{'dataPubblicazione':'2026-10-28T12:00:00'},{'dataPubblicazione':'2026-10-27T12:00:00'}]
        bad=good+[{'dataPubblicazione':None}]
        self.assertEqual(len(m.publication_dates_strict(good)),2)
        self.assertIsNone(m.publication_dates_strict(bad))

    def test_desc_order_and_threshold_stop(self):
        content=[{'dataPubblicazione':'2026-10-25T12:00:00'},{'dataPubblicazione':'2026-10-24T12:00:00'}]
        ds=m.publication_dates_strict(content)
        self.assertTrue(m.dates_are_nonincreasing(ds))
        self.assertTrue(m.page_should_stop_on_old_publication(content,datetime(2026,10,26)))
        self.assertFalse(m.page_should_stop_on_old_publication(content,datetime(2026,10,24)))

if __name__=='__main__': unittest.main()
