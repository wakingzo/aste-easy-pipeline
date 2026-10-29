#!/usr/bin/env python3
"""
Aste Easy - historical locality repair planner v3.18

READ ONLY:
- reads latest locality_consistency_v3_17_1 CSV (or --audit-csv)
- reads current rows from Supabase
- ZERO DB writes
- ZERO Google calls
- produces JSON + CSV plan under runtime/reports

Purpose:
Recover historical explicit-locality records that were left AMBER by the
conservative v3.17 audit, while preserving any CIVICO/STRADA-quality point.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path


ROOT = Path(os.environ.get("ASTE_EASY_ROOT", "/app")).resolve()
REPORTS = ROOT / "runtime" / "reports"

PRECISE_PRECISIONS = {
    "CIVICO",
    "STRADA",
    "PVP_CIVICO_COMPATIBILE",
    "PVP_STRADA_COMPATIBILE",
}

COARSE_PRECISIONS = {
    "COMUNE",
    "PVP_IN_COMUNE",
    "PVP_INDICATIVO_ALTRO_COMUNE",
}

# Sources whose semantics are explicitly municipality/coarse fallback.
COARSE_SOURCES = {
    "google_missing_comune",
    "istat_comune_indicativo",
    "istat_corrected_boundary_comune",
    "istat_comune_fallback_v312",
    "pvp_comune_compatibile",
}

TARGET_CLASS = "LOCALITY_DISTANCE_WARN_ISTAT_POINT"


def clean(v) -> str:
    return "" if v is None else str(v).strip()


def fnum(v):
    try:
        if v in (None, ""):
            return None
        return float(v)
    except Exception:
        return None


def latest_audit_csv() -> Path:
    files = sorted(
        REPORTS.glob("locality_consistency_v3_17_1_*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise SystemExit(
            "ERRORE: nessun locality_consistency_v3_17_1_*.csv in "
            f"{REPORTS}. Passa --audit-csv PATH."
        )
    return files[0]


def load_audit(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def is_base_candidate(r: dict, min_distance_m: float) -> bool:
    if clean(r.get("severity")).upper() != "AMBER":
        return False
    if clean(r.get("classification")) != TARGET_CLASS:
        return False
    if clean(r.get("ref_source")) != "ISTAT_LOCALITA_2021":
        return False
    if clean(r.get("ref_strength")) != "EXACT_UNIQUE":
        return False
    if clean(r.get("reference_warning")):
        return False
    d = fnum(r.get("distance_to_ref_m"))
    if d is None or d < min_distance_m:
        return False
    if fnum(r.get("ref_lat")) is None or fnum(r.get("ref_lon")) is None:
        return False
    return True


def supabase_get_rows(ids: list[str]) -> dict[str, dict]:
    base = clean(os.environ.get("SUPABASE_URL")).rstrip("/")
    key = clean(os.environ.get("SUPABASE_SERVICE_ROLE"))
    if not base or not key:
        raise SystemExit("ERRORE: SUPABASE_URL / SUPABASE_SERVICE_ROLE non impostate.")

    wanted = (
        "pvp_id,indirizzo,comune,provincia,latitudine,longitudine,"
        "geocode_status,geocode_precision,geocode_source,geocode_location_type"
    )
    out: dict[str, dict] = {}

    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        params = urllib.parse.urlencode([
            ("select", wanted),
            ("pvp_id", "in.(" + ",".join(chunk) + ")"),
        ])
        req = urllib.request.Request(
            base + "/rest/v1/listings?" + params,
            headers={
                "apikey": key,
                "Authorization": "Bearer " + key,
                "Accept": "application/json",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for row in data:
            out[clean(row.get("pvp_id"))] = row

    return out


def coords_same(audit: dict, db: dict, tol=1e-7) -> bool:
    pairs = [
        (fnum(audit.get("current_lat")), fnum(db.get("latitudine"))),
        (fnum(audit.get("current_lon")), fnum(db.get("longitudine"))),
    ]
    for a, b in pairs:
        if a is None or b is None:
            return a is None and b is None
        if abs(a - b) > tol:
            return False
    return True


def classify(audit: dict, db: dict | None) -> tuple[str, str]:
    if db is None:
        return "REVIEW_DB_MISSING", "record_not_returned_by_supabase"

    if not coords_same(audit, db):
        return "REVIEW_STALE_DB_CHANGED", "coordinates_changed_since_audit"

    precision = clean(db.get("geocode_precision") or audit.get("current_precision")).upper()
    source = clean(db.get("geocode_source") or audit.get("current_source"))
    loc_type = clean(db.get("geocode_location_type")).upper()

    if precision in PRECISE_PRECISIONS:
        return "KEEP_PRECISE", "precise_civico_or_strada_never_replaced"

    if precision in COARSE_PRECISIONS:
        return "READY_SAFE_COARSE", f"coarse_precision:{precision}"

    if source in COARSE_SOURCES:
        return "READY_SAFE_COARSE", f"coarse_source:{source}"

    # Legacy records pre-dating geocode_precision.
    # Only old generic Google + APPROXIMATE is promoted automatically.
    if not precision and source == "google" and loc_type == "APPROXIMATE":
        return "READY_SAFE_LEGACY_APPROX", "legacy_google_approximate"

    if source == "google_missing_civico":
        return "REVIEW_POSSIBLY_PRECISE", "legacy_google_missing_civico"

    if source == "google_corrected_pvp":
        return "REVIEW_PVP_DERIVED", "preserve_pvp_derived_until_manual_review"

    return "REVIEW_LEGACY_UNKNOWN", (
        f"precision={precision or '<blank>'};"
        f"source={source or '<blank>'};"
        f"location_type={loc_type or '<blank>'}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit-csv", default="")
    ap.add_argument("--min-distance-m", type=float, default=2000.0)
    args = ap.parse_args()

    audit_csv = Path(args.audit_csv).resolve() if args.audit_csv else latest_audit_csv()
    rows = load_audit(audit_csv)
    candidates = [r for r in rows if is_base_candidate(r, args.min_distance_m)]
    ids = [clean(r.get("pvp_id")) for r in candidates]

    db_by_id = supabase_get_rows(ids)

    plan = []
    for r in candidates:
        pid = clean(r.get("pvp_id"))
        db = db_by_id.get(pid)
        action, reason = classify(r, db)

        plan.append({
            "pvp_id": pid,
            "indirizzo": clean((db or {}).get("indirizzo") or r.get("indirizzo")),
            "comune": clean((db or {}).get("comune") or r.get("comune")),
            "provincia": clean((db or {}).get("provincia") or r.get("provincia")),
            "current_lat": fnum((db or {}).get("latitudine")),
            "current_lon": fnum((db or {}).get("longitudine")),
            "current_status": clean((db or {}).get("geocode_status")),
            "current_precision": clean((db or {}).get("geocode_precision")),
            "current_source": clean((db or {}).get("geocode_source")),
            "current_location_type": clean((db or {}).get("geocode_location_type")),
            "locality_hint": clean(r.get("hint")),
            "istat_name": clean(r.get("ref_name")),
            "istat_strength": clean(r.get("ref_strength")),
            "istat_lat": fnum(r.get("ref_lat")),
            "istat_lon": fnum(r.get("ref_lon")),
            "distance_to_istat_m": fnum(r.get("distance_to_ref_m")),
            "reference_warning": clean(r.get("reference_warning")),
            "plan_action": action,
            "plan_reason": reason,
        })

    counts = Counter(x["plan_action"] for x in plan)
    ready = [
        x for x in plan
        if x["plan_action"] in {"READY_SAFE_COARSE", "READY_SAFE_LEGACY_APPROX"}
    ]

    now = datetime.now().astimezone()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    REPORTS.mkdir(parents=True, exist_ok=True)
    out_json = REPORTS / f"historical_locality_repair_plan_v3_18_{stamp}.json"
    out_csv = REPORTS / f"historical_locality_repair_plan_v3_18_{stamp}.csv"

    report = {
        "version": "historical_locality_repair_plan_v3_18",
        "mode": "READ_ONLY_ZERO_DB_WRITES_ZERO_GOOGLE",
        "created_at": now.isoformat(),
        "audit_csv": str(audit_csv),
        "policy": {
            "severity": "AMBER",
            "classification": TARGET_CLASS,
            "ref_source": "ISTAT_LOCALITA_2021",
            "ref_strength": "EXACT_UNIQUE",
            "min_distance_m": args.min_distance_m,
            "reference_warning": "must_be_empty",
            "precise_points_never_replaced": sorted(PRECISE_PRECISIONS),
            "legacy_google_rule": "blank precision + source=google + location_type=APPROXIMATE",
        },
        "audit_rows": len(rows),
        "base_candidates": len(candidates),
        "supabase_rows_returned": len(db_by_id),
        "counts": dict(sorted(counts.items())),
        "ready_safe_count": len(ready),
        "ready_safe_ids": [x["pvp_id"] for x in ready],
        "plan": plan,
        "csv": str(out_csv),
    }

    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = list(plan[0].keys()) if plan else [
        "pvp_id", "indirizzo", "comune", "provincia", "plan_action", "plan_reason"
    ]
    with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(plan)

    print(json.dumps({
        "version": report["version"],
        "mode": report["mode"],
        "audit_csv": str(audit_csv),
        "audit_rows": len(rows),
        "base_candidates": len(candidates),
        "supabase_rows_returned": len(db_by_id),
        "counts": report["counts"],
        "ready_safe_count": len(ready),
        "migliarina_4620724": next(
            (x for x in plan if x["pvp_id"] == "4620724"), None
        ),
        "json": str(out_json),
        "csv": str(out_csv),
    }, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
