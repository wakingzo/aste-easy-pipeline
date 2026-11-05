#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
for _p in (SRC, HERE, SRC / "pipeline", SRC / "geocoding"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pvp_import_atomic_v2 as importer
from project_env import load_project_env

VALIDATE_RPC = "aste_validate_replace_listing_locations_v1"

def fetch_existing_pvp_ids(base,key):
    out=set()
    offset=0
    while True:
        url=base.rstrip("/")+"/rest/v1/listings?select=pvp_id&order=pvp_id.asc&limit=1000&offset="+str(offset)
        req=Request(url,method="GET",headers={
            "apikey":key,
            "Authorization":"Bearer "+key,
            "Accept":"application/json",
        })
        with urlopen(req,timeout=120) as response:
            rows=json.loads(response.read().decode("utf-8") or "[]")
        for row in rows:
            pid=str(row.get("pvp_id") or "")
            if pid:
                out.add(pid)
        if len(rows)<1000:
            break
        offset += len(rows)
    return out

def choose_pid(locations_by_id, existing_pvp_ids, explicit=None):
    if explicit:
        if explicit not in locations_by_id:
            raise RuntimeError("Requested pvp_id has no location preview: " + explicit)
        if explicit not in existing_pvp_ids:
            raise RuntimeError("Requested pvp_id is no longer present in listings: " + explicit)
        return explicit
    candidates = sorted(
        (pid for pid in locations_by_id if pid in existing_pvp_ids),
        key=lambda pid: (len(locations_by_id[pid]) <= 1, pid)
    )
    if not candidates:
        raise RuntimeError("No current DB listing has location preview rows")
    return candidates[0]

def rpc_validate(base,key,pvp_id,locations):
    url=base.rstrip("/")+"/rest/v1/rpc/"+VALIDATE_RPC
    payload=json.dumps({
        "p_pvp_id":pvp_id,
        "p_locations":locations,
    },ensure_ascii=False).encode("utf-8")
    req=Request(url,data=payload,method="POST",headers={
        "apikey":key,
        "Authorization":"Bearer "+key,
        "Content-Type":"application/json",
        "Accept":"application/json",
    })
    try:
        with urlopen(req,timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail=exc.read().decode("utf-8",errors="replace")
        raise RuntimeError(f"Validator RPC failed HTTP {exc.code}: {detail[:2000]}") from exc

def main(argv=None):
    ap=argparse.ArgumentParser(description="Validate resolver location writer with rollback")
    ap.add_argument("--pvp-id")
    args=ap.parse_args(argv)

    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    locations_by_id=importer.load_location_preview()
    existing_pvp_ids=fetch_existing_pvp_ids(base,key)
    pid=choose_pid(locations_by_id,existing_pvp_ids,args.pvp_id)
    locations=locations_by_id[pid]

    result=rpc_validate(base,key,pid,locations)
    rpc_result=result.get("rpc_result") or {}

    if result.get("validated") is not True or result.get("rolled_back") is not True:
        raise RuntimeError("Validator returned unexpected response")
    if rpc_result.get("pvp_id") != pid:
        raise RuntimeError("Validator returned different pvp_id")
    if rpc_result.get("locations") != len(locations):
        raise RuntimeError("Location count mismatch")
    if rpc_result.get("primary_locations") != 1:
        raise RuntimeError("Expected exactly one primary location")

    summary={
        "validated":True,
        "rolled_back":True,
        "pvp_id":pid,
        "locations":len(locations),
        "primary_locations":sum(bool(x.get("is_primary")) for x in locations),
        "rpc_result":rpc_result,
    }
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
