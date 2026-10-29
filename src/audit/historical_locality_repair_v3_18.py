#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aste Easy - historical locality repair executor v3.18

Default: DRY RUN / Supabase GET only / ZERO Google.
--apply is required to PATCH.

Input:
  historical_locality_repair_plan_v3_18_*.json

Eligible rows:
  READY_SAFE_COARSE
  READY_SAFE_LEGACY_APPROX

Fail-closed:
- current DB row must still match the plan snapshot
- address, status, precision, source, location_type and coordinates are rechecked
- CIVICO/STRADA rows are never part of the input safe set
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve()
SRC = HERE.parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from project_env import load_project_env
from aste_paths import REPORTS_DIR, ensure_runtime_dirs

TIMEOUT = 90
VERSION = "historical_locality_repair_v3_18"

SAFE_ACTIONS = {
    "READY_SAFE_COARSE",
    "READY_SAFE_LEGACY_APPROX",
}


def clean(v):
    return "" if v is None else str(v).strip()


def num(v):
    try:
        x = float(v)
    except Exception:
        return None
    return x if math.isfinite(x) else None


def eqnum(a, b, tol=1e-7):
    a = num(a)
    b = num(b)
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


def headers(key, content=False, prefer=None):
    h = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    }
    if content:
        h["Content-Type"] = "application/json"
    if prefer:
        h["Prefer"] = prefer
    return h


def getrow(base, key, pid):
    sel = (
        "pvp_id,indirizzo,comune,provincia,latitudine,longitudine,"
        "geocode_status,geocode_precision,geocode_source,geocode_location_type"
    )
    u = base.rstrip("/") + "/rest/v1/listings?" + urlencode([
        ("select", sel),
        ("pvp_id", "eq." + pid),
        ("limit", "1"),
    ])
    with urlopen(Request(u, headers=headers(key)), timeout=TIMEOUT) as r:
        rows = json.loads(r.read().decode() or "[]")
        return rows[0] if rows else None


def patch(base, key, pid, payload):
    u = base.rstrip("/") + "/rest/v1/listings?" + urlencode([
        ("pvp_id", "eq." + pid)
    ])
    req = Request(
        u,
        method="PATCH",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers(key, True, "return=representation"),
    )
    with urlopen(req, timeout=TIMEOUT) as r:
        rows = json.loads(r.read().decode() or "[]")
        if not isinstance(rows, list) or len(rows) != 1:
            raise RuntimeError("PATCH response inattesa")
        return rows[0]


def latest_plan():
    cand = sorted(
        REPORTS_DIR.glob("historical_locality_repair_plan_v3_18_*.json"),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    if not cand:
        raise SystemExit(
            "ERRORE: nessun historical_locality_repair_plan_v3_18_*.json trovato; "
            "usa --plan FILE.json"
        )
    return cand[0]


def race_ok(plan, now):
    if not now:
        return False, "missing"

    if clean(now.get("indirizzo")).casefold() != clean(plan.get("indirizzo")).casefold():
        return False, "address_changed"

    if clean(now.get("geocode_status")) != clean(plan.get("current_status")):
        return False, "status_changed"

    if clean(now.get("geocode_precision")) != clean(plan.get("current_precision")):
        return False, "precision_changed"

    if clean(now.get("geocode_source")) != clean(plan.get("current_source")):
        return False, "source_changed"

    if clean(now.get("geocode_location_type")) != clean(plan.get("current_location_type")):
        return False, "location_type_changed"

    if not eqnum(now.get("latitudine"), plan.get("current_lat")):
        return False, "lat_changed"

    if not eqnum(now.get("longitudine"), plan.get("current_lon")):
        return False, "lon_changed"

    return True, "ok"


def build_payload(r):
    la = num(r.get("istat_lat"))
    lo = num(r.get("istat_lon"))
    if la is None or lo is None:
        raise ValueError("coordinate ISTAT mancanti")

    locality = clean(r.get("istat_name") or r.get("locality_hint"))
    dist = num(r.get("distance_to_istat_m"))

    return {
        "latitudine": la,
        "longitudine": lo,
        "maps_link": f"https://www.google.com/maps?q={la:.8f}%2C{lo:.8f}",
        "geocode_status": "REVIEW",
        "geocode_precision": "LOCALITA",
        "geocode_location_type": "LOCALITA",
        "geocode_source": "locality_guard_istat_localita_2021",
        "geocode_version": VERSION,
        "geocode_checked_at": datetime.now(timezone.utc).isoformat(),
        "geocode_decision_reason": (
            f"Bonifica storica v3.18: località esplicita {locality!r}; "
            f"pin precedente coarse/incompatibile ({dist:.1f} m dal riferimento ISTAT 2021). "
            "Spostato al punto indicativo della località ISTAT; non prova la posizione esatta dell'immobile."
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", type=Path)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    ensure_runtime_dirs()
    env = load_project_env()
    base = clean(env.get("SUPABASE_URL"))
    key = clean(env.get("SUPABASE_SERVICE_ROLE"))
    if not base or not key:
        raise SystemExit("ERRORE: SUPABASE_URL / SUPABASE_SERVICE_ROLE mancanti")

    plan_path = args.plan or latest_plan()
    report = json.loads(plan_path.read_text(encoding="utf-8"))
    rows = report.get("plan") or []

    targets = [
        r for r in rows
        if clean(r.get("plan_action")) in SAFE_ACTIONS
        and clean(r.get("istat_strength")) == "EXACT_UNIQUE"
        and not clean(r.get("reference_warning"))
        and num(r.get("istat_lat")) is not None
        and num(r.get("istat_lon")) is not None
        and (num(r.get("distance_to_istat_m")) or -1) >= 2000.0
    ]

    excluded = [
        r for r in rows if clean(r.get("plan_action")) not in SAFE_ACTIONS
    ]

    print(f"[HIST LOCALITY REPAIR] plan={plan_path}", flush=True)
    print(
        f"[HIST LOCALITY REPAIR] mode={'APPLY' if args.apply else 'DRY_RUN'} "
        f"targets={len(targets)} excluded_review={len(excluded)}",
        flush=True,
    )
    print("[HIST LOCALITY REPAIR] Google calls: ZERO", flush=True)

    results = []
    cnt = Counter()

    for i, r in enumerate(targets, 1):
        pid = clean(r.get("pvp_id"))
        status = ""
        reason = ""

        try:
            now = getrow(base, key, pid)
            ok, reason = race_ok(r, now)

            if not ok:
                status = "SKIP_RECHECK"
            else:
                payload = build_payload(r)
                if args.apply:
                    patch(base, key, pid, payload)
                    status = "PATCHED"
                    reason = ""
                else:
                    status = "READY_DRYRUN"
                    reason = ""

        except Exception as e:
            status = "ERROR"
            reason = f"{type(e).__name__}: {e}"

        cnt[status] += 1
        x = dict(r)
        x["run_status"] = status
        x["run_reason"] = reason
        results.append(x)

        print(
            f"[{i}/{len(targets)}] {pid} -> {status}"
            + (f" {reason}" if reason else ""),
            flush=True,
        )

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    mode = "apply" if args.apply else "dryrun"
    out = REPORTS_DIR / f"historical_locality_repair_v3_18_{mode}_{stamp}.json"

    summary = {
        "version": VERSION,
        "mode": "APPLY" if args.apply else "DRY_RUN",
        "plan": str(plan_path),
        "google_calls": 0,
        "policy": {
            "safe_actions": sorted(SAFE_ACTIONS),
            "istat_strength": "EXACT_UNIQUE",
            "min_distance_m": 2000.0,
            "reference_warning_required_empty": True,
            "race_guard": [
                "indirizzo",
                "geocode_status",
                "geocode_precision",
                "geocode_source",
                "geocode_location_type",
                "latitudine",
                "longitudine",
            ],
            "target_payload": {
                "geocode_status": "REVIEW",
                "geocode_precision": "LOCALITA",
                "geocode_location_type": "LOCALITA",
                "geocode_source": "locality_guard_istat_localita_2021",
                "geocode_version": VERSION,
            },
        },
        "targets": len(targets),
        "target_ids": [clean(r.get("pvp_id")) for r in targets],
        "excluded_review": len(excluded),
        "excluded_review_ids": [clean(r.get("pvp_id")) for r in excluded],
        "counts": dict(cnt),
        "results": results,
    }

    out.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("[HIST LOCALITY REPAIR] counts:", dict(cnt), flush=True)
    print("[HIST LOCALITY REPAIR] report:", out, flush=True)

    return 0 if not cnt.get("ERROR") else 2


if __name__ == "__main__":
    raise SystemExit(main())
