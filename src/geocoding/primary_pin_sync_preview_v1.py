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

def _distance_m(lat1,lon1,lat2,lon2):
    vals=[_f(lat1),_f(lon1),_f(lat2),_f(lon2)]
    if any(v is None for v in vals):
        return None
    a,b,c,d=vals
    r=6371008.8
    p1,p2=math.radians(a),math.radians(c)
    dp=math.radians(c-a); dl=math.radians(d-b)
    z=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(min(1.0,math.sqrt(z)))

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
    coord_changes=0
    link_only_changes=0
    publishable=0
    coord_distance_bins={"<=25m":0,"25-100m":0,"100-500m":0,"500m-1km":0,"1-5km":0,">5km":0,"OLD_MISSING":0}
    coord_source_counts={}
    coord_precision_counts={}
    source_distance_bins={}
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
        coords_same=_same(parent.get("latitudine"),lat) and _same(parent.get("longitudine"),lon)
        link_same=str(parent.get("maps_link") or "")==target_link
        if coords_same and link_same:
            exact += 1
            continue
        distance_m=None
        if coords_same:
            link_only_changes += 1
        else:
            coord_changes += 1
            src=str(loc.get("source") or "NONE")
            prec=str(loc.get("precision") or "NONE")
            coord_source_counts[src]=coord_source_counts.get(src,0)+1
            coord_precision_counts[prec]=coord_precision_counts.get(prec,0)+1
            distance_m=_distance_m(parent.get("latitudine"),parent.get("longitudine"),lat,lon)
            if distance_m is None:
                distance_bin="OLD_MISSING"
            elif distance_m <= 25:
                distance_bin="<=25m"
            elif distance_m <= 100:
                distance_bin="25-100m"
            elif distance_m <= 500:
                distance_bin="100-500m"
            elif distance_m <= 1000:
                distance_bin="500m-1km"
            elif distance_m <= 5000:
                distance_bin="1-5km"
            else:
                distance_bin=">5km"
            coord_distance_bins[distance_bin] += 1
            source_distance_bins.setdefault(src,{"<=25m":0,"25-100m":0,"100-500m":0,"500m-1km":0,"1-5km":0,">5km":0,"OLD_MISSING":0})
            source_distance_bins[src][distance_bin] += 1
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
            "change_type":"LINK_ONLY" if coords_same else "COORDS",
            "distance_m":round(distance_m,1) if distance_m is not None else None,
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
        "coordinate_changes":coord_changes,
        "maps_link_only_changes":link_only_changes,
        "coordinate_distance_bins":coord_distance_bins,
        "coordinate_change_sources":dict(sorted(coord_source_counts.items())),
        "coordinate_change_precisions":dict(sorted(coord_precision_counts.items())),
        "source_distance_bins":{k:source_distance_bins[k] for k in sorted(source_distance_bins)},
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
