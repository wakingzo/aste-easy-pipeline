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

VERSION="daily_pipeline_v4"
RAW=Path(RAW_DIR)/"listings_api_raw.jsonl"
DELTA_RAW=Path(RAW_DIR)/"listings_api_raw_delta_v4.jsonl"
RESOLVER=Path(REPORTS_DIR)/"address_resolver_v1.jsonl"
ANNCSU=Path(REPORTS_DIR)/"anncsu_match_v1.jsonl"
LOCALITY=Path(REPORTS_DIR)/"locality_match_v1.jsonl"
UNIFIED=Path(REPORTS_DIR)/"unified_offline_v1.jsonl"
LOCATIONS=Path(REPORTS_DIR)/"listing_locations_materializer_v1.jsonl"

def _run(label,args):
    print(f"[DAILY V4] {label}",flush=True)
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

def _parent_map_precision(technical_precision):
    """Map rich technical precision to the four user-facing map classes."""
    precision=str(technical_precision or "").strip().upper()
    mapping={
        "CIVICO":"PRECISA",
        "PVP_COORD":"APPROSSIMATIVA",
        "STRADA":"APPROSSIMATIVA",
        "LOCALITA":"INDICATIVA",
        "COMUNE":"INDICATIVA",
    }
    if precision not in mapping:
        raise RuntimeError(f"Unsupported published technical precision for parent projection: {precision or '<blank>'}")
    return mapping[precision]

def _verify_imported_locations(plan):
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]
    expected_counts={str(r[0]["pvp_id"]):len(r[3]) for r in plan["records"]}
    expected_primary={}
    for listing,_,_,locations in plan["records"]:
        pid=str(listing["pvp_id"])
        primaries=[x for x in locations if x.get("is_primary") is True]
        if len(primaries)!=1:
            raise RuntimeError(f"Verification plan has invalid primary count pvp_id={pid}")
        expected_primary[pid]={
            "latitudine":float(listing["latitudine"]),
            "longitudine":float(listing["longitudine"]),
            "maps_link":listing["maps_link"],
            "geocode_precision":str(listing.get("geocode_precision") or ""),
            "location":primaries[0],
        }

    ids=list(expected_counts)
    seen_listings=set()
    bad_parent_pins=set()
    bad_primary_payload=set()
    location_counts=Counter()
    primary_counts=Counter()

    for start in range(0,len(ids),50):
        batch=ids[start:start+50]
        filt="in.("+",".join(batch)+")"
        listings=_get_json(base,key,"listings",{
            "select":"pvp_id,latitudine,longitudine,maps_link,geocode_precision",
            "pvp_id":filt,
        })
        for row in listings:
            pid=str(row.get("pvp_id") or "")
            seen_listings.add(pid)
            exp=expected_primary.get(pid) or {}
            try:
                same_coords=(
                    abs(float(row.get("latitudine"))-float(exp["latitudine"])) < 1e-8
                    and abs(float(row.get("longitudine"))-float(exp["longitudine"])) < 1e-8
                )
            except (TypeError,ValueError,KeyError):
                same_coords=False
            if (
                not same_coords
                or row.get("maps_link") != exp.get("maps_link")
                or str(row.get("geocode_precision") or "") != str(exp.get("geocode_precision") or "")
            ):
                bad_parent_pins.add(pid)

        locations=_get_json(base,key,"listing_locations",{
            "select":"pvp_id,is_primary,latitudine,longitudine,source,precision,status,formatted_address,resolution_reason",
            "pvp_id":filt,
        })
        for row in locations:
            pid=str(row.get("pvp_id") or "")
            location_counts[pid]+=1
            if row.get("is_primary") is True:
                primary_counts[pid]+=1
                exp=(expected_primary.get(pid) or {}).get("location") or {}
                try:
                    same_coords=(
                        abs(float(row.get("latitudine"))-float(exp.get("latitudine"))) < 1e-8
                        and abs(float(row.get("longitudine"))-float(exp.get("longitudine"))) < 1e-8
                    )
                except (TypeError,ValueError):
                    same_coords=False
                if (
                    not same_coords
                    or str(row.get("source") or "") != str(exp.get("source") or "")
                    or str(row.get("precision") or "") != str(exp.get("precision") or "")
                    or str(row.get("status") or "") != "RESOLVED"
                    or str(row.get("formatted_address") or "") != str(exp.get("formatted_address") or "")
                    or str(row.get("resolution_reason") or "") != str(exp.get("resolution_reason") or "")
                ):
                    bad_primary_payload.add(pid)

    missing=sorted(set(ids)-seen_listings)
    bad_locations=sorted(pid for pid in ids if location_counts[pid] != expected_counts[pid])
    bad_primary=sorted(pid for pid in ids if primary_counts[pid] != 1)
    if missing or bad_locations or bad_primary or bad_parent_pins or bad_primary_payload:
        raise RuntimeError(
            "Post-import verification failed: "
            f"missing_listings={missing[:10]} "
            f"bad_location_counts={bad_locations[:10]} "
            f"bad_primary_counts={bad_primary[:10]} "
            f"bad_parent_pins={sorted(bad_parent_pins)[:10]} "
            f"bad_primary_payload={sorted(bad_primary_payload)[:10]}"
        )
    return {
        "status":"PASS",
        "listings_verified":len(ids),
        "location_rows_verified":sum(location_counts.values()),
        "one_primary_per_listing":len(ids),
        "parent_pins_verified":len(ids),
        "primary_location_payload_verified":len(ids),
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
    boundary_fallback_ids=[]
    pvp_outside_ids=[]
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
                rep=boundary.representative_point({
                    "comune":listing.get("comune"),
                    "provincia":listing.get("provincia"),
                    "sigla_provincia":listing.get("sigla_provincia"),
                    "regione":listing.get("regione"),
                })
                if not rep.get("ok"):
                    raise RuntimeError(
                        f"NO_SAFE_PIN pvp_id={listing.get('pvp_id')} reason={reason} "
                        f"boundary_fallback={rep.get('reason')}"
                    )
                pin={
                    "lat":rep["lat"],"lon":rep["lon"],
                    "source":"istat_boundary_comune_indicativo",
                    "precision":"COMUNE",
                    "reason":"Punto rappresentativo verificato all'interno del confine comunale ISTAT 2026; non rappresenta la posizione esatta dell'immobile.",
                    "formatted_address":"Comune di "+str(listing.get("comune") or ""),
                    "fallback_from_status":"BOUNDARY_COMUNE",
                }
                boundary_fallback_ids.append(str(listing.get("pvp_id")))

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
                if position=="OUTSIDE":
                    pvp_outside_ids.append(str(listing.get("pvp_id")))
                if check.get("safe"):
                    pin=dict(pin)
                    pin["reason"]="Coordinate fornite dal bene PVP e validate territorialmente nel confine comunale ISTAT 2026 ("+position+")."
                else:
                    fallback,fallback_reason=pin_preview.municipality_fallback(loc,ann,istat,cache)
                    if fallback is None:
                        rep=boundary.representative_point({
                            "comune":listing.get("comune"),
                            "provincia":listing.get("provincia"),
                            "sigla_provincia":listing.get("sigla_provincia"),
                            "regione":listing.get("regione"),
                        })
                        if not rep.get("ok"):
                            raise RuntimeError(
                                f"PVP_COORD_NOT_TERRITORIALLY_SAFE pvp_id={listing.get('pvp_id')} "
                                f"position={position} fallback={fallback_reason} boundary_fallback={rep.get('reason')}"
                            )
                        fallback={
                            "lat":rep["lat"],"lon":rep["lon"],
                            "source":"istat_boundary_comune_indicativo",
                            "precision":"COMUNE",
                            "reason":"Punto rappresentativo verificato all'interno del confine comunale ISTAT 2026; non rappresenta la posizione esatta dell'immobile.",
                            "formatted_address":"Comune di "+str(listing.get("comune") or ""),
                            "fallback_from_status":"BOUNDARY_COMUNE",
                        }
                        boundary_fallback_ids.append(str(listing.get("pvp_id")))
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
                "geocode_precision":_parent_map_precision(pin["precision"]),
            })

            published_locations=[]
            for location in locations:
                if location.get("is_primary") is not True:
                    published_locations.append(location)
                    continue
                primary=dict(location)
                warnings=list(primary.get("warnings") or [])
                original_status=str(primary.get("status") or "")
                if pin.get("fallback_from_status") is not None and original_status and original_status!="RESOLVED":
                    marker="PUBLISHED_FALLBACK_FROM_"+original_status.upper()
                    if marker not in warnings:
                        warnings.append(marker)
                primary.update({
                    "latitudine":float(pin["lat"]),
                    "longitudine":float(pin["lon"]),
                    "source":pin["source"],
                    "precision":pin["precision"],
                    "status":"RESOLVED",
                    "formatted_address":pin["formatted_address"],
                    "resolution_reason":pin["reason"],
                    "warnings":warnings,
                })
                published_locations.append(primary)

            rebuilt.append((row,subjects,attachments,published_locations))
            pin_rows.append({
                "pvp_id":str(row["pvp_id"]),
                "latitudine":row["latitudine"],
                "longitudine":row["longitudine"],
                "maps_link":row["maps_link"],
                "geocode_precision":row["geocode_precision"],
                "source":pin["source"],
                "resolution_reason":pin["reason"],
                "formatted_address":pin["formatted_address"],
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
        "pvp_outside_ids":sorted(set(pvp_outside_ids),key=lambda x:int(x)),
        "boundary_fallback_ids":sorted(set(boundary_fallback_ids),key=lambda x:int(x)),
        "plan_sha256":pin_sha,
        "google_calls":0,
    }

def main(argv=None):
    ap=argparse.ArgumentParser(description="Aste Easy daily pipeline v4; preview by default, guarded apply optional")
    ap.add_argument("--apply",action="store_true")
    ap.add_argument("--auto-apply",action="store_true",
                    help="Build, freeze, validate and apply the same plan in one process; intended for scheduled runs")
    ap.add_argument("--expected-new",type=int)
    ap.add_argument("--expected-raw-sha256")
    ap.add_argument("--expected-pin-plan-sha")
    ap.add_argument("--page-size",type=int,default=200)
    ap.add_argument("--safety-days",type=int,default=2)
    args=ap.parse_args(argv)
    if args.apply and args.auto_apply:
        ap.error("--apply and --auto-apply are mutually exclusive")
    if not 10 <= args.page_size <= 500:
        ap.error("--page-size must be 10..500")
    if not 0 <= args.safety_days <= 14:
        ap.error("--safety-days must be 0..14")

    ensure_runtime_dirs()
    do_apply=bool(args.apply or args.auto_apply)
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
            "mode":"AUTO_APPLY_GUARDED" if args.auto_apply else ("APPLY_GUARDED" if args.apply else "PREVIEW_ZERO_DB_WRITES"),
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
        out=Path(REPORTS_DIR)/("daily_pipeline_v4_apply.json" if do_apply else "daily_pipeline_v4_preview.json")
        out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
        print("[OUT]",out,flush=True)
        return 0

    timings["resolver"]=_run("address resolver DELTA",[
        SRC/"geocoding"/"address_resolver_v1.py",DELTA_RAW
    ])
    timings["anncsu"]=_run("ANNCSU matcher",[
        SRC/"geocoding"/"anncsu_match_v1.py",RESOLVER
    ])
    timings["locality"]=_run("ISTAT locality matcher",[
        SRC/"geocoding"/"locality_match_v1.py",RESOLVER
    ])
    timings["unified"]=_run("unified offline ranker",[
        SRC/"geocoding"/"unified_offline_v1.py",RESOLVER,ANNCSU,LOCALITY
    ])
    timings["location_preview"]=_run("listing_locations materializer",[
        SRC/"importer"/"listing_locations_materializer_v1.py",RESOLVER,UNIFIED
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
    plan["location_plan_sources"]={"RESOLVER_MATERIALIZED":len(rebuilt_records)}
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
                f"CURRENT eligible_new={len(plan['records'])} "
                f"raw_sha256={plan['raw_sha256']} "
                f"pin_plan_sha256={pin_plan['plan_sha256']}. "
                "Run preview again and use these current authorization values."
            )
    elif args.auto_apply:
        # The plan is frozen in memory and will be passed unchanged to the importer.
        # All exact payloads are rollback-validated by importer_v2 before its first write.
        args.expected_new=len(plan["records"])
        args.expected_raw_sha256=plan["raw_sha256"]
        args.expected_pin_plan_sha=pin_plan["plan_sha256"]
    importer_summary=importer_v2.run(
        apply=do_apply,
        expected_new=args.expected_new if do_apply else None,
        expected_sha=args.expected_raw_sha256 if do_apply else None,
        prebuilt_plan=plan,
    )
    post_verify=None
    if do_apply:
        t=time.perf_counter()
        post_verify=_verify_imported_locations(plan)
        timings["post_import_verify"]=round(time.perf_counter()-t,3)
    timings["total"]=round(time.perf_counter()-total,3)

    location_stats=_read_json(Path(REPORTS_DIR)/"listing_locations_materializer_v1_stats.json")
    unified_stats=_read_json(Path(REPORTS_DIR)/"unified_offline_v1_stats.json")

    summary={
        "version":VERSION,
        "mode":"AUTO_APPLY_GUARDED" if args.auto_apply else ("APPLY_GUARDED" if args.apply else "PREVIEW_ZERO_DB_WRITES"),
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
        "db_writes":importer_summary.get("inserted",0) if do_apply else 0,
        "inserted":importer_summary.get("inserted",0),
        "skipped_concurrent":importer_summary.get("skipped_concurrent",0),
        "post_import_verify":post_verify,
        "google_calls":0,
        "timings_seconds":timings,
    }
    out=Path(REPORTS_DIR)/("daily_pipeline_v4_apply.json" if do_apply else "daily_pipeline_v4_preview.json")
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
    print("[OUT]",out,flush=True)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
