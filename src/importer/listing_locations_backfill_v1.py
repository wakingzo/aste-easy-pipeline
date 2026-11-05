#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
for _p in (SRC, HERE):
    if str(_p) not in __import__("sys").path:
        __import__("sys").path.insert(0, str(_p))

from project_env import load_project_env
from aste_paths import REPORTS_DIR, ensure_runtime_dirs

VERSION = "listing_locations_backfill_v1"
PREVIEW = REPORTS_DIR / "experimental_listing_locations_preview_v1.jsonl"
BATCH = 250

def _get(base,key,path,params):
    url=base.rstrip("/")+"/rest/v1/"+path+"?"+urlencode(params)
    req=Request(url,method="GET",headers={
        "apikey":key,"Authorization":"Bearer "+key,"Accept":"application/json"
    })
    with urlopen(req,timeout=120) as r:
        return json.loads(r.read().decode("utf-8") or "[]")

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
    with urlopen(req,timeout=120) as r:
        r.read()

def load_preview(path):
    h=hashlib.sha256()
    rows=[]
    seen=set()
    with path.open("rb") as f:
        for raw in f:
            h.update(raw)
            line=raw.decode("utf-8",errors="replace").strip()
            if not line: continue
            x=json.loads(line)
            pid=str(x.get("pvp_id") or "")
            bi=x.get("bene_index")
            if not re.fullmatch(r"[0-9]{1,19}",pid) or not isinstance(bi,int) or bi<0:
                raise RuntimeError("Invalid location preview row")
            k=(pid,bi)
            if k in seen: raise RuntimeError("Duplicate preview key "+str(k))
            seen.add(k)
            rows.append(x)
    return rows,h.hexdigest()

def fetch_listing_map(base,key):
    out={}
    offset=0
    while True:
        data=_get(base,key,"listings",{
            "select":"listing_id,pvp_id","order":"pvp_id.asc",
            "limit":"1000","offset":str(offset)
        })
        if not data: break
        for x in data:
            pid=str(x.get("pvp_id") or "")
            lid=x.get("listing_id")
            if pid and lid: out[pid]=lid
        if len(data)<1000: break
        offset += len(data)
    return out

def fetch_existing_location_keys(base,key):
    out=set()
    offset=0
    while True:
        data=_get(base,key,"listing_locations",{
            "select":"pvp_id,bene_index","order":"pvp_id.asc,bene_index.asc",
            "limit":"1000","offset":str(offset)
        })
        if not data: break
        for x in data:
            out.add((str(x.get("pvp_id") or ""),int(x.get("bene_index"))))
        if len(data)<1000: break
        offset += len(data)
    return out

def normalize_for_db(x,listing_id):
    allowed={
        "pvp_id","bene_index","is_primary","address_raw","comune_pvp","provincia_pvp",
        "sigla_provincia_pvp","cap_pvp","codice_istat","pvp_latitudine","pvp_longitudine",
        "street","civic","internal","locality","snc","is_intersection","latitudine",
        "longitudine","source","precision","confidence","status","formatted_address",
        "anncsu_status","google_status","warnings","resolution_reason","resolver_version",
        "checked_at"
    }
    row={k:v for k,v in x.items() if k in allowed and v is not None}
    row["listing_id"]=listing_id
    row["pvp_id"]=str(x["pvp_id"])
    row["bene_index"]=int(x["bene_index"])
    row["is_primary"]=bool(x.get("is_primary"))
    row["snc"]=bool(x.get("snc"))
    row["is_intersection"]=bool(x.get("is_intersection"))
    row["warnings"]=list(x.get("warnings") or [])
    return row

def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument("--apply",action="store_true")
    ap.add_argument("--expected-new-locations",type=int)
    ap.add_argument("--expected-preview-sha256")
    args=ap.parse_args(argv)

    ensure_runtime_dirs()
    if not PREVIEW.is_file():
        raise RuntimeError("Missing preview: "+str(PREVIEW))
    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    src,sha=load_preview(PREVIEW)
    listings=fetch_listing_map(base,key)
    existing=fetch_existing_location_keys(base,key)

    prepared=[]
    missing_parent=Counter()
    for x in src:
        pid=str(x["pvp_id"])
        lid=listings.get(pid)
        if not lid:
            missing_parent[pid]+=1
            continue
        k=(pid,int(x["bene_index"]))
        if k in existing:
            continue
        prepared.append(normalize_for_db(x,lid))

    primary_by_pid=Counter(r["pvp_id"] for r in prepared if r["is_primary"])
    all_by_pid=Counter(r["pvp_id"] for r in prepared)
    bad_primary=[pid for pid in all_by_pid if primary_by_pid[pid] != 1 and not any(k[0]==pid for k in existing)]
    if bad_primary:
        raise RuntimeError("Backfill would create listing without exactly one primary: "+",".join(bad_primary[:10]))

    summary={
        "version":VERSION,
        "mode":"APPLY" if args.apply else "PREVIEW",
        "preview_rows":len(src),
        "preview_sha256":sha,
        "db_listings":len(listings),
        "existing_location_keys":len(existing),
        "new_locations":len(prepared),
        "listings_receiving_locations":len(set(r["pvp_id"] for r in prepared)),
        "preview_pvp_ids_without_parent_listing":len(missing_parent),
        "rows_without_parent_listing":sum(missing_parent.values()),
        "new_primary_rows":sum(bool(r["is_primary"]) for r in prepared),
        "status":dict(Counter(r.get("status") for r in prepared)),
        "source":dict(Counter(r.get("source") or "NONE" for r in prepared)),
        "inserted":0,
    }

    if args.apply:
        if args.expected_new_locations is None or not args.expected_preview_sha256:
            raise RuntimeError("--apply requires --expected-new-locations and --expected-preview-sha256")
        if args.expected_new_locations != len(prepared) or args.expected_preview_sha256 != sha:
            raise RuntimeError("Authorization mismatch: preview changed; zero writes")
        for i in range(0,len(prepared),BATCH):
            batch=prepared[i:i+BATCH]
            _post(base,key,"listing_locations",batch,on_conflict="listing_id,bene_index")
            summary["inserted"] += len(batch)

    out=REPORTS_DIR/("listing_locations_backfill_v1_"+("apply" if args.apply else "preview")+".json")
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print("[OUT]",out)

if __name__=="__main__":
    main()
