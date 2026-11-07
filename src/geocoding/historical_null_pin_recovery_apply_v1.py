#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE=Path(__file__).resolve().parent
SRC=HERE.parent
for p in (SRC,HERE):
    if str(p) not in __import__("sys").path:
        __import__("sys").path.insert(0,str(p))

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env
import istat_boundary_runtime_v11_6 as boundary_runtime

VERSION="historical_null_pin_recovery_apply_v1"
PAGE=1000

def _request_json(req):
    with urlopen(req,timeout=120) as r:
        return json.loads(r.read().decode("utf-8") or "null")

def _get(base,key,path,params):
    url=base.rstrip("/")+"/rest/v1/"+path+"?"+urlencode(params,safe="(),.*")
    req=Request(url,method="GET",headers={
        "apikey":key,
        "Authorization":"Bearer "+key,
        "Accept":"application/json",
    })
    return _request_json(req)

def _all(base,key,path,params):
    out=[]; offset=0
    while True:
        p=dict(params); p["limit"]=str(PAGE); p["offset"]=str(offset)
        rows=_get(base,key,path,p); out.extend(rows)
        if len(rows)<PAGE: break
        offset += len(rows)
    return out

def _rpc(base,key,name,payload):
    url=base.rstrip("/")+"/rest/v1/rpc/"+name
    data=json.dumps(payload,ensure_ascii=False).encode("utf-8")
    req=Request(url,data=data,method="POST",headers={
        "apikey":key,
        "Authorization":"Bearer "+key,
        "Content-Type":"application/json",
        "Accept":"application/json",
    })
    return _request_json(req)

def maps_link(lat,lon):
    return f"https://www.google.com/maps?q={float(lat)},{float(lon)}"

def build_plan(base,key):
    listings=_all(base,key,"listings",{
        "select":"listing_id,pvp_id,regione,provincia,sigla_provincia,comune,indirizzo,latitudine,longitudine,maps_link,geocode_precision",
        "geocode_precision":"is.null",
        "order":"pvp_id.asc",
    })
    boundary=boundary_runtime.BoundaryContext()
    plan=[]
    failures=[]
    for row in listings:
        rep=boundary.representative_point({
            "comune":row.get("comune"),
            "provincia":row.get("provincia"),
            "sigla_provincia":row.get("sigla_provincia"),
            "regione":row.get("regione"),
        })
        if not rep.get("ok"):
            failures.append({
                "pvp_id":str(row.get("pvp_id") or ""),
                "listing_id":row.get("listing_id"),
                "comune":row.get("comune"),
                "reason":str(rep.get("reason") or "UNKNOWN"),
            })
            continue
        plan.append({
            "pvp_id":str(row.get("pvp_id") or ""),
            "listing_id":str(row.get("listing_id") or ""),
            "latitudine":float(rep["lat"]),
            "longitudine":float(rep["lon"]),
            "maps_link":maps_link(rep["lat"],rep["lon"]),
        })
    plan.sort(key=lambda x:int(x["pvp_id"]))
    canonical=json.dumps(plan,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return plan,failures,digest

def verify(base,key,plan):
    ids=[x["pvp_id"] for x in plan]
    seen={}
    for i in range(0,len(ids),50):
        batch=ids[i:i+50]
        rows=_get(base,key,"listings",{
            "select":"pvp_id,latitudine,longitudine,maps_link,geocode_precision",
            "pvp_id":"in.("+",".join(batch)+")",
        })
        for r in rows:
            seen[str(r["pvp_id"])]=r
    bad=[]
    for x in plan:
        r=seen.get(x["pvp_id"])
        if not r:
            bad.append(x["pvp_id"]); continue
        try:
            same=(abs(float(r["latitudine"])-x["latitudine"])<1e-8 and
                  abs(float(r["longitudine"])-x["longitudine"])<1e-8)
        except Exception:
            same=False
        if (not same or r.get("maps_link")!=x["maps_link"] or
                str(r.get("geocode_precision") or "")!="INDICATIVA"):
            bad.append(x["pvp_id"])
    return bad

def main(argv=None):
    ap=argparse.ArgumentParser(description="Guarded repair for historical listings without published map pins")
    ap.add_argument("--apply",action="store_true")
    ap.add_argument("--expected-count",type=int)
    ap.add_argument("--expected-plan-sha256")
    args=ap.parse_args(argv)

    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    plan,failures,digest=build_plan(base,key)
    summary={
        "version":VERSION,
        "mode":"APPLY_GUARDED" if args.apply else "PREVIEW_ZERO_DB_WRITES",
        "plan_count":len(plan),
        "unresolved":len(failures),
        "plan_sha256":digest,
        "db_writes":0,
        "google_calls":0,
        "failures":failures,
    }

    if failures:
        raise RuntimeError(f"Unresolved municipality pins remain: {len(failures)}; zero writes")

    if not args.apply:
        out=REPORTS_DIR/"historical_null_pin_recovery_apply_v1_preview.json"
        out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        print("[OUT]",out)
        return 0

    if args.expected_count is None or not args.expected_plan_sha256:
        raise RuntimeError("--apply requires --expected-count and --expected-plan-sha256")
    if args.expected_count!=len(plan) or args.expected_plan_sha256!=digest:
        raise RuntimeError("Authorization mismatch: plan changed; zero writes")

    for i,row in enumerate(plan,1):
        _rpc(base,key,"aste_publish_comune_fallback_v1",{
            "p_listing_id":row["listing_id"],
            "p_latitudine":row["latitudine"],
            "p_longitudine":row["longitudine"],
            "p_maps_link":row["maps_link"],
        })
        summary["db_writes"]+=1
        if i % 50 == 0 or i == len(plan):
            print(f"[APPLY] {i}/{len(plan)}",flush=True)

    bad=verify(base,key,plan)
    if bad:
        raise RuntimeError(f"Post-apply verification failed for {len(bad)} listings: {bad[:20]}")

    remaining=_all(base,key,"listings",{
        "select":"listing_id",
        "geocode_precision":"is.null",
        "limit":"1",
    })
    summary["remaining_null_precision"]=len(remaining)
    summary["verify_status"]="PASS"
    out=REPORTS_DIR/"historical_null_pin_recovery_apply_v1_apply.json"
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print("[OUT]",out)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
