import json, sys, tempfile, types, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
tmp=Path(tempfile.mkdtemp())
paths=types.ModuleType('aste_paths')
paths.IMPORTER_DIR=tmp/'importer'; paths.RAW_DIR=tmp/'raw'; paths.REPORTS_DIR=tmp/'reports'; paths.ANNCSU_DIR=tmp/'anncsu'
paths.ensure_runtime_dirs=lambda:[p.mkdir(parents=True,exist_ok=True) for p in (paths.IMPORTER_DIR,paths.RAW_DIR,paths.REPORTS_DIR,paths.ANNCSU_DIR)]
sys.modules['aste_paths']=paths
proj=types.ModuleType('project_env'); proj.load_project_env=lambda required=():None; sys.modules['project_env']=proj
imp=types.ModuleType('pvp_import_atomic_v1'); imp.assert_rpc_available=lambda:True; sys.modules['pvp_import_atomic_v1']=imp
geo=types.ModuleType('geocode_pending_v2'); geo.prepare_context=lambda no_anncsu=False:(None,None,None); sys.modules['geocode_pending_v2']=geo
prod=types.ModuleType('google_production_v3_12'); sys.modules['google_production_v3_12']=prod
aud=types.ModuleType('production_audit_v3_13'); sys.modules['production_audit_v3_13']=aud
sys.path.insert(0,str(ROOT/'src'/'pipeline'))
import daily_pipeline_v3_16 as m
class DailyTests(unittest.TestCase):
    def setUp(self):
        paths.ensure_runtime_dirs()
        for p in paths.REPORTS_DIR.glob('daily_manifest_v3_16_*.json'): p.unlink()
    def test_fmt(self): self.assertEqual(m._fmt(125),'2m 5s')
    def test_staging_roundtrip(self):
        p=paths.REPORTS_DIR/'s.jsonl'; plan={'records':[({'pvp_id':'1'},[],[])]}
        m._write_staging(p,plan); self.assertEqual(m._load_staging(p),plan['records'])
    def test_latest_unfinished_v316(self):
        a=paths.REPORTS_DIR/'daily_manifest_v3_16_1.json'; a.write_text(json.dumps({'status':'COMPLETED'}))
        b=paths.REPORTS_DIR/'daily_manifest_v3_16_2.json'; b.write_text(json.dumps({'status':'GEOCODE_INTERRUPTED'}))
        p,o=m._latest_unfinished_manifest(); self.assertEqual(p.name,b.name)
    def test_default_caps(self):
        old=m.run_daily; seen={}
        try:
            m.run_daily=lambda args:(seen.update(vars(args)) or 0)
            self.assertEqual(m.main(['--run']),0); self.assertEqual(seen['daily_max_new'],1500)
        finally: m.run_daily=old
if __name__=='__main__': unittest.main()
