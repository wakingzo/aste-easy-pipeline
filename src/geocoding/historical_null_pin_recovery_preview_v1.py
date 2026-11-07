#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from collections import Counter
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

VERSION="historical_null_pin_recovery_preview_v1"
PAGE=1000

def _get(base,key,path,params):
    url=base.rstrip("/")+"/rest/v1/"+path+"?"+urlencode(params,safe="(),.*")
    req=Request(url,method="GET",headers={
        "apikey":key,
        "Authorization":"Bearer "+key,
        "Accept":"application/json",
    })
    with urlopen(req,timeout=120) as r:
        return json.loads(r.read().decode("utf-8") or "[]")

def _all(base,key,path,params):
    out=[]; offset=0
    while True:
        p=dict(params); p["limit"]=str(PAGE); p["offset"]=str(offset)
        rows=_get(base,key,path,p); out.extend(rows)
        if len(rows)<PAGE: break
        offset += len(rows)
    return out

def maps_link(lat,lon):
    return f"https://www.google.com/maps?q={float(lat)},{float(lon)}"

def main():
    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    listings=_all(base,key,"listings",{
        "select":"listing_id,pvp_id,regione,provincia,sigla_provincia,comune,indirizzo,latitudine,longitudine,maps_link,geocode_precision",
        "geocode_precision":"is.null",
        "order":"pvp_id.asc",
    })

    boundary=boundary_runtime.BoundaryContext()
    actions=Counter()
    reasons=Counter()
    plans=[]

    for row in listings:
        rep=boundary.representative_point({
            "comune":row.get("comune"),
            "provincia":row.get("provincia"),
            "sigla_provincia":row.get("sigla_provincia"),
            "regione":row.get("regione"),
        })
        pid=str(row.get("pvp_id") or "")
        if not rep.get("ok"):
            actions["NO_SAFE_COMUNE_PIN"]+=1
            reason=str(rep.get("reason") or "UNKNOWN")
            reasons[reason]+=1
            plans.append({
                "pvp_id":pid,
                "action":"NO_SAFE_COMUNE_PIN",
                "comune":row.get("comune"),
                "provincia":row.get("provincia"),
                "regione":row.get("regione"),
                "indirizzo":row.get("indirizzo"),
                "reason":reason,
            })
            continue

        actions["PUBLISH_COMUNE_FALLBACK"]+=1
        plans.append({
            "pvp_id":pid,
            "listing_id":row.get("listing_id"),
            "action":"PUBLISH_COMUNE_FALLBACK",
            "comune":row.get("comune"),
            "provincia":row.get("provincia"),
            "regione":row.get("regione"),
            "indirizzo":row.get("indirizzo"),
            "latitudine":float(rep["lat"]),
            "longitudine":float(rep["lon"]),
            "maps_link":maps_link(rep["lat"],rep["lon"]),
            "parent_precision":"INDICATIVA",
            "technical_precision":"COMUNE",
            "source":"istat_boundary_comune_indicativo",
            "reason":"Punto rappresentativo verificato all'interno del confine comunale ISTAT 2026; non rappresenta la posizione esatta dell'immobile.",
        })

    summary={
        "version":VERSION,
        "mode":"PREVIEW_ZERO_DB_WRITES",
        "null_precision_listings":len(listings),
        "publishable":actions["PUBLISH_COMUNE_FALLBACK"],
        "unresolved":actions["NO_SAFE_COMUNE_PIN"],
        "actions":dict(actions),
        "failure_reasons":dict(reasons),
        "google_calls":0,
        "db_writes":0,
        "sample_publishable":[x for x in plans if x["action"]=="PUBLISH_COMUNE_FALLBACK"][:50],
        "unresolved_cases":[x for x in plans if x["action"]=="NO_SAFE_COMUNE_PIN"],
    }
    out=REPORTS_DIR/"historical_null_pin_recovery_preview_v1.json"
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print("[OUT]",out)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
