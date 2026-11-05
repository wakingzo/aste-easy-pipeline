#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import socket
import ssl
import time

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
for _p in (SRC, HERE, SRC / "importer"):
    if str(_p) not in __import__("sys").path:
        __import__("sys").path.insert(0, str(_p))

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env

VERSION = "primary_pin_sync_preview_v1"
PAGE = 1000

def _get(base,key,path,params):
    url=base.rstrip("/")+"/rest/v1/"+path+"?"+urlencode(params)
    max_attempts=5
    for attempt in range(max_attempts):
        req=Request(url,method="GET",headers={
            "apikey":key,"Authorization":"Bearer "+key,"Accept":"application/json"
        })
        try:
            with urlopen(req,timeout=120) as r:
                return json.loads(r.read().decode("utf-8") or "[]")
        except HTTPError:
            raise
        except (URLError, ConnectionResetError, TimeoutError, socket.timeout, ssl.SSLError, OSError) as exc:
            if attempt >= max_attempts-1:
                raise
            wait=1.5*(attempt+1)
            print(f"[RETRY] GET {path} transport error {type(exc).__name__}; retry {attempt+2}/{max_attempts}", flush=True)
            time.sleep(wait)
    raise RuntimeError("GET retry loop exhausted")

def _all(base,key,path,select,order,extra=None):
    out=[]
    offset=0
    while True:
        params={"select":select,"order":order,"limit":str(PAGE),"offset":str(offset)}
        if extra: params.update(extra)
        rows=_get(base,key,path,params)
        out.extend(rows)
        if len(rows)<PAGE: break
        offset += len(rows)
    return out

def _f(v):
    try:
        x=float(v)
    except (TypeError,ValueError):
        return None
    return x if math.isfinite(x) else None

def _same(a,b,eps=1e-7):
    a=_f(a); b=_f(b)
    if a is None or b is None:
        return a is None and b is None
    return abs(a-b)<=eps

def maps_link(lat,lon):
    return f"https://www.google.com/maps?q={lat},{lon}"

def main(argv=None):
    ap=argparse.ArgumentParser(description="Preview primary pin publication from listing_locations to listings")
    ap.add_argument("--sample",type=int,default=25)
    args=ap.parse_args(argv)

    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    listings=_all(
        base,key,"listings",
        "listing_id,pvp_id,latitudine,longitudine,maps_link",
        "pvp_id.asc"
    )
    primaries=_all(
        base,key,"listing_locations",
        "listing_id,pvp_id,bene_index,is_primary,latitudine,longitudine,status,source,precision,confidence,resolver_version",
        "pvp_id.asc",
        {"is_primary":"eq.true"}
    )

    by_listing={str(x["listing_id"]):x for x in listings}
    changes=[]
    exact=0
    publishable=0
    unresolved=0
    review=0
    missing_parent=0

    for loc in primaries:
        parent=by_listing.get(str(loc.get("listing_id")))
        if parent is None:
            missing_parent += 1
            continue
        lat=_f(loc.get("latitudine")); lon=_f(loc.get("longitudine"))
        status=str(loc.get("status") or "")
        if status=="REVIEW":
            review += 1
        if status!="RESOLVED" or lat is None or lon is None:
            unresolved += 1
            continue
        publishable += 1
        target_link=maps_link(lat,lon)
        same=_same(parent.get("latitudine"),lat) and _same(parent.get("longitudine"),lon) and str(parent.get("maps_link") or "")==target_link
        if same:
            exact += 1
            continue
        changes.append({
            "pvp_id":str(loc.get("pvp_id")),
            "listing_id":loc.get("listing_id"),
            "old_latitudine":parent.get("latitudine"),
            "old_longitudine":parent.get("longitudine"),
            "new_latitudine":lat,
            "new_longitudine":lon,
            "source":loc.get("source"),
            "precision":loc.get("precision"),
            "confidence":loc.get("confidence"),
            "resolver_version":loc.get("resolver_version"),
        })

    digest=hashlib.sha256(
        json.dumps(
            [{"pvp_id":x["pvp_id"],"lat":x["new_latitudine"],"lon":x["new_longitudine"]} for x in changes],
            sort_keys=True,separators=(",",":")
        ).encode("utf-8")
    ).hexdigest()

    summary={
        "version":VERSION,
        "db_listings":len(listings),
        "primary_location_rows":len(primaries),
        "publishable_resolved_primary":publishable,
        "already_exact":exact,
        "would_update":len(changes),
        "non_publishable_primary":unresolved,
        "review_primary":review,
        "missing_parent":missing_parent,
        "change_set_sha256":digest,
        "sample_changes":changes[:max(0,args.sample)],
    }
    out=REPORTS_DIR/"primary_pin_sync_preview_v1.json"
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print("[OUT]",out)

if __name__=="__main__":
    main()
