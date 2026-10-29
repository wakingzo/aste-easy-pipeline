#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aste Easy - Historical Geocache Builder v1

Builds a small persistent SQLite cache from trustworthy geocodes already
present in Supabase.

READ ONLY toward Supabase.
ZERO Google calls.
Writes only local runtime/cache + runtime/reports.

The cache is intentionally conservative:
- accepts explicit CIVICO/STRADA precision classes
- accepts LOCALITA only when produced by the verified ISTAT locality guard
- rejects COMUNE, PVP_IN_COMUNE, suspect/unknown legacy records
- exact normalized address+admin key only
- if the same key has materially conflicting coordinates (>250 m), it is
  marked unusable rather than choosing one blindly
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

VERSION = "historical_geocache_v1"
PAGE_SIZE = 1000
CONFLICT_M = 250.0

ROOT = Path(os.environ.get("ASTE_PROJECT_ROOT", "/app")).resolve()
CACHE_DIR = Path(os.environ.get("ASTE_RUNTIME_CACHE_DIR", str(ROOT / "runtime" / "cache"))).resolve()
REPORTS_DIR = ROOT / "runtime" / "reports"
DEFAULT_DB = CACHE_DIR / "historical_geocache_v1.sqlite3"

FIELDS = (
    "pvp_id,indirizzo,comune,provincia,sigla_provincia,cap,"
    "latitudine,longitudine,geocode_status,geocode_precision,"
    "geocode_source,geocode_location_type,geocode_version"
)

QUALITY = {
    "CIVICO": 50,
    "PVP_CIVICO_COMPATIBILE": 45,
    "STRADA": 40,
    "PVP_STRADA_COMPATIBILE": 35,
    "LOCALITA": 25,
}

PRECISE = {
    "CIVICO",
    "PVP_CIVICO_COMPATIBILE",
    "STRADA",
    "PVP_STRADA_COMPATIBILE",
}

LOCALITA_SAFE_SOURCES = {
    "locality_guard_istat_localita_2021",
    "historical_geocache_v1",
}

LOCATION_TYPE_BONUS = {
    "ROOFTOP": 5,
    "RANGE_INTERPOLATED": 3,
    "GEOMETRIC_CENTER": 1,
    "APPROXIMATE": 0,
    "LOCALITA": 0,
}


def clean(v) -> str:
    return "" if v is None else str(v).strip()


def fnum(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def valid_coords(lat, lon):
    lat, lon = fnum(lat), fnum(lon)
    return (
        lat is not None and lon is not None
        and -90 <= lat <= 90 and -180 <= lon <= 180
        and not (abs(lat) < 1e-9 and abs(lon) < 1e-9)
    )


def norm_text(v: str) -> str:
    s = clean(v).casefold()
    s = s.replace("’", "'").replace("`", "'").replace("´", "'")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("°", " ").replace("º", " ")
    s = re.sub(r"\b(?:n|num|numero)\s*[.:#-]*\s*(?=\d)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_cap(v: str) -> str:
    d = re.sub(r"\D", "", clean(v))
    return d[:5] if len(d) >= 5 else d


def province_key(row: dict) -> str:
    sigla = norm_text(row.get("sigla_provincia"))
    if sigla:
        return sigla
    return norm_text(row.get("provincia"))


def canonical_parts(row: dict):
    return (
        norm_text(row.get("comune")),
        province_key(row),
        norm_cap(row.get("cap")),
        norm_text(row.get("indirizzo")),
    )


def fingerprint(row: dict):
    comune, prov, cap, addr = canonical_parts(row)
    if not comune or not addr:
        return None, None
    canonical = "|".join((comune, prov, cap, addr))
    fp = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return fp, canonical


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def row_quality(row: dict):
    precision = clean(row.get("geocode_precision")).upper()
    source = clean(row.get("geocode_source"))

    if precision in PRECISE:
        base = QUALITY[precision]
    elif precision == "LOCALITA" and source in LOCALITA_SAFE_SOURCES:
        base = QUALITY["LOCALITA"]
    else:
        return None

    loc_type = clean(row.get("geocode_location_type")).upper()
    return base + LOCATION_TYPE_BONUS.get(loc_type, 0)


def supabase_rows():
    base = clean(os.environ.get("SUPABASE_URL")).rstrip("/")
    key = clean(os.environ.get("SUPABASE_SERVICE_ROLE"))
    if not base or not key:
        raise SystemExit("ERRORE: SUPABASE_URL / SUPABASE_SERVICE_ROLE mancanti.")

    offset = 0
    page = 0
    total = 0

    while True:
        params = urllib.parse.urlencode([
            ("select", FIELDS),
            ("order", "pvp_id.asc"),
            ("limit", str(PAGE_SIZE)),
            ("offset", str(offset)),
        ])
        req = urllib.request.Request(
            base + "/rest/v1/listings?" + params,
            headers={
                "apikey": key,
                "Authorization": "Bearer " + key,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=90) as r:
            rows = json.loads(r.read().decode("utf-8") or "[]")

        page += 1
        total += len(rows)
        print(f"[GEOCACHE] page={page} rows={len(rows)} total={total}", flush=True)

        for row in rows:
            yield row

        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE


def choose_better(existing: dict, row: dict, quality: int):
    old_q = existing["quality_score"]
    if quality > old_q:
        return True
    if quality < old_q:
        return False

    old_lt = clean(existing.get("location_type")).upper()
    new_lt = clean(row.get("geocode_location_type")).upper()
    return LOCATION_TYPE_BONUS.get(new_lt, 0) > LOCATION_TYPE_BONUS.get(old_lt, 0)


def build_index():
    all_rows = 0
    eligible_rows = 0
    excluded = Counter()
    by_precision = Counter()
    by_source = Counter()

    entries = {}

    for row in supabase_rows():
        all_rows += 1
        lat, lon = fnum(row.get("latitudine")), fnum(row.get("longitudine"))
        if not valid_coords(lat, lon):
            excluded["invalid_or_missing_coords"] += 1
            continue

        quality = row_quality(row)
        if quality is None:
            excluded["quality_not_eligible"] += 1
            continue

        fp, canonical = fingerprint(row)
        if not fp:
            excluded["missing_address_or_comune"] += 1
            continue

        eligible_rows += 1
        precision = clean(row.get("geocode_precision")).upper()
        source = clean(row.get("geocode_source"))
        by_precision[precision] += 1
        by_source[source or "<blank>"] += 1

        comune, prov, cap, addr = canonical_parts(row)

        if fp not in entries:
            entries[fp] = {
                "fingerprint": fp,
                "canonical_key": canonical,
                "comune_norm": comune,
                "provincia_norm": prov,
                "cap_norm": cap,
                "indirizzo_norm": addr,
                "lat": lat,
                "lon": lon,
                "precision": precision,
                "source": source,
                "location_type": clean(row.get("geocode_location_type")),
                "geocode_version": clean(row.get("geocode_version")),
                "quality_score": quality,
                "sample_pvp_id": clean(row.get("pvp_id")),
                "sample_indirizzo": clean(row.get("indirizzo")),
                "observations": 1,
                "near_observations": 1,
                "conflict_observations": 0,
                "max_distance_m": 0.0,
                "usable": 1,
            }
            continue

        e = entries[fp]
        e["observations"] += 1
        d = haversine_m(e["lat"], e["lon"], lat, lon)
        e["max_distance_m"] = max(e["max_distance_m"], d)

        if d > CONFLICT_M:
            e["conflict_observations"] += 1
            e["usable"] = 0
        else:
            e["near_observations"] += 1

        if choose_better(e, row, quality):
            e.update({
                "lat": lat,
                "lon": lon,
                "precision": precision,
                "source": source,
                "location_type": clean(row.get("geocode_location_type")),
                "geocode_version": clean(row.get("geocode_version")),
                "quality_score": quality,
                "sample_pvp_id": clean(row.get("pvp_id")),
                "sample_indirizzo": clean(row.get("indirizzo")),
            })

    return {
        "all_rows": all_rows,
        "eligible_rows": eligible_rows,
        "excluded": excluded,
        "by_precision": by_precision,
        "by_source": by_source,
        "entries": entries,
    }


def write_sqlite(dest: Path, built: dict):
    dest.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=dest.name + ".tmp.",
        suffix=".sqlite3",
        dir=str(dest.parent),
    )
    os.close(fd)
    tmp = Path(tmp_name)

    try:
        con = sqlite3.connect(tmp)
        cur = con.cursor()

        cur.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE geocache (
            fingerprint TEXT PRIMARY KEY,
            canonical_key TEXT NOT NULL,
            comune_norm TEXT NOT NULL,
            provincia_norm TEXT NOT NULL,
            cap_norm TEXT NOT NULL,
            indirizzo_norm TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            precision TEXT NOT NULL,
            source TEXT NOT NULL,
            location_type TEXT NOT NULL,
            geocode_version TEXT NOT NULL,
            quality_score INTEGER NOT NULL,
            sample_pvp_id TEXT NOT NULL,
            sample_indirizzo TEXT NOT NULL,
            observations INTEGER NOT NULL,
            near_observations INTEGER NOT NULL,
            conflict_observations INTEGER NOT NULL,
            max_distance_m REAL NOT NULL,
            usable INTEGER NOT NULL
        );

        CREATE INDEX idx_geocache_usable ON geocache(usable);
        CREATE INDEX idx_geocache_admin_addr
            ON geocache(comune_norm, provincia_norm, cap_norm, indirizzo_norm);
        """)

        created = datetime.now().astimezone().isoformat()
        meta = {
            "version": VERSION,
            "created_at": created,
            "conflict_threshold_m": str(CONFLICT_M),
            "policy": "CIVICO/STRADA explicit precision + verified ISTAT LOCALITA only",
        }
        cur.executemany(
            "INSERT INTO meta(key,value) VALUES (?,?)",
            list(meta.items()),
        )

        sql = """
        INSERT INTO geocache(
            fingerprint, canonical_key, comune_norm, provincia_norm, cap_norm,
            indirizzo_norm, lat, lon, precision, source, location_type,
            geocode_version, quality_score, sample_pvp_id, sample_indirizzo,
            observations, near_observations, conflict_observations,
            max_distance_m, usable
        ) VALUES (
            :fingerprint, :canonical_key, :comune_norm, :provincia_norm, :cap_norm,
            :indirizzo_norm, :lat, :lon, :precision, :source, :location_type,
            :geocode_version, :quality_score, :sample_pvp_id, :sample_indirizzo,
            :observations, :near_observations, :conflict_observations,
            :max_distance_m, :usable
        )
        """
        cur.executemany(sql, built["entries"].values())
        con.commit()

        qc = cur.execute("PRAGMA quick_check").fetchone()[0]
        if qc != "ok":
            raise RuntimeError(f"SQLite quick_check={qc}")

        con.close()
        os.replace(tmp, dest)

    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, default=DEFAULT_DB)
    args = ap.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print("[GEOCACHE] mode=BUILD_LOCAL_SQLITE | Supabase=READ_ONLY | Google=ZERO", flush=True)
    built = build_index()
    write_sqlite(args.output, built)

    entries = list(built["entries"].values())
    usable = sum(1 for e in entries if e["usable"])
    conflicts = sum(1 for e in entries if not e["usable"])
    repeated = sum(1 for e in entries if e["observations"] > 1)
    repeat_observations = sum(max(0, e["observations"] - 1) for e in entries)

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    report_path = REPORTS_DIR / f"historical_geocache_build_v1_{stamp}.json"

    report = {
        "version": VERSION,
        "mode": "SUPABASE_READ_ONLY_GOOGLE_ZERO",
        "created_at": datetime.now().astimezone().isoformat(),
        "sqlite": str(args.output),
        "sqlite_bytes": args.output.stat().st_size,
        "policy": {
            "eligible_precisions": sorted(PRECISE | {"LOCALITA"}),
            "localita_safe_sources": sorted(LOCALITA_SAFE_SOURCES),
            "exact_normalized_key_only": True,
            "conflict_threshold_m": CONFLICT_M,
            "conflicting_keys_are_unusable": True,
        },
        "supabase_rows_seen": built["all_rows"],
        "eligible_rows": built["eligible_rows"],
        "unique_keys": len(entries),
        "usable_keys": usable,
        "conflicting_keys": conflicts,
        "repeated_keys": repeated,
        "repeat_observations": repeat_observations,
        "excluded": dict(built["excluded"]),
        "eligible_by_precision": dict(built["by_precision"]),
        "eligible_by_source": dict(built["by_source"]),
    }

    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
