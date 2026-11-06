#!/usr/bin/env python3
from __future__ import annotations
import json, os, re, sys, unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SRC=Path(__file__).resolve().parents[1]
for p in (SRC,SRC/"geocoding"):
    if str(p) not in sys.path:
        sys.path.insert(0,str(p))

from aste_paths import RAW_DIR, REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env
import experimental_address_resolver_v1 as resolver

VERSION="historical_missing_pin_physical_geo_audit_v1"
PAGE=1000
RAW=Path(RAW_DIR)/"listings_api_raw.jsonl"

def clean(v):
    return "" if v is None else re.sub(r"\\s+"," ",str(v)).strip()

def norm(v):
    s=clean(v).replace("’","'")
    s=unicodedata.normalize("NFKD",s)
    s="".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    return re.sub(r"\\s+"," ",re.sub(r"[^a-z0-9]+"," ",s)).strip()

def _get(base,key,path,params):
    url=base.rstrip("/")+"/rest/v1/"+path+"?"+urlencode(params,safe="(),.*")
    req=Request(url,method="GET",headers={"apikey":key,"Authorization":"Bearer "+key,"Accept":"application/json"})
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

def load_raw_targets(path,wanted):
    found={}
    if not path.is_file():
        return found
    with path.open("r",encoding="utf-8",errors="replace") as f:
        for line in f:
            if not line.strip(): continue
            try:
                obj=json.loads(line)
            except Exception:
                continue
            pid=str(obj.get("pvp_id") or "")
            if pid in wanted:
                found[pid]=obj
    return found

def main():
    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    listings=_all(base,key,"listings",{
        "select":"listing_id,pvp_id,comune,provincia,sigla_provincia,regione,indirizzo,latitudine,longitudine,maps_link,geocode_status",
        "latitudine":"is.null","longitudine":"is.null","geocode_status":"is.null","order":"pvp_id.asc"
    })
    wanted={str(x["pvp_id"]) for x in listings}
    raw_by_id=load_raw_targets(RAW,wanted)

    counts=Counter(); cases=[]
    for row in listings:
        pid=str(row["pvp_id"])
        raw=raw_by_id.get(pid)
        if raw is None:
            cls="RAW_NOT_FOUND"
            counts[cls]+=1
            cases.append({"pvp_id":pid,"classification":cls,"parent_comune":row.get("comune"),
                          "parent_provincia":row.get("provincia"),"parent_indirizzo":row.get("indirizzo")})
            continue

        parsed=resolver.process_row(raw)
        beni=parsed.get("beni") or []
        physical_names=[]
        seen=set()
        for b in beni:
            c=clean(b.get("comune"))
            nc=norm(c)
            if c and nc and nc not in seen:
                seen.add(nc); physical_names.append(c)

        parent=clean(row.get("comune")); parent_norm=norm(parent)
        physical_norms={norm(x) for x in physical_names if norm(x)}
        primary=parsed.get("primary") or {}

        if not beni:
            cls="RAW_NO_BENI"
        elif not physical_names:
            cls="RAW_BENI_NO_COMUNE"
        elif len(physical_norms)>1:
            cls="MULTI_COMUNE"
        elif parent_norm in physical_norms:
            cls="MATCH_PARENT"
        else:
            cls="DIFFERENT_PHYSICAL_COMUNE"

        counts[cls]+=1
        cases.append({
            "pvp_id":pid,"classification":cls,
            "parent_comune":parent,"parent_provincia":row.get("provincia"),"parent_indirizzo":row.get("indirizzo"),
            "beni_count":parsed.get("beni_count"),"multi_address":parsed.get("multi_address"),
            "multi_comune":parsed.get("multi_comune"),"physical_comuni":physical_names,
            "primary_bene_index":parsed.get("primary_bene_index"),"primary_comune":primary.get("comune"),
            "primary_codice_istat":primary.get("codice_istat"),"primary_raw_via":primary.get("raw_via"),
            "primary_coord_valid":primary.get("coord_valid"),"primary_lat_pvp":primary.get("lat_pvp"),
            "primary_lon_pvp":primary.get("lon_pvp"),
        })

    suspicious=[x for x in cases if x["classification"] in {
        "DIFFERENT_PHYSICAL_COMUNE","MULTI_COMUNE","RAW_BENI_NO_COMUNE","RAW_NO_BENI"
    }]
    summary={
        "version":VERSION,"mode":"AUDIT_ZERO_DB_WRITES",
        "historical_missing_listings":len(listings),
        "raw_file":str(RAW),"raw_targets_found":len(raw_by_id),
        "raw_targets_missing":len(listings)-len(raw_by_id),
        "classifications":dict(counts),"suspicious_count":len(suspicious),
        "google_calls":0,"db_writes":0,
        "suspicious_cases":suspicious,"all_cases":cases,
    }
    out=Path(REPORTS_DIR)/"historical_missing_pin_physical_geo_audit_v1.json"
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\\n",encoding="utf-8")
    compact={k:v for k,v in summary.items() if k not in {"all_cases","suspicious_cases"}}
    print(json.dumps(compact,ensure_ascii=False,indent=2))
    print("[SUSPICIOUS]",len(suspicious))
    for x in suspicious[:30]:
        print(json.dumps(x,ensure_ascii=False))
    print("[OUT]",out)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
