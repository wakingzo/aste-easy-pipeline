#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import time
from collections import Counter
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE=Path(__file__).resolve().parent
SRC=HERE.parent
for p in (SRC,HERE,SRC/"importer"):
    if str(p) not in __import__("sys").path:
        __import__("sys").path.insert(0,str(p))

from aste_paths import REPORTS_DIR, REFERENCE_DIR, ensure_runtime_dirs
from project_env import load_project_env
import pvp_import_consolidated_preview as previous

VERSION="administrative_normalization_guarded_v1"
PAGE=1000

def _is_transient_transport(exc):
    if isinstance(exc,(ConnectionResetError,ConnectionAbortedError,TimeoutError,socket.timeout,URLError)):
        return True
    if isinstance(exc,OSError):
        return getattr(exc,"winerror",None) in {10053,10054,10060} or getattr(exc,"errno",None) in {54,60,104,110}
    # http.client.RemoteDisconnected subclasses ConnectionResetError on some
    # runtimes and OSError on others; keep a final class-name guard.
    return type(exc).__name__=="RemoteDisconnected"

def _get(base,key,path,params):
    url=base.rstrip("/")+"/rest/v1/"+path+"?"+urlencode(params,safe="(),.*")
    max_attempts=4
    for attempt in range(max_attempts):
        req=Request(url,method="GET",headers={"apikey":key,"Authorization":"Bearer "+key,"Accept":"application/json"})
        try:
            with urlopen(req,timeout=120) as r:
                return json.loads(r.read().decode("utf-8") or "[]")
        except Exception as exc:
            if not _is_transient_transport(exc) or attempt>=max_attempts-1:
                raise
            print(f"[HTTP RETRY] {path} transient {type(exc).__name__}; retry {attempt+2}/{max_attempts}",flush=True)
            time.sleep(1.5*(attempt+1))
    raise RuntimeError("HTTP retry loop exhausted")

def _all(base,key,path,params):
    out=[]; offset=0
    while True:
        p=dict(params); p["limit"]=str(PAGE); p["offset"]=str(offset)
        rows=_get(base,key,path,p); out.extend(rows)
        if len(rows)<PAGE: break
        offset+=len(rows)
    return out

def _patch(base,key,pvp_id,payload):
    url=base.rstrip("/")+"/rest/v1/listings?"+urlencode({"pvp_id":"eq."+str(pvp_id)})
    data=json.dumps(payload,ensure_ascii=False).encode("utf-8")
    req=Request(url,data=data,method="PATCH",headers={
        "apikey":key,"Authorization":"Bearer "+key,
        "Content-Type":"application/json","Accept":"application/json",
        "Prefer":"return=representation"
    })
    with urlopen(req,timeout=120) as r:
        return json.loads(r.read().decode("utf-8") or "[]")

def canonical_plan(rows,istat):
    plan=[]
    unresolved=[]
    for r in rows:
        admin=previous.canonical_admin(r,istat)
        if not admin.get("ok"):
            unresolved.append({"pvp_id":str(r.get("pvp_id") or ""),"reason":admin.get("reason")})
            continue
        changes={}
        for field in ("comune","provincia","sigla_provincia","regione"):
            old=previous.clean(r.get(field))
            new=previous.clean(admin.get(field))
            if old!=new:
                changes[field]=new
        if changes:
            plan.append({
                "pvp_id":str(r["pvp_id"]),
                "listing_id":r.get("listing_id"),
                "before":{f:r.get(f) for f in ("comune","provincia","sigla_provincia","regione")},
                "after":{f:admin.get(f) for f in ("comune","provincia","sigla_provincia","regione")},
                "changes":changes,
            })
    plan.sort(key=lambda x:int(x["pvp_id"]))
    return plan,unresolved

def plan_sha(plan):
    canonical=json.dumps(plan,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def main(argv=None):
    ap=argparse.ArgumentParser(description="Guarded administrative normalization of public.listings")
    ap.add_argument("--apply",action="store_true")
    ap.add_argument("--expected-count",type=int)
    ap.add_argument("--expected-plan-sha")
    args=ap.parse_args(argv)

    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    istat=previous.load_istat_admin(REFERENCE_DIR/"Elenco-comuni-italiani.xlsx")
    if not istat.get("ok"):
        raise RuntimeError("ISTAT unavailable: "+str(istat.get("reason")))

    rows=_all(base,key,"listings",{
        "select":"listing_id,pvp_id,comune,provincia,sigla_provincia,regione",
        "order":"pvp_id.asc"
    })
    plan,unresolved=canonical_plan(rows,istat)
    sha=plan_sha(plan)

    field_counts=Counter()
    signature_counts=Counter()
    for item in plan:
        fields=tuple(sorted(item["changes"].keys()))
        signature_counts["+".join(fields)]+=1
        for field in fields:
            field_counts[field]+=1

    summary={
        "version":VERSION,
        "mode":"APPLY" if args.apply else "PREVIEW_ZERO_DB_WRITES",
        "db_listings":len(rows),
        "planned_updates":len(plan),
        "change_counts_by_field":dict(field_counts),
        "change_signature_counts":dict(signature_counts),
        "only_comune_updates":sum(1 for x in plan if set(x["changes"])=={"comune"}),
        "unresolved":unresolved,
        "plan_sha256":sha,
        "google_calls":0,
        "db_writes":0,
        "sample":plan[:100],
    }

    if not args.apply:
        out=REPORTS_DIR/"administrative_normalization_guarded_v1_preview.json"
        out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        print("[OUT]",out)
        return 0

    if args.expected_count is None or args.expected_plan_sha is None:
        raise SystemExit("--apply requires --expected-count and --expected-plan-sha")
    if unresolved:
        raise RuntimeError("Refusing apply: unresolved canonical admin rows exist")
    if len(plan)!=args.expected_count:
        raise RuntimeError(f"Refusing apply: planned_updates={len(plan)} expected={args.expected_count}")
    if sha!=args.expected_plan_sha:
        raise RuntimeError("Refusing apply: plan SHA mismatch")

    writes=0
    for item in plan:
        got=_patch(base,key,item["pvp_id"],item["changes"])
        if not isinstance(got,list) or len(got)!=1:
            raise RuntimeError("Unexpected PATCH result for pvp_id="+item["pvp_id"])
        writes+=1

    # Full read-back and exact no-drift verification.
    after_rows=_all(base,key,"listings",{
        "select":"listing_id,pvp_id,comune,provincia,sigla_provincia,regione",
        "order":"pvp_id.asc"
    })
    residual,residual_unresolved=canonical_plan(after_rows,istat)
    if residual_unresolved:
        raise RuntimeError("Post-verify unresolved rows remain")
    if residual:
        raise RuntimeError(f"Post-verify failed: {len(residual)} rows still non-canonical")

    summary["db_writes"]=writes
    summary["post_verify"]="PASS"
    summary["remaining_noncanonical"]=0
    out=REPORTS_DIR/"administrative_normalization_guarded_v1_apply.json"
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print("[OUT]",out)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
