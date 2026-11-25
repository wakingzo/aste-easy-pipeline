#!/usr/bin/env python3
"""Daily bounded PVP digital-text archiver (no OCR, no AI, no historical backfill).

Processes one notice per new PVP auction, from a DB-side service-role queue.
This can run after daily import. All failures leave eligible rows retryable:
the queue tracks missing rows plus transient failures after an 18-hour delay.
No modification to listings or attachment catalog.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import time

import requests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0,str(SRC))
from importer.pvp_notice_enrichment_v1 import fetch_pdf, safe_url
from audit.pvp_document_text_smoketest_v1 import extract_digital_text

VERSION = "pvp_notice_text_daily_v1"
REPORTS = ROOT / "runtime" / "reports"
LOW_WATERMARK_BYTES = 430_000_000  # 430 MB: leave ~70 MB of Free 500 MB quota.
MAX_BATCH = 500
MAX_DURATION = 900
ROW_KEYS = {"allegato_id","pvp_id","stato_estrazione","testo_prima_pagina",
            "testo_completo","numero_pagine","sha256_pdf","parser_versione",
            "errore_tipo","estratto_il","aggiornato_il"}


def supabase(session, method, base, key, resource, *, params=None, payload=None):
    if not base.startswith("https://"):
        raise ValueError("Supabase URL must be HTTPS")
    headers={"apikey":key,"Authorization":"Bearer "+key, "Accept":"application/json"}
    if payload is not None:
        headers["Content-Type"]="application/json"
        # RPC endpoints must return their JSON payloads. Only table upserts
        # need Prefer return=minimal and conflict resolution.
        if resource == "listing_allegati_testo":
            headers["Prefer"]="resolution=merge-duplicates,return=minimal"
    response=session.request(method,base.rstrip("/")+"/rest/v1/"+resource,
        params=params,json=payload,headers=headers,timeout=(10,45),
        allow_redirects=False)
    if not response.ok:
        raise RuntimeError(f"Supabase HTTP {response.status_code} {resource}")
    return response.json() if response.content else None


def queue(session,base,key,limit):
    rows=supabase(session,"POST",base,key,"rpc/aste_pending_notice_text_v1",
        payload={"p_limit":limit})
    if not isinstance(rows,list):
        raise RuntimeError("Queue expected a JSON list")
    return rows


def database_size(session,base,key):
    n=supabase(session,"POST",base,key,"rpc/aste_notice_storage_bytes_v1",
               payload={})
    if not isinstance(n,int) or n<0:
        raise RuntimeError("Database size RPC returned invalid value")
    return n


def normalize_error(error):
    msg=str(error).lower()
    if "encrypted pdf" in msg:
        return "unsupported"
    if "pdf metadata size" in msg or "pdf response size" in msg or "pdf stream too large" in msg or "exceeds 256" in msg or "unsupported page count" in msg:
        return "too_large"
    if "404" in msg or "410" in msg:
        return "not_available"
    if "not pdf" in msg or "redirect blocked" in msg or "unapproved" in msg:
        return "unsupported"
    return "error"


def process_one(db,pvp,base,key,row,*,apply):
    """Return compact metadata, never log document text or tokens."""
    pid=str(row.get("pvp_id") or "")
    aid=str(row.get("allegato_id") or "")
    if not pid or not aid or not safe_url(row.get("resource_url")):
        raise ValueError("Malformed queue row or unapproved PVP host")
    data = fetch_pdf(pvp,row["resource_url"],row.get("size_bytes"))
    try:
        extracted=extract_digital_text(data)
        status=extracted["status"]
        error_kind=None
    except Exception as exc:
        # Retain error status, not the potentially sensitive PDF text.
        extracted={"first":None,"full":None,"pages":None,
                   "parsed_experiment":None,"full_text_bytes":0}
        status=normalize_error(exc)
        error_kind=type(exc).__name__

    payload={
        "allegato_id":aid,
        "pvp_id":pid,
        "stato_estrazione":status,
        "testo_prima_pagina":extracted["first"],
        "testo_completo":extracted["full"],
        "numero_pagine":extracted["pages"],
        "sha256_pdf":sha256(data).hexdigest(),
        "parser_versione":"pypdf_notice_v1_all_pages",
        "errore_tipo":error_kind,
        "estratto_il":datetime.now(timezone.utc).isoformat(),
        "aggiornato_il":datetime.now(timezone.utc).isoformat(),
    }
    if set(payload)!=ROW_KEYS:
        raise RuntimeError("Unexpected payload columns")
    if apply:
        supabase(db,"POST",base,key,"listing_allegati_testo",
            params={"on_conflict":"allegato_id"},payload=payload)
        # Verify roundtrip without sending text to logs.
        verified=supabase(db,"GET",base,key,"listing_allegati_testo",
            params={"select":"allegato_id,pvp_id,stato_estrazione,numero_pagine,sha256_pdf,"
                     "testo_completo",
                    "allegato_id":"eq."+aid,"limit":"2"})
        if not isinstance(verified,list) or len(verified)!=1:
            raise AssertionError("Text write verification: missing/duplicate")
        saved=verified[0]
        for col in ("allegato_id","pvp_id","stato_estrazione","numero_pagine","sha256_pdf"):
            if saved.get(col)!=payload.get(col):
                raise AssertionError("Text write verification mismatch: "+col)
        if sha256((saved.get("testo_completo") or "").encode("utf-8")).hexdigest()!=sha256((extracted["full"] or "").encode("utf-8")).hexdigest():
            raise AssertionError("Text write verification: content mismatch")
    return {
        "pvp_id":pid,"allegato_id":aid,"status":status,
        "page_count":extracted["pages"],
        "pdf_bytes":len(data),"text_bytes":extracted["full_text_bytes"],
        "experiment_found":extracted.get("parsed_experiment"),
        "verified":bool(apply),
    }


def run(*,apply=False,max_items=MAX_BATCH,max_seconds=MAX_DURATION):
    if not 0 <= max_items <= MAX_BATCH:
        raise ValueError(f"max-items outside 0..{MAX_BATCH}")
    if not 30 <= max_seconds <= 900:
        raise ValueError("max-seconds outside 30..900")
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]
    if apply and os.environ.get("ALLOW_PVP_TEXT_DAILY_WRITE")!="YES_NEW_ONLY":
        raise PermissionError("Daily document text write opt-in missing")

    started=time.monotonic()
    outcomes=[]
    counts=Counter()
    stop_reason=None
    db_bytes_before=None
    db_bytes_after=None
    with requests.Session() as db, requests.Session() as pvp:
        db_bytes_before=database_size(db,base,key)
        if db_bytes_before >= LOW_WATERMARK_BYTES:
            stop_reason="database_size_guard"
            candidates=[]
        else:
            candidates=queue(db,base,key,max_items)
        for i,row in enumerate(candidates,1):
            if time.monotonic()-started >= max_seconds:
                stop_reason="time_budget"
                break
            if i == 1 or i % 10 == 0:
                # An explicit real DB size read, not local estimates.
                now_size=database_size(db,base,key)
                if now_size >= LOW_WATERMARK_BYTES:
                    stop_reason="database_size_guard"
                    break
            pid=str(row.get("pvp_id") or "")
            try:
                item=process_one(db,pvp,base,key,row,apply=apply)
                counts[item["status"]]+=1
                outcomes.append(item)
            except Exception as exc:
                # A download or transport failure leaves the row absent and
                # therefore eligible for retry in the next daily run.
                item={"pvp_id":pid,"status":"retry_next_daily",
                      "error_type":type(exc).__name__}
                counts["retry_next_daily"]+=1
                outcomes.append(item)
            time.sleep(0.1)
        db_bytes_after=database_size(db,base,key)

    report={
        "version":VERSION,"mode":"APPLY" if apply else "PREVIEW",
        "created_at_utc":datetime.now(timezone.utc).isoformat(),
        "cutoff":"2026-11-24T19:42:41+00:00",
        "database_bytes_before":db_bytes_before,
        "database_bytes_after":db_bytes_after,
        "guard_bytes":LOW_WATERMARK_BYTES,
        "candidates":len(candidates),"processed":len(outcomes),
        "counts":dict(counts),"stop_reason":stop_reason,
        "duration_seconds":round(time.monotonic()-started,2),
        "results":outcomes,
        "no_ocr":True,"no_history_backfill":True,
    }
    REPORTS.mkdir(parents=True,exist_ok=True)
    destination=REPORTS / ("pvp_document_text_daily_"+
                          datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")+".json")
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("[PVP DAILY TEXT] "+json.dumps({
        "mode":report["mode"],"candidates":len(candidates),"processed":len(outcomes),
        "counts":dict(counts),"stop_reason":stop_reason,
        "db_before":db_bytes_before,"db_after":db_bytes_after,
        "report":str(destination)},ensure_ascii=False),flush=True)
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--max-items",type=int,default=MAX_BATCH)
    parser.add_argument("--max-seconds",type=int,default=MAX_DURATION)
    options=parser.parse_args()
    run(apply=options.apply,max_items=options.max_items,max_seconds=options.max_seconds)
