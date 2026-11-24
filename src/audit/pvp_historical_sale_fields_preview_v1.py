#!/usr/bin/env python3
"""READ-ONLY full-history preview for PVP sale attempt and promotional lot codes.

Reads public.listings and public.listing_allegati with Supabase REST GET only.
Recomputes sale attempts using the production contextual parser, compares to
stored fields, and emits a full proposed-diff JSONL + compact summary JSON.
NEVER updates Supabase. No live PVP or Google requests. No geocoding changes.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from importer.pvp_sale_attempt_parser_v1 import parse_sale_attempt_from_filenames
from importer.pvp_sale_fields_v1 import pvp_lot_number

EXPECTED_ROWS = 21740
EXPECTED_BACKUP_SHA256 = "6cf6ce9265b70dc0f286de8c8e19f07c6617ac9b447d843cdf264e61cd0d749c"
PAGE = 1000
MAX_WORKERS = 6


def request_json(base: str, key: str, table: str, params: dict, want_count=False):
    url = base.rstrip("/") + "/rest/v1/" + table + "?" + urlencode(params)
    headers = {"apikey": key, "Authorization": "Bearer " + key,
               "Accept": "application/json"}
    if want_count:
        headers["Prefer"] = "count=exact"
    for attempt in range(5):
        try:
            with urlopen(Request(url, method="GET", headers=headers), timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content_range = resp.headers.get("Content-Range", "")
            if not isinstance(data, list):
                raise RuntimeError(f"Unexpected non-list for {table}")
            return data, content_range
        except (HTTPError, URLError, TimeoutError) as exc:
            if isinstance(exc, HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                raise
            if attempt == 4:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("Unreachable")


def read_table(base, key, table, fields, order="pvp_id.asc"):
    first, content_range = request_json(
        base, key, table,
        {"select": ",".join(fields), "order": order, "limit": "1", "offset": "0"},
        want_count=True,
    )
    if "/" not in content_range or not content_range.rsplit("/", 1)[1].isdigit():
        raise RuntimeError(f"Cannot confirm exact count of {table}: {content_range!r}")
    count = int(content_range.rsplit("/", 1)[1])
    offsets = list(range(0, count, PAGE))
    if not offsets:
        return []
    def fetch(offset):
        data, _ = request_json(
            base, key, table,
            {"select": ",".join(fields), "order": order,
             "limit": str(PAGE), "offset": str(offset)},
        )
        return offset, data
    pages = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch, offset) for offset in offsets]
        for fut in as_completed(futures):
            offset, data = fut.result()
            if len(data) != min(PAGE, count-offset):
                raise RuntimeError(f"{table} offset {offset} returned {len(data)} rows")
            pages[offset] = data
    all_rows = [row for offset in offsets for row in pages[offset]]
    if len(all_rows) != count:
        raise RuntimeError(f"{table} row count mismatch {len(all_rows)} vs {count}")
    latest, latest_range = request_json(
        base, key, table,
        {"select": "pvp_id", "order": order, "limit": "1"}, want_count=True,
    )
    latest_count = int(latest_range.rsplit("/", 1)[1])
    if latest_count != count:
        raise RuntimeError(f"{table} changed during preview: count {count} -> {latest_count}")
    print(f"[HISTORICAL READ ONLY] {table}: {count} rows", flush=True)
    return all_rows


def norm(value):
    return str(value or "").strip()


def main():
    base = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE", "").strip()
    if not base or not key:
        raise RuntimeError("Missing SUPABASE_URL/SUPABASE_SERVICE_ROLE")
    listings = read_table(base, key, "listings",
                          ["pvp_id", "lotto", "esperimento_vendita", "descrizione"])
    attachments = read_table(base, key, "listing_allegati",
                             ["pvp_id", "filename"], order="pvp_id.asc,id.asc")
    if len(listings) != EXPECTED_ROWS:
        raise RuntimeError(
            f"Expected frozen snapshot {EXPECTED_ROWS} listings, got {len(listings)};"
            " do a fresh backup and rerun before considering historical updates"
        )
    ids = [str(x.get("pvp_id") or "") for x in listings]
    if len(set(ids)) != len(ids) or any(not pid for pid in ids):
        raise RuntimeError("Duplicate/empty PVP ID in listings export")
    filenames = defaultdict(list)
    for a in attachments:
        filenames[str(a["pvp_id"])].append(str(a["filename"]))
    if set(ids) != set(filenames):
        raise RuntimeError(
            f"Mismatch IDs: {len(set(ids)-set(filenames))} without attachments, "
            f"{len(set(filenames)-set(ids))} orphan attachment IDs"
        )

    # Independent sha256 reproduction of the original read-only backup.
    sha = hashlib.sha256()
    for row in listings:
        obj = {key: row.get(key) for key in ("pvp_id", "lotto", "esperimento_vendita")}
        sha.update((json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    db_sha = sha.hexdigest()
    same_backup = (db_sha == EXPECTED_BACKUP_SHA256)

    counts = Counter()
    old_counts = Counter()
    proposal_counts = Counter()
    examples = defaultdict(list)
    proposed = []
    for row in listings:
        pid = str(row["pvp_id"])
        old_attempt = norm(row.get("esperimento_vendita"))
        old_lotto = norm(row.get("lotto"))
        extracted = parse_sale_attempt_from_filenames(filenames[pid])
        suggested_attempt = extracted["value"]
        suggested_lot = pvp_lot_number(
            {"lotto": {"codLotto": old_lotto}}, {}, norm(row.get("descrizione"))
        )
        conflicting = bool(extracted["multiple_values"])
        if conflicting:
            attempt_action = "REVIEW_MULTIPLE"
        elif old_attempt == suggested_attempt:
            attempt_action = "KEEP_MATCH" if old_attempt else "KEEP_EMPTY"
        elif suggested_attempt:
            attempt_action = "CORRECT_WITH_EVIDENCE"
        elif old_attempt:
            attempt_action = "CLEAR_UNSUPPORTED_CANDIDATE"
        else:
            attempt_action = "KEEP_EMPTY"
        lot_action = "CLEAR_PROMOTIONAL_CODE" if old_lotto and not suggested_lot else "KEEP"

        counts["scanned"] += 1
        counts["filename_attempt_found"] += bool(suggested_attempt)
        counts["file_attempt_unresolved"] += not bool(suggested_attempt)
        counts["multiple_values"] += conflicting
        counts["no_attachment"] += not bool(filenames[pid])
        counts["promotional_lot_candidates"] += (lot_action == "CLEAR_PROMOTIONAL_CODE")
        old_counts[old_attempt or "(empty)"] += 1
        proposal_counts[attempt_action] += 1
        if attempt_action != "KEEP_EMPTY" or lot_action != "KEEP":
            item = {
                "pvp_id": pid, "old_attempt": old_attempt,
                "proposed_attempt": suggested_attempt,
                "attempt_action": attempt_action,
                "old_lotto": old_lotto,
                "proposed_lotto": suggested_lot,
                "lotto_action": lot_action,
                "multiple_values": conflicting,
                "evidence": extracted["evidence"],
            }
            proposed.append(item)
            if len(examples[attempt_action]) < 15:
                examples[attempt_action].append(item)
            if lot_action != "KEEP" and len(examples[lot_action]) < 15:
                examples[lot_action].append(item)

    counts["attempt_corrections_candidate"] = proposal_counts["CORRECT_WITH_EVIDENCE"]
    counts["attempt_clears_candidate"] = proposal_counts["CLEAR_UNSUPPORTED_CANDIDATE"]
    counts["conflicts_to_review"] = proposal_counts["REVIEW_MULTIPLE"]
    counts["lot_clears_candidate"] = counts["promotional_lot_candidates"]
    report = {
        "mode": "READ_ONLY_NO_DB_WRITES_NO_PVP_NO_GOOGLE",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expected_backup_rows": EXPECTED_ROWS,
        "current_rows": len(listings),
        "expected_backup_sha256": EXPECTED_BACKUP_SHA256,
        "current_backup_compatible_sha256": db_sha,
        "snapshot_matches_original_backup": same_backup,
        "counts": dict(counts),
        "attempt_action_counts": dict(proposal_counts),
        "original_attempt_counts": dict(old_counts),
        "examples": dict(examples),
        "critical_warning": (
            "A missing filename match does NOT prove the historical attempt was first "
            "or false. CLEAR_UNSUPPORTED_CANDIDATE is for human review only. "
            "No historical API numeEsperimento values were available in Supabase. "
            "This is a preview, NOT permission to mass-clear fields."
        ),
    }
    outdir = ROOT / "runtime" / "reports"
    outdir.mkdir(parents=True, exist_ok=True)
    stem = "pvp_historical_sale_fields_preview_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    full = outdir / (stem + "_diffs.jsonl")
    summary = outdir / (stem + ".json")
    with full.open("x", encoding="utf-8") as fh:
        for item in proposed:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    report["diffs_file"] = str(full)
    report["diff_rows_including_supported_values"] = len(proposed)
    summary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("[HISTORICAL PREVIEW] " + json.dumps(
        {"backup_matches": same_backup, "rows": len(listings),
         "attachments": len(attachments), "counts": report["counts"],
         "actions": report["attempt_action_counts"], "report": str(summary),
         "diffs": str(full)}, ensure_ascii=False), flush=True)
    if not same_backup:
        print("[HISTORICAL PREVIEW] WARNING original backup SHA differs;"
              " NO historical apply should be attempted from this snapshot", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
