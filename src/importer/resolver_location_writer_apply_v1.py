#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import ssl
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
for _p in (SRC, HERE, SRC / "pipeline", SRC / "geocoding"):
    if str(_p) not in __import__("sys").path:
        __import__("sys").path.insert(0, str(_p))

import pvp_import_atomic_v2 as importer
from aste_paths import REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env

VERSION = "resolver_location_writer_apply_v1"
RPC = "aste_replace_listing_locations_v1"
PREVIEW_PATH = REPORTS_DIR / "experimental_listing_locations_preview_v1.jsonl"

def _request_json(req, timeout=120, retries=4):
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8") or "[]")
        except HTTPError:
            raise
        except (URLError, ConnectionResetError, TimeoutError, socket.timeout, ssl.SSLError, OSError) as exc:
            if attempt >= retries-1:
                raise
            print(f"[RETRY] transport {type(exc).__name__}; retry {attempt+2}/{retries}", flush=True)
            time.sleep(1.5*(attempt+1))
    raise RuntimeError("request retry loop exhausted")

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
        rows=_request_json(req)
        for row in rows:
            pid=str(row.get("pvp_id") or "")
            if pid:
                out.add(pid)
        if len(rows)<1000:
            break
        offset += len(rows)
    return out

def load_locations(path):
    if not path.is_file():
        raise RuntimeError("Missing location preview: "+str(path))
    h=hashlib.sha256()
    by_id=defaultdict(list)
    with path.open("rb") as f:
        for raw in f:
            h.update(raw)
            line=raw.decode("utf-8",errors="replace").strip()
            if not line:
                continue
            obj=json.loads(line)
            pid=str(obj.get("pvp_id") or "")
            by_id[pid].append(obj)
    for pid,rows in by_id.items():
        if sum(bool(r.get("is_primary")) for r in rows) != 1:
            raise RuntimeError("Expected exactly one primary for "+pid)
        rows.sort(key=lambda r:int(r.get("bene_index") or 0))
    return by_id,h.hexdigest()

def rpc_replace(base,key,pid,rows):
    url=base.rstrip("/")+"/rest/v1/rpc/"+RPC
    payload=json.dumps({
        "p_pvp_id":pid,
        "p_locations":rows,
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
        raise RuntimeError(f"Writer RPC failed HTTP {exc.code} for pvp_id={pid}: {detail[:2000]}") from exc

def main(argv=None):
    ap=argparse.ArgumentParser(description="Write validated resolver output to listing_locations only")
    ap.add_argument("--apply",action="store_true")
    ap.add_argument("--expected-listings",type=int)
    ap.add_argument("--expected-preview-sha256")
    args=ap.parse_args(argv)

    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    by_id,sha=load_locations(PREVIEW_PATH)
    existing=fetch_existing_pvp_ids(base,key)

    eligible={pid:rows for pid,rows in by_id.items() if pid in existing}
    expired_or_missing=sorted(set(by_id)-existing)

    summary={
        "version":VERSION,
        "mode":"APPLY" if args.apply else "PREVIEW",
        "preview_sha256":sha,
        "preview_listings":len(by_id),
        "current_db_matches":len(eligible),
        "preview_ids_not_in_current_db":len(expired_or_missing),
        "location_rows_to_write":sum(len(rows) for rows in eligible.values()),
        "primary_rows":sum(sum(bool(r.get("is_primary")) for r in rows) for rows in eligible.values()),
        "status":dict(Counter((r.get("status") or "NONE") for rows in eligible.values() for r in rows)),
        "source":dict(Counter((r.get("source") or "NONE") for rows in eligible.values() for r in rows)),
        "applied_listings":0,
        "applied_location_rows":0,
    }

    if args.apply:
        if args.expected_listings is None or not args.expected_preview_sha256:
            raise RuntimeError("--apply requires --expected-listings and --expected-preview-sha256")
        if args.expected_listings != len(eligible) or args.expected_preview_sha256 != sha:
            raise RuntimeError("Authorization mismatch: plan changed; zero writes")
        total=len(eligible)
        for idx,pid in enumerate(sorted(eligible,key=lambda x:int(x)),1):
            rows=eligible[pid]
            result=rpc_replace(base,key,pid,rows)
            if result.get("pvp_id") != pid:
                raise RuntimeError("Writer returned different pvp_id")
            if result.get("locations") != len(rows):
                raise RuntimeError("Writer location count mismatch for "+pid)
            if result.get("primary_locations") != 1:
                raise RuntimeError("Writer primary count mismatch for "+pid)
            summary["applied_listings"] += 1
            summary["applied_location_rows"] += len(rows)
            if idx==1 or idx%100==0 or idx==total:
                print(f"[WRITER] {idx}/{total} listings; location_rows={summary['applied_location_rows']}",flush=True)

    out=REPORTS_DIR/("resolver_location_writer_apply_v1_"+("apply" if args.apply else "preview")+".json")
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print("[OUT]",out)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
