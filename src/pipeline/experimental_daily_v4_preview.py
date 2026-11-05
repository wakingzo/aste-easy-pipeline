#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
for p in (SRC, SRC/"pipeline", SRC/"importer", SRC/"geocoding", SRC/"audit"):
    if str(p) not in sys.path:
        sys.path.insert(0,str(p))

from aste_paths import RAW_DIR, REPORTS_DIR, ensure_runtime_dirs
import daily_pipeline_v3_18 as production_daily
import pvp_import_atomic_v2 as importer_v2

VERSION="experimental_daily_v4_preview"
RAW=Path(RAW_DIR)/"listings_api_raw.jsonl"
RESOLVER=Path(REPORTS_DIR)/"experimental_address_resolver_v1.jsonl"
ANNCSU=Path(REPORTS_DIR)/"experimental_anncsu_match_v1.jsonl"
LOCALITY=Path(REPORTS_DIR)/"experimental_locality_match_v1.jsonl"
UNIFIED=Path(REPORTS_DIR)/"experimental_unified_offline_v1.jsonl"
LOCATIONS=Path(REPORTS_DIR)/"experimental_listing_locations_preview_v1.jsonl"

def _run(label,args):
    print(f"[EXP DAILY] {label}",flush=True)
    t=time.perf_counter()
    cp=subprocess.run([sys.executable,*map(str,args)],check=False)
    if cp.returncode:
        raise RuntimeError(f"{label} failed with exit code {cp.returncode}")
    return round(time.perf_counter()-t,3)

def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))

def main(argv=None):
    ap=argparse.ArgumentParser(description="Aste Easy experimental daily v4 preview; zero DB writes")
    ap.add_argument("--page-size",type=int,default=200)
    ap.add_argument("--safety-days",type=int,default=2)
    args=ap.parse_args(argv)
    if not 10 <= args.page_size <= 500:
        ap.error("--page-size must be 10..500")
    if not 0 <= args.safety_days <= 14:
        ap.error("--safety-days must be 0..14")

    ensure_runtime_dirs()
    total=time.perf_counter()

    # Reuse the production discovery implementation only. It reads the DB threshold
    # and refreshes local RAW files, but performs no listing/import/geocode writes.
    build_args=argparse.Namespace(page_size=args.page_size,safety_days=args.safety_days)
    t=time.perf_counter()
    build_summary=production_daily._run_builder(build_args)
    build_sec=round(time.perf_counter()-t,3)

    if not RAW.is_file():
        raise RuntimeError("PVP builder completed but RAW is missing: "+str(RAW))

    importer_v2.assert_rpc_available()

    timings={"pvp_build":build_sec}
    timings["resolver"]=_run("address resolver",[
        SRC/"geocoding"/"experimental_address_resolver_v1.py",RAW
    ])
    timings["anncsu"]=_run("ANNCSU matcher",[
        SRC/"geocoding"/"experimental_anncsu_match_v1.py",RESOLVER
    ])
    timings["locality"]=_run("ISTAT locality matcher",[
        SRC/"geocoding"/"experimental_locality_match_v1.py",RESOLVER
    ])
    timings["unified"]=_run("unified offline ranker",[
        SRC/"geocoding"/"experimental_unified_offline_v1.py",RESOLVER,ANNCSU,LOCALITY
    ])
    timings["location_preview"]=_run("listing_locations materialization preview",[
        SRC/"importer"/"experimental_listing_locations_preview_v1.py",RESOLVER,UNIFIED
    ])

    # Crucial: build_plan now sees the freshly generated location preview, so any
    # eligible new listing is planned with rich location children atomically.
    t=time.perf_counter()
    plan=importer_v2.build_plan()
    timings["import_plan"]=round(time.perf_counter()-t,3)
    importer_summary=importer_v2.run(apply=False,prebuilt_plan=plan)
    timings["total"]=round(time.perf_counter()-total,3)

    location_stats=_read_json(Path(REPORTS_DIR)/"experimental_listing_locations_preview_v1_stats.json")
    unified_stats=_read_json(Path(REPORTS_DIR)/"experimental_unified_offline_v1_stats.json")

    summary={
        "version":VERSION,
        "mode":"PREVIEW_ZERO_DB_WRITES",
        "created_at":datetime.now().astimezone().isoformat(),
        "build_summary":build_summary,
        "raw_rows":location_stats.get("listings"),
        "location_rows":location_stats.get("location_rows"),
        "offline_final_status":unified_stats.get("final_status"),
        "offline_final_source":unified_stats.get("final_source"),
        "eligible_new":importer_summary.get("eligible_new"),
        "new_soggetti":importer_summary.get("new_soggetti"),
        "new_allegati":importer_summary.get("new_allegati"),
        "new_locations":importer_summary.get("new_locations"),
        "location_plan_sources":importer_summary.get("location_plan_sources"),
        "raw_sha256":importer_summary.get("raw_sha256"),
        "db_writes":0,
        "google_calls":0,
        "timings_seconds":timings,
    }
    out=Path(REPORTS_DIR)/"experimental_daily_v4_preview.json"
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
    print("[OUT]",out,flush=True)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
