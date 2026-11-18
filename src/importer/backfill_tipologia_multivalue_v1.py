#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-off safe backfill of normalized multi-value PVP tipologia.

Default is preview-only. With --apply, updates ONLY public.listings.tipologia
for existing pvp_id values found in the current RAW PVP payload.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
for _p in (SRC, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pvp_import_consolidated_preview as importer
from aste_paths import REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env

VERSION = "tipologia_multivalue_backfill_v1"


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def load_desired_tipologie():
    desired = {}
    duplicates = 0
    if not importer.IN_RAW.is_file():
        raise RuntimeError(f"RAW PVP file missing: {importer.IN_RAW}")

    with importer.IN_RAW.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            pid = importer.clean(obj.get("pvp_id"))
            if not pid:
                continue
            search_item = obj.get("search_item") or {}
            detail_body = obj.get("detail_body") or {}
            value = importer.pick_tipologie(detail_body, search_item, "")
            if not value:
                continue
            if pid in desired:
                duplicates += 1
                if desired[pid] != value:
                    raise RuntimeError(f"Conflicting RAW tipologia for pvp_id={pid}")
            desired[pid] = value
    return desired, duplicates


def fetch_existing(base, key, page_size=1000):
    rows = {}
    offset = 0
    while True:
        params = urlencode({
            "select": "pvp_id,tipologia",
            "order": "pvp_id.asc",
            "limit": str(page_size),
            "offset": str(offset),
        })
        req = Request(
            base.rstrip("/") + "/rest/v1/listings?" + params,
            method="GET",
            headers={
                "apikey": key,
                "Authorization": "Bearer " + key,
                "Accept": "application/json",
            },
        )
        with urlopen(req, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8") or "[]")
        if not isinstance(data, list):
            raise RuntimeError("Unexpected listings response")
        if not data:
            break
        for row in data:
            pid = str(row.get("pvp_id") or "").strip()
            if pid:
                rows[pid] = importer.clean(row.get("tipologia"))
        if len(data) < page_size:
            break
        offset += len(data)
    return rows


def patch_group(base, key, pvp_ids, tipologia):
    if not pvp_ids:
        return
    filt = "in.(" + ",".join(pvp_ids) + ")"
    params = urlencode({"pvp_id": filt}, safe="(),.")
    payload = json.dumps({"tipologia": tipologia}, ensure_ascii=False).encode("utf-8")
    req = Request(
        base.rstrip("/") + "/rest/v1/listings?" + params,
        data=payload,
        method="PATCH",
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    with urlopen(req, timeout=120) as response:
        response.read()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backfill normalized multi-value tipologia from PVP RAW")
    parser.add_argument("--apply", action="store_true", help="apply DB patches; default is preview only")
    parser.add_argument("--sample", type=int, default=30, help="number of changed examples in the report")
    args = parser.parse_args(argv)

    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE"))
    base = os.environ["SUPABASE_URL"].strip()
    key = os.environ["SUPABASE_SERVICE_ROLE"].strip()

    desired, duplicate_raw = load_desired_tipologie()
    existing = fetch_existing(base, key)

    changes = []
    unchanged = 0
    raw_not_in_db = 0
    for pid, wanted in desired.items():
        if pid not in existing:
            raw_not_in_db += 1
            continue
        current = importer.clean(existing.get(pid))
        if current == wanted:
            unchanged += 1
            continue
        changes.append({
            "pvp_id": pid,
            "before": current,
            "after": wanted,
        })

    by_value = defaultdict(list)
    for change in changes:
        by_value[change["after"]].append(change["pvp_id"])

    applied = 0
    request_count = 0
    if args.apply:
        for value, ids in sorted(by_value.items(), key=lambda item: item[0].casefold()):
            for group in chunked(sorted(ids), 120):
                patch_group(base, key, group, value)
                applied += len(group)
                request_count += 1

    multi_count = sum(1 for item in desired.values() if importer.TIPOLOGIA_SEPARATOR in item)
    canonical_counts = Counter()
    for value in desired.values():
        for part in value.split(importer.TIPOLOGIA_SEPARATOR):
            part = part.strip()
            if part:
                canonical_counts[part] += 1

    report = {
        "version": VERSION,
        "mode": "APPLY" if args.apply else "PREVIEW",
        "raw_tipologie": len(desired),
        "raw_duplicate_rows": duplicate_raw,
        "db_rows": len(existing),
        "raw_not_in_db": raw_not_in_db,
        "unchanged": unchanged,
        "changes": len(changes),
        "multi_tipologia_rows": multi_count,
        "distinct_normalized_tipologie": len(canonical_counts),
        "normalized_tipologia_counts": dict(sorted(canonical_counts.items(), key=lambda item: (-item[1], item[0]))),
        "applied": applied,
        "patch_requests": request_count,
        "sample_changes": changes[:max(0, args.sample)],
    }

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    path = REPORTS_DIR / f"tipologia_multivalue_backfill_v1_{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("[OUT]", path)

    if not args.apply and changes:
        print("PREVIEW ONLY: re-run with --apply after reviewing the report.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
