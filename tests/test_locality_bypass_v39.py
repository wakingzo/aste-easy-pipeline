"""Offline locality-only map fallback tests. No ANNCSU files are read."""
import sys, types, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
paths=sys.modules.get("aste_paths") or types.ModuleType("aste_paths")
paths.REFERENCE_DIR=Path(".")
paths.REPORTS_DIR=Path(".")
paths.ensure_runtime_dirs=lambda: None
sys.modules["aste_paths"]=paths
proj=types.ModuleType("project_env")
proj.load_project_env=lambda **kwargs: None
sys.modules["project_env"]=proj
sys.path.insert(0,str(ROOT/"src"/"geocoding"))
import anncsu_runtime_consolidated_v2 as ann
import geocode_territorial_offline_v1 as terr

class Ctx:
    def test(self,row,lat=None,lon=None,admin_meta=None):
        return {"position":"INSIDE"}

class LocalityBypassTests(unittest.TestCase):
    def ev(self, **kw):
        d={
            "runtime_locality_match_mode":"EXACT",
            "runtime_locality_source_specificity":"LOCALITY_ONLY",
            "runtime_locality_guard_reason":"explicit_exact",
            "runtime_locality_lat":"44.88",
            "runtime_locality_lon":"8.68",
            "runtime_locality_accesses":"25",
            "runtime_locality_p90_m":"1500",
        }
        d.update(kw); return d

    def test_broad_locality_is_accepted_only_as_indicative(self):
        ok,why=ann.locality_for_indicative_map({"indirizzo":"Frazione X"},self.ev())
        self.assertTrue(ok); self.assertEqual(why,"locality_indicative_exact_anncsu")

    def test_specific_road_never_uses_broad_locality_fallback(self):
        ok,_=ann.locality_for_indicative_map({"indirizzo":"Via Roma"},self.ev(runtime_locality_source_specificity="HAS_SPECIFIC"))
        self.assertFalse(ok)

    def test_diffuse_over_5km_is_rejected(self):
        ok,why=ann.locality_for_indicative_map({},self.ev(runtime_locality_p90_m="6000"))
        self.assertFalse(ok); self.assertEqual(why,"locality_too_diffuse_gt5km")

    def test_territorial_option_marks_indicative_source(self):
        class Local:
            direct_civic_for_missing=lambda self,ev: False
            street_for_missing=lambda self,row,ev: (False,"no")
            locality_for_missing=lambda self,row,ev,allow_fallback=False: (False,"not_compact")
            locality_for_indicative_map=lambda self,row,ev: (True,"locality_indicative_exact_anncsu")
        out=terr.local_option({"indirizzo":"Frazione X"},self.ev(),Local(),Ctx(),{})
        self.assertEqual(out["precision"],"LOCALITA")
        self.assertEqual(out["source"],"anncsu_localita_indicativa")

if __name__=="__main__":
    unittest.main(verbosity=2)
