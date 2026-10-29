#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy: import only new PVP IDs using a single Supabase RPC transaction per ID.

Safe default: read-only preview; no Google. SQL migration must be applied separately.
Existing IDs are never updated, and a partial child failure rolls back the parent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import socket
import time
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
# Standalone bootstrap: this file may be executed directly, outside the main
# pipeline launcher. Make the same project modules visible explicitly.
for _p in (SRC, HERE, SRC / "pipeline", SRC / "geocoding"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import pvp_import_consolidated_preview as previous
from aste_paths import REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env

VERSION = "atomic_import_v1"
RPC = "aste_insert_new_listing_v1"
# Only these columns from the read-only preview may be sent to the RPC.
LISTING_COLS = frozenset(previous.LISTINGS_FIELDS) - {
    "latitudine", "longitudine", "maps_link"
}
SUBJECT_COLS = frozenset((
    "pvp_id", "role_code", "role_label", "nome", "cognome", "display_name",
    "email", "telefono", "sort_order"
))
ATTACH_COLS = frozenset(("pvp_id", "filename", "resource_url", "mime", "size_bytes", "seen_at"))


def fingerprint(row):
    keys = ("pvp_id", "indirizzo", "comune", "sigla_provincia", "cap",
            "pvp_latitudine", "pvp_longitudine", "pvp_geo_source")
    data = json.dumps({k: row.get(k) for k in keys}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def prepare_listing(preview_row, raw_object):
    geo = previous.primary_geo(raw_object)
    row = {key: value for key, value in preview_row.items() if key in LISTING_COLS and value not in ("", None)}
    # The preview's text/geography is the source of truth; raw PVP coordinates
    # are separate. Coordinates and Maps link are not published until checked.
    row["pvp_id"] = str(preview_row["pvp_id"])
    row["pvp_latitudine"] = geo["latitudine"]
    row["pvp_longitudine"] = geo["longitudine"]
    detail = raw_object.get("detail_body") or {}
    beni = list(detail.get("beni") or []) + list((detail.get("lotto") or {}).get("beni") or [])
    n_beni = len(beni)
    if geo["source"] == "bene":
        selected_index = next((i for i, bene in enumerate(beni)
            if isinstance(bene, dict) and isinstance(bene.get("indirizzo"), dict) and
            previous.first_non_empty(bene["indirizzo"].get("descComune"),
                                     bene["indirizzo"].get("citta"),
                                     bene["indirizzo"].get("comune"))), None)
        if selected_index is None:
            raise ValueError("Selected geographic bene not found")
        kind = "bene_multi" if n_beni > 1 else "bene"
        row["pvp_geo_source"] = f"{kind}:{selected_index+1}/{n_beni}"
    else:
        row["pvp_geo_source"] = geo["source"] + ("_multi" if n_beni > 1 else "")
    row["geocode_status"] = "PENDING"
    row["geocode_input_hash"] = fingerprint(row)
    return row


def prepare_children(pvp_id, subjects, attachments):
    ss, aa, seen_s, seen_a = [], [], set(), set()
    for subj in subjects:
        if str(subj.get("pvp_id")) != pvp_id:
            raise ValueError("Subject belonging to another listing: " + pvp_id)
        row = {k: v for k, v in subj.items() if k in SUBJECT_COLS and v not in (None, "")}
        row["pvp_id"] = pvp_id
        # Matches expression-based unique index on the subjects table.
        natural = (pvp_id, row.get("role_code", ""), row.get("display_name", ""), row.get("email", ""))
        if natural in seen_s:
            previous_row = next(s for s in ss if (
                pvp_id, s.get("role_code", ""), s.get("display_name", ""), s.get("email", "")
            ) == natural)
            if previous_row != row:
                raise ValueError("Conflicting subject duplicate: " + pvp_id)
            continue
        seen_s.add(natural)
        ss.append(row)
    for attachment in attachments:
        if str(attachment.get("pvp_id")) != pvp_id:
            raise ValueError("Attachment belonging to another listing: " + pvp_id)
        row = {k: v for k, v in attachment.items() if k in ATTACH_COLS and v not in (None, "")}
        row["pvp_id"] = pvp_id
        if not row.get("filename") or not row.get("resource_url"):
            raise ValueError("Attachment without filename/resource_url: " + pvp_id)
        natural = (pvp_id, row["resource_url"])
        if natural in seen_a:
            # If different names on the same URL, do not silently choose one.
            old = next(a for a in aa if a["resource_url"] == row["resource_url"])
            if old != row:
                raise ValueError("Conflicting attachment duplicate: " + pvp_id)
            continue
        seen_a.add(natural)
        aa.append(row)
    return ss, aa


def _load_raw_unique_with_sha(path):
    """Load RAW once, preserving legacy duplicate validation and exact file SHA."""
    h = hashlib.sha256()
    raw_by_id = {}
    duplicates = 0
    with path.open("rb") as f:
        for raw_line in f:
            h.update(raw_line)
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            obj = json.loads(line)
            pid = previous.clean(obj.get("pvp_id"))
            if not re.fullmatch(r"[0-9]{1,19}", pid):
                raise ValueError("RAW: pvp_id non valido")
            if pid in raw_by_id:
                duplicates += 1
                if obj != raw_by_id[pid]:
                    raise ValueError("RAW: dati diversi per lo stesso pvp_id " + pid)
            raw_by_id[pid] = obj
    return raw_by_id, h.hexdigest(), duplicates


def _fetch_all_existing_listing_ids(base, key, page_size=1000):
    """Fetch all existing listing pvp_id values with stable offset pagination.

    pvp_id may be stored as text or as a numeric type in Supabase.  Keyset
    pagination coupled with a Python-side numeric ordering assertion is therefore
    brittle: PostgreSQL legitimately orders text and numeric columns differently.
    The daily planner runs before any importer writes, so a stable ORDER BY plus
    offset/limit is safe here and remains type-agnostic.
    """
    out = set()
    pages = 0
    offset = 0
    page_size = int(page_size)
    if page_size < 1:
        raise ValueError("page_size must be >= 1")
    while True:
        params = {
            "select": "pvp_id",
            "order": "pvp_id.asc",
            "limit": str(page_size),
            "offset": str(offset),
        }
        url = base.rstrip("/") + "/rest/v1/listings?" + urlencode(params)
        req = Request(url, method="GET", headers={
            "apikey": key, "Authorization": "Bearer " + key, "Accept": "application/json"
        })
        with urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8") or "[]")
        pages += 1
        if not isinstance(data, list):
            raise RuntimeError("Supabase: pagina ID listings inattesa")
        if not data:
            break
        got = []
        for row in data:
            pid = str(row.get("pvp_id") or "")
            if not re.fullmatch(r"[0-9]{1,19}", pid):
                raise RuntimeError("Supabase: pvp_id non valido nella pagina listings")
            got.append(pid)
        if len(got) != len(set(got)):
            raise RuntimeError("Supabase: pvp_id duplicati nella pagina listings")
        overlap = out.intersection(got)
        if overlap:
            raise RuntimeError("Supabase: paginazione listings ha restituito ID duplicati tra pagine")
        out.update(got)
        if len(got) < page_size:
            break
        offset += len(got)
    return out, pages


def _raw_auction_date(obj):
    """Exactly the same auction-date source used by derive_listing_row(), but cheap."""
    search_item = obj.get("search_item") or {}
    detail_body = obj.get("detail_body") or {}
    return previous.date_it(previous.first_non_empty(search_item.get("dataVendita"), detail_body.get("dataVendita")))


def _scan_jsonl_for_ids(path, wanted_ids):
    """Read a child JSONL once and retain only rows belonging to wanted IDs."""
    wanted = set(wanted_ids)
    if not wanted:
        return []
    out = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if previous.clean(obj.get("pvp_id")) in wanted:
                out.append(obj)
    return out


def _build_plan_fast_core(raw_by_id, existing_ids, istat, today, attachments_for_survivors, subjects_path=None):
    """Legacy-equivalent plan with cheap lifecycle pre-screen before full derivation."""
    decisions = Counter()
    sources = Counter()
    survivor_ids = []
    # preserve RAW insertion order, matching legacy plan_new_only()
    for pid, obj in raw_by_id.items():
        if pid in existing_ids:
            continue
        d = previous.parse_auction_date(_raw_auction_date(obj))
        if d is None:
            decisions["SKIP_DATE_UNKNOWN"] += 1
            continue
        age_days = (today - d).days
        future_days = -age_days
        if future_days > previous.MAX_FUTURE_AUCTION_DAYS:
            decisions["SKIP_DATE_FAR_FUTURE_REVIEW"] += 1
            continue
        if age_days > previous.RETENTION_DAYS:
            decisions["SKIP_EXPIRED_BEYOND_RETENTION"] += 1
            continue
        survivor_ids.append(pid)

    att_by_id = defaultdict(list)
    for a in attachments_for_survivors:
        att_by_id[previous.clean(a.get("pvp_id"))].append(a)

    eligible = []
    for pid in survivor_ids:
        obj = raw_by_id[pid]
        row, source = previous.derive_new_consistent(obj, att_by_id)
        decision, reason, admin = previous.classify_new_candidate(row, today, istat)
        decisions[decision] += 1
        if decision.startswith("ELIGIBLE_"):
            row.update({k: admin[k] for k in ("comune", "provincia", "sigla_provincia", "regione")})
            row["maps_link"] = previous.build_maps_link(
                row["latitudine"], row["longitudine"], row["indirizzo"], row["comune"], row["provincia"]
            )
            eligible.append(row)
            sources[source] += 1

    eligible_ids = {str(r["pvp_id"]) for r in eligible}
    subjects = _scan_jsonl_for_ids(subjects_path, eligible_ids) if subjects_path is not None else []
    attachments = [a for a in attachments_for_survivors if previous.clean(a.get("pvp_id")) in eligible_ids]
    return {
        "eligible": eligible,
        "decisions": decisions,
        "sources": sources,
        "subjects_raw": subjects,
        "attachments": attachments,
        "date_survivor_ids": survivor_ids,
    }


def build_plan():
    """Fast daily planner; output contract is identical to the v3.14 atomic importer."""
    for path in (previous.IN_RAW, previous.IN_ALLEGATI, previous.IN_SOGGETTI, previous.ISTAT_XLSX):
        if not path.is_file():
            raise RuntimeError("Input missing: " + str(path))
    load_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE"))
    base, key = os.environ["SUPABASE_URL"].rstrip("/"), os.environ["SUPABASE_SERVICE_ROLE"]

    raw_by_id, raw_sha, duplicate_count = _load_raw_unique_with_sha(previous.IN_RAW)
    raw_ids = set(raw_by_id)
    db_ids, db_pages = _fetch_all_existing_listing_ids(base, key)
    existing = raw_ids & db_ids
    today = datetime.now().astimezone().date()

    # Cheap date-only pass first.  This avoids deriving 10k+ historical rows that
    # will be rejected before canonical-admin resolution anyway.
    prescreen_survivors = []
    prescreen_decisions = Counter()
    for pid, obj in raw_by_id.items():
        if pid in existing:
            continue
        d = previous.parse_auction_date(_raw_auction_date(obj))
        if d is None:
            prescreen_decisions["SKIP_DATE_UNKNOWN"] += 1
            continue
        age_days = (today - d).days
        future_days = -age_days
        if future_days > previous.MAX_FUTURE_AUCTION_DAYS:
            prescreen_decisions["SKIP_DATE_FAR_FUTURE_REVIEW"] += 1
            continue
        if age_days > previous.RETENTION_DAYS:
            prescreen_decisions["SKIP_EXPIRED_BEYOND_RETENTION"] += 1
            continue
        prescreen_survivors.append(pid)

    attachments_relevant = _scan_jsonl_for_ids(previous.IN_ALLEGATI, set(prescreen_survivors))
    istat = previous.load_istat_admin(previous.ISTAT_XLSX)
    if not istat.get("ok"):
        raise RuntimeError("ISTAT unavailable: " + str(istat.get("reason")))

    # Core repeats the date test intentionally: it is a small, deterministic
    # guard against the helper being called independently with a different set.
    fast = _build_plan_fast_core(
        raw_by_id, existing, istat, today, attachments_relevant, subjects_path=previous.IN_SOGGETTI
    )

    sub_by_id, att_by_id = defaultdict(list), defaultdict(list)
    eligible_ids = {str(r["pvp_id"]) for r in fast["eligible"]}
    # Preserve the stricter atomic-import behavior: normalize each subject row
    # independently so conflicting duplicates are detected by prepare_children().
    for original in fast["subjects_raw"]:
        converted = previous.normalize_soggetti_rows([original])
        for subj in converted:
            sub_by_id[str(subj["pvp_id"])].append(subj)
    for a in fast["attachments"]:
        att_by_id[str(a["pvp_id"])].append(a)

    records = []
    for row in fast["eligible"]:
        pid = str(row["pvp_id"])
        ss, aa = prepare_children(pid, sub_by_id[pid], att_by_id[pid])
        records.append((prepare_listing(row, raw_by_id[pid]), ss, aa))

    return {
        "records": records,
        "raw_sha256": raw_sha,
        "incoming_unique": len(raw_by_id),
        "existing": len(existing),
        "new_candidates": len(raw_by_id) - len(existing),
        "decisions": dict(fast["decisions"]),
        "primary_sources": dict(fast["sources"]),
        "subjects": sum(len(r[1]) for r in records),
        "attachments": sum(len(r[2]) for r in records),
        "planner_stats": {
            "version": "delta_plan_v1_1",
            "db_listing_ids_total": len(db_ids),
            "db_id_pages": db_pages,
            "raw_duplicate_rows_same_payload": duplicate_count,
            "date_prescreen_survivors": len(prescreen_survivors),
            "date_prescreen_rejected": (len(raw_by_id) - len(existing) - len(prescreen_survivors)),
            "full_rows_derived": len(prescreen_survivors),
        },
    }


def assert_rpc_available():
    """Zero-write probe: missing pvp_id must reach the SQL function and fail with 22023.

    A PGRST202 means the migration/function is not visible to PostgREST. No row is
    inserted by this probe.
    """
    load_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE"))
    base, key = os.environ["SUPABASE_URL"].rstrip("/"), os.environ["SUPABASE_SERVICE_ROLE"]
    url = base + "/rest/v1/rpc/" + RPC
    payload = json.dumps({"p_listing": {}, "p_soggetti": [], "p_allegati": []}).encode("utf-8")
    req = Request(url, data=payload, method="POST", headers={
        "apikey": key, "Authorization": "Bearer " + key,
        "Content-Type": "application/json", "Accept": "application/json"
    })
    try:
        with urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
        raise RuntimeError("Atomic RPC probe unexpectedly succeeded; expected validation error for empty p_listing: " + body[:200])
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            obj = json.loads(detail or "{}")
        except Exception:
            obj = {}
        if exc.code == 404 and obj.get("code") == "PGRST202":
            raise RuntimeError(
                "Atomic RPC public.aste_insert_new_listing_v1 is missing from PostgREST. "
                "Apply sql/001_aste_insert_new_listing_v1.sql in Supabase SQL Editor first."
            ) from exc
        if str(obj.get("code")) == "22023" and "pvp_id" in str(obj.get("message", "")):
            print("[RPC CHECK] OK: public.aste_insert_new_listing_v1 visible; zero writes.", flush=True)
            return True
        raise RuntimeError(
            f"Atomic RPC probe returned unexpected HTTP {exc.code}: {detail[:350]}"
        ) from exc


def _is_transient_transport(exc):
    if isinstance(exc, (ConnectionResetError, TimeoutError, socket.timeout, URLError)):
        return True
    if isinstance(exc, OSError):
        return getattr(exc, "winerror", None) in {10053, 10054, 10060} or getattr(exc, "errno", None) in {54, 60, 104, 110}
    return False


def rpc_insert(base, key, listing, subjects, attachments):
    """Idempotent RPC insert with transport retry.

    Retrying after an unknown-commit transport failure is safe because the SQL RPC
    uses ON CONFLICT (pvp_id) DO NOTHING and inserts children only when the parent
    was inserted in that same transaction. A retry after a successful-but-lost
    response therefore returns inserted=false without duplicating children.
    """
    url = base.rstrip("/") + "/rest/v1/rpc/" + RPC
    data = json.dumps({"p_listing": listing, "p_soggetti": subjects, "p_allegati": attachments},
                      ensure_ascii=False).encode("utf-8")
    max_attempts = 4
    retries = 0
    for attempt in range(max_attempts):
        req = Request(url, data=data, method="POST", headers={
            "apikey": key, "Authorization": "Bearer " + key,
            "Content-Type": "application/json", "Accept": "application/json"
        })
        try:
            with urlopen(req, timeout=120) as response:
                result = json.loads(response.read().decode("utf-8"))
            break
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:350]
            raise RuntimeError(f"RPC error HTTP {exc.code} for pvp_id={listing['pvp_id']}: {detail}") from exc
        except Exception as exc:
            if not _is_transient_transport(exc) or attempt >= max_attempts - 1:
                raise
            retries += 1
            print(f"[IMPORT RETRY] pvp_id={listing['pvp_id']} transport error {type(exc).__name__}; retry {attempt+2}/{max_attempts}", flush=True)
            time.sleep(1.5 * (attempt + 1))
    else:
        raise RuntimeError("RPC retry loop exhausted")

    if not isinstance(result, dict) or type(result.get("inserted")) is not bool:
        raise RuntimeError("RPC returned unexpected response; stop, check DB before retry")
    if result.get("pvp_id") != listing["pvp_id"]:
        raise RuntimeError("RPC returned different pvp_id; stop")
    if result["inserted"]:
        if result.get("soggetti") != len(subjects) or result.get("allegati") != len(attachments):
            raise RuntimeError("RPC child count mismatch; stop, check DB")
    result = dict(result)
    result["network_retries"] = retries
    result["recovered_after_transport"] = bool(retries and not result["inserted"])
    return result


def run(*, apply=False, expected_new=None, expected_sha=None, preview_geo=None, prebuilt_plan=None):
    ensure_runtime_dirs()
    plan = prebuilt_plan if prebuilt_plan is not None else build_plan()
    if not isinstance(plan, dict) or "records" not in plan or "raw_sha256" not in plan:
        raise RuntimeError("Invalid prebuilt importer plan")
    records = plan["records"]
    summary = {"version": VERSION, "mode": "APPLY" if apply else "GET_ONLY_PREVIEW",
               "incoming_unique": plan["incoming_unique"], "existing": plan["existing"],
               "new_candidates": plan["new_candidates"], "eligible_new": len(records),
               "new_soggetti": plan["subjects"], "new_allegati": plan["attachments"],
               "eligible_with_pvp_coords": sum(r[0]["pvp_latitudine"] is not None for r in records),
               "eligible_without_pvp_coords": sum(r[0]["pvp_latitudine"] is None for r in records),
               "decision_counts": plan["decisions"], "primary_address_sources": plan["primary_sources"],
               "raw_sha256": plan["raw_sha256"], "google_calls": 0,
               "inserted": 0, "skipped_concurrent": 0, "existing_writes": 0,
               "rpc_network_retries": 0, "rpc_recovered_after_transport": 0,
               "planner_stats": plan.get("planner_stats", {})}
    if not apply and preview_geo is not None:
        summary["geocode_preview"] = preview_geo([r[0] for r in records])
    if apply:
        if expected_new is None or not expected_sha:
            raise RuntimeError("--apply requires --expected-new and --expected-raw-sha256 from preview")
        if expected_new != len(records) or expected_sha != plan["raw_sha256"]:
            raise RuntimeError("Preview count or RAW checksum changed; no writes. Run preview again")
        base, key = os.environ["SUPABASE_URL"].rstrip("/"), os.environ["SUPABASE_SERVICE_ROLE"]
        try:
            total = len(records)
            if total:
                print(f"[IMPORT] starting atomic RPC insert: {total} listings", flush=True)
            for idx, (row, ss, aa) in enumerate(records, 1):
                result = rpc_insert(base, key, row, ss, aa)
                summary["rpc_network_retries"] += int(result.get("network_retries") or 0)
                summary["rpc_recovered_after_transport"] += int(bool(result.get("recovered_after_transport")))
                if result["inserted"]:
                    summary["inserted"] += 1
                else:
                    summary["skipped_concurrent"] += 1
                step = 25 if total <= 500 else 100
                if idx == 1 or idx % step == 0 or idx == total:
                    print(f"[IMPORT] {idx}/{total} inserted={summary['inserted']} skipped={summary['skipped_concurrent']} retries={summary['rpc_network_retries']}", flush=True)
        except Exception as exc:
            # Earlier successful RPCs remain committed; the failed RPC has rolled back.
            # Keep a compact run summary without copying sensitive response bodies.
            summary["stopped_reason"] = type(exc).__name__
            write_summary(summary)
            raise
    write_summary(summary)
    return summary


def write_summary(summary):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / ("pvp_import_atomic_v1_" + datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f") + ".json")
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print("[OUT]", path, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Importer atomico solo nuovi ID; preview predefinita")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check-rpc", action="store_true", help="Zero-write PostgREST visibility check for atomic RPC")
    ap.add_argument("--expected-new", type=int)
    ap.add_argument("--expected-raw-sha256")
    args = ap.parse_args(argv)
    if args.check_rpc:
        if args.apply:
            ap.error("--check-rpc cannot be combined with --apply")
        assert_rpc_available()
        return 0
    run(apply=args.apply, expected_new=args.expected_new, expected_sha=args.expected_raw_sha256)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
