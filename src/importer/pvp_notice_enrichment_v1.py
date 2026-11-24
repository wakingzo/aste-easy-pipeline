#!/usr/bin/env python3
"""Optional filename + first-page PDF enrichment for NEW-ONLY atomic import plans.

No OCR, AI, geocoding, archive downloads or writes to Supabase. Default is
report-only; apply mode changes only esperimento_vendita in NEW in-memory rows.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlparse

import requests

from importer.pvp_notice_text_v1 import read_first_page_pdf
from importer.pvp_sale_attempt_parser_v1 import resolve_sale_attempt

MAX_BYTES = 12 * 1024 * 1024
MAX_HOST = "resource-pvp.giustizia.it"


def notice_score(filename):
    f = str(filename or "").casefold().strip()
    if not f.endswith(".pdf"):
        return -1
    if any(x in f for x in ("perizia", "planimetr", "fotograf", "foto", "delega",
                           "ordinanza", "modulo", "regolamento", "affissione",
                           "pubblicita")):
        return -1
    if re.search(r"avvis\w{0,2}[\s_.-]*(?:di[\s_.-]*)?vendita", f):
        return 8
    if "avviso" in f and ("vendita" in f or "asta" in f):
        return 7
    if "avviso" in f:
        return 6
    if "bando" in f and "vendita" in f:
        return 5
    return 3 if "bando" in f else -1


def safe_url(url):
    try:
        u = urlparse(url or "")
        return (u.scheme == "https" and u.hostname == MAX_HOST
                and u.username is None and u.password is None
                and (u.port is None or u.port == 443))
    except (ValueError, TypeError):
        return False


def choose_notice(attachments):
    candidates = []
    for a in attachments:
        score = notice_score(a.get("filename"))
        if score < 0 or not safe_url(a.get("resource_url")):
            continue
        try:
            size = int(a.get("size_bytes") or MAX_BYTES)
        except (ValueError, TypeError):
            continue
        if size > MAX_BYTES:
            continue
        candidates.append((-score, size, a))
    return sorted(candidates, key=lambda v: (v[0], v[1]))[0][2] if candidates else None


def fetch_pdf(session, url, size_bytes=None):
    if not safe_url(url):
        raise ValueError("Unapproved URL")
    if size_bytes is not None and int(size_bytes) > MAX_BYTES:
        raise ValueError("PDF metadata size exceeded")
    with session.get(url, stream=True, allow_redirects=False,
                     timeout=(10, 25), headers={
                         "User-Agent": "AsteEasy-PVP-NoticeParser/1.0"}) as response:
        if response.status_code in (301, 302, 303, 307, 308):
            raise ValueError("Redirect blocked")
        response.raise_for_status()
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_BYTES:
            raise ValueError("PDF response size exceeded")
        buf = bytearray()
        for chunk in response.iter_content(65536):
            if chunk:
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    raise ValueError("PDF stream too large")
        if not buf.startswith(b"%PDF-"):
            raise ValueError("Response is not PDF")
        return bytes(buf)


def read_delta(path):
    by_id = {}
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            pid = str(obj.get("pvp_id") or "")
            if not pid or pid in by_id:
                raise ValueError("Duplicate or missing PVP ID in delta RAW")
            by_id[pid] = obj
    return by_id


def assess(listing, attachments, raw, session):
    """Return (audit, safe new attempt or None); never make DB writes."""
    pid = str(listing["pvp_id"])
    if str(raw.get("pvp_id") or "") != pid:
        raise ValueError("RAW PVP ID mismatch")
    names = [str(a.get("filename") or "") for a in attachments]
    sale = resolve_sale_attempt(
        raw.get("detail_body") or {}, raw.get("search_item") or {},
        extra_filenames=names)
    doc = choose_notice(attachments)
    audit = {
        "pvp_id":pid, "existing_value":listing.get("esperimento_vendita"),
        "api_value":sale["api_value"] or None,
        "filename_value":sale["parsed_value"] or None,
        "filename_conflict":sale["multiple_filename_values"],
        "notice_filename":doc.get("filename") if doc else None,
        "pdf_status":"pending" if doc else "no_notice",
        "pdf_value":None, "possible_previous_sale":None, "document_type":None,
        "decision":"keep_existing",
    }
    if not doc:
        audit["decision"]="filename_only"
        return audit, None
    try:
        pdf = fetch_pdf(session, doc["resource_url"], doc.get("size_bytes"))
        found = read_first_page_pdf(pdf)
        audit.update({
            "pdf_status":found["status"], "pdf_value":found.get("roman"),
            "document_type":found.get("document_type"),
            "possible_previous_sale":found.get("possible_previous_sale"),
            "page":found.get("page"),
            "excerpt":(found.get("evidence") or [{}])[0].get("text"),
            "sha256":hashlib.sha256(pdf).hexdigest(), "size_bytes":len(pdf),
        })
    except Exception as e:
        audit.update({"pdf_status":"error", "decision":"download_or_parse_failed",
                      "error_type":type(e).__name__, "error":str(e)[:160]})
        return audit, None

    # The structured API takes priority; an explicit PDF can corroborate it
    # but never override it. A PDF/name disagreement requires review.
    if sale["api_value"]:
        audit["decision"]="api_priority"
        audit["pdf_api_disagree"] = bool(
            found.get("number") is not None and sale["api_number"] is not None
            and found["number"] != sale["api_number"])
        return audit, None
    if found["status"] == "ambiguous":
        audit["decision"]="ambiguous_pdf_review"
        return audit, None
    value = found.get("roman")
    if not value:
        audit["decision"]=("possible_prior_sale_review"
                          if found.get("possible_previous_sale")
                          else "no_explicit_value")
        return audit, None
    if (sale["multiple_filename_values"] or
            (sale["parsed_value"] and sale["parsed_value"] != value)):
        audit["decision"]="filename_pdf_disagree_review"
        return audit, None
    if value == str(listing.get("esperimento_vendita") or ""):
        audit["decision"]="confirmed"
        return audit, None
    audit["decision"]="explicit_pdf_override"
    return audit, value


def enrich_new_plan(plan, *, delta_raw, report_dir, mode="report",
                    max_pdfs=150, session=None, pause=0.15):
    """Transform only in-memory NEW rows. Any single PDF failure is non-fatal."""
    if mode not in ("report", "apply") or not 0 <= max_pdfs <= 300:
        raise ValueError("Invalid mode or PDF budget")
    records = plan["records"]
    raw = read_delta(delta_raw)
    pids = [str(r[0]["pvp_id"]) for r in records]
    if len(set(pids)) != len(pids) or set(pids) != set(raw):
        raise RuntimeError("New-only plan does not match delta RAW; abort before GET")
    owns_session = session is None
    if owns_session:
        session = requests.Session()
    tally = Counter()
    report_rows, updated = [], []
    num_pdf = 0
    started = time.monotonic()
    try:
        for row, people, attachments, locations in records:
            pid = str(row["pvp_id"])
            wants_pdf = choose_notice(attachments) is not None
            # Independent wall-clock guard: a slow PVP host must never
            # hold the daily pipeline indefinitely. No more than 180 s
            # are spent starting new downloads in this phase.
            elapsed = time.monotonic() - started
            if wants_pdf and (num_pdf >= max_pdfs or elapsed >= 180):
                item, override = ({"pvp_id":pid, "decision":"budget_exhausted",
                                   "pdf_status":"not_attempted"}, None)
            else:
                num_pdf += int(wants_pdf)
                try:
                    item,override = assess(row,attachments,raw[pid],session)
                except Exception as e:
                    item,override = ({"pvp_id":pid, "decision":"internal_error_keep",
                                       "error_type":type(e).__name__},None)
                if wants_pdf and pause > 0:
                    time.sleep(pause)
            proposed_row = dict(row)
            if override:
                item["suggested_value"] = override
                item["applied"] = mode == "apply"
                if mode == "apply":
                    proposed_row["esperimento_vendita"] = override
                    tally["changed_new_plan_rows"] += 1
            tally[item["decision"]] += 1
            report_rows.append(item)
            updated.append((proposed_row,people,attachments,locations))
    finally:
        if owns_session:
            session.close()
    report = {
        "mode":mode.upper()+"_NEW_ONLY", "created_at":datetime.now(timezone.utc).isoformat(),
        "eligible_new":len(records), "pdf_requests":num_pdf,
        "counts":dict(tally), "elapsed_seconds":round(time.monotonic()-started,2),
        "note":"No OCR/AI. No fallback I. Never updates existing records.",
        "results":report_rows,
    }
    target = Path(report_dir)/(
        "pvp_new_notice_pdf_"+datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")+".json")
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("[PVP NOTICE] "+json.dumps({
        "mode":mode,"eligible_new":len(records),"pdf_requests":num_pdf,
        "counts":dict(tally),"report":str(target)},ensure_ascii=False),flush=True)
    result = dict(plan)
    result["records"] = updated
    result["notice_report"] = str(target)
    result["notice_counts"] = dict(tally)
    return result
