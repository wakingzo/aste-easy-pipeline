#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import time
from collections import Counter
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from pathlib import Path

HERE=Path(__file__).resolve().parent
SRC=HERE.parent
for p in (SRC,HERE):
    if str(p) not in __import__("sys").path:
        __import__("sys").path.insert(0,str(p))

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env
import primary_pin_publisher_v2_preview as preview
import istat_boundary_runtime_v11_6 as boundary_runtime

VERSION="primary_pin_publisher_v2_guarded"
PAGE=500

FIELDS=("latitudine","longitudine","maps_link","geocode_status","geocode_source",
        "geocode_precision","geocode_decision_reason","geocode_formatted_address")

def clean(v):
    return "" if v is None else str(v).strip()

def _is_transient(exc):
    if isinstance(exc,(ConnectionResetError,ConnectionAbortedError,TimeoutError,socket.timeout,URLError)):
        return True
    if isinstance(exc,OSError):
        return getattr(exc,"winerror",None) in {10053,10054,10060} or getattr(exc,"errno",None) in {54,60,104,110}
    return type(exc).__name__=="RemoteDisconnected"

def _request_json(req, label):
    for attempt in range(4):
        try:
            with urlopen(req,timeout=120) as r:
                return json.loads(r.read().decode("utf-8") or "[]")
        except Exception as exc:
            if not _is_transient(exc) or attempt>=3:
                raise
            print(f"[HTTP RETRY] {label} transient {type(exc).__name__}; retry {attempt+2}/4",flush=True)
            time.sleep(1.5*(attempt+1))
    raise RuntimeError("retry loop exhausted")

def _get(base,key,path,params):
    url=base.rstrip("/")+"/rest/v1/"+path+"?"+urlencode(params,safe="(),.*")
    req=Request(url,method="GET",headers={"apikey":key,"Authorization":"Bearer "+key,"Accept":"application/json"})
    return _request_json(req,path)

def _all(base,key,path,params):
    out=[]; offset=0
    while True:
        p=dict(params); p["limit"]=str(PAGE); p["offset"]=str(offset)
        rows=_get(base,key,path,p); out.extend(rows)
        if len(rows)<PAGE: break
        offset += len(rows)
    return out

def _patch(base,key,pvp_id,payload):
    url=base.rstrip("/")+"/rest/v1/listings?"+urlencode({"pvp_id":"eq."+str(pvp_id)})
    data=json.dumps(payload,ensure_ascii=False).encode("utf-8")
    for attempt in range(4):
        req=Request(url,data=data,method="PATCH",headers={
            "apikey":key,"Authorization":"Bearer "+key,"Content-Type":"application/json",
            "Accept":"application/json","Prefer":"return=representation"})
        try:
            return _request_json(req,"PATCH "+str(pvp_id))
        except Exception:
            raise
    raise RuntimeError("PATCH retry loop exhausted")

def _one(base,key,pvp_id):
    rows=_get(base,key,"listings",{
        "select":"pvp_id,latitudine,longitudine,maps_link,geocode_status,geocode_source,geocode_precision,geocode_decision_reason,geocode_formatted_address",
        "pvp_id":"eq."+str(pvp_id),"limit":"2"})
    if len(rows)!=1:
        raise RuntimeError("Expected exactly one listing pvp_id="+str(pvp_id))
    return rows[0]

def build_plan(base,key,target_ids=None):
    select_fields="pvp_id,comune,provincia,sigla_provincia,regione,latitudine,longitudine,maps_link,geocode_status,geocode_source,geocode_precision,geocode_decision_reason,geocode_formatted_address"
    if target_ids is None:
        listings=_all(base,key,"listings",{
            "select":select_fields,
            "geocode_status":"eq.PENDING","order":"pvp_id.asc"})
    else:
        wanted=sorted({str(x) for x in target_ids},key=lambda x:int(x) if x.isdigit() else x)
        listings=[]
        for start in range(0,len(wanted),50):
            batch=wanted[start:start+50]
            if not batch:
                continue
            listings.extend(_get(base,key,"listings",{
                "select":select_fields,
                "pvp_id":"in.("+",".join(batch)+")",
                "geocode_status":"eq.PENDING",
            }))
        got={str(x.get("pvp_id") or "") for x in listings}
        missing=sorted(set(wanted)-got)
        if missing:
            raise RuntimeError("Target listing(s) missing or not PENDING: "+",".join(missing[:20]))
        listings.sort(key=lambda x:int(str(x["pvp_id"])))
    ids=[str(x["pvp_id"]) for x in listings]

    primaries=[]
    for start in range(0,len(ids),50):
        batch=ids[start:start+50]
        if not batch: continue
        primaries.extend(_get(base,key,"listing_locations",{
            "select":"pvp_id,is_primary,address_raw,comune_pvp,provincia_pvp,sigla_provincia_pvp,cap_pvp,codice_istat,latitudine,longitudine,source,precision,status,formatted_address,resolution_reason,warnings",
            "pvp_id":"in.("+",".join(batch)+")","is_primary":"eq.true"}))
    by_pid={}
    for x in primaries:
        pid=str(x.get("pvp_id") or "")
        if pid in by_pid:
            raise RuntimeError("Multiple primary locations for pvp_id="+pid)
        by_pid[pid]=x
    missing=sorted(set(ids)-set(by_pid))
    if missing:
        raise RuntimeError("Missing primary listing_locations: "+",".join(missing[:20]))

    ann=preview.open_anncsu_bbox()
    istat=preview.open_istat_localities()
    boundary=boundary_runtime.BoundaryContext()
    cache={}
    plan=[]
    actions=Counter(); sources=Counter(); precisions=Counter()
    boundary_counts=Counter()
    try:
        for row in listings:
            pid=str(row["pvp_id"]); loc=by_pid[pid]
            pin,reason=preview.choose_pin(loc,ann,istat,cache)
            if pin is None:
                rep=boundary.representative_point({
                    "comune":row.get("comune"),
                    "provincia":row.get("provincia"),
                    "sigla_provincia":row.get("sigla_provincia"),
                    "regione":row.get("regione"),
                })
                if not rep.get("ok"):
                    raise RuntimeError(
                        f"NO_SAFE_PIN pvp_id={pid} reason={reason} boundary_fallback={rep.get('reason')}"
                    )
                pin={
                    "lat":rep["lat"],"lon":rep["lon"],
                    "source":"istat_boundary_comune_indicativo",
                    "precision":"COMUNE",
                    "reason":"Punto rappresentativo verificato all'interno del confine comunale ISTAT 2026; non rappresenta la posizione esatta dell'immobile.",
                    "formatted_address":"Comune di "+str(row.get("comune") or ""),
                    "fallback_from_status":"BOUNDARY_COMUNE",
                }

            if pin.get("source")=="PVP" and pin.get("precision")=="PVP_COORD":
                check=boundary.test(
                    {
                        "comune":row.get("comune"),
                        "provincia":row.get("provincia"),
                        "sigla_provincia":row.get("sigla_provincia"),
                        "regione":row.get("regione"),
                    },
                    lat=pin.get("lat"),
                    lon=pin.get("lon"),
                )
                position=str(check.get("position") or "UNKNOWN")
                boundary_counts[position]+=1
                if check.get("safe"):
                    pin=dict(pin)
                    pin["reason"]="Coordinate fornite dal bene PVP e validate territorialmente nel confine comunale ISTAT 2026 ("+position+")."
                else:
                    fallback,fallback_reason=preview.municipality_fallback(loc,ann,istat,cache)
                    if fallback is None:
                        rep=boundary.representative_point({
                            "comune":row.get("comune"),
                            "provincia":row.get("provincia"),
                            "sigla_provincia":row.get("sigla_provincia"),
                            "regione":row.get("regione"),
                        })
                        if not rep.get("ok"):
                            raise RuntimeError(
                                f"PVP_COORD_NOT_TERRITORIALLY_SAFE pvp_id={pid} position={position} "
                                f"fallback={fallback_reason} boundary_fallback={rep.get('reason')}"
                            )
                        fallback={
                            "lat":rep["lat"],"lon":rep["lon"],
                            "source":"istat_boundary_comune_indicativo",
                            "precision":"COMUNE",
                            "reason":"Punto rappresentativo verificato all'interno del confine comunale ISTAT 2026; non rappresenta la posizione esatta dell'immobile.",
                            "formatted_address":"Comune di "+str(row.get("comune") or ""),
                            "fallback_from_status":"BOUNDARY_COMUNE",
                        }
                    pin=fallback
                    pin["reason"] += " Coordinate PVP scartate dal controllo territoriale ISTAT 2026 ("+position+")."

            action="PUBLISH_RESOLVED" if pin["fallback_from_status"] is None else "PUBLISH_COMUNE_FALLBACK"
            after={
                "latitudine":float(pin["lat"]),
                "longitudine":float(pin["lon"]),
                "maps_link":preview.maps_link(pin["lat"],pin["lon"]),
                "geocode_status":"DONE",
                "geocode_source":pin["source"],
                "geocode_precision":pin["precision"],
                "geocode_decision_reason":pin["reason"],
                "geocode_formatted_address":pin["formatted_address"],
            }
            before={f:row.get(f) for f in FIELDS}
            plan.append({"pvp_id":pid,"action":action,"before":before,"after":after})
            actions[action]+=1; sources[pin["source"]]+=1; precisions[pin["precision"]]+=1
    finally:
        ann.close(); istat.close()

    plan.sort(key=lambda x:int(x["pvp_id"]))
    return plan,dict(actions),dict(sources),dict(precisions),dict(boundary_counts)

def sha_plan(plan):
    raw=json.dumps(plan,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def same_value(a,b):
    if a is None and b in (None,""): return True
    if isinstance(a,(int,float)) or isinstance(b,(int,float)):
        try:
            return abs(float(a)-float(b))<1e-10
        except Exception:
            pass
    return clean(a)==clean(b)

def apply_plan(base,key,plan):
    writes=0
    for item in plan:
        current=_one(base,key,item["pvp_id"])
        for f,expected in item["before"].items():
            if not same_value(current.get(f),expected):
                raise RuntimeError(f"PRECONDITION FAILED pvp_id={item['pvp_id']} field={f}")
        got=_patch(base,key,item["pvp_id"],item["after"])
        if not isinstance(got,list) or len(got)!=1:
            raise RuntimeError("Unexpected PATCH result pvp_id="+item["pvp_id"])
        patched=got[0]
        for f,expected in item["after"].items():
            if not same_value(patched.get(f),expected):
                raise RuntimeError(f"PATCH READBACK FAILED pvp_id={item['pvp_id']} field={f}")
        writes+=1
    return writes

def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument("--apply",action="store_true")
    ap.add_argument("--expected-count",type=int)
    ap.add_argument("--expected-plan-sha")
    ap.add_argument("--pvp-ids",help="Comma-separated explicit cohort; default is all PENDING")
    args=ap.parse_args(argv)

    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    target_ids=[x.strip() for x in (args.pvp_ids or "").split(",") if x.strip()] or None
    plan,actions,sources,precisions,boundary_counts=build_plan(base,key,target_ids=target_ids)
    sha=sha_plan(plan)
    summary={
        "version":VERSION,
        "mode":"APPLY" if args.apply else "PREVIEW_ZERO_DB_WRITES",
        "pending_listings":len(plan),
        "publishable":len(plan),
        "actions":actions,
        "sources":sources,
        "precisions":precisions,
        "pvp_boundary_validation":dict(boundary_counts),
        "plan_sha256":sha,
        "google_calls":0,
        "db_writes":0,
        "sample":plan[:30],
    }

    if not args.apply:
        out=REPORTS_DIR/"primary_pin_publisher_v2_guarded_preview.json"
        out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        print("[OUT]",out)
        return 0

    if args.expected_count is None or not args.expected_plan_sha:
        raise SystemExit("--apply requires --expected-count and --expected-plan-sha")
    if len(plan)!=args.expected_count:
        raise RuntimeError(f"COUNT MISMATCH current={len(plan)} expected={args.expected_count}; zero writes")
    if sha!=args.expected_plan_sha:
        raise RuntimeError("PLAN SHA MISMATCH; zero writes")

    writes=apply_plan(base,key,plan)

    if target_ids is None:
        remaining=_all(base,key,"listings",{
            "select":"pvp_id","geocode_status":"eq.PENDING","order":"pvp_id.asc"})
        if remaining:
            raise RuntimeError(f"POST VERIFY FAILED: {len(remaining)} PENDING remain")
    else:
        remaining=[]
        for start in range(0,len(target_ids),50):
            batch=target_ids[start:start+50]
            remaining.extend(_get(base,key,"listings",{
                "select":"pvp_id",
                "pvp_id":"in.("+",".join(batch)+")",
                "geocode_status":"eq.PENDING",
            }))
        if remaining:
            raise RuntimeError(f"POST VERIFY FAILED: {len(remaining)} target PENDING remain")

    summary["db_writes"]=writes
    summary["post_verify"]="PASS"
    summary["remaining_pending"]=0
    out=REPORTS_DIR/"primary_pin_publisher_v2_guarded_apply.json"
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print("[OUT]",out)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
