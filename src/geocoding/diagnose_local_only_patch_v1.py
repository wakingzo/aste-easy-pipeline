#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnose an empty conditional PATCH result using Supabase GET only.

Does NOT PATCH, import Google code, access Google APIs, or alter Supabase.
Tests filters produced by the installed local_only_repair_touched_v1.py,
without modifying that script. Never writes or prints API credentials.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import local_only_repair_touched_v1 as repair
from project_env import load_project_env

FIELDS = (
    "indirizzo", "comune", "sigla_provincia", "cap",
    "latitudine", "longitudine", "geocode_source",
    "geocode_location_type", "geocode_query_used", "geocode_formatted_address",
)


def get_by_filters(base: str, key: str, filters: dict[str, str]) -> bool:
    params = {"select": "pvp_id", **filters}
    url = base.rstrip("/") + "/rest/v1/listings?" + urlencode(params)
    with urlopen(Request(url, headers=repair.db_headers(key)), timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if not isinstance(result, list):
        raise RuntimeError("Supabase GET did not return a list")
    if len(result) > 1:
        raise RuntimeError("Ambiguous result: multiple rows returned")
    return len(result) == 1


def diagnostic(base: str, key: str, pid: str) -> dict:
    row = repair.db_get_one(base, key, pid)  # GET: no writes.
    result = {"pvp_id": pid, "coords_now_present": False}
    if repair.float_checked(row.get("latitudine")) is not None or repair.float_checked(row.get("longitudine")) is not None:
        result["coords_now_present"] = True
        result["outcome"] = "COORDS_ALREADY_PRESENT_CHECK_DB_BEFORE_ANY_RETRY"
        return result
    if any(repair.clean(row.get(x)) for x in
           ("geocode_source", "geocode_location_type", "geocode_query_used", "geocode_formatted_address")):
        result["outcome"] = "GEOCODE_FIELDS_ALREADY_PRESENT_CHECK_DB_BEFORE_ANY_RETRY"
        return result

    id_filter = {"pvp_id": repair.db_filter_value(pid)}
    if not get_by_filters(base, key, id_filter):
        raise RuntimeError(f"ID {pid} not found by exact PostgREST filter")
    filters = {field: repair.db_filter_value(row.get(field)) for field in FIELDS}
    all_filters = {**id_filter, **filters}
    result["all_guards_get_match"] = get_by_filters(base, key, all_filters)
    if result["all_guards_get_match"]:
        result["outcome"] = "GET_ALL_GUARDS_MATCH_BUT_PRIOR_PATCH_RETURNED_EMPTY; STOP_AND_INVESTIGATE_WRITE_PATH"
        return result

    failed = []
    for field in FIELDS:
        if not get_by_filters(base, key, {**id_filter, field: filters[field]}):
            value = row.get(field)
            failed.append({
                "field": field,
                "value_class": "SQL_NULL" if value is None else "EMPTY_STRING" if value == "" else "OTHER",
                "filter": filters[field],
            })
    result["single_field_failures"] = failed
    if failed:
        result["outcome"] = "SINGLE_FILTER_MISMATCH"
        return result

    prefix = dict(id_filter)
    for field in FIELDS:
        prefix[field] = filters[field]
        if not get_by_filters(base, key, prefix):
            result["first_combination_failure_at"] = field
            result["outcome"] = "COMBINED_FILTER_MISMATCH"
            return result
    result["outcome"] = "UNEXPLAINED_EMPTY_GET_RECHECK_API_STATE"
    return result


def main() -> None:
    load_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE"))
    base = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE"]
    reports = repair.REPORTS_DIR
    report_file = reports / "local_only_touched_v1_apply_20260917-000056.csv"
    if not report_file.is_file():
        raise SystemExit(f"Missing apply CSV: {report_file}")
    with report_file.open("r", encoding="utf-8-sig", newline="") as f:
        entries = list(csv.DictReader(f))
    candidates = [x["pvp_id"] for x in entries if x.get("apply_status") == "SKIP_CONCURRENT_CHANGE"]
    if len(candidates) != 50 or len(set(candidates)) != len(candidates):
        raise SystemExit("Expected precisely 50 distinct SKIP_CONCURRENT_CHANGE IDs; no requests sent")

    samples = candidates[:3]
    print("[READ ONLY] GET su massimo 3 ID, nessun PATCH e zero Google")
    results = []
    for pid in samples:
        result = diagnostic(base, key, pid)
        results.append(result)
        print("ID:", pid, "OUTCOME:", result["outcome"])
        if result.get("single_field_failures"):
            print("  Filtri che non coincidono:")
            for item in result["single_field_failures"]:
                print("   ", item["field"], item["value_class"], "->", item["filter"])
        if result.get("first_combination_failure_at"):
            print("  Prima combinazione fallita:", result["first_combination_failure_at"])

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    reports.mkdir(parents=True, exist_ok=True)
    destination = reports / f"local_only_conditional_get_diagnostic_{stamp}.json"
    destination.write_text(json.dumps({"mode":"GET_ONLY_ZERO_PATCH_ZERO_GOOGLE", "results":results},
                                      indent=2, ensure_ascii=False), encoding="utf-8")
    print("[OUT]", destination)
    print("[DONE] Nessuna scrittura DB. Non rilanciare il repair --apply finché non analizziamo i risultati.")


if __name__ == "__main__":
    main()
