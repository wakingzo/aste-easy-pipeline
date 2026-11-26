#!/usr/bin/env python3
"""Read-only coverage audit for sale attempts in already archived PVP notice text.

Reads only two Supabase tables using the service-role REST API.
Never writes or prints full notice text, contact details, or credentials.
No archive deletion, no listing mutation, no PDF download, no OCR.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import requests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from importer.pvp_notice_text_v1 import extract_attempt_from_first_page_text

PAGE_SIZE = 150
MAX_ROWS = 10000
REPORT = ROOT / "runtime/reports/pvp_notice_experiment_coverage_v1.json"


def get_rows(session, base, key, table, params):
    response = session.get(
        base.rstrip("/") + "/rest/v1/" + table,
        params=params,
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
        },
        timeout=(10, 90),
        allow_redirects=False,
    )
    if not response.ok:
        raise RuntimeError(f"Read-only Supabase GET failed: {table} HTTP {response.status_code}")
    value = response.json()
    if not isinstance(value, list):
        raise TypeError("Unexpected Supabase list response")
    return value


def fetch_texts(session, base, key):
    found = []
    for offset in range(0, MAX_ROWS, PAGE_SIZE):
        batch = get_rows(
            session, base, key, "listing_allegati_testo",
            {
                "select": "pvp_id,stato_estrazione,testo_prima_pagina",
                "order": "pvp_id.asc",
                "limit": PAGE_SIZE,
                "offset": offset,
            },
        )
        found.extend(batch)
        if len(batch) < PAGE_SIZE:
            return found
    raise RuntimeError("Archive exceeds preview safety limit: no partial report")


def fetch_existing(session, base, key, pvp_ids):
    found = {}
    for start in range(0, len(pvp_ids), 80):
        chunk = pvp_ids[start:start + 80]
        if not all(pid.isdigit() for pid in chunk):
            raise ValueError("Invalid PVP identifier in archive")
        rows = get_rows(
            session, base, key, "listings",
            {
                "select": "pvp_id,esperimento_vendita",
                "pvp_id": "in.(" + ",".join(chunk) + ")",
            },
        )
        for item in rows:
            found[str(item["pvp_id"])] = str(item.get("esperimento_vendita") or "").strip()
    return found


def run():
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE"]
    if not base.startswith("https://"):
        raise ValueError("HTTPS required")
    with requests.Session() as session:
        rows = fetch_texts(session, base, key)
        ids = sorted({str(row["pvp_id"]) for row in rows})
        existing = fetch_existing(session, base, key, ids)

    counts = Counter()
    samples = {"missing_recovered": [], "present_conflict": []}
    counts["archived_rows"] = len(rows)
    counts["distinct_pvp_ids"] = len(ids)
    for row in rows:
        pid = str(row["pvp_id"])
        counts["archive_status_" + str(row.get("stato_estrazione") or "unknown")] += 1
        value = existing.get(pid, "")
        has_existing = bool(value and value not in ("-", "—"))
        if pid not in existing:
            counts["missing_parent_listing"] += 1
            continue
        counts["existing_present" if has_existing else "existing_missing"] += 1
        first_page = row.get("testo_prima_pagina") or ""
        parsed = extract_attempt_from_first_page_text(first_page)
        status = parsed["status"]
        counts["parse_" + status] += 1
        counts[("existing_present_" if has_existing else "existing_missing_") + status] += 1
        if status != "explicit":
            continue

        extracted = str(parsed["roman"])
        evidence = (parsed.get("evidence") or [{}])[0].get("text", "")
        if has_existing:
            if value.upper() == extracted:
                counts["existing_confirmed_by_pdf"] += 1
            else:
                counts["existing_disagreement_with_pdf"] += 1
                if len(samples["present_conflict"]) < 20:
                    samples["present_conflict"].append({
                        "pvp_id": pid,
                        "stored": value[:32],
                        "pdf": extracted,
                        "matched_label": evidence[:100],
                    })
        else:
            counts["missing_recovered_explicit"] += 1
            if len(samples["missing_recovered"]) < 20:
                samples["missing_recovered"].append({
                    "pvp_id": pid,
                    "pdf": extracted,
                    "matched_label": evidence[:100],
                })

    denominator = counts["existing_present"] + counts["existing_missing"]
    projected = counts["existing_present"] + counts["missing_recovered_explicit"]
    report = {
        "version": "pvp_notice_experiment_coverage_v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_PREVIEW",
        "database_writes": 0,
        "no_pdf_downloads": True,
        "no_full_text_export": True,
        "counts": dict(sorted(counts.items())),
        "projected_populated_after_explicit_fallback": projected,
        "eligible_rows_with_parent": denominator,
        "projected_population_percent": round(100 * projected / denominator, 2) if denominator else None,
        "samples": samples,
        "interpretation": (
            "This is regex/parser coverage, NOT independently verified correctness. "
            "Potential PDF-versus-PVP disagreements require manual review."
        ),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Console output contains only aggregate counts and no source document text.
    print(json.dumps({k: v for k, v in report.items() if k != "samples"}, ensure_ascii=False))
    print("[OUT]", REPORT)


if __name__ == "__main__":
    run()
