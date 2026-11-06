#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SRC = Path(__file__).resolve().parents[1]
for p in (SRC, SRC/"pipeline", SRC/"importer", SRC/"geocoding", SRC/"audit"):
    if str(p) not in sys.path:
        sys.path.insert(0,str(p))

from aste_paths import RAW_DIR, REPORTS_DIR, ensure_runtime_dirs
import daily_pipeline_v3_18 as production_daily
import pvp_import_atomic_v2 as importer_v2
import primary_pin_publisher_v2_preview as pin_preview
import istat_boundary_runtime_v11_6 as boundary_runtime

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

def _get_json(base,key,path,params):
    url=base.rstrip("/")+"/rest/v1/"+path+"?"+urlencode(params,safe="(),.*")
    req=Request(url,method="GET",headers={
        "apikey":key,
        "Authorization":"Bearer "+key,
        "Accept":"application/json",
    })
    with urlopen(req,timeout=120) as response:
        return json.loads(response.read().decode("utf-8") or "[]")

def _verify_imported_locations(plan):
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]
    expected={str(r[0]["pvp_id"]):len(r[3]) for r in plan["records"]}
    ids=list(expected)
    seen_listings=set()
    bad_parent_pins=set()
    location_counts=Counter()
    primary_counts=Counter()

    for start in range(0,len(ids),50):
        batch=ids[start:start+50]
        filt="in.("+",".join(batch)+")"
        listings=_get_json(base,key,"listings",{
            "select":"pvp_id,latitudine,longitudine,maps_link,geocode_status",
            "pvp_id":filt,
        })
        for row in listings:
            pid=str(row.get("pvp_id") or "")
            seen_listings.add(pid)
            if (
                row.get("latitudine") is None
                or row.get("longitudine") is None
                or not row.get("maps_link")
                or str(row.get("geocode_status") or "").upper()!="DONE"
            ):
                bad_parent_pins.add(pid)

        locations=_get_json(base,key,"listing_locations",{
            "select":"pvp_id,is_primary",
            "pvp_id":filt,
        })
        for row in locations:
            pid=str(row.get("pvp_id") or "")
            location_counts[pid]+=1
            if row.get("is_primary") is True:
                primary_counts[pid]+=1

    missing=sorted(set(ids)-seen_listings)
    bad_locations=sorted(pid for pid in ids if location_counts[pid] != expected[pid])
    bad_primary=sorted(pid for pid in ids if primary_counts[pid] != 1)
    if missing or bad_locations or bad_primary or bad_parent_pins:
        raise RuntimeError(
            "Post-import verification failed: "
            f"missing_listings={missing[:10]} "
            f"bad_location_counts={bad_locations[:10]} "
            f"bad_primary_counts={bad_primary[:10]} "
            f"bad_parent_pins={sorted(bad_parent_pins)[:10]}"
        )
    return {
        "status":"PASS",
        "listings_verified":len(ids),
        "location_rows_verified":sum(location_counts.values()),
        "one_primary_per_listing":len(ids),
        "parent_pins_verified":len(ids),
    }

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

def _attach_primary_pins(plan):
    ann=pin_preview.open_anncsu_bbox()
    istat=pin_preview.open_istat_localities()
    boundary=boundary_runtime.BoundaryContext()
    cache={}
    actions=Counter()
    sources=Counter()
    precisions=Counter()
    boundary_counts=Counter()
    pin_rows=[]
    rebuilt=[]
    try:
        for listing,subjects,attachments,locations in plan["records"]:
            primaries=[x for x in locations if x.get("is_primary") is True]
            if len(primaries)!=1:
                raise RuntimeError(
                    f"PIN ATTACH requires exactly one primary location pvp_id={listing.get('pvp_id')} got={len(primaries)}"
                )
            loc=primaries[0]
            pin,reason=pin_preview.choose_pin(loc,ann,istat,cache)
            if pin is None:
                raise RuntimeError(f"NO_SAFE_PIN pvp_id={listing.get('pvp_id')} reason={reason}")

            if pin.get("source")=="PVP" and pin.get("precision")=="PVP_COORD":
                check=boundary.test(
                    {
                        "comune":listing.get("comune"),
                        "provincia":listing.get("provincia"),
                        "sigla_provincia":listing.get("sigla_provincia"),
                        "regione":listing.get("regione"),
                    },
                    lat=pin.get("lat"),lon=pin.get("lon"),
                )
                position=str(check.get("position") or "UNKNOWN")
                boundary_counts[position]+=1
                if check.get("safe"):
                    pin=dict(pin)
                    pin["reason"]="Coordinate fornite dal bene PVP e validate territorialmente nel confine comunale ISTAT 2026 ("+position+")."
                else:
                    fallback,fallback_reason=pin_preview.municipality_fallback(loc,ann,istat,cache)
                    if fallback is None:
                        raise RuntimeError(
                            f"PVP_COORD_NOT_TERRITORIALLY_SAFE pvp_id={listing.get('pvp_id')} "
                            f"position={position} fallback={fallback_reason}"
                        )
                    pin=fallback
                    pin["reason"] += " Coordinate PVP scartate dal controllo territoriale ISTAT 2026 ("+position+")."

            action="PUBLISH_RESOLVED" if pin["fallback_from_status"] is None else "PUBLISH_COMUNE_FALLBACK"
            actions[action]+=1
            sources[str(pin["source"])]+=1
            precisions[str(pin["precision"])]+=1

            row=dict(listing)
            row.update({
                "latitudine":float(pin["lat"]),
                "longitudine":float(pin["lon"]),
                "maps_link":pin_preview.maps_link(pin["lat"],pin["lon"]),
                "geocode_status":"DONE",
                "geocode_source":pin["source"],
                "geocode_precision":pin["precision"],
                "geocode_decision_reason":pin["reason"],
                "geocode_formatted_address":pin["formatted_address"],
            })
            rebuilt.append((row,subjects,attachments,locations))
            pin_rows.append({
                "pvp_id":str(row["pvp_id"]),
                "latitudine":row["latitudine"],
                "longitudine":row["longitudine"],
                "maps_link":row["maps_link"],
                "geocode_status":row["geocode_status"],
                "geocode_source":row["geocode_source"],
                "geocode_precision":row["geocode_precision"],
                "geocode_decision_reason":row["geocode_decision_reason"],
                "geocode_formatted_address":row["geocode_formatted_address"],
            })
    finally:
        ann.close()
        istat.close()

    pin_rows.sort(key=lambda x:int(x["pvp_id"]))
    canonical=json.dumps(pin_rows,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    pin_sha=hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    out=dict(plan)
    out["records"]=rebuilt
    return out,{
        "count":len(pin_rows),
        "actions":dict(actions),
        "sources":dict(sources),
        "precisions":dict(precisions),
        "pvp_boundary_validation":dict(boundary_counts),
        "plan_sha256":pin_sha,
        "google_calls":0,
    }

def main(argv=None):
    ap=argparse.ArgumentParser(description="Aste Easy experimental daily v4; preview by default, guarded apply optional")
    ap.add_argument("--apply",action="store_true")
    ap.add_argument("--expected-new",type=int)
    ap.add_argument("--expected-raw-sha256")
    ap.add_argument("--expected-pin-plan-sha")
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
            "mode":"APPLY_GUARDED" if args.apply else "PREVIEW_ZERO_DB_WRITES",
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
        out=Path(REPORTS_DIR)/("experimental_daily_v4_apply.json" if args.apply else "experimental_daily_v4_preview.json")
        out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
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

    t=time.perf_counter()
    plan,pin_plan=_attach_primary_pins(plan)
    timings["attach_primary_pins"]=round(time.perf_counter()-t,3)

    if args.apply:
        if args.expected_new is None or not args.expected_raw_sha256 or not args.expected_pin_plan_sha:
            raise RuntimeError(
                "--apply requires --expected-new, --expected-raw-sha256 and --expected-pin-plan-sha from the latest preview"
            )
        if (
            args.expected_new != len(plan["records"])
            or args.expected_raw_sha256 != plan["raw_sha256"]
            or args.expected_pin_plan_sha != pin_plan["plan_sha256"]
        ):
            raise RuntimeError(
                "APPLY AUTHORIZATION MISMATCH: current import/pin plan differs from preview; zero writes. "
                "Run preview again and use eligible_new + raw_sha256 + pin_plan.plan_sha256."
            )
    importer_summary=importer_v2.run(
        apply=args.apply,
        expected_new=args.expected_new if args.apply else None,
        expected_sha=args.expected_raw_sha256 if args.apply else None,
        prebuilt_plan=plan,
    )
    post_verify=None
    if args.apply:
        t=time.perf_counter()
        post_verify=_verify_imported_locations(plan)
        timings["post_import_verify"]=round(time.perf_counter()-t,3)
    timings["total"]=round(time.perf_counter()-total,3)

    location_stats=_read_json(Path(REPORTS_DIR)/"experimental_listing_locations_preview_v1_stats.json")
    unified_stats=_read_json(Path(REPORTS_DIR)/"experimental_unified_offline_v1_stats.json")

    summary={
        "version":VERSION,
        "mode":"APPLY_GUARDED" if args.apply else "PREVIEW_ZERO_DB_WRITES",
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
        "pin_plan":pin_plan,
        "raw_sha256":importer_summary.get("raw_sha256"),
        "db_writes":importer_summary.get("inserted",0) if args.apply else 0,
        "inserted":importer_summary.get("inserted",0),
        "skipped_concurrent":importer_summary.get("skipped_concurrent",0),
        "post_import_verify":post_verify,
        "google_calls":0,
        "timings_seconds":timings,
    }
    out=Path(REPORTS_DIR)/("experimental_daily_v4_apply.json" if args.apply else "experimental_daily_v4_preview.json")
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
    print("[OUT]",out,flush=True)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
