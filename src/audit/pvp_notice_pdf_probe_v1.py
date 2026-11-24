#!/usr/bin/env python3
"""One-time READ-ONLY PVP notice PDF sample: 40 filename-missing + 10 controls.

GET public.listings and public.listing_allegati, then GET one PVP-hosted
notice PDF per selected listing. No DB writes, no OCR/AI/Google calls,
no PDF storage. Outputs JSON evidence/summary to runtime/reports.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from importer.pvp_notice_text_v1 import read_first_page_pdf
from importer.pvp_sale_attempt_parser_v1 import parse_sale_attempt_from_filenames

MAX_PDF_BYTES = 12 * 1024 * 1024
ALLOWED_HOSTS = {"resource-pvp.giustizia.it"}
USER_AGENT = "AsteEasy-ReadOnly-PDF-Notice-Validation/1.0"


def supabase_get(session, base, token, table, params):
    if not base.startswith("https://"):
        raise RuntimeError("Supabase URL must be HTTPS")
    headers = {"apikey": token, "Authorization": "Bearer " + token}
    resp = session.get(base.rstrip("/") + "/rest/v1/" + table,
                       params=params, headers=headers, timeout=(10, 35))
    resp.raise_for_status()
    obj = resp.json()
    if not isinstance(obj, list):
        raise RuntimeError(f"Supabase {table} expected JSON list")
    return obj


def is_allowed_url(url):
    try:
        u = urlparse(url or "")
        return (u.scheme == "https" and u.hostname in ALLOWED_HOSTS
                and u.username is None and u.password is None and
                (u.port is None or u.port == 443))
    except ValueError:
        return False


def get_pdf_bytes(session, url, known_size=None):
    if not is_allowed_url(url):
        raise ValueError("Unapproved URL host, scheme or port")
    if known_size is not None and int(known_size) > MAX_PDF_BYTES:
        raise ValueError("PDF exceeds size limit in metadata")
    # No cross-host redirects, no auth tokens shared with external source.
    with session.get(url, stream=True, allow_redirects=False,
                     timeout=(10, 25), headers={"User-Agent": USER_AGENT}) as resp:
        if resp.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(f"PDF redirect blocked (HTTP {resp.status_code})")
        resp.raise_for_status()
        length = resp.headers.get("Content-Length")
        if length and int(length) > MAX_PDF_BYTES:
            raise ValueError("PDF exceeds Content-Length limit")
        data = bytearray()
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                data.extend(chunk)
                if len(data) > MAX_PDF_BYTES:
                    raise ValueError("PDF exceeds streaming size limit")
        if not data.startswith(b"%PDF-"):
            raise ValueError("Response is not a PDF")
        return bytes(data)


def notice_score(filename):
    f = (filename or "").lower()
    if not f.endswith(".pdf"):
        return -1
    if any(bad in f for bad in ("perizia", "planimetria", "fotograf", "delega", "ordinanza", "modulo")):
        return -1
    if re.search(r"avvis[oa]?\s*(?:di[_ -]*)?vendita", f):
        return 5
    if "avviso" in f and ("vendita" in f or "asta" in f):
        return 4
    if "avviso" in f:
        return 3
    if "bando" in f:
        return 2
    if "vendita" in f and "avvis" in f:
        return 2
    return -1


def sample_candidates(session, base, token, missing=40, controls=10, lookback=600):
    # Only use existing listings and attachments, not stale PVP search pages.
    listings = supabase_get(
        session, base, token, "listings",
        {"select": "pvp_id,esperimento_vendita", "order": "created_at.desc,pvp_id.desc",
         "limit": str(lookback)},
    )
    if not listings:
        raise RuntimeError("No listings retrieved")
    by_id = {str(r["pvp_id"]):r for r in listings}
    att_by_id = defaultdict(list)
    ids = list(by_id)
    for pos in range(0, len(ids), 35):
        subset = ids[pos:pos+35]
        rows = supabase_get(session, base, token, "listing_allegati",
            {"select": "pvp_id,filename,resource_url,size_bytes",
             "pvp_id": "in.(" + ",".join(subset) + ")", "limit":"2000"})
        if len(rows) >= 2000:
            raise RuntimeError("Attachment page truncated")
        for a in rows:
            att_by_id[str(a["pvp_id"])].append(a)

    chosen = []
    quotas = {"missing": missing, "control": controls}
    selected = Counter()
    seen_notice_signatures = set()
    skipped_duplicate_signatures = 0
    for listing in listings:
        pid = str(listing["pvp_id"])
        all_attachments = att_by_id.get(pid, [])
        matches = [a for a in all_attachments
                   if notice_score(str(a.get("filename") or "")) >= 0
                   and is_allowed_url(a.get("resource_url"))
                   and (a.get("size_bytes") is None or
                        int(a["size_bytes"]) <= MAX_PDF_BYTES)]
        if not matches:
            continue
        # Prefer actual notice titles, smaller files in ties.
        matches.sort(key=lambda a: (-notice_score(a.get("filename")),
                                     int(a.get("size_bytes") or MAX_PDF_BYTES)))
        # One notice can be attached to dozens of lots in the same procedure.
        # A document signature (name + byte size) prevents those duplicates
        # from distorting coverage. The final report also counts PDF hashes.
        chosen_notice = next((
            a for a in matches if (
                str(a["filename"]).strip().casefold(),
                int(a.get("size_bytes") or -1),
            ) not in seen_notice_signatures
        ), None)
        if chosen_notice is None:
            skipped_duplicate_signatures += 1
            continue
        filename_result = parse_sale_attempt_from_filenames(
            [x.get("filename") or "" for x in all_attachments])
        group = "control" if filename_result["value"] else "missing"
        if selected[group] >= quotas[group]:
            continue
        seen_notice_signatures.add((
            str(chosen_notice["filename"]).strip().casefold(),
            int(chosen_notice.get("size_bytes") or -1),
        ))
        chosen.append({
            "pvp_id": pid, "group": group,
            "old_db_attempt": listing.get("esperimento_vendita"),
            "filename_attempt": filename_result["value"],
            "filename_multiple_values": filename_result["multiple_values"],
            "filename": chosen_notice["filename"],
            "url": chosen_notice["resource_url"],
            "size_bytes": chosen_notice.get("size_bytes"),
            "attachment_count": len(all_attachments),
        })
        selected[group] += 1
        if all(selected[k] >= quotas[k] for k in quotas):
            break
    return chosen, {"sample_target": quotas, "selected": dict(selected),
                    "lookback": len(listings),
                    "deduplication": "casefold(filename)+size_bytes",
                    "duplicate_signatures_skipped": skipped_duplicate_signatures}


def run(missing=40, controls=10, lookback=600):
    base = os.environ.get("SUPABASE_URL", "").strip()
    token = os.environ.get("SUPABASE_SERVICE_ROLE", "").strip()
    if not base or not token:
        raise RuntimeError("Missing Supabase URL and service role; no fallback to anonymous")
    started = time.monotonic()
    with requests.Session() as session:
        candidates, selection = sample_candidates(
            session, base, token, missing, controls, lookback)
        outcomes = []
        for i,c in enumerate(candidates,1):
            item = {k:v for k,v in c.items() if k not in {"url"}}
            try:
                content = get_pdf_bytes(session, c["url"], c["size_bytes"])
                item["pdf_sha256"] = hashlib.sha256(content).hexdigest()
                item["download_bytes"] = len(content)
                item.update(read_first_page_pdf(content))
                if item.get("roman") and c["filename_attempt"]:
                    item["filename_pdf_agree"] = (
                        item["roman"] == c["filename_attempt"])
                else:
                    item["filename_pdf_agree"] = None
            except Exception as e:
                item.update({"status":"error", "error_type":type(e).__name__,
                             "error":str(e)[:180]})
            outcomes.append(item)
            if i % 10 == 0 or i == len(candidates):
                print(f"[PVP PDF READ ONLY] processed {i}/{len(candidates)}", flush=True)
            # Keep request rate modest; no retries for rejected/blocked PDFs.
            time.sleep(0.15)

    counts = Counter(x["status"] for x in outcomes)
    grp = {g:dict(Counter(x["status"] for x in outcomes if x["group"]==g))
           for g in ("missing","control")}
    comparisons = [x["filename_pdf_agree"] for x in outcomes
                   if x.get("filename_pdf_agree") is not None]
    unique_pdf_hashes = {x["pdf_sha256"] for x in outcomes if x.get("pdf_sha256")}
    doc_types = Counter(x.get("document_type") or "(unavailable)" for x in outcomes)
    summary = {
        "mode": "READ_ONLY_SINGLE_SAMPLE_NO_DB_WRITES_NO_OCR_NO_AI",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "selection": selection,
        "counts": dict(counts), "by_group": grp,
        "unique_pdf_hashes": len(unique_pdf_hashes),
        "duplicate_pdf_hash_count": sum(bool(x.get("pdf_sha256")) for x in outcomes)-len(unique_pdf_hashes),
        "document_types": dict(doc_types),
        "filename_pdf_comparable": len(comparisons),
        "filename_pdf_equal": sum(x is True for x in comparisons),
        "filename_pdf_different": sum(x is False for x in comparisons),
        "duration_seconds": round(time.monotonic()-started,2),
        "caution": ("The absence of 'ESPERIMENTO' on page 1 is not evidence of "
                    "a first attempt. Procedure type and document are not legally verified."),
        "rows": outcomes,
    }
    target = ROOT/"runtime"/"reports"/(
        "pvp_notice_pdf_probe_"+datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")+".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n",encoding="utf-8")
    print("[PVP PDF READ ONLY] "+json.dumps({
        "file":str(target), "selection":selection, "counts":dict(counts),
        "by_group":grp, "comparable":len(comparisons),
        "agree":sum(x is True for x in comparisons),
        "disagree":sum(x is False for x in comparisons),
        "duration_seconds":summary["duration_seconds"]},ensure_ascii=False),flush=True)
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--missing", type=int,default=40)
    ap.add_argument("--controls",type=int,default=10)
    ap.add_argument("--lookback",type=int,default=600)
    args = ap.parse_args()
    if not 0 <= args.missing <= 50 or not 0 <= args.controls <= 20:
        ap.error("Read-only sample cap: <=50 missing and <=20 control documents")
    if not 100 <= args.lookback <= 1000:
        ap.error("lookback must be between 100 and 1000")
    run(missing=args.missing, controls=args.controls, lookback=args.lookback)
