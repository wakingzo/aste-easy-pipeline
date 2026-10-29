import json, sys, tempfile, types, unittest
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
tmp = Path(tempfile.mkdtemp())
paths = types.ModuleType('aste_paths')
paths.RAW_DIR = tmp/'raw'; paths.REFERENCE_DIR = tmp/'reference'; paths.REPORTS_DIR = tmp/'reports'
paths.ensure_runtime_dirs = lambda: [p.mkdir(parents=True, exist_ok=True) for p in (paths.RAW_DIR, paths.REFERENCE_DIR, paths.REPORTS_DIR)]
sys.modules['aste_paths'] = paths
paths.ensure_runtime_dirs()
(paths.REFERENCE_DIR/'comuni_cap.csv').write_text('comune;regione;provincia;sigla_provincia;cap\nRoma;Lazio;Roma;RM;00100\n', encoding='utf-8')
proj = types.ModuleType('project_env'); proj.load_project_env=lambda required=():None; sys.modules['project_env']=proj
sys.path.insert(0, str(ROOT/'src'/'importer'))
import pvp_import_consolidated_preview as previous
import pvp_import_atomic_v1 as m


def raw(pid, d, comune='Roma'):
    ds = d.strftime('%Y-%m-%d') if d else None
    addr = {'descComune':comune,'descProvincia':'Roma','siglaProvincia':'RM','descRegione':'Lazio','cap':'00100','via':'Via Test','numeroCivico':'1'}
    return {
        'pvp_id':str(pid), 'link_pvp':'https://example/'+str(pid), 'raw_text':'',
        'search_item':{'dataVendita':ds,'indirizzo':addr,'descLotto':'lotto'},
        'detail_body':{'dataVendita':ds,'beni':[{'indirizzo':addr}]}
    }

class Resp:
    def __init__(self, obj): self.obj=obj
    def __enter__(self): return self
    def __exit__(self,*a): return False
    def read(self): return json.dumps(self.obj).encode()

class PlannerTests(unittest.TestCase):
    def test_fast_core_matches_legacy_on_lifecycle_and_admin(self):
        today = date(2026,10,28)
        rows = [
            raw(1,today),                              # existing -> ignored
            raw(2,today-timedelta(days=30)),          # expired -> skip
            raw(3,today+timedelta(days=800)),         # far future -> skip
            raw(4,today),                              # eligible active
            raw(5,today, comune='Comune Fantasma'),   # admin review
            raw(6,today-timedelta(days=7)),           # eligible retention
        ]
        existing={'1'}
        attachments=[{'pvp_id':'4','filename':'a.pdf','resource_url':'u4'}, {'pvp_id':'6','filename':'b.pdf','resource_url':'u6'}]
        istat={'ok':True,'reason':'ok','by_comune':{
            previous._norm_admin_text('Roma'):[{'comune':'Roma','sigla_provincia':'RM','provincia':'Roma','regione':'Lazio'}]
        }}
        legacy=previous.plan_new_only(rows,attachments,[],existing,istat,today)
        rb={str(x['pvp_id']):x for x in rows}
        fast=m._build_plan_fast_core(rb,existing,istat,today,attachments,subjects_path=None)
        self.assertEqual(dict(fast['decisions']),dict(legacy['decisions']))
        self.assertEqual(dict(fast['sources']),dict(legacy['sources']))
        self.assertEqual(fast['eligible'],legacy['eligible'])
        self.assertEqual(fast['attachments'],legacy['attachments'])
        self.assertEqual(fast['date_survivor_ids'],['4','5','6'])

    def test_offset_fetch_reads_whole_table_not_raw_batches(self):
        old=m.urlopen
        calls=[]
        try:
            def fake(req, timeout=120):
                q=parse_qs(urlparse(req.full_url).query)
                calls.append(q)
                off=int((q.get('offset') or ['0'])[0])
                if off==0: return Resp([{'pvp_id':'10'},{'pvp_id':'2'}])
                if off==2: return Resp([{'pvp_id':'7'}])
                raise AssertionError(off)
            m.urlopen=fake
            ids,pages=m._fetch_all_existing_listing_ids('https://x','k',page_size=2)
            self.assertEqual(ids,{'10','2','7'})
            self.assertEqual(pages,2)
            self.assertEqual((calls[0].get('order') or [''])[0],'pvp_id.asc')
            self.assertEqual((calls[1].get('offset') or [''])[0],'2')
        finally:
            m.urlopen=old

    def test_raw_loader_sha_and_duplicate_same_payload(self):
        p=tmp/'raw_test.jsonl'
        obj={'pvp_id':'123','x':1}
        raw_bytes=(json.dumps(obj)+'\n'+json.dumps(obj)+'\n').encode()
        p.write_bytes(raw_bytes)
        rows,sha,dups=m._load_raw_unique_with_sha(p)
        import hashlib
        self.assertEqual(sha,hashlib.sha256(raw_bytes).hexdigest())
        self.assertEqual(list(rows),['123'])
        self.assertEqual(dups,1)

if __name__=='__main__': unittest.main()
