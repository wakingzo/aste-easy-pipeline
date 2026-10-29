import io, json, sys, types, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'/'importer'))

prev=types.ModuleType('pvp_import_consolidated_preview')
prev.LISTINGS_FIELDS=['pvp_id','indirizzo','latitudine','longitudine','maps_link']
sys.modules['pvp_import_consolidated_preview']=prev
paths=types.ModuleType('aste_paths'); paths.REPORTS_DIR=Path('/tmp'); paths.ensure_runtime_dirs=lambda:None; sys.modules['aste_paths']=paths
proj=types.ModuleType('project_env'); proj.load_project_env=lambda required=():None; sys.modules['project_env']=proj
import pvp_import_atomic_v1 as m

class Resp:
    def __init__(self,obj): self.obj=obj
    def __enter__(self): return self
    def __exit__(self,*a): return False
    def read(self): return json.dumps(self.obj).encode()

class RetryTests(unittest.TestCase):
    def setUp(self):
        self.old_urlopen=m.urlopen; self.old_sleep=m.time.sleep; m.time.sleep=lambda x:None
    def tearDown(self):
        m.urlopen=self.old_urlopen; m.time.sleep=self.old_sleep

    def test_transport_retry_then_inserted(self):
        calls={'n':0}
        def fake(req,timeout=120):
            calls['n']+=1
            if calls['n']==1: raise ConnectionResetError('reset')
            return Resp({'pvp_id':'1','inserted':True,'soggetti':1,'allegati':1})
        m.urlopen=fake
        z=m.rpc_insert('https://x','k',{'pvp_id':'1'},[{'pvp_id':'1'}],[{'pvp_id':'1'}])
        self.assertTrue(z['inserted']); self.assertEqual(z['network_retries'],1)
        self.assertFalse(z['recovered_after_transport'])

    def test_lost_commit_retry_existing_is_safe_recovery(self):
        calls={'n':0}
        def fake(req,timeout=120):
            calls['n']+=1
            if calls['n']==1: raise ConnectionResetError('response lost')
            return Resp({'pvp_id':'1','inserted':False,'soggetti':0,'allegati':0})
        m.urlopen=fake
        z=m.rpc_insert('https://x','k',{'pvp_id':'1'},[],[])
        self.assertFalse(z['inserted']); self.assertEqual(z['network_retries'],1)
        self.assertTrue(z['recovered_after_transport'])

if __name__=='__main__': unittest.main()
