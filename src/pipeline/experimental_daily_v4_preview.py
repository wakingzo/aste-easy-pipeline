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
DELTA_RAW=Path(RAW_DIR)/"listings_api_raw_delta_v4.jsonl"
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

def _write_delta_raw(source_path, dest_path, wanted_ids):
    wanted={str(x) for x in wanted_ids}
    found=set()
    rows=0
    tmp=dest_path.with_suffix(dest_path.suffix+".tmp")
    with source_path.open("r",encoding="utf-8",errors="replace") as src, tmp.open("w",encoding="utf-8") as out:
        for line in src:
            if not line.strip():
                continue
            obj=json.loads(line)
            pid=str(obj.get("pvp_id") or "")
            if pid in wanted:
                out.write(json.dumps(obj,ensure_ascii=False)+"\n")
                found.add(pid)
                rows += 1
    if found != wanted:
        missing=sorted(wanted-found)
        raise RuntimeError("Delta RAW missing planned pvp_id(s): "+",".join(missing[:20]))
    tmp.replace(dest_path)
    return {"wanted_ids":len(wanted),"rows":rows}

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

    # First read-only plan: determine exactly which RAW IDs are truly new/eligible
    # using the validated importer rules (DB presence, retention/date and admin gates).
    t=time.perf_counter()
    preplan=importer_v2.build_plan()
    timings["delta_plan"]=round(time.perf_counter()-t,3)
    eligible_ids=[str(r[0]["pvp_id"]) for r in preplan["records"]]
    delta_meta=_write_delta_raw(RAW,DELTA_RAW,eligible_ids)

    if not eligible_ids:
        summary={
            "version":VERSION,
            "mode":"PREVIEW_ZERO_DB_WRITES",
            "created_at":datetime.now().astimezone().isoformat(),
            "build_summary":build_summary,
            "raw_window_rows":preplan.get("incoming_unique"),
            "eligible_delta":0,
            "delta_raw_rows":0,
            "eligible_new":0,
            "db_writes":0,
            "google_calls":0,
            "timings_seconds":{**timings,"total":round(time.perf_counter()-total,3)},
        }
        out=Path(REPORTS_DIR)/"experimental_daily_v4_preview.json"
        out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\\n",encoding="utf-8")
        print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
        print("[OUT]",out,flush=True)
        return 0

    timings["resolver"]=_run("address resolver DELTA",[
        SRC/"geocoding"/"experimental_address_resolver_v1.py",DELTA_RAW
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

    # Reuse the already authorized preplan and only replace its location payload
    # with the freshly materialized resolver output. This avoids a second full DB scan.
    location_by_id=importer_v2.load_location_preview()
    rebuilt_records=[]
    for listing,subjects,attachments,_old_locations in preplan["records"]:
        pid=str(listing["pvp_id"])
        locations=location_by_id.get(pid) or []
        if not locations:
            raise RuntimeError("Fresh resolver output missing locations for planned pvp_id="+pid)
        rebuilt_records.append((listing,subjects,attachments,locations))
    plan=dict(preplan)
    plan["records"]=rebuilt_records
    plan["locations"]=sum(len(r[3]) for r in rebuilt_records)
    plan["location_plan_sources"]={"EXPERIMENTAL_PREVIEW":len(rebuilt_records)}
    timings["attach_locations"]=round(0.0,3)
    importer_summary=importer_v2.run(apply=False,prebuilt_plan=plan)
    timings["total"]=round(time.perf_counter()-total,3)

    location_stats=_read_json(Path(REPORTS_DIR)/"experimental_listing_locations_preview_v1_stats.json")
    unified_stats=_read_json(Path(REPORTS_DIR)/"experimental_unified_offline_v1_stats.json")

    summary={
        "version":VERSION,
        "mode":"PREVIEW_ZERO_DB_WRITES",
        "created_at":datetime.now().astimezone().isoformat(),
        "build_summary":build_summary,
        "raw_window_rows":preplan.get("incoming_unique"),
        "raw_existing_in_db":preplan.get("existing"),
        "raw_new_candidates":preplan.get("new_candidates"),
        "eligible_delta":len(eligible_ids),
        "delta_raw_rows":delta_meta.get("rows"),
        "resolved_delta_rows":location_stats.get("listings"),
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
