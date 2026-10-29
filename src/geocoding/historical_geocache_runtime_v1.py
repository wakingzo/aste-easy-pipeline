#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aste Easy - historical geocache runtime v1.

Conservative exact-key reuse of trustworthy geocodes from prior PVP listings.

Safety:
- exact normalized address + comune/admin key only
- only rows pre-vetted as usable=1
- current-run precise offline evidence always wins
- no fuzzy matching
- conflicting historical keys are never returned
- SQLite is local/persistent runtime state; no network calls here
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import unicodedata
from collections import Counter
from pathlib import Path

VERSION = "historical_geocache_v1"
CONFLICT_M = 250.0

PRECISE = {
    "CIVICO",
    "PVP_CIVICO_COMPATIBILE",
    "STRADA",
    "PVP_STRADA_COMPATIBILE",
}
SAFE_PRECISIONS = PRECISE | {"LOCALITA"}

QUALITY = {
    "CIVICO": 50,
    "PVP_CIVICO_COMPATIBILE": 45,
    "STRADA": 40,
    "PVP_STRADA_COMPATIBILE": 35,
    "LOCALITA": 25,
}
LOCATION_TYPE_BONUS = {
    "ROOFTOP": 5,
    "RANGE_INTERPOLATED": 3,
    "GEOMETRIC_CENTER": 1,
    "APPROXIMATE": 0,
    "LOCALITA": 0,
}
LOCALITA_SAFE_SOURCES = {
    "locality_guard_istat_localita_2021",
    "historical_geocache_v1",
}


def clean(v):
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


def norm_text(v):
    s = clean(v).casefold()
    s = s.replace("’", "'").replace("`", "'").replace("´", "'")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("°", " ").replace("º", " ")
    s = re.sub(r"\b(?:n|num|numero)\s*[.:#-]*\s*(?=\d)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_cap(v):
    d = re.sub(r"\D", "", clean(v))
    return d[:5] if len(d) >= 5 else d


def province_key(row):
    sigla = norm_text(row.get("sigla_provincia"))
    return sigla or norm_text(row.get("provincia"))


def canonical_parts(row):
    return (
        norm_text(row.get("comune")),
        province_key(row),
        norm_cap(row.get("cap")),
        norm_text(row.get("indirizzo")),
    )


def fingerprint(row):
    comune, prov, cap, addr = canonical_parts(row)
    if not comune or not addr:
        return None, None
    canonical = "|".join((comune, prov, cap, addr))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), canonical


def default_path():
    state = os.environ.get("ASTE_RUNTIME_CACHE_DIR", "").strip()
    if state:
        return Path(state).expanduser().resolve() / "historical_geocache_v1.sqlite3"
    root = Path(os.environ.get("ASTE_PROJECT_ROOT", Path(__file__).resolve().parents[2])).resolve()
    return root / "runtime" / "cache" / "historical_geocache_v1.sqlite3"


def _uri(path):
    return "file:" + str(Path(path).resolve()).replace("\\", "/") + "?mode=ro"


def open_optional(path=None):
    path = Path(path or default_path()).resolve()
    if not path.is_file():
        return None, {
            "available": False,
            "path": str(path),
            "reason": "missing",
            "version": VERSION,
        }

    con = sqlite3.connect(_uri(path), uri=True)
    con.row_factory = sqlite3.Row
    try:
        meta = {k: v for k, v in con.execute("SELECT key,value FROM meta")}
        if meta.get("version") != VERSION:
            raise RuntimeError(
                f"historical geocache version mismatch: {meta.get('version')} != {VERSION}"
            )
        qc = con.execute("PRAGMA quick_check").fetchone()[0]
        if qc != "ok":
            raise RuntimeError("historical geocache quick_check=" + str(qc))
        usable = int(con.execute("SELECT count(*) FROM geocache WHERE usable=1").fetchone()[0])
        conflicts = int(con.execute("SELECT count(*) FROM geocache WHERE usable=0").fetchone()[0])
    except Exception:
        con.close()
        raise

    return con, {
        "available": True,
        "path": str(path),
        "bytes": path.stat().st_size,
        "usable_keys": usable,
        "conflicting_keys": conflicts,
        "version": meta.get("version"),
    }


def lookup(con, row):
    if con is None:
        return None
    fp, canonical = fingerprint(row)
    if not fp:
        return None
    r = con.execute(
        """
        SELECT fingerprint,canonical_key,lat,lon,precision,source,location_type,
               geocode_version,quality_score,sample_pvp_id,sample_indirizzo,
               observations,conflict_observations,max_distance_m,usable
        FROM geocache
        WHERE fingerprint=? AND usable=1
        LIMIT 1
        """,
        (fp,),
    ).fetchone()
    if not r:
        return None
    out = dict(r)
    if clean(out.get("canonical_key")) != canonical:
        # SHA collision / schema corruption guard.
        raise RuntimeError("historical geocache canonical-key mismatch")
    if clean(out.get("precision")).upper() not in SAFE_PRECISIONS:
        raise RuntimeError("historical geocache unsafe precision in usable row")
    if not valid_coords(out.get("lat"), out.get("lon")):
        raise RuntimeError("historical geocache invalid coordinates")
    return out


def should_reuse(hit, baseline):
    if not hit:
        return False, "MISS"
    # Fresh current-run precise evidence always wins over historical state.
    bp = clean((baseline or {}).get("geocode_precision")).upper()
    if bp in PRECISE:
        return False, "BYPASS_CURRENT_PRECISE"
    return True, "REUSE"


def final_from_hit(hit):
    precision = clean(hit.get("precision")).upper()
    status = "REVIEW" if precision == "LOCALITA" else "DONE"
    sample = clean(hit.get("sample_pvp_id"))
    source = clean(hit.get("source"))
    obs = int(hit.get("observations") or 1)
    reason = (
        "Riuso geocache storica su chiave indirizzo normalizzata esatta; "
        f"precisione storica={precision}; sorgente storica={source or 'n/d'}; "
        f"sample_pvp_id={sample or 'n/d'}; osservazioni={obs}. "
        "Nessuna chiamata Google eseguita per questo riuso."
    )
    final = {
        "final_status": status,
        "final_source": "historical_geocache_v1",
        "final_precision": precision,
        "final_reason": reason,
        "policy_action": "HISTORICAL_GEOCACHE_REUSE",
    }
    map_data = {
        "map_lat": float(hit["lat"]),
        "map_lon": float(hit["lon"]),
        "map_source": "historical_geocache_v1",
        "map_precision": precision,
        "map_is_fallback": False,
    }
    evidence = {
        "google_location_type": clean(hit.get("location_type")) or precision,
    }
    return final, map_data, evidence


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _payload_quality(payload):
    precision = clean(payload.get("geocode_precision")).upper()
    source = clean(payload.get("geocode_source"))
    if precision in PRECISE:
        pass
    elif precision == "LOCALITA" and source in LOCALITA_SAFE_SOURCES:
        pass
    else:
        return None
    return QUALITY[precision] + LOCATION_TYPE_BONUS.get(
        clean(payload.get("geocode_location_type")).upper(), 0
    )


def update_verified(path, verified_pairs):
    """Incrementally add successful DB PATCH results to the persistent cache.

    verified_pairs: iterable of (input_row, applied_payload).
    A conflict never overwrites silently: the key is marked unusable.
    """
    path = Path(path or default_path()).resolve()
    if not path.is_file():
        return {"updated": 0, "inserted": 0, "conflicts": 0, "skipped": 0, "reason": "cache_missing"}

    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    cnt = Counter()
    try:
        meta = {k: v for k, v in con.execute("SELECT key,value FROM meta")}
        if meta.get("version") != VERSION:
            raise RuntimeError("historical geocache version mismatch during update")

        for row, payload in verified_pairs:
            q = _payload_quality(payload)
            lat, lon = fnum(payload.get("latitudine")), fnum(payload.get("longitudine"))
            fp, canonical = fingerprint(row)
            if q is None or not fp or not valid_coords(lat, lon):
                cnt["skipped"] += 1
                continue

            comune, prov, cap, addr = canonical_parts(row)
            precision = clean(payload.get("geocode_precision")).upper()
            source = clean(payload.get("geocode_source"))
            loc_type = clean(payload.get("geocode_location_type"))
            version = clean(payload.get("geocode_version"))
            pid = clean(row.get("pvp_id"))
            indirizzo = clean(row.get("indirizzo"))

            old = con.execute("SELECT * FROM geocache WHERE fingerprint=?", (fp,)).fetchone()
            if old is None:
                con.execute(
                    """
                    INSERT INTO geocache(
                        fingerprint,canonical_key,comune_norm,provincia_norm,cap_norm,
                        indirizzo_norm,lat,lon,precision,source,location_type,
                        geocode_version,quality_score,sample_pvp_id,sample_indirizzo,
                        observations,near_observations,conflict_observations,max_distance_m,usable
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        fp, canonical, comune, prov, cap, addr, lat, lon, precision,
                        source, loc_type, version, q, pid, indirizzo,
                        1, 1, 0, 0.0, 1,
                    ),
                )
                cnt["inserted"] += 1
                continue

            old = dict(old)
            if clean(old.get("canonical_key")) != canonical:
                raise RuntimeError("historical geocache canonical mismatch during update")

            d = _haversine_m(float(old["lat"]), float(old["lon"]), lat, lon)
            observations = int(old.get("observations") or 0) + 1
            near_obs = int(old.get("near_observations") or 0)
            conflict_obs = int(old.get("conflict_observations") or 0)
            usable = int(old.get("usable") or 0)
            max_d = max(float(old.get("max_distance_m") or 0), d)

            if d > CONFLICT_M:
                conflict_obs += 1
                usable = 0
                cnt["conflicts"] += 1
            else:
                near_obs += 1
                cnt["updated"] += 1

            replace_best = q > int(old.get("quality_score") or 0)
            if replace_best:
                best = (lat, lon, precision, source, loc_type, version, q, pid, indirizzo)
            else:
                best = (
                    old["lat"], old["lon"], old["precision"], old["source"],
                    old["location_type"], old["geocode_version"], old["quality_score"],
                    old["sample_pvp_id"], old["sample_indirizzo"],
                )

            con.execute(
                """
                UPDATE geocache SET
                    lat=?,lon=?,precision=?,source=?,location_type=?,geocode_version=?,
                    quality_score=?,sample_pvp_id=?,sample_indirizzo=?,
                    observations=?,near_observations=?,conflict_observations=?,
                    max_distance_m=?,usable=?
                WHERE fingerprint=?
                """,
                (
                    *best,
                    observations, near_obs, conflict_obs, max_d, usable, fp,
                ),
            )

        con.commit()
    finally:
        con.close()

    return dict(cnt)
