#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy Google live budget guard v3.11.5 (NO DB writes).

Fixes the v3.11 bootstrap double-count: historical calls must exist exactly once
in the core counter and exactly once in the app ledger. The v3.11 bug could seed
them as both core local usage and core external_offset.

Safety model:
- reconstruct billable local history from comparison + prior live-guard reports;
- repair the known v3.11 double-count ONLY on an exact signature;
- otherwise never lower any counter;
- canonicalize the core monthly total into network_attempts with external_offset=0;
- seed the app ledger to the same conservative total;
- reserve the app ledger BEFORE each real Google HTTP attempt;
- let the Google core independently reserve/count its own attempt before urlopen;
- no Supabase writes.
"""
from __future__ import annotations

import csv
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
import geocode_pending_v2 as offline
import google_budget_v3_10 as budget
import google_preview_v3 as preview

VERSION = "google_live_guard_v3_11_5"
MAX_SMOKE_REQUESTS = 50


def _safe_int(v, default=0):
    try:
        return max(0, int(v))
    except Exception:
        return default


def _utc_month(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


def core_usage_path() -> Path:
    """Return the exact usage file path the legacy Google core will use.

    The core supports GOOGLE_API_USAGE_FILE; v3.11.1 ignored that override when
    reconciling, which could compare two different state files.
    """
    name = (os.environ.get("GOOGLE_API_USAGE_FILE", "google_api_usage_v9_2.json") or
            "google_api_usage_v9_2.json").strip()
    return Path(offline.HERE) / name


def read_core_month_usage(path: Optional[Path] = None, *, month: Optional[str] = None) -> dict:
    path = Path(path or core_usage_path())
    month = month or _utc_month()
    if not path.exists():
        return {"month": month, "local": 0, "external_offset": 0,
                "effective": 0, "source": "NO_CORE_USAGE_FILE"}
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
        d = (((obj.get("months") or {}).get(month) or {}).get("geocoding") or {})
        local = _safe_int(d.get("network_attempts"))
        ext = _safe_int(d.get("external_offset"))
        return {"month": month, "local": local, "external_offset": ext,
                "effective": local + ext, "source": "CORE_USAGE"}
    except Exception as exc:
        raise RuntimeError(f"Core Google usage file corrupt/unreadable: {path.name}") from exc


def _atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_core_month_total(total: int, *, path: Optional[Path] = None, month: Optional[str] = None) -> dict:
    """Canonicalize core usage: one total in local network_attempts, zero offset."""
    total = _safe_int(total)
    path = Path(path or core_usage_path())
    month = month or _utc_month()
    if path.exists():
        try:
            obj = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(obj, dict):
                raise ValueError("root not object")
        except Exception as exc:
            raise RuntimeError(f"Core Google usage file corrupt/unreadable: {path.name}") from exc
    else:
        obj = {"version": 2, "months": {}}
    obj["version"] = max(2, _safe_int(obj.get("version"), 2))
    months = obj.setdefault("months", {})
    m = months.setdefault(month, {})
    g = m.setdefault("geocoding", {})
    g["network_attempts"] = total
    g["external_offset"] = 0
    # Preserve/create sibling SKUs expected by old core.
    for sku in ("places_autocomplete", "address_validation"):
        d = m.setdefault(sku, {})
        d.setdefault("network_attempts", 0)
        d.setdefault("external_offset", 0)
    obj["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    obj["note"] = "Canonicalized by Aste Easy live guard v3.11.5; monthly total stored once, external_offset=0."
    _atomic_write_json(path, obj)
    return {"month": month, "local": total, "external_offset": 0, "effective": total,
            "source": "CORE_USAGE_CANONICAL"}


def detect_live_guard_report_rows(report_dir: Path, *, month: str) -> dict:
    """Legacy diagnostic only: count rows in live-guard CSVs.

    IMPORTANT: a CSV row is a selected case, NOT necessarily an HTTP attempt; cache
    hits also produce rows. v3.11.4 incorrectly treated this value as billable usage.
    """
    yyyymm = month.replace("-", "")
    total = 0
    files = []
    for p in sorted(Path(report_dir).glob(f"google_live_guard_v3_11*_{yyyymm}*.csv")):
        try:
            with p.open("r", encoding="utf-8-sig", newline="") as f:
                rd = csv.reader(f, delimiter=";")
                next(rd, None)
                n = sum(1 for _ in rd)
            total += n
            files.append({"file": p.name, "rows": n})
        except Exception:
            files.append({"file": p.name, "rows": None, "warning": "UNREADABLE"})
    return {"month": month, "rows": total, "files": files}


def detect_live_guard_network_attempts(report_dir: Path, *, month: str) -> dict:
    """Exact completed live-guard HTTP history from importer JSON summaries.

    The importer summary records google_live_guard.run_network_attempts. Unlike CSV
    row counts this excludes cache hits. Only successfully written summaries count;
    crash reservations remain protected by the persistent core/ledger max path.
    """
    yyyymm = month.replace("-", "")
    total = 0
    files = []
    for p in sorted(Path(report_dir).glob(f"pvp_import_atomic_v1_{yyyymm}*.json")):
        try:
            obj = json.loads(p.read_text(encoding="utf-8-sig"))
            gl = (((obj.get("geocode_preview") or {}).get("google_live_guard")) or {})
            if not gl:
                continue
            n = _safe_int(gl.get("run_network_attempts"))
            total += n
            files.append({
                "file": p.name,
                "version": gl.get("version"),
                "network_attempts": n,
                "cache_hits": _safe_int(gl.get("run_cache_hits")),
            })
        except Exception:
            files.append({"file": p.name, "network_attempts": None, "warning": "UNREADABLE"})
    return {"month": month, "attempts": total, "files": files}

def reconcile_usage_state(*, monthly_cap: int, monthly_used_override: Optional[int] = None,
                          usage_path: Optional[Path] = None) -> dict:
    """Canonicalize current-month usage without confusing cache hits with HTTP.

    Sources, in decreasing authority for live operation:
    - persistent core counter and independent app ledger;
    - exact run_network_attempts from completed importer JSON summaries;
    - diagnostic comparison reports (their rows were known network attempts in the
      development phase);
    - optional operator override.

    CSV rows from live-guard reports are diagnostic only. v3.11.4 counted them as
    HTTP and therefore over-counted a cache-hit-only smoke by 10. We repair that
    exact signature once, then never use live CSV row counts as billing usage again.
    """
    month = budget._month_key()
    comparisons = budget.detect_local_report_attempts(REPORTS_DIR, month=month)
    live_exact = detect_live_guard_network_attempts(REPORTS_DIR, month=month)
    live_rows = detect_live_guard_report_rows(REPORTS_DIR, month=month)
    comparison_attempts = _safe_int(comparisons.get("attempts"))
    live_network_attempts = _safe_int(live_exact.get("attempts"))
    legacy_live_rows = _safe_int(live_rows.get("rows"))
    observed_exact = comparison_attempts + live_network_attempts
    legacy_observed = comparison_attempts + legacy_live_rows

    ledger = budget.read_ledger(month=month)
    if ledger.get("corrupt"):
        raise RuntimeError("Google budget ledger is corrupt; refuse live Google mode")
    usage_path = Path(usage_path) if usage_path is not None else core_usage_path()
    core_before = read_core_month_usage(path=usage_path, month=month)
    override = _safe_int(monthly_used_override) if monthly_used_override is not None else 0
    ledger_used = _safe_int(ledger.get("used"))

    # Known v3.11 original bug: comparison history duplicated as external_offset.
    sig_original_v311 = (
        core_before["external_offset"] == comparison_attempts
        and comparison_attempts > 0
        and ledger_used == core_before["effective"]
        and core_before["local"] >= observed_exact
    )

    # Known v3.11.1 partial crash: core already canonical, ledger still has the
    # duplicated comparison floor.
    sig_partial_v3111 = (
        core_before["external_offset"] == 0
        and comparison_attempts > 0
        and ledger_used == core_before["local"] + comparison_attempts
        and core_before["local"] >= observed_exact
    )

    # Known v3.11.4 over-count: legacy live CSV rows included cache hits. This is
    # safe to lower only when BOTH persistent counters equal exactly the old buggy
    # formula and the exact JSON-summary history proves a smaller completed HTTP total.
    phantom_live_rows = max(0, legacy_live_rows - live_network_attempts)
    sig_v3114_csv_cache_overcount = (
        core_before["external_offset"] == 0
        and ledger_used == core_before["local"]
        and phantom_live_rows > 0
        and core_before["local"] == legacy_observed
        and observed_exact < legacy_observed
        and legacy_observed - observed_exact == phantom_live_rows
    )

    repair = None
    repair_signature = None
    if sig_original_v311:
        corrected = max(observed_exact, core_before["local"], override)
        repair = budget.repair_ledger_exact(
            expected_current=ledger_used, corrected_used=corrected,
            repair_reason="V3_11_DOUBLE_COUNT_LOCAL_PLUS_EXTERNAL_OFFSET",
            cap=monthly_cap, month=month)
        core = write_core_month_total(corrected, path=usage_path, month=month)
        canonical = corrected
        repair_signature = "V3_11_ORIGINAL_DOUBLE_COUNT"
    elif sig_partial_v3111:
        corrected = max(observed_exact, core_before["local"], override)
        repair = budget.repair_ledger_exact(
            expected_current=ledger_used, corrected_used=corrected,
            repair_reason="V3_11_1_PARTIAL_CRASH_LEDGER_ONLY",
            cap=monthly_cap, month=month)
        core = write_core_month_total(corrected, path=usage_path, month=month)
        canonical = corrected
        repair_signature = "V3_11_1_PARTIAL_CRASH_LEDGER_ONLY"
    elif sig_v3114_csv_cache_overcount:
        corrected = max(observed_exact, override)
        repair = budget.repair_ledger_exact(
            expected_current=ledger_used, corrected_used=corrected,
            repair_reason="V3_11_4_LIVE_CSV_CACHE_HIT_OVERCOUNT",
            cap=monthly_cap, month=month)
        core = write_core_month_total(corrected, path=usage_path, month=month)
        canonical = corrected
        repair_signature = "V3_11_4_LIVE_CSV_CACHE_HIT_OVERCOUNT"
    else:
        # Normal fail-safe path: never lower persistent counters. Exact completed
        # history is a floor, never a replacement for reservations after a crash.
        canonical = max(observed_exact, ledger_used, core_before["effective"], override)
        core = write_core_month_total(canonical, path=usage_path, month=month)
        budget.seed_ledger_floor(floor_used=canonical, cap=monthly_cap, month=month)

    if canonical >= monthly_cap:
        raise RuntimeError(f"Google monthly cap already exhausted before live run: {canonical}/{monthly_cap}")
    return {
        "month": month,
        "comparison_attempts": comparison_attempts,
        "comparison_files": len(comparisons.get("files") or []),
        "prior_live_guard_network_attempts_exact": live_network_attempts,
        "prior_live_guard_summary_files": len(live_exact.get("files") or []),
        "legacy_live_guard_csv_rows": legacy_live_rows,
        "legacy_live_guard_csv_files": len(live_rows.get("files") or []),
        "phantom_live_rows_detected": phantom_live_rows,
        "observed_billable_attempts_exact": observed_exact,
        "legacy_buggy_observed_attempts": legacy_observed,
        "ledger_before": ledger,
        "core_usage_path": str(usage_path),
        "core_before": core_before,
        "bug_signature_repaired": bool(repair_signature),
        "repair_signature": repair_signature,
        "repair_result": repair,
        "canonical_used_before": canonical,
        "core_canonical_before": core,
        "override": monthly_used_override,
    }

def _planned_rows(listings, max_rows):
    if max_rows < 1 or len(listings) > max_rows:
        raise RuntimeError("Incomplete offline batch: raise --max-geocode before live guard")
    ctx, territorial, local = offline.prepare_context(no_anncsu=False)
    rows = []
    for listing in listings:
        row = {k: listing.get(k) for k in offline.SELECT}
        row["latitudine"] = row["longitudine"] = None
        if row.get("geocode_status") != "PENDING":
            raise RuntimeError("Non-PENDING listing: stop")
        rows.append(row)
    return ctx, offline.plan_rows(rows, ctx, territorial, local)


def _forward_constrained_cache_key(g, query: str, row: dict) -> str:
    """Build the exact FC cache key used by google_forward_constrained()."""
    address_part = g.clean(query).strip(" ,")
    cap = __import__("re").sub(r"\D", "", g.clean(row.get("cap")))
    comune = g.clean(row.get("comune"))
    components = ["country:IT"]
    if cap:
        components.append(f"postal_code:{cap}")
    if comune:
        components.append(f"locality:{comune}")
    return "FC|" + address_part + "|" + "|".join(components)


def _pick_network_misses(planned, g, prior_ids, prior_queries, cache, max_requests):
    """Pick cases guaranteed to miss the constrained-forward cache.

    We inspect candidates without spending quota. This does NOT bypass or delete
    cache entries; it simply skips already cached FC queries and continues down
    the deterministic P1->P5 candidate order until enough true misses are found.
    Candidate discovery is paged in offline chunks so it can search beyond the
    diagnostic 500-row window when older project runs have already filled cache.
    """
    temp_ids = set(prior_ids)
    temp_queries = set(prior_queries)
    selected = []
    cached_skipped = 0
    candidates_scanned = 0
    available = {}
    batch_size = int(getattr(preview, "MAX_CASES", 500) or 500)
    batch_size = max(1, batch_size)

    while len(selected) < max_requests:
        batch, available_now = preview.pick_fresh_cases(
            planned, g, temp_ids, temp_queries, batch_size, priority_only=None)
        if not available:
            available = available_now
        if not batch:
            break

        for item in batch:
            row, baseline, group, query, priority_rank, priority_label = item
            candidates_scanned += 1
            pid = str(row.get("pvp_id") or "").strip()
            canon = preview.normalize_query(query)
            if pid:
                temp_ids.add(pid)
            if canon:
                temp_queries.add(canon)

            key = _forward_constrained_cache_key(g, query, row)
            if isinstance(cache.get(key), dict):
                cached_skipped += 1
                continue
            selected.append(item)
            if len(selected) >= max_requests:
                break

        # A short batch means there are no further report-fresh candidates.
        if len(batch) < batch_size:
            break

    return selected, available, cached_skipped, candidates_scanned


def preview_new_rows(listings, *, max_rows, monthly_cap=9000,
                     max_requests=10, monthly_used_override=None):
    if monthly_cap < 1:
        raise ValueError("monthly_cap must be positive")
    if not 1 <= max_requests <= MAX_SMOKE_REQUESTS:
        raise ValueError(f"live smoke max_requests must be 1..{MAX_SMOKE_REQUESTS}")

    # Load project env before resolving the usage-file path, so reconciliation uses
    # the same GOOGLE_API_USAGE_FILE as the Google core. This does not make network calls.
    from project_env import load_project_env
    load_project_env(required=("GOOGLE_GEOCODING_API_KEY",))

    # Offline planning may import legacy parsers. Do it before touching canonical counters.
    ctx, planned = _planned_rows(listings, max_rows)
    prior_ids, prior_queries, previous_reports = preview.previously_compared(REPORTS_DIR)

    ledger_before_raw = budget.read_ledger(month=budget._month_key())

    # Configure hard caps BEFORE importing the legacy core, but do not inject any
    # historical offset. Loading the module is network-free and gives us the exact
    # persistent API_USAGE_FILE chosen by that core in this installation.
    os.environ["MAX_MONTH_GEOCODING_CALLS"] = str(monthly_cap)
    os.environ.pop("MONTH_USAGE_OFFSET_GEOCODING", None)
    os.environ["MAX_RUN_GEOCODING_CALLS"] = str(max_requests)
    os.environ["PLACES_AUTOCOMPLETE_FALLBACK"] = "0"
    os.environ["ADDRESS_VALIDATION_FALLBACK"] = "0"
    os.environ["HTTP_RETRIES"] = "1"
    os.environ["API_USAGE_SAVE_EVERY"] = "1"

    g = preview.load_google_core(max_requests)
    loaded_usage_path = Path(g.API_USAGE_FILE).resolve()
    state_before_raw = read_core_month_usage(path=loaded_usage_path, month=budget._month_key())

    usage = reconcile_usage_state(
        monthly_cap=monthly_cap, monthly_used_override=monthly_used_override,
        usage_path=loaded_usage_path)
    canonical_before = usage["canonical_used_before"]
    reconciled_usage_path = Path(usage["core_usage_path"]).resolve()
    if loaded_usage_path != reconciled_usage_path:
        # This should now be structurally impossible because reconciliation received
        # the path directly from the core. Keep it as a fail-closed invariant.
        raise RuntimeError(
            f"Internal Google usage path invariant failed: reconciled={reconciled_usage_path} "
            f"loaded={loaded_usage_path}; zero requests made"
        )

    # Recreate only the in-memory tracker after canonicalizing the on-disk file.
    # Module import itself made no HTTP requests. The core constants already carry
    # the hard caps set above; the canonical file now has external_offset=0.
    g.API_USAGE = g.ApiUsageTracker(g.API_USAGE_FILE)
    if getattr(g, "MAX_MONTH_GEOCODING_CALLS", 0) != monthly_cap:
        raise RuntimeError("Google core ignored monthly hard cap")
    local_loaded, ext_loaded, eff_loaded = g.API_USAGE.month_usage("geocoding")
    if ext_loaded != 0 or eff_loaded != canonical_before:
        raise RuntimeError(
            f"Google core usage mismatch after canonicalization: local={local_loaded} ext={ext_loaded} "
            f"effective={eff_loaded} expected={canonical_before}"
        )

    # Add an independent ledger reservation immediately before every core attempt.
    original_before = g.API_USAGE.before_network_attempt
    def guarded_before_network_attempt(sku, operation, attempt_index):
        if sku == "geocoding":
            res = budget.reserve_before_http(cap=monthly_cap, count=1, month=usage["month"])
            if not res.get("ok"):
                raise g.ApiBudgetExceeded(
                    f"Aste Easy ledger monthly hard cap reached: {res.get('used')}/{monthly_cap}"
                )
        return original_before(sku, operation, attempt_index)
    g.API_USAGE.before_network_attempt = guarded_before_network_attempt

    # Select TRUE network misses. A case can be absent from comparison reports but
    # still be present in the legacy Google cache from older project runs.
    cache = g.CacheStore(g.CACHE_FILE)
    selected, available, cached_candidates_skipped, candidates_scanned = _pick_network_misses(
        planned, g, prior_ids, prior_queries, cache, max_requests)

    ensure_runtime_dirs()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    csv_path = REPORTS_DIR / ("google_live_guard_v3_11_5_" + stamp + ".csv")
    fields = (
        "pvp_id", "group", "priority_rank", "priority_label", "comune", "sigla_provincia",
        "indirizzo", "query", "assessment", "google_lat", "google_lon", "google_location_type",
        "google_formatted_address", "google_score_diagnostico", "google_civic_relation",
        "google_distance_from_pvp_m", "google_distance_from_offline_m", "google_istat_position",
        "google_reason", "core_effective_after_row", "ledger_used_after_row",
    )

    details = []
    stop_reason = "COMPLETED_SELECTION"
    try:
        for row, baseline, group, query, priority_rank, priority_label in selected:
            rec = {
                "pvp_id": row.get("pvp_id"), "group": group,
                "priority_rank": priority_rank, "priority_label": priority_label,
                "comune": row.get("comune"), "sigla_provincia": row.get("sigla_provincia"),
                "indirizzo": row.get("indirizzo"), "query": query,
            }
            try:
                results = g.google_forward_constrained(query, row, cache)
                rec.update(preview.compare_candidate(row, baseline, results, ctx, g))
            except g.ApiBudgetExceeded:
                rec["assessment"] = "STOP_MONTHLY_OR_RUN_HARD_CAP"
                details.append(rec); stop_reason = "CORE_OR_LEDGER_HARD_CAP"; break
            except Exception as exc:
                rec["assessment"] = "GOOGLE_ERROR_" + type(exc).__name__
                if hasattr(exc, "code"):
                    rec["google_reason"] = "HTTP_" + str(exc.code)
                details.append(rec); stop_reason = "GOOGLE_ERROR"; break
            _, _, eff = g.API_USAGE.month_usage("geocoding")
            led = budget.read_ledger(month=usage["month"])
            rec["core_effective_after_row"] = eff
            rec["ledger_used_after_row"] = _safe_int(led.get("used"))
            details.append(rec)
    finally:
        cache.save(); g.API_USAGE.save()

    run_usage = g.API_USAGE.run["geocoding"]
    local_after, offset_after, effective_after = g.API_USAGE.month_usage("geocoding")
    ledger_after = budget.read_ledger(month=usage["month"])
    ledger_used_after = _safe_int(ledger_after.get("used"))
    # They should match exactly; ledger may only be +1 on a blocked internal attempt,
    # which is safe but should be explicit rather than silently reconciled downward.
    counter_delta = ledger_used_after - effective_after
    if counter_delta < 0:
        raise RuntimeError("Ledger fell below core usage after live run; refuse continuation")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";", extrasaction="ignore")
        w.writeheader(); w.writerows(details)

    return {
        "reviewed_without_db_writes": len(listings), "total_new": len(listings),
        "selection_truncated": False, "google_calls": int(run_usage.get("network_attempts", 0)),
        "google_live_guard": {
            "version": VERSION,
            "mode": "REAL_GOOGLE_CALLS_DUAL_HARD_CAP_ZERO_DB_WRITES",
            "monthly_cap": monthly_cap, "run_cap": max_requests,
            "raw_core_before_reconcile": state_before_raw,
            "raw_ledger_before_reconcile": ledger_before_raw,
            "usage_reconciliation": usage,
            "core_effective_loaded": eff_loaded,
            "fresh_selected_cases": len(selected), "available_priority_counts": available,
            "network_miss_selection": True,
            "candidates_scanned_for_network_miss": candidates_scanned,
            "cached_candidates_skipped_before_run": cached_candidates_skipped,
            "previous_reports": len(previous_reports),
            "run_network_attempts": int(run_usage.get("network_attempts", 0)),
            "run_cache_hits": int(run_usage.get("cache_hits", 0)),
            "core_local_after": local_after, "core_external_offset_after": offset_after,
            "core_effective_after": effective_after,
            "ledger_after": ledger_after,
            "counter_delta_ledger_minus_core": counter_delta,
            "remaining_after": max(0, monthly_cap - max(effective_after, ledger_used_after)),
            "stop_reason": stop_reason,
            "assessment_counts": dict(Counter(d.get("assessment") or "UNKNOWN" for d in details)),
            "report_csv": str(csv_path),
            "safety_note": "v3.11.5 uses exact run_network_attempts from completed importer JSON summaries; live CSV rows are diagnostic only. It repairs the known v3.11.4 cache-hit over-count exactly once, keeps dual pre-HTTP caps, and performs no Supabase writes.",
        },
    }
