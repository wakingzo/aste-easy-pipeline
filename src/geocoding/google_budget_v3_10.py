#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy Google monthly budget preflight v3.10.

Offline-only production simulation:
- reuses v3.9 policy/gate (ZERO Google calls, ZERO DB writes)
- applies deterministic P1 -> P2 -> P3 -> P4 -> P5 monthly budget order
- defaults to a 9,000 request hard cap
- estimates already-used requests conservatively from BOTH a persistent ledger
  and current-month local google_comparison_v3 reports (takes the maximum)
- provides a crash-safe ledger helper for the later live caller: reserve BEFORE HTTP.
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
import google_eligibility_v3_2 as elig
import google_policy_v3_9 as v39

VERSION = "google_budget_v3_10_production_preflight"
DEFAULT_MONTHLY_CAP = 9000
LEDGER_NAME = "google_geocoding_usage_v1.json"


def _month_key(now: Optional[datetime] = None) -> str:
    now = now or datetime.now().astimezone()
    return now.strftime("%Y-%m")


def _stamp(now: Optional[datetime] = None) -> str:
    now = now or datetime.now().astimezone()
    return now.strftime("%Y%m%d-%H%M%S-%f")


def _runtime_state_dir() -> Path:
    # Keep the state next to runtime reports, but in a separate explicit directory.
    d = Path(REPORTS_DIR).parent / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ledger_path() -> Path:
    return _runtime_state_dir() / LEDGER_NAME


def _safe_int(v, default=0) -> int:
    try:
        return max(0, int(v))
    except Exception:
        return default


def read_ledger(path: Optional[Path] = None, *, month: Optional[str] = None) -> dict:
    path = Path(path or ledger_path())
    month = month or _month_key()
    if not path.exists():
        return {"month": month, "used": 0, "source": "NO_LEDGER"}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # Safety-first: a corrupt ledger must not silently reset live billing.
        return {"month": month, "used": 0, "source": "CORRUPT_LEDGER", "corrupt": True}
    if str(obj.get("month") or "") != month:
        return {"month": month, "used": 0, "source": "ROLLED_MONTH"}
    return {"month": month, "used": _safe_int(obj.get("used")), "source": "LEDGER", **obj}


def _atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _acquire_lock(lock: Path, timeout_s: float = 10.0) -> int:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Google budget ledger lock timeout: {lock}")
            time.sleep(0.05)


def seed_ledger_floor(*, floor_used: int, cap: int = DEFAULT_MONTHLY_CAP,
                      path: Optional[Path] = None, month: Optional[str] = None) -> dict:
    """Raise the current-month ledger to at least floor_used, never lower it.

    Call once at live-process startup with the conservative current-month history
    detected from local reports (and, when available, a Cloud Console value).
    """
    if floor_used < 0 or cap < 1:
        raise ValueError("floor_used must be >=0 and cap positive")
    path = Path(path or ledger_path())
    month = month or _month_key()
    lock = path.with_suffix(path.suffix + ".lock")
    fd = _acquire_lock(lock)
    try:
        os.close(fd)
        cur = read_ledger(path, month=month)
        if cur.get("corrupt"):
            raise RuntimeError("Google budget ledger is corrupt; refuse live seeding")
        used = max(_safe_int(cur.get("used")), int(floor_used))
        obj = {
            "version": 1, "month": month, "used": used, "cap": cap,
            "updated_at": datetime.now().astimezone().isoformat(),
            "reservation_semantics": "COUNT_BEFORE_HTTP_ATTEMPT",
            "seed_floor": int(floor_used),
        }
        _atomic_write_json(path, obj)
        return {"ok": used <= cap, "month": month, "used": used, "cap": cap,
                "remaining": max(0, cap-used), "reason": "SEEDED_FLOOR"}
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass



def repair_ledger_exact(*, expected_current: int, corrected_used: int, repair_reason: str,
                        cap: int = DEFAULT_MONTHLY_CAP, path: Optional[Path] = None,
                        month: Optional[str] = None) -> dict:
    """Lower the ledger only when the current value matches an exact known-bug value.

    This is intentionally stricter than normal seeding: no fuzzy correction, no max/min
    guessing. It exists only for migrations of bugs whose inflated value can be proven.
    """
    if cap < 1 or expected_current < 0 or corrected_used < 0 or corrected_used > expected_current:
        raise ValueError("invalid ledger repair values")
    repair_reason = str(repair_reason or "KNOWN_COUNTER_BUG").strip()
    path = Path(path or ledger_path())
    month = month or _month_key()
    lock = path.with_suffix(path.suffix + ".lock")
    fd = _acquire_lock(lock)
    try:
        os.close(fd)
        cur = read_ledger(path, month=month)
        if cur.get("corrupt"):
            raise RuntimeError("Google budget ledger is corrupt; refuse repair")
        current = _safe_int(cur.get("used"))
        if current != int(expected_current):
            raise RuntimeError(
                f"Refuse ledger repair: current={current} expected_bug_value={expected_current}"
            )
        obj = {
            "version": 1, "month": month, "used": int(corrected_used), "cap": cap,
            "updated_at": datetime.now().astimezone().isoformat(),
            "reservation_semantics": "COUNT_BEFORE_HTTP_ATTEMPT",
            "repair_reason": repair_reason,
            "repaired_from": int(expected_current),
        }
        _atomic_write_json(path, obj)
        return {"ok": corrected_used <= cap, "month": month, "used": int(corrected_used),
                "cap": cap, "remaining": max(0, cap-int(corrected_used)),
                "reason": repair_reason}
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def repair_ledger_known_double_count(*, expected_inflated: int, corrected_used: int,
                                     cap: int = DEFAULT_MONTHLY_CAP,
                                     path: Optional[Path] = None, month: Optional[str] = None) -> dict:
    """Backward-compatible wrapper for the original v3.11 double-count repair."""
    return repair_ledger_exact(
        expected_current=expected_inflated, corrected_used=corrected_used,
        repair_reason="V3_11_DOUBLE_COUNT_LOCAL_PLUS_EXTERNAL_OFFSET",
        cap=cap, path=path, month=month)

def reserve_before_http(*, cap: int = DEFAULT_MONTHLY_CAP, count: int = 1,
                        path: Optional[Path] = None, month: Optional[str] = None) -> dict:
    """Atomically reserve billable attempts BEFORE network I/O.

    This intentionally over-counts rather than under-counts if the process crashes
    after reservation but before HTTP. That is the safe failure mode for a hard cap.
    """
    if cap < 1 or count < 1:
        raise ValueError("cap and count must be positive")
    path = Path(path or ledger_path())
    month = month or _month_key()
    lock = path.with_suffix(path.suffix + ".lock")
    fd = _acquire_lock(lock)
    try:
        os.close(fd)
        cur = read_ledger(path, month=month)
        if cur.get("corrupt"):
            raise RuntimeError("Google budget ledger is corrupt; refuse live reservation")
        used = _safe_int(cur.get("used"))
        if used + count > cap:
            return {"ok": False, "month": month, "used": used, "cap": cap,
                    "remaining": max(0, cap - used), "reason": "MONTHLY_HARD_CAP"}
        new_used = used + count
        obj = {
            "version": 1, "month": month, "used": new_used, "cap": cap,
            "updated_at": datetime.now().astimezone().isoformat(),
            "reservation_semantics": "COUNT_BEFORE_HTTP_ATTEMPT",
        }
        _atomic_write_json(path, obj)
        return {"ok": True, "month": month, "used": new_used, "cap": cap,
                "remaining": max(0, cap - new_used), "reason": "RESERVED"}
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def detect_local_report_attempts(report_dir: Path, *, month: Optional[str] = None) -> dict:
    """Conservative local-history estimate of HTTP attempts for a YYYY-MM month.

    Each diagnostic comparison row represents one selected network case in the
    preview tooling. This intentionally counts repeated queries/reruns because the
    provider also bills repeated HTTP attempts.
    """
    month = month or _month_key()
    yyyymm = month.replace("-", "")
    total = 0
    files = []
    for p in sorted(Path(report_dir).glob(f"google_comparison_v3_{yyyymm}*.csv")):
        try:
            with p.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f, delimiter=";")
                next(reader, None)
                n = sum(1 for _ in reader)
            total += n
            files.append({"file": p.name, "rows": n})
        except Exception:
            # Do not make the estimate smaller due to a malformed report silently.
            files.append({"file": p.name, "rows": None, "warning": "UNREADABLE"})
    return {"month": month, "attempts": total, "files": files}


def budget_priority(row: dict) -> tuple[int, str]:
    if not str(row.get("future_google_eligible") or "").strip().lower() in {"true", "1", "yes"}:
        return (99, "NOT_ELIGIBLE")
    group = str(row.get("future_google_group") or "")
    addr = elig.address_parts({"indirizzo": row.get("indirizzo") or "", "comune": row.get("comune") or ""})
    has_civic = bool(addr.get("civic"))
    if group == "REVIEW_DISCORDANZA":
        return (1, "P1_DISCORDANZA_PVP_ANNCSU")
    if group == "SENZA_COORDINATE_PVP" and has_civic:
        return (2, "P2_SENZA_PVP_CON_CIVICO")
    if group == "PVP_NON_CONFERMATO" and has_civic:
        return (3, "P3_PVP_NON_CONFERMATO_CON_CIVICO")
    if group == "SENZA_COORDINATE_PVP":
        return (4, "P4_SENZA_PVP_TOPONIMO")
    if group == "PVP_NON_CONFERMATO":
        return (5, "P5_PVP_NON_CONFERMATO_TOPONIMO")
    return (98, "ELIGIBLE_UNCLASSIFIED")


def schedule_rows(detail_rows: list[dict], *, cap: int, used: int) -> tuple[list[dict], dict]:
    if cap < 1:
        raise ValueError("monthly cap must be positive")
    used = max(0, used)
    remaining = max(0, cap - used)
    candidates = []
    for idx, row in enumerate(detail_rows):
        rank, label = budget_priority(row)
        row["budget_priority_rank"] = rank if rank < 90 else ""
        row["budget_priority"] = label
        row["budget_selected"] = False
        row["budget_reason"] = "NOT_GOOGLE_ELIGIBLE" if rank == 99 else ""
        if rank < 90:
            candidates.append((rank, idx, row))
    # Stable, deterministic: P1..P5, preserving original pipeline order within each class.
    candidates.sort(key=lambda x: (x[0], x[1]))
    selected = candidates[:remaining]
    selected_ids = {id(r) for _, _, r in selected}
    scheduled_counts = Counter()
    deferred_counts = Counter()
    for rank, _, row in candidates:
        if id(row) in selected_ids:
            row["budget_selected"] = True
            row["budget_reason"] = "SCHEDULED_WITHIN_MONTHLY_CAP"
            scheduled_counts[row["budget_priority"]] += 1
        else:
            row["budget_selected"] = False
            row["budget_reason"] = "DEFERRED_MONTHLY_CAP_MAP_FALLBACK_RETAINED"
            deferred_counts[row["budget_priority"]] += 1
    scheduled_n = len(selected)
    return detail_rows, {
        "cap": cap,
        "used_before": used,
        "remaining_before": remaining,
        "eligible_rows": len(candidates),
        "scheduled_rows": scheduled_n,
        "deferred_rows": len(candidates) - scheduled_n,
        "remaining_after_scheduled": max(0, remaining - scheduled_n),
        "scheduled_priority_counts": dict(scheduled_counts),
        "deferred_priority_counts": dict(deferred_counts),
    }


def preview_new_rows(listings, *, max_rows: int, monthly_cap: int = DEFAULT_MONTHLY_CAP,
                     monthly_used_override: Optional[int] = None):
    """Production budget dry-run. ZERO Google calls and ZERO DB writes."""
    if monthly_cap < 1:
        raise RuntimeError("--google-monthly-cap must be positive")
    if monthly_used_override is not None and monthly_used_override < 0:
        raise RuntimeError("--google-monthly-used cannot be negative")

    base = v39.preview_new_rows(listings, max_rows=max_rows)
    p = Path(base["policy_preview"]["report_csv"])
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        detail = list(reader)
        base_fields = list(reader.fieldnames or [])

    month = _month_key()
    ledger = read_ledger(month=month)
    detected = detect_local_report_attempts(Path(REPORTS_DIR), month=month)
    if monthly_used_override is None:
        used = max(_safe_int(ledger.get("used")), _safe_int(detected.get("attempts")))
        usage_basis = "MAX_LEDGER_AND_LOCAL_REPORT_HISTORY"
    else:
        used = max(_safe_int(monthly_used_override), _safe_int(ledger.get("used")), _safe_int(detected.get("attempts")))
        usage_basis = "MAX_EXPLICIT_LEDGER_AND_LOCAL_REPORT_HISTORY"

    detail, budget = schedule_rows(detail, cap=monthly_cap, used=used)
    ensure_runtime_dirs()
    out = Path(REPORTS_DIR) / ("google_budget_preflight_v3_10_" + _stamp() + ".csv")
    extra = ["budget_priority_rank", "budget_priority", "budget_selected", "budget_reason"]
    fields = base_fields + [x for x in extra if x not in base_fields]
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";", extrasaction="ignore")
        w.writeheader(); w.writerows(detail)

    # Deferred cases retain their already-computed offline/PVP/ANNCSU/COMUNE map point.
    deferred_mapped = sum(
        1 for r in detail
        if r.get("budget_reason") == "DEFERRED_MONTHLY_CAP_MAP_FALLBACK_RETAINED"
        and str(r.get("map_lat") or "").strip() and str(r.get("map_lon") or "").strip()
    )
    budget.update({
        "version": VERSION,
        "month": month,
        "usage_basis": usage_basis,
        "ledger_used": _safe_int(ledger.get("used")),
        "ledger_source": ledger.get("source"),
        "local_report_attempts_detected": _safe_int(detected.get("attempts")),
        "local_report_files_counted": len(detected.get("files") or []),
        "explicit_used_override": monthly_used_override,
        "effective_used_before": used,
        "deferred_rows_still_mapped": deferred_mapped,
        "report_csv": str(out),
        "base_policy_report_csv": str(p),
        "safety_note": "Local report history is only a conservative app-side estimate; Cloud Console quota remains the independent provider-side guard.",
    })
    base["policy_preview"]["production_budget_preflight"] = budget
    base["policy_preview"]["version"] = VERSION
    base["policy_preview"]["mode"] = "OFFLINE_PRODUCTION_BUDGET_SIMULATION_NO_DB_WRITES_NO_GOOGLE_CALLS"
    base["policy_preview"]["report_csv"] = str(out)
    return base
