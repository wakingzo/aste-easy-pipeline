#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

HERE=Path(__file__).resolve().parent
SRC=HERE.parent
for p in (SRC,HERE,SRC/"importer",SRC/"pipeline"):
    if str(p) not in sys.path:
        sys.path.insert(0,str(p))

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env
import geocode_pending_v2 as legacy_geo

VERSION="primary_pin_publisher_v2_preview"
PAGE=500

def _json_get(base,key,path,params):
    url=base.rstrip("/")+"/rest/v1/"+path+"?"+urlencode(params,safe="(),.*")
    req=Request(url,method="GET",headers={
        "apikey":key,
        "Authorization":"Bearer "+key,
        "Accept":"application/json",
    })
    with urlopen(req,timeout=120) as r:
        return json.loads(r.read().decode("utf-8") or "[]")

def _all(base,key,path,params):
    out=[]
    offset=0
    while True:
        p=dict(params)
        p["limit"]=str(PAGE)
        p["offset"]=str(offset)
        rows=_json_get(base,key,path,p)
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

def valid_pair(lat,lon):
    la,lo=_f(lat),_f(lon)
    return la is not None and lo is not None and -90<=la<=90 and -180<=lo<=180 and not (la==0 and lo==0)

def maps_link(lat,lon):
    return "https://www.google.com/maps?q="+quote(f"{float(lat):.8f},{float(lon):.8f}")

def municipality_fallback(loc,ctx,cache):
    # Only physical-bene geography from listing_locations is allowed here.
    comune=str(loc.get("comune_pvp") or "").strip()
    if not comune:
        return None
    sample={
        "comune":comune,
        "provincia":str(loc.get("provincia_pvp") or "").strip(),
        "sigla_provincia":str(loc.get("sigla_provincia_pvp") or "").strip(),
        "cap":str(loc.get("cap_pvp") or "").strip(),
        "indirizzo":str(loc.get("address_raw") or "").strip(),
    }
    meta=ctx.resolve_admin(sample)
    if meta.get("status")!="RESOLVED" or not meta.get("canonical_code"):
        return None
    code=str(meta["canonical_code"])
    if code not in cache:
        cache[code]=ctx.representative_point(sample,admin_meta=meta)
    rep=cache[code]
    if not rep.get("ok") or not valid_pair(rep.get("lat"),rep.get("lon")):
        return None
    pos=ctx.test(sample,lat=rep["lat"],lon=rep["lon"],admin_meta=meta).get("position")
    if pos!="INSIDE":
        return None
    return {
        "lat":float(rep["lat"]),
        "lon":float(rep["lon"]),
        "source":"istat_comune_indicativo",
        "precision":"COMUNE",
        "status":"DONE",
        "reason":"Punto indicativo nel Comune del bene fisico PVP; non rappresenta la posizione esatta dell'immobile.",
        "formatted_address":"Comune di "+comune,
        "fallback_from_status":str(loc.get("status") or "UNKNOWN"),
    }

def choose_pin(loc,ctx,cache):
    status=str(loc.get("status") or "").upper()
    if status=="RESOLVED" and valid_pair(loc.get("latitudine"),loc.get("longitudine")):
        return {
            "lat":float(loc["latitudine"]),
            "lon":float(loc["longitudine"]),
            "source":str(loc.get("source") or "listing_locations"),
            "precision":str(loc.get("precision") or "UNKNOWN"),
            "status":"DONE",
            "reason":str(loc.get("resolution_reason") or "Primary listing_location resolved"),
            "formatted_address":str(loc.get("formatted_address") or loc.get("address_raw") or ""),
            "fallback_from_status":None,
        }
    # REVIEW / UNRESOLVED / NEEDS_COORD / PENDING are deliberately not trusted
    # as exact points. Preserve map coverage with an explicit municipality marker.
    return municipality_fallback(loc,ctx,cache)

def main(argv=None):
    ap=argparse.ArgumentParser(description="Preview publication of primary map pins for newly imported PENDING listings")
    ap.add_argument("--sample",type=int,default=30)
    args=ap.parse_args(argv)

    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    listings=_all(base,key,"listings",{
        "select":"listing_id,pvp_id,geocode_status,latitudine,longitudine,maps_link",
        "geocode_status":"eq.PENDING",
        "order":"pvp_id.asc",
    })
    pending_ids={str(x.get("pvp_id") or "") for x in listings}
    if not pending_ids:
        summary={"version":VERSION,"pending_listings":0,"publishable":0,"google_calls":0}
        out=REPORTS_DIR/"primary_pin_publisher_v2_preview.json"
        out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        print("[OUT]",out)
        return 0

    primaries=[]
    ids=sorted(pending_ids,key=lambda x:int(x) if x.isdigit() else x)
    for start in range(0,len(ids),50):
        batch=ids[start:start+50]
        rows=_json_get(base,key,"listing_locations",{
            "select":"listing_id,pvp_id,is_primary,address_raw,comune_pvp,provincia_pvp,sigla_provincia_pvp,cap_pvp,codice_istat,latitudine,longitudine,source,precision,confidence,status,formatted_address,resolution_reason,resolver_version,warnings",
            "pvp_id":"in.("+",".join(batch)+")",
            "is_primary":"eq.true",
        })
        primaries.extend(rows)

    by_pid={str(x.get("pvp_id") or ""):x for x in primaries}
    if len(by_pid)!=len(pending_ids):
        missing=sorted(pending_ids-set(by_pid))
        raise RuntimeError("PENDING listing(s) without exactly one primary location; first="+str(missing[:10]))

    ctx,_,_=legacy_geo.prepare_context(no_anncsu=True)
    rep_cache={}
    plans=[]
    counts=Counter()
    source_counts=Counter()
    precision_counts=Counter()

    for listing in listings:
        pid=str(listing["pvp_id"])
        loc=by_pid[pid]
        pin=choose_pin(loc,ctx,rep_cache)
        if pin is None:
            counts["NO_SAFE_PIN"]+=1
            plans.append({
                "pvp_id":pid,
                "action":"NO_SAFE_PIN",
                "location_status":loc.get("status"),
                "comune_pvp":loc.get("comune_pvp"),
            })
            continue
        action="PUBLISH_RESOLVED" if pin["fallback_from_status"] is None else "PUBLISH_COMUNE_FALLBACK"
        counts[action]+=1
        source_counts[pin["source"]]+=1
        precision_counts[pin["precision"]]+=1
        plans.append({
            "pvp_id":pid,
            "action":action,
            "location_status":loc.get("status"),
            "comune_pvp":loc.get("comune_pvp"),
            "latitudine":pin["lat"],
            "longitudine":pin["lon"],
            "maps_link":maps_link(pin["lat"],pin["lon"]),
            "geocode_status":pin["status"],
            "geocode_source":pin["source"],
            "geocode_precision":pin["precision"],
            "geocode_decision_reason":pin["reason"],
            "geocode_formatted_address":pin["formatted_address"],
            "fallback_from_status":pin["fallback_from_status"],
        })

    summary={
        "version":VERSION,
        "mode":"PREVIEW_ZERO_DB_WRITES",
        "pending_listings":len(listings),
        "primary_locations":len(primaries),
        "publishable":sum(v for k,v in counts.items() if k.startswith("PUBLISH_")),
        "actions":dict(counts),
        "sources":dict(source_counts),
        "precisions":dict(precision_counts),
        "google_calls":0,
        "db_writes":0,
        "sample":plans[:max(0,args.sample)],
    }
    out=REPORTS_DIR/"primary_pin_publisher_v2_preview.json"
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print("[OUT]",out)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
