#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import socket
import ssl
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
for _p in (SRC, HERE):
    if str(_p) not in __import__("sys").path:
        __import__("sys").path.insert(0, str(_p))

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env

VERSION = "listing_locations_legacy_backfill_v1"
PAGE = 1000
BATCH = 250

def _request_json(req, timeout=120, retries=5):
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8")
                return json.loads(body or "[]")
        except HTTPError:
            raise
        except (URLError, ConnectionResetError, TimeoutError, socket.timeout, ssl.SSLError, OSError) as exc:
            if attempt >= retries - 1:
                raise
            print(f"[RETRY] transport {type(exc).__name__}; retry {attempt+2}/{retries}", flush=True)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("request retry loop exhausted")

def _get(base,key,path,params):
    url=base.rstrip("/")+"/rest/v1/"+path+"?"+urlencode(params)
    req=Request(url,method="GET",headers={
        "apikey":key,"Authorization":"Bearer "+key,"Accept":"application/json"
    })
    return _request_json(req)

def _post(base,key,path,rows,on_conflict=None):
    url=base.rstrip("/")+"/rest/v1/"+path
    if on_conflict:
        url += "?"+urlencode({"on_conflict":on_conflict})
    data=json.dumps(rows,ensure_ascii=False).encode("utf-8")
    req=Request(url,data=data,method="POST",headers={
        "apikey":key,"Authorization":"Bearer "+key,
        "Content-Type":"application/json","Accept":"application/json",
        "Prefer":"resolution=ignore-duplicates,return=minimal"
    })
    try:
        with urlopen(req,timeout=120) as r:
            r.read()
    except HTTPError as exc:
        detail=exc.read().decode("utf-8",errors="replace")
        raise RuntimeError(f"Supabase POST {path} failed HTTP {exc.code}: {detail[:1200]}") from exc

def _all(base,key,path,select,order):
    out=[]
    offset=0
    while True:
        rows=_get(base,key,path,{
            "select":select,
            "order":order,
            "limit":str(PAGE),
            "offset":str(offset)
        })
        out.extend(rows)
        if len(rows)<PAGE:
            break
        offset += len(rows)
    return out

def _f(v):
    try:
        x=float(v)
    except (TypeError,ValueError):
        return None
    return x if math.isfinite(x) else None

def _maps_link(lat,lon,existing):
    if lat is not None and lon is not None:
        return f"https://www.google.com/maps?q={lat},{lon}"
    return str(existing or "")

def main(argv=None):
    ap=argparse.ArgumentParser(description="Backfill listing_locations for listings still missing a child row")
    ap.add_argument("--apply",action="store_true")
    ap.add_argument("--expected-new-locations",type=int)
    ap.add_argument("--expected-plan-sha256")
    args=ap.parse_args(argv)

    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    listings=_all(
        base,key,"listings",
        "listing_id,pvp_id,indirizzo,comune,provincia,sigla_provincia,cap,latitudine,longitudine,maps_link",
        "pvp_id.asc"
    )
    locations=_all(
        base,key,"listing_locations",
        "listing_id,pvp_id,bene_index,is_primary",
        "pvp_id.asc,bene_index.asc"
    )

    has_location={str(x.get("listing_id")) for x in locations}
    prepared=[]

    for x in listings:
        lid=str(x.get("listing_id") or "")
        pid=str(x.get("pvp_id") or "")
        if not lid or not pid or lid in has_location:
            continue
        lat=_f(x.get("latitudine"))
        lon=_f(x.get("longitudine"))
        resolved=(lat is not None and lon is not None)
        prepared.append({
            "listing_id":lid,
            "pvp_id":pid,
            "bene_index":0,
            "is_primary":True,
            "address_raw":x.get("indirizzo"),
            "comune_pvp":x.get("comune"),
            "provincia_pvp":x.get("provincia"),
            "sigla_provincia_pvp":x.get("sigla_provincia"),
            "cap_pvp":x.get("cap"),
            "codice_istat":None,
            "pvp_latitudine":lat,
            "pvp_longitudine":lon,
            "street":None,
            "civic":None,
            "internal":None,
            "locality":None,
            "snc":False,
            "is_intersection":False,
            "latitudine":lat,
            "longitudine":lon,
            "source":"LEGACY_LISTINGS",
            "precision":"LEGACY_COORD" if resolved else "NESSUNA",
            "confidence":"LEGACY",
            "status":"RESOLVED" if resolved else "PENDING",
            "formatted_address":None,
            "anncsu_status":None,
            "google_status":None,
            "warnings":[],
            "resolution_reason":"Copied from current public.listings during non-destructive legacy backfill.",
            "resolver_version":"legacy_backfill_v1",
            "checked_at":None,
        })

    digest=hashlib.sha256(
        json.dumps(
            [{"listing_id":r["listing_id"],"pvp_id":r["pvp_id"],"lat":r["latitudine"],"lon":r["longitudine"]} for r in prepared],
            ensure_ascii=False,sort_keys=True,separators=(",",":")
        ).encode("utf-8")
    ).hexdigest()

    summary={
        "version":VERSION,
        "mode":"APPLY" if args.apply else "PREVIEW",
        "db_listings":len(listings),
        "existing_location_rows":len(locations),
        "listings_with_any_location":len(has_location),
        "new_locations":len(prepared),
        "with_coordinates":sum(r["latitudine"] is not None for r in prepared),
        "without_coordinates":sum(r["latitudine"] is None for r in prepared),
        "plan_sha256":digest,
        "inserted":0,
    }

    if args.apply:
        if args.expected_new_locations is None or not args.expected_plan_sha256:
            raise RuntimeError("--apply requires --expected-new-locations and --expected-plan-sha256")
        if args.expected_new_locations != len(prepared) or args.expected_plan_sha256 != digest:
            raise RuntimeError("Authorization mismatch: plan changed; zero writes")
        for i in range(0,len(prepared),BATCH):
            batch=prepared[i:i+BATCH]
            _post(base,key,"listing_locations",batch,on_conflict="listing_id,bene_index")
            summary["inserted"] += len(batch)

    out=REPORTS_DIR/("listing_locations_legacy_backfill_v1_"+("apply" if args.apply else "preview")+".json")
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print("[OUT]",out)

if __name__=="__main__":
    main()
