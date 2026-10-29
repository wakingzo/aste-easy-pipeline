#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy v3.11.5 - reconcile Google usage counters only.

ZERO Google HTTP, ZERO Supabase reads/writes, ZERO ANNCSU scan.
Loads the existing Google core only to ask it which persistent API_USAGE_FILE it
actually uses, then applies the exact-known-bug reconciliation from live guard v3.11.5.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SRC / "geocoding") not in sys.path:
    sys.path.insert(0, str(SRC / "geocoding"))

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env
import google_budget_v3_10 as budget
import google_live_guard_v3_11 as live
import google_preview_v3 as preview

VERSION = "google_usage_reconcile_v3_11_5"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reconcile Aste Easy Google monthly counters; no HTTP, no DB, no ANNCSU scan")
    ap.add_argument("--monthly-cap", type=int, default=9000)
    ap.add_argument("--monthly-used", type=int, help="Optional conservative operator floor")
    args = ap.parse_args(argv)
    if args.monthly_cap < 1:
        ap.error("--monthly-cap must be positive")
    if args.monthly_used is not None and args.monthly_used < 0:
        ap.error("--monthly-used cannot be negative")

    # Loading the key/env and importing the core makes no network request. We use
    # the core only as the authority for its exact API_USAGE_FILE path.
    load_project_env(required=("GOOGLE_GEOCODING_API_KEY",))
    os.environ["MAX_MONTH_GEOCODING_CALLS"] = str(args.monthly_cap)
    os.environ.pop("MONTH_USAGE_OFFSET_GEOCODING", None)
    os.environ["MAX_RUN_GEOCODING_CALLS"] = "1"
    os.environ["PLACES_AUTOCOMPLETE_FALLBACK"] = "0"
    os.environ["ADDRESS_VALIDATION_FALLBACK"] = "0"
    os.environ["HTTP_RETRIES"] = "1"
    os.environ["API_USAGE_SAVE_EVERY"] = "1"

    g = preview.load_google_core(1)
    usage_path = Path(g.API_USAGE_FILE).resolve()
    month = budget._month_key()
    core_before = live.read_core_month_usage(path=usage_path, month=month)
    ledger_before = budget.read_ledger(month=month)

    reconciled = live.reconcile_usage_state(
        monthly_cap=args.monthly_cap,
        monthly_used_override=args.monthly_used,
        usage_path=usage_path,
    )

    core_after = live.read_core_month_usage(path=usage_path, month=month)
    ledger_after = budget.read_ledger(month=month)
    delta = int(ledger_after.get("used", 0) or 0) - int(core_after.get("effective", 0) or 0)
    if delta != 0:
        raise RuntimeError(f"Post-reconcile counter mismatch: ledger-core={delta}")

    out = {
        "version": VERSION,
        "mode": "COUNTER_RECONCILE_ONLY_ZERO_HTTP_ZERO_DB_ZERO_ANNCSU",
        "monthly_cap": args.monthly_cap,
        "core_usage_path": str(usage_path),
        "core_before": core_before,
        "ledger_before": ledger_before,
        "reconciliation": reconciled,
        "core_after": core_after,
        "ledger_after": ledger_after,
        "counter_delta_ledger_minus_core": delta,
        "remaining_after": max(0, args.monthly_cap - max(
            int(core_after.get("effective", 0) or 0), int(ledger_after.get("used", 0) or 0))),
        "google_http_attempts": 0,
        "supabase_writes": 0,
    }

    ensure_runtime_dirs()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    report = Path(REPORTS_DIR) / f"google_usage_reconcile_v3_11_5_{stamp}.json"
    report.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out["report_json"] = str(report)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
