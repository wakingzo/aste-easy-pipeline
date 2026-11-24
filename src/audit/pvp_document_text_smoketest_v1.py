#!/usr/bin/env python3
"""Small explicit live PVP -> text-only -> Supabase end-to-end test.

Only 3 preselected PVP IDs that already exist in public.listings.
No OCR, original PDFs never persisted, no update to listings or attachment
catalog. Saved text remains private and follows existing ON DELETE CASCADE.
Requires explicit --apply + matching workflow environment opt-in for writes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import uuid

import requests
from pypdf import PdfReader

SRC=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SRC))
from importer.pvp_notice_enrichment_v1 import choose_notice, fetch_pdf
from importer.pvp_notice_text_v1 import extract_attempt_from_first_page_text

TARGET_IDS=("4664833", "4664011", "4664765")
MAX_PAGES=80
MAX_TEXT_BYTES=256*1024
REPORT=Path(__file__).resolve().parents[2]/"runtime"/"reports"/"pvp_document_text_smoketest.json"


def api_request(session, method, base, key, table, *, params=None, payload=None):
    if not base.startswith("https://"):
        raise ValueError("Supabase URL must use https")
    headers={
        "apikey":key, "Authorization":"Bearer "+key,
        "Accept":"application/json",
    }
    if payload is not None:
        headers["Content-Type"]="application/json"
        headers["Prefer"]="resolution=merge-duplicates,return=minimal"
    response=session.request(method,base.rstrip("/")+"/rest/v1/"+table,
                             params=params,headers=headers,json=payload,
                             timeout=(10,45),allow_redirects=False)
    if not response.ok:
        # Do not include response body, which could contain submitted text.
        raise RuntimeError(f"Supabase {method} {table}: HTTP {response.status_code}")
    return response.json() if response.content else None


def extract_digital_text(pdf_bytes):
    reader=PdfReader(BytesIO(pdf_bytes),strict=False)
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF: no text without supported decryption")
    count=len(reader.pages)
    if count<1 or count>MAX_PAGES:
        raise ValueError(f"Unsupported page count: {count}")
    texts=[]
    for page in reader.pages:
        t=(page.extract_text() or "").replace("\x00","")
        texts.append(t)
        if sum(len(v.encode("utf-8")) for v in texts)>MAX_TEXT_BYTES:
            raise ValueError("Extracted text exceeds 256 KiB pilot limit")
    first=texts[0]
    full="\n\n".join(texts)
    if len(full.strip())<40:
        return {"status":"no_text","first":None,"full":None,"pages":count,
                "parsed_experiment":None, "full_text_bytes":0}
    parsed=extract_attempt_from_first_page_text(first)
    return {"status":"ok","first":first,"full":full,"pages":count,
            "parsed_experiment":parsed.get("roman"),
            "parsed_status":parsed.get("status"),
            "full_text_bytes":len(full.encode("utf-8"))}


def get_selected_attachment(session,base,key,pvp_id):
    rows=api_request(session,"GET",base,key,"listing_allegati",
        params={"select":"id,pvp_id,filename,resource_url,size_bytes",
                "pvp_id":"eq."+pvp_id,"limit":"250"})
    if not isinstance(rows,list) or len(rows)>=250:
        raise RuntimeError("Bad or truncated attachment listing")
    doc=choose_notice(rows)
    if doc is None:
        raise ValueError("No eligible notice PDF for ID "+pvp_id)
    if doc["pvp_id"]!=pvp_id:
        raise RuntimeError("Attachment PVP ID mismatch")
    uuid.UUID(doc["id"])
    return doc


def verify_after_write(session,base,key,payload):
    rows=api_request(session,"GET",base,key,"listing_allegati_testo",
        params={"select":"allegato_id,pvp_id,stato_estrazione,testo_prima_pagina,"
                         "testo_completo,numero_pagine,sha256_pdf",
                "allegato_id":"eq."+payload["allegato_id"],"limit":"2"})
    if not isinstance(rows,list) or len(rows)!=1:
        raise AssertionError("Cannot verify exactly one inserted text record")
    row=rows[0]
    for field in ("allegato_id","pvp_id","stato_estrazione",
                  "numero_pagine","sha256_pdf"):
        if row.get(field)!=payload.get(field):
            raise AssertionError("Inserted field mismatch: "+field)
    for field in ("testo_prima_pagina","testo_completo"):
        if row.get(field)!=payload.get(field):
            raise AssertionError("Stored text mismatch: "+field)
    return True


def pilot(apply=False):
    if apply and os.environ.get("ALLOW_PVP_TEXT_SMOKETEST_WRITE")!="YES_ONLY_3_IDS":
        raise PermissionError("Explicit pilot write opt-in missing")
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]
    report={"mode":"APPLY_3_EXISTING_NOTICE_TEXTS" if apply else "PREVIEW_READ_ONLY",
            "sample_ids":list(TARGET_IDS),"created_at":datetime.now(timezone.utc).isoformat(),
            "rows":[]}
    successes=0
    # Separate sessions: no Supabase auth headers ever sent to PVP.
    with requests.Session() as db, requests.Session() as pvp:
        for pid in TARGET_IDS:
            info={"pvp_id":pid}
            try:
                doc=get_selected_attachment(db,base,key,pid)
                data=fetch_pdf(pvp,doc["resource_url"],doc.get("size_bytes"))
                result=extract_digital_text(data)
                payload={
                    "allegato_id":doc["id"],"pvp_id":pid,
                    "stato_estrazione":result["status"],
                    "testo_prima_pagina":result["first"],
                    "testo_completo":result["full"],
                    "numero_pagine":result["pages"],
                    "sha256_pdf":sha256(data).hexdigest(),
                    "parser_versione":"pypdf_notice_v1_fulltext_pilot",
                    "errore_tipo":None,
                    "estratto_il":datetime.now(timezone.utc).isoformat(),
                }
                info.update({
                    "filename":doc["filename"],"attachment_id":doc["id"],
                    "pdf_bytes":len(data),"pdf_sha256":payload["sha256_pdf"],
                    "pages":result["pages"],"extraction_status":result["status"],
                    "text_bytes":result["full_text_bytes"],
                    "first_page_chars":len(result["first"] or ""),
                    "parsed_experiment":result.get("parsed_experiment"),
                    "parsed_status":result.get("parsed_status"),
                    "text_sha256":sha256((result["full"] or "").encode("utf-8")).hexdigest(),
                })
                if apply:
                    api_request(db,"POST",base,key,"listing_allegati_testo",
                                params={"on_conflict":"allegato_id"},payload=payload)
                    verify_after_write(db,base,key,payload)
                    info["db_verified"]=True
                    successes+=1
                else:
                    info["db_verified"]=False
                info["status"]="PASS"
            except Exception as exc:
                info["status"]="FAIL"
                info["error_type"]=type(exc).__name__
                info["error"]=str(exc)[:180]
            report["rows"].append(info)
            print("[NOTICE TEXT TEST] "+json.dumps({
                k:v for k,v in info.items() if k not in ("filename",)},
                ensure_ascii=False),flush=True)
    report["rows_pass"]=sum(x["status"]=="PASS" for x in report["rows"])
    report["rows_fail"]=sum(x["status"]=="FAIL" for x in report["rows"])
    report["rows_db_verified"]=successes
    REPORT.parent.mkdir(parents=True,exist_ok=True)
    REPORT.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print("[NOTICE TEXT TEST] report="+str(REPORT)+" pass="+str(report["rows_pass"]),
          "db_verified="+str(successes),flush=True)
    return 0 if report["rows_fail"]==0 and (not apply or successes==len(TARGET_IDS)) else 1


if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--apply",action="store_true")
    args=ap.parse_args()
    raise SystemExit(pilot(args.apply))
