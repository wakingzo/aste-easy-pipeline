#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
for p in (SRC, HERE):
    if str(p) not in __import__("sys").path:
        __import__("sys").path.insert(0, str(p))

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env

VERSION = "listings_coordinate_coverage_v1"
PAGE = 1000

def _get(base, key, path, params):
    url = base.rstrip("/") + "/rest/v1/" + path + "?" + urlencode(params, safe="(),.*")
    req = Request(url, method="GET", headers={
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    })
    with urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8") or "[]")

def _all(base, key, path, params):
    out = []
    offset = 0
    while True:
        p = dict(params)
        p["limit"] = str(PAGE)
        p["offset"] = str(offset)
        rows = _get(base, key, path, p)
        out.extend(rows)
        if len(rows) < PAGE:
            break
        offset += len(rows)
    return out

def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None

def valid_pair(lat, lon):
    la, lo = _f(lat), _f(lon)
    return (
        la is not None and lo is not None
        and -90 <= la <= 90 and -180 <= lo <= 180
        and not (la == 0 and lo == 0)
    )

def classify(row):
    lat = _f(row.get("latitudine"))
    lon = _f(row.get("longitudine"))
    if lat is None and lon is None:
        return "BOTH_MISSING"
    if lat is None or lon is None:
        return "PARTIAL_COORDS"
    if not valid_pair(lat, lon):
        return "INVALID_COORDS"
    return "VALID_COORDS"

def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only audit of listings map-coordinate coverage")
    ap.add_argument("--sample", type=int, default=50)
    args = ap.parse_args(argv)

    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE"))
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE"]

    rows = _all(base, key, "listings", {
        "select": "listing_id,pvp_id,status,data_asta_date,regione,provincia,comune,indirizzo,latitudine,longitudine,maps_link,geocode_status,geocode_source,geocode_precision,created_at",
        "order": "pvp_id.asc",
    })

    today = date.today().isoformat()
    coord_counts = Counter()
    geocode_counts = Counter()
    status_counts = Counter()
    source_counts = Counter()
    precision_counts = Counter()

    active = []
    upcoming_active = []
    missing_any = []
    missing_upcoming_active = []
    missing_maps_with_coords = []
    done_without_coords = []
    pending_without_coords = []

    for r in rows:
        coord = classify(r)
        coord_counts[coord] += 1
        gs = str(r.get("geocode_status") or "NULL")
        geocode_counts[gs] += 1
        st = str(r.get("status") or "NULL")
        status_counts[st] += 1
        source_counts[str(r.get("geocode_source") or "NULL")] += 1
        precision_counts[str(r.get("geocode_precision") or "NULL")] += 1

        is_active = st.lower() == "active"
        d = str(r.get("data_asta_date") or "")
        is_upcoming = bool(d and d >= today)

        if is_active:
            active.append(r)
        if is_active and is_upcoming:
            upcoming_active.append(r)

        if coord != "VALID_COORDS":
            missing_any.append(r)
            if is_active and is_upcoming:
                missing_upcoming_active.append(r)

        if coord == "VALID_COORDS" and not str(r.get("maps_link") or "").strip():
            missing_maps_with_coords.append(r)

        if gs.upper() == "DONE" and coord != "VALID_COORDS":
            done_without_coords.append(r)

        if gs.upper() == "PENDING" and coord != "VALID_COORDS":
            pending_without_coords.append(r)

    def compact(r):
        return {
            "pvp_id": str(r.get("pvp_id") or ""),
            "listing_id": r.get("listing_id"),
            "status": r.get("status"),
            "data_asta_date": r.get("data_asta_date"),
            "regione": r.get("regione"),
            "provincia": r.get("provincia"),
            "comune": r.get("comune"),
            "indirizzo": r.get("indirizzo"),
            "latitudine": r.get("latitudine"),
            "longitudine": r.get("longitudine"),
            "maps_link": r.get("maps_link"),
            "geocode_status": r.get("geocode_status"),
            "geocode_source": r.get("geocode_source"),
            "geocode_precision": r.get("geocode_precision"),
            "coord_state": classify(r),
        }

    summary = {
        "version": VERSION,
        "mode": "READ_ONLY_ZERO_DB_WRITES",
        "today": today,
        "db_listings": len(rows),
        "active_listings": len(active),
        "upcoming_active_listings": len(upcoming_active),
        "coordinate_states": dict(coord_counts),
        "geocode_statuses": dict(geocode_counts),
        "listing_statuses": dict(status_counts),
        "geocode_sources": dict(source_counts),
        "geocode_precisions": dict(precision_counts),
        "missing_or_invalid_coords_all": len(missing_any),
        "missing_or_invalid_coords_upcoming_active": len(missing_upcoming_active),
        "valid_coords_but_missing_maps_link": len(missing_maps_with_coords),
        "done_but_missing_or_invalid_coords": len(done_without_coords),
        "pending_but_missing_or_invalid_coords": len(pending_without_coords),
        "samples": {
            "missing_upcoming_active": [compact(x) for x in missing_upcoming_active[:max(0, args.sample)]],
            "done_without_coords": [compact(x) for x in done_without_coords[:max(0, args.sample)]],
            "pending_without_coords": [compact(x) for x in pending_without_coords[:max(0, args.sample)]],
            "valid_coords_missing_maps_link": [compact(x) for x in missing_maps_with_coords[:max(0, args.sample)]],
        },
    }

    out = REPORTS_DIR / "listings_coordinate_coverage_v1.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("[OUT]", out)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
