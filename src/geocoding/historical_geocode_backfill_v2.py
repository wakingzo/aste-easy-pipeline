#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
for p in (SRC, SRC / "geocoding", SRC / "importer", SRC / "pipeline"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from aste_paths import ANNCSU_DIR, DATA_DIR, RAW_DIR, REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env
import address_resolver_v1 as address_resolver
import anncsu_match_v1 as anncsu_match
import locality_match_v1 as locality_match
import unified_offline_v1 as unified_offline
import listing_locations_materializer_v1 as materializer
import primary_pin_publisher_v2_preview as pin_publisher
import istat_boundary_runtime_v11_6 as boundary_runtime
import google_civic_arbitration_v1 as google_civic

VERSION = "historical_geocode_backfill_v2"
PAGE = 1000
MODERN_RESOLVER = "unified_offline_v1"

LOCATION_FIELDS = (
    "pvp_id,bene_index,is_primary,address_raw,comune_pvp,provincia_pvp,"
    "sigla_provincia_pvp,cap_pvp,codice_istat,pvp_latitudine,pvp_longitudine,"
    "street,civic,internal,locality,snc,is_intersection,latitudine,longitudine,"
    "source,precision,confidence,status,formatted_address,anncsu_status,"
    "google_status,warnings,resolution_reason,resolver_version,checked_at"
)

PRECISION_RANK = {
    "": 0,
    "DA_VERIFICARE": 0,
    "INDICATIVA": 1,
    "APPROSSIMATIVA": 2,
    "PRECISA": 3,
}


def _request_json(req):
    with urlopen(req, timeout=120) as response:
        return json.loads(response.read().decode("utf-8") or "null")


def _get(base, key, path, params):
    url = base.rstrip("/") + "/rest/v1/" + path + "?" + urlencode(params, safe="(),.*")
    req = Request(
        url,
        method="GET",
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
        },
    )
    return _request_json(req)


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


def _rpc(base, key, name, payload):
    url = base.rstrip("/") + "/rest/v1/rpc/" + name
    req = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    return _request_json(req)


def _patch(base, key, path, filters, payload):
    url = base.rstrip("/") + "/rest/v1/" + path + "?" + urlencode(filters, safe="(),.*")
    req = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="PATCH",
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=representation",
        },
    )
    return _request_json(req)


def _delete(base, key, path, filters):
    url = base.rstrip("/") + "/rest/v1/" + path + "?" + urlencode(filters, safe="(),.*")
    req = Request(
        url,
        method="DELETE",
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
            "Prefer": "return=representation",
        },
    )
    return _request_json(req)


def _f(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _same_coord(a_lat, a_lon, b_lat, b_lon, tol=1e-8):
    vals = [_f(a_lat), _f(a_lon), _f(b_lat), _f(b_lon)]
    if any(v is None for v in vals):
        return False
    return abs(vals[0] - vals[2]) < tol and abs(vals[1] - vals[3]) < tol


def _haversine_m(a_lat, a_lon, b_lat, b_lon):
    vals = [_f(a_lat), _f(a_lon), _f(b_lat), _f(b_lon)]
    if any(v is None for v in vals):
        return None
    lat1, lon1, lat2, lon2 = vals
    r = 6371008.8
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    z = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(z)))


def _maps_link(lat, lon):
    return f"https://www.google.com/maps?q={float(lat):.8f},{float(lon):.8f}"


def _parent_precision(technical_precision):
    mapping = {
        "CIVICO": "PRECISA",
        "PVP_COORD": "APPROSSIMATIVA",
        "STRADA": "APPROSSIMATIVA",
        "LOCALITA": "INDICATIVA",
        "COMUNE": "INDICATIVA",
    }
    value = str(technical_precision or "").strip().upper()
    if value not in mapping:
        raise RuntimeError(f"Unsupported technical precision: {value or '<blank>'}")
    return mapping[value]


def _safe_to_apply(*, input_source, technical_precision, original_status,
                   location_count, has_conflict, old_pvp_inside, new_pin_safe,
                   admin_matches=True):
    """Rebaseline against RAW + current geocoder, NEVER old claimed precision.

    No synthetic RAW writes, no partial replacement of multi-bene records,
    no unresolved conflicts, no unverified municipality escapes. A COMUNE
    fallback is only acceptable when RAW has no coherent PVP coordinate.
    """
    if input_source != "RAW_PVP_HISTORICAL":
        return False, "RAW_REQUIRED_BEFORE_APPLY"
    if location_count < 1:
        return False, "RAW_BENI_MISSING"
    if admin_matches is False:
        return False, "RAW_SOURCE_COMUNE_CONFLICT"
    if admin_matches is None:
        return False, "RAW_SOURCE_COMUNE_UNRESOLVED"
    if not new_pin_safe:
        return False, "NEW_PIN_NOT_INSIDE_SOURCE_COMUNE"
    tech = str(technical_precision or "").upper()
    if tech not in {"CIVICO", "PVP_COORD", "STRADA", "LOCALITA", "COMUNE"}:
        return False, "UNSUPPORTED_NEW_TECHNICAL_PRECISION"
    if has_conflict and str(original_status or "").upper() != "RESOLVED":
        return False, "UNRESOLVED_NEW_GEOCODER_CONFLICT"
    if tech == "COMUNE":
        if old_pvp_inside:
            return False, "RAW_PVP_INSIDE_BUT_NEW_ONLY_COMUNE"
        return True, "RAW_REBASELINE_INDICATIVE_COMUNE"
    if str(original_status or "").upper() != "RESOLVED":
        return False, "NEW_GEOCODER_REVIEW_NOT_APPLICABLE"
    return True, "RAW_REBASELINE_" + tech


def _parse_ids(raw):
    out = []
    seen = set()
    for token in str(raw or "").replace(";", ",").split(","):
        value = token.strip()
        if not value:
            continue
        if not value.isdigit():
            raise ValueError(f"Invalid pvp_id: {value}")
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _fetch_active_listings(base, key, explicit_ids):
    today = datetime.now().astimezone().date().isoformat()
    params = {
        "select": (
            "listing_id,pvp_id,link_pvp,status,data_asta_date,created_at,regione,provincia,"
            "sigla_provincia,comune,cap,indirizzo,tipologia,latitudine,longitudine,maps_link,"
            "geocode_precision"
        ),
        "status": "eq.active",
        "data_asta_date": "gte." + today,
        "order": "pvp_id.asc",
    }
    if explicit_ids:
        params["pvp_id"] = "in.(" + ",".join(explicit_ids) + ")"
    return _all(base, key, "listings", params)


def _fetch_primary_location_meta(base, key, pvp_ids):
    out = {}
    for start in range(0, len(pvp_ids), 100):
        batch = pvp_ids[start:start + 100]
        rows = _get(
            base,
            key,
            "listing_locations",
            {
                "select": (
                    "pvp_id,resolver_version,source,precision,status,address_raw,"
                    "comune_pvp,provincia_pvp,sigla_provincia_pvp,cap_pvp,codice_istat,"
                    "pvp_latitudine,pvp_longitudine"
                ),
                "pvp_id": "in.(" + ",".join(batch) + ")",
                "is_primary": "eq.true",
                "limit": "500",
            },
        )
        for row in rows:
            out[str(row.get("pvp_id") or "")] = row
    return out


def _fetch_locations_snapshot(base, key, listing_id):
    return _all(
        base,
        key,
        "listing_locations",
        {
            "select": LOCATION_FIELDS,
            "listing_id": "eq." + str(listing_id),
            "order": "bene_index.asc",
        },
    )


def _sanitize_locations_for_rpc(rows):
    keys = LOCATION_FIELDS.split(",")
    out = []
    for row in rows:
        item = {k: row.get(k) for k in keys}
        item["warnings"] = list(item.get("warnings") or [])
        out.append(item)
    return out


def _build_candidates(base, key, explicit_ids, limit):
    listings = _fetch_active_listings(base, key, explicit_ids)
    ids = [str(x.get("pvp_id") or "") for x in listings if x.get("pvp_id")]
    meta = _fetch_primary_location_meta(base, key, ids)
    candidates = []
    skipped_modern = 0
    for row in listings:
        pid = str(row.get("pvp_id") or "")
        primary = meta.get(pid) or {}
        modern = str(primary.get("resolver_version") or "") == MODERN_RESOLVER
        if explicit_ids or not modern:
            item = dict(row)
            item["_legacy_primary"] = primary
            candidates.append(item)
        else:
            skipped_modern += 1
    if limit and limit > 0:
        candidates = candidates[:limit]
    return listings, candidates, skipped_modern


def _load_raw_index(path, wanted_ids):
    path = Path(path)
    wanted = {str(x) for x in wanted_ids}
    found = {}
    scanned = 0
    malformed = 0
    if not path.is_file():
        return found, {
            "raw_file": str(path),
            "raw_file_present": False,
            "raw_rows_scanned": 0,
            "raw_rows_malformed": 0,
            "raw_targets_found": 0,
            "raw_targets_missing": len(wanted),
        }
    with path.open("r", encoding="utf-8", errors="replace") as src:
        for line in src:
            if not line.strip():
                continue
            scanned += 1
            try:
                obj = json.loads(line)
            except Exception:
                malformed += 1
                continue
            pid = str(obj.get("pvp_id") or "")
            if pid in wanted:
                found[pid] = obj
                if len(found) == len(wanted):
                    break
    return found, {
        "raw_file": str(path),
        "raw_file_present": True,
        "raw_rows_scanned": scanned,
        "raw_rows_malformed": malformed,
        "raw_targets_found": len(found),
        "raw_targets_missing": len(wanted) - len(found),
    }


def _synthetic_raw_from_db(listing):
    primary = listing.get("_legacy_primary") or {}
    via = (
        primary.get("address_raw")
        or listing.get("indirizzo")
        or ""
    )
    comune = primary.get("comune_pvp") or listing.get("comune") or ""
    provincia = (
        listing.get("sigla_provincia")
        or primary.get("sigla_provincia_pvp")
        or primary.get("provincia_pvp")
        or listing.get("provincia")
        or ""
    )
    addr = {
        "via": via,
        "cap": primary.get("cap_pvp") or listing.get("cap"),
        "descComune": comune,
        "codComune": primary.get("codice_istat"),
        "codProvincia": provincia,
        "descProvincia": listing.get("provincia") or primary.get("provincia_pvp"),
        "descRegione": listing.get("regione"),
        # Deliberately do not reuse legacy coordinates: they may be the bad pin
        # we are trying to repair.
        "coordinate": {},
    }
    return {
        "pvp_id": str(listing.get("pvp_id") or ""),
        "link_pvp": listing.get("link_pvp"),
        "search_item": {},
        "detail_body": {
            "beni": [
                {
                    "descTipologiaBene": listing.get("tipologia"),
                    "indirizzo": addr,
                }
            ]
        },
    }


def _merge_stored_secondary_locations(fresh_locations, stored_locations):
    """Keep every RAW bene and prevent loss of already saved secondary data.

    RAW bene_index is authoritative for the new shape; legacy secondary fields
    only fill holes, and existing secondary geocodes survive only when the
    original address/municipality matches at the same bene_index. There is
    still exactly ONE primary, and no new secondary map marker is published.
    """
    fresh = [dict(r) for r in fresh_locations]
    if len({r["bene_index"] for r in fresh}) != len(fresh):
        raise RuntimeError("Duplicate RAW bene_index during secondary merge")
    if sum(r.get("is_primary") is True for r in fresh) != 1:
        raise RuntimeError("RAW materialization must contain exactly one primary")

    old = {}
    for row in stored_locations or []:
        idx = row.get("bene_index")
        if idx in old:
            raise RuntimeError("Duplicate stored bene_index during secondary merge")
        old[idx] = row

    # We cannot safely discard extra rows whose identity cannot be reconciled
    # against the primary PVP source. Require a dedicated review instead.
    extra = set(old).difference(r["bene_index"] for r in fresh)
    if extra:
        raise RuntimeError(
            "Stored secondary bene_index missing in RAW: " + ",".join(map(str, sorted(extra)))
        )

    fallback_fields = (
        "address_raw", "comune_pvp", "provincia_pvp", "sigla_provincia_pvp",
        "cap_pvp", "codice_istat", "street", "civic", "internal", "locality",
    )
    result = []
    for item in fresh:
        if item.get("is_primary") is True:
            result.append(item)
            continue
        existing = old.get(item["bene_index"])
        if existing is None:
            result.append(item)
            continue

        used_existing = False
        for k in fallback_fields:
            if item.get(k) in (None, "") and existing.get(k) not in (None, ""):
                item[k] = existing[k]
                used_existing = True
        if (
            item.get("pvp_latitudine") is None
            and item.get("pvp_longitudine") is None
            and existing.get("pvp_latitudine") is not None
            and existing.get("pvp_longitudine") is not None
            and existing.get("is_primary") is False
        ):
            item["pvp_latitudine"] = existing["pvp_latitudine"]
            item["pvp_longitudine"] = existing["pvp_longitudine"]
            used_existing = True

        # Do not silently erase an existing secondary resolution. Only retain
        # it when the physical address and commune have the same RAW identity.
        if existing.get("is_primary") is False and (
            existing.get("latitudine") is not None
            or existing.get("longitudine") is not None
        ):
            def normalized(v):
                return " ".join(str(v or "").casefold().split())
            if (
                normalized(existing.get("address_raw")) != normalized(item.get("address_raw"))
                or normalized(existing.get("comune_pvp")) != normalized(item.get("comune_pvp"))
                or existing.get("latitudine") is None
                or existing.get("longitudine") is None
            ):
                raise RuntimeError("Secondary resolved location identity changed; refuse overwrite")
            for k in (
                "latitudine", "longitudine", "source", "precision", "confidence",
                "status", "formatted_address", "anncsu_status", "google_status",
                "resolution_reason", "resolver_version",
            ):
                item[k] = existing.get(k)
            item["warnings"] = list(dict.fromkeys(
                list(item.get("warnings") or []) + list(existing.get("warnings") or [])
            ))
            used_existing = True

        if used_existing:
            item["warnings"] = list(dict.fromkeys(
                list(item.get("warnings") or []) + ["HISTORICAL_SECONDARY_DATA_PRESERVED"]
            ))
        result.append(item)
    return result


def _resolve_one(listing, raw, input_source, ann_db, loc_db, pin_ann, pin_istat, pin_cache, boundary, arbitrator=None, stored_locations_loader=None):
    pid = str(listing["pvp_id"])

    resolved = address_resolver.process_row(raw)
    if not resolved.get("primary"):
        raise RuntimeError("No physical bene address found in PVP detail")

    ann = anncsu_match.process(resolved, ann_db)
    loc = locality_match.process(resolved, loc_db)
    unified = unified_offline.process(resolved, ann, loc)
    locations, problems = materializer.build_rows({pid: resolved}, {pid: unified})
    if problems:
        raise RuntimeError("Materialized location problems: " + json.dumps(problems[:5], ensure_ascii=False))
    if not locations:
        raise RuntimeError("No listing_locations rows materialized")
    if input_source == "RAW_PVP_HISTORICAL" and len(locations) != int(resolved.get("beni_count") or 0):
        raise RuntimeError("RAW bene count differs from location materialization")
    if input_source == "RAW_PVP_HISTORICAL" and len(locations) > 1:
        stored = stored_locations_loader(listing["listing_id"]) if stored_locations_loader else []
        locations = _merge_stored_secondary_locations(locations, stored)

    primaries = [x for x in locations if x.get("is_primary") is True]
    if len(primaries) != 1:
        raise RuntimeError(f"Expected exactly one primary location; got {len(primaries)}")
    primary = primaries[0]
    if arbitrator is not None and input_source == "RAW_PVP_HISTORICAL":
        reviewed = arbitrator.resolve(primary, len(locations), listing, boundary)
        if reviewed is not primary:
            locations = [reviewed if r.get("is_primary") is True else r for r in locations]
            primary = reviewed

    pin, no_pin_reason = pin_publisher.choose_pin(primary, pin_ann, pin_istat, pin_cache)

    if pin is not None and pin.get("source") == "PVP" and pin.get("precision") == "PVP_COORD":
        check = boundary.test(
            {
                "comune": listing.get("comune"),
                "provincia": listing.get("provincia"),
                "sigla_provincia": listing.get("sigla_provincia"),
                "regione": listing.get("regione"),
            },
            lat=pin.get("lat"),
            lon=pin.get("lon"),
        )
        if not check.get("safe"):
            pin, no_pin_reason = pin_publisher.municipality_fallback(
                primary, pin_ann, pin_istat, pin_cache
            )

    if pin is None:
        rep = boundary.representative_point(
            {
                "comune": listing.get("comune"),
                "provincia": listing.get("provincia"),
                "sigla_provincia": listing.get("sigla_provincia"),
                "regione": listing.get("regione"),
            }
        )
        if not rep.get("ok"):
            raise RuntimeError(
                f"No safe fallback pin: {no_pin_reason}; boundary={rep.get('reason')}"
            )
        pin = {
            "lat": rep["lat"],
            "lon": rep["lon"],
            "source": "istat_boundary_comune_indicativo",
            "precision": "COMUNE",
            "reason": (
                "Punto rappresentativo verificato all'interno del confine comunale ISTAT 2026; "
                "non rappresenta la posizione esatta dell'immobile."
            ),
            "formatted_address": "Comune di " + str(listing.get("comune") or ""),
            "fallback_from_status": str(primary.get("status") or "UNKNOWN"),
        }

    now = datetime.now().astimezone().isoformat()
    updated_locations = []
    for location in locations:
        item = dict(location)
        item["checked_at"] = now
        if item.get("is_primary") is True:
            warnings = list(item.get("warnings") or [])
            if pin.get("fallback_from_status") is not None:
                marker = (
                    "HISTORICAL_BACKFILL_FALLBACK_FROM_"
                    + str(pin.get("fallback_from_status") or "UNKNOWN").upper()
                )
                if marker not in warnings:
                    warnings.append(marker)
            item.update(
                {
                    "latitudine": float(pin["lat"]),
                    "longitudine": float(pin["lon"]),
                    "source": pin["source"],
                    "precision": pin["precision"],
                    "confidence": pin_publisher.published_confidence(pin, item.get("confidence")),
                    "status": "RESOLVED",
                    "formatted_address": pin.get("formatted_address"),
                    "resolution_reason": pin.get("reason"),
                    "warnings": warnings,
                    "resolver_version": MODERN_RESOLVER,
                }
            )
        updated_locations.append(item)

    parent_precision = _parent_precision(pin["precision"])
    admin_row = {
        "comune": listing.get("comune"),
        "provincia": listing.get("provincia"),
        "sigla_provincia": listing.get("sigla_provincia"),
        "regione": listing.get("regione"),
    }
    new_safe = boundary.test(
        admin_row, lat=pin["lat"], lon=pin["lon"]
    ).get("safe") is True
    pvp_safe = boundary.test(
        admin_row,
        lat=primary.get("pvp_latitudine"),
        lon=primary.get("pvp_longitudine"),
    ).get("safe") is True if (
        pin_publisher.valid_pair(
            primary.get("pvp_latitudine"),
            primary.get("pvp_longitudine"),
        )
    ) else False
    admin_matches, admin_reason = pin_publisher.source_admin_matches_listing(
        listing, primary, boundary
    )
    safe, safe_reason = _safe_to_apply(
        input_source=input_source,
        technical_precision=pin.get("precision"),
        original_status=primary.get("status"),
        location_count=len(updated_locations),
        has_conflict="PVP_ANNCSU_CIVIC_CONFLICT" in (primary.get("warnings") or []),
        old_pvp_inside=pvp_safe,
        new_pin_safe=new_safe,
        admin_matches=admin_matches,
    )
    movement = _haversine_m(
        listing.get("latitudine"),
        listing.get("longitudine"),
        pin["lat"],
        pin["lon"],
    )
    primary_resolved = resolved.get("primary") or {}

    return {
        "pvp_id": pid,
        "listing_id": listing.get("listing_id"),
        "comune": listing.get("comune"),
        "indirizzo_db": listing.get("indirizzo"),
        "input_source": input_source,
        "raw_beni_count": resolved.get("beni_count"),
        "raw_multi_address": bool(resolved.get("multi_address")),
        "raw_multi_comune": bool(resolved.get("multi_comune")),
        "address_raw_pvp": primary_resolved.get("raw_via"),
        "street_pvp": primary_resolved.get("street"),
        "civic_pvp": primary_resolved.get("civic_for_geocode"),
        "old": {
            "latitudine": listing.get("latitudine"),
            "longitudine": listing.get("longitudine"),
            "geocode_precision": listing.get("geocode_precision"),
        },
        "new": {
            "latitudine": float(pin["lat"]),
            "longitudine": float(pin["lon"]),
            "geocode_precision": parent_precision,
            "technical_precision": pin.get("precision"),
            "source": pin.get("source"),
            "reason": pin.get("reason"),
            "formatted_address": pin.get("formatted_address"),
        },
        "movement_m": round(movement, 1) if movement is not None else None,
        "same_parent_pin": (
            _same_coord(
                listing.get("latitudine"),
                listing.get("longitudine"),
                pin["lat"],
                pin["lon"],
            )
            and str(listing.get("geocode_precision") or "").upper() == parent_precision
        ),
        "safe_to_apply": safe,
        "safe_reason": safe_reason,
        "raw_parent_admin_match": admin_matches,
        "raw_parent_admin_reason": admin_reason,
        "locations": updated_locations,
    }


def _replace_locations(base, key, pid, locations, validate=False):
    rpc = (
        "aste_validate_replace_listing_locations_v1"
        if validate
        else "aste_replace_listing_locations_v1"
    )
    return _rpc(
        base,
        key,
        rpc,
        {
            "p_pvp_id": str(pid),
            "p_locations": _sanitize_locations_for_rpc(locations),
        },
    )


def _patch_parent(base, key, listing_id, payload):
    rows = _patch(
        base,
        key,
        "listings",
        {"listing_id": "eq." + str(listing_id)},
        payload,
    )
    if not isinstance(rows, list) or len(rows) != 1:
        raise RuntimeError(
            f"Parent PATCH expected one row, got {len(rows) if isinstance(rows, list) else 'non-list'}"
        )
    return rows[0]


def _coords_equal_or_both_missing(a_lat, a_lon, b_lat, b_lon):
    if all(v is None for v in (a_lat, a_lon, b_lat, b_lon)):
        return True
    return _same_coord(a_lat, a_lon, b_lat, b_lon)


def _verify_case(base, key, case):
    pid = str(case["pvp_id"])
    rows = _get(
        base,
        key,
        "listings",
        {
            "select": "pvp_id,latitudine,longitudine,maps_link,geocode_precision",
            "pvp_id": "eq." + pid,
            "limit": "2",
        },
    )
    if len(rows) != 1:
        return False, "parent row missing/duplicate"
    parent = rows[0]
    expected = case["new"]
    if not _same_coord(
        parent.get("latitudine"),
        parent.get("longitudine"),
        expected["latitudine"],
        expected["longitudine"],
    ):
        return False, "parent coordinates mismatch"
    if str(parent.get("geocode_precision") or "") != str(expected["geocode_precision"]):
        return False, "parent precision mismatch"

    locs = _get(
        base, key, "listing_locations",
        {
            "select": (
                "pvp_id,bene_index,is_primary,address_raw,comune_pvp,"
                "pvp_latitudine,pvp_longitudine,latitudine,longitudine,"
                "source,precision,status,resolver_version"
            ),
            "pvp_id": "eq." + pid,
            "order": "bene_index.asc",
            "limit": "1000",
        },
    )
    expected_locs = case["locations"]
    if len(locs) != len(expected_locs):
        return False, "location count mismatch"
    if sum(x.get("is_primary") is True for x in locs) != 1:
        return False, "primary location count mismatch"
    indexed = {x["bene_index"]: x for x in locs}
    if len(indexed) != len(locs):
        return False, "duplicate stored bene_index"
    for planned in expected_locs:
        idx = planned["bene_index"]
        got = indexed.get(idx)
        if got is None:
            return False, f"missing secondary or primary bene_index={idx}"
        if got.get("is_primary") is not planned.get("is_primary"):
            return False, f"is_primary mismatch bene_index={idx}"
        for field in ("address_raw", "comune_pvp", "source", "precision", "status", "resolver_version"):
            if str(got.get(field) or "") != str(planned.get(field) or ""):
                return False, f"{field} mismatch bene_index={idx}"
        for prefix in ("", "pvp_"):
            la, lo = prefix + "latitudine", prefix + "longitudine"
            if not _coords_equal_or_both_missing(
                got.get(la), got.get(lo), planned.get(la), planned.get(lo)
            ):
                return False, f"{prefix}coordinates mismatch bene_index={idx}"
    return True, "PASS"


def _apply_case(base, key, case):
    pid = str(case["pvp_id"])
    listing_id = str(case["listing_id"])
    old_parent_rows = _get(
        base,
        key,
        "listings",
        {
            "select": "listing_id,latitudine,longitudine,maps_link,geocode_precision",
            "listing_id": "eq." + listing_id,
            "limit": "2",
        },
    )
    if len(old_parent_rows) != 1:
        raise RuntimeError("Unable to snapshot parent listing")
    old_parent = old_parent_rows[0]
    # Basic compare-and-refuse: don't overwrite a parent already changed
    # since the frozen plan was built.
    planned_old = case["old"]
    if not _coords_equal_or_both_missing(
        old_parent.get("latitudine"), old_parent.get("longitudine"),
        planned_old.get("latitudine"), planned_old.get("longitudine"),
    ) or str(old_parent.get("geocode_precision") or "") != str(planned_old.get("geocode_precision") or ""):
        raise RuntimeError("STALE_PARENT_DATA: parent changed since frozen plan")
    old_locations = _fetch_locations_snapshot(base, key, listing_id)

    new_locations = case["locations"]
    _replace_locations(base, key, pid, new_locations, validate=True)

    child_written = False
    parent_written = False
    try:
        child_written = True  # A network response may fail AFTER commit.
        _replace_locations(base, key, pid, new_locations, validate=False)

        new = case["new"]
        parent_written = True  # Roll back even if the response gets lost.
        _patch_parent(
            base,
            key,
            listing_id,
            {
                "latitudine": new["latitudine"],
                "longitudine": new["longitudine"],
                "maps_link": _maps_link(new["latitudine"], new["longitudine"]),
                "geocode_precision": new["geocode_precision"],
            },
        )

        ok, reason = _verify_case(base, key, case)
        if not ok:
            raise RuntimeError("Post-apply verification failed: " + reason)
        return {"status": "PASS"}

    except Exception:
        rollback_errors = []
        if child_written:
            try:
                if old_locations:
                    _replace_locations(base, key, pid, old_locations, validate=False)
                else:
                    _delete(base, key, "listing_locations", {"listing_id": "eq." + listing_id})
            except Exception as exc:
                rollback_errors.append("locations=" + repr(exc))
        if parent_written:
            try:
                _patch_parent(
                    base,
                    key,
                    listing_id,
                    {
                        "latitudine": old_parent.get("latitudine"),
                        "longitudine": old_parent.get("longitudine"),
                        "maps_link": old_parent.get("maps_link"),
                        "geocode_precision": old_parent.get("geocode_precision"),
                    },
                )
            except Exception as exc:
                rollback_errors.append("parent=" + repr(exc))
        if rollback_errors:
            raise RuntimeError(
                "Apply failed and rollback was incomplete: " + "; ".join(rollback_errors)
            )
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Re-resolve active historical auction pins with the current offline geocoder"
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Apply ONLY a preview-authorized exact RAW batch; default is zero writes",
    )
    ap.add_argument(
        "--expected-plan-sha",
        default="",
        help="Required with --apply: plan_sha256 copied from matching RAW-only batch preview",
    )
    ap.add_argument(
        "--pvp-ids",
        default="",
        help="Optional comma-separated PVP IDs; bypasses modern-resolver skip",
    )
    ap.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="0 = all eligible candidates; with --require-raw selects RAW-only batch",
    )
    ap.add_argument(
        "--after-pvp-id",
        default="",
        help="Continue after this PVP ID in database TEXT order; avoid rescanning permanently blocked IDs",
    )
    ap.add_argument("--google-conflict-max-http", type=int, default=0,
                    help="0=offline; explicitly authorize at most 1..3 Google requests in PREVIEW only for >1 km PVP/ANNCSU conflicts")
    ap.add_argument("--google-conflict-monthly-cap", type=int, default=9000,
                    help="Existing shared Google ledger hard cap (<=9000); never a new quota")
    ap.add_argument(
        "--raw-file",
        default=str(Path(RAW_DIR) / "listings_api_raw.jsonl"),
        help="Historical PVP RAW JSONL. Missing rows fall back to DB-derived synthetic input.",
    )
    ap.add_argument(
        "--require-raw",
        action="store_true",
        help="Do not synthesize missing RAW rows; report them as errors instead.",
    )
    args = ap.parse_args(argv)
    if args.max_rows < 0:
        ap.error("--max-rows must be >= 0")
    if args.after_pvp_id and not args.after_pvp_id.isdigit():
        ap.error("--after-pvp-id must be a numeric PVP ID")
    if not 0 <= args.google_conflict_max_http <= 3:
        ap.error("--google-conflict-max-http must be 0..3")
    if not 1 <= args.google_conflict_monthly_cap <= 9000:
        ap.error("--google-conflict-monthly-cap must be 1..9000")
    if args.apply and args.google_conflict_max_http:
        ap.error("Live Google cross-check is PREVIEW-only for historical backfill; zero apply writes")
    if args.apply and (not args.expected_plan_sha or not args.require_raw or not 1 <= args.max_rows <= 250):
        ap.error("--apply requires --expected-plan-sha, --require-raw and --max-rows 1..250")
    if args.apply and not re.fullmatch(r"[a-fA-F0-9]{64}", args.expected_plan_sha or ""):
        ap.error("--expected-plan-sha must be a 64-character SHA256")
    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE"))
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE"]
    explicit_ids = _parse_ids(args.pvp_ids)

    # Collect RAW coverage before applying the batch limit, so a batch of 50
    # means 50 RAW auctions, not 50 legacy rows with mostly synthetic input.
    all_active, candidates, skipped_modern = _build_candidates(
        base, key, explicit_ids, 0
    )
    wanted_ids = [str(x.get("pvp_id") or "") for x in candidates]
    raw_index, raw_stats = _load_raw_index(args.raw_file, wanted_ids)
    if args.require_raw:
        candidates = [row for row in candidates if str(row["pvp_id"]) in raw_index]
    if args.after_pvp_id:
        # pvp_id is TEXT in Supabase, and the query orders it lexically.
        candidates = [
            row for row in candidates
            if str(row["pvp_id"]) > args.after_pvp_id
        ]
    if args.max_rows:
        candidates = candidates[:args.max_rows]
    print(
        f"[BACKFILL] active_future={len(all_active)} candidates={len(candidates)} "
        f"skipped_modern={skipped_modern} explicit={len(explicit_ids)} apply={int(args.apply)} "
        f"raw_found={raw_stats['raw_targets_found']} raw_missing={raw_stats['raw_targets_missing']} "
        f"raw_file_present={int(raw_stats['raw_file_present'])}",
        flush=True,
    )

    ann_path = Path(
        os.environ.get("ANNCSU_SQLITE_INDEX", "")
        or (ANNCSU_DIR / "anncsu_runtime.sqlite3")
    )
    istat_path = Path(
        os.environ.get("ISTAT_LOCALITY_INDEX", "")
        or (DATA_DIR / "istat" / "istat_localita_2021.sqlite3")
    )

    ann_db = anncsu_match.DB(ann_path)
    loc_db = locality_match.DB(istat_path)
    pin_ann = pin_publisher.open_anncsu_bbox()
    pin_istat = pin_publisher.open_istat_localities()
    pin_cache = {}
    boundary = boundary_runtime.BoundaryContext()
    arbitrator = google_civic.ConflictArbitrator(
        max_http=args.google_conflict_max_http,
        monthly_cap=args.google_conflict_monthly_cap,
    )

    cases = []
    errors = []
    counters = Counter()
    started = time.perf_counter()

    try:
        for index, listing in enumerate(candidates, 1):
            pid = str(listing["pvp_id"])
            try:
                raw = raw_index.get(pid)
                if raw is not None:
                    input_source = "RAW_PVP_HISTORICAL"
                elif args.require_raw:
                    raise RuntimeError("Historical RAW not available for this pvp_id")
                else:
                    raw = _synthetic_raw_from_db(listing)
                    input_source = "DB_SYNTHETIC"
                case = _resolve_one(
                    listing,
                    raw,
                    input_source,
                    ann_db,
                    loc_db,
                    pin_ann,
                    pin_istat,
                    pin_cache,
                    boundary,
                    arbitrator=arbitrator,
                    stored_locations_loader=lambda lid: _fetch_locations_snapshot(base, key, lid),
                )
                cases.append(case)
                counters["resolved"] += 1
                counters[
                    "safe_to_apply" if case["safe_to_apply"] else "blocked_rebaseline"
                ] += 1
                counters["decision:" + str(case["safe_reason"])] += 1
                counters[
                    "same_parent_pin" if case["same_parent_pin"] else "changed_parent_pin"
                ] += 1
                counters["input:" + str(case.get("input_source") or "NONE")] += 1
                counters["source:" + str(case["new"]["source"] or "NONE")] += 1
                counters[
                    "precision:" + str(case["new"]["technical_precision"] or "NONE")
                ] += 1

                # Phase 1 is ALWAYS read-only: authorization must verify the
                # full frozen plan SHA before we perform even the first write.
                print(
                    f"[BACKFILL] {index}/{len(candidates)} pvp={pid} "
                    f"{case['old']['geocode_precision']} -> {case['new']['geocode_precision']} "
                    f"{case['new']['source']}/{case['new']['technical_precision']} "
                    f"input={case['input_source']} move={case['movement_m']}m safe={case['safe_to_apply']}",
                    flush=True,
                )
            except Exception as exc:
                errors.append(
                    {
                        "pvp_id": pid,
                        "listing_id": listing.get("listing_id"),
                        "comune": listing.get("comune"),
                        "indirizzo": listing.get("indirizzo"),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                counters["errors"] += 1
                print(
                    f"[BACKFILL ERROR] pvp={pid} {type(exc).__name__}: {exc}",
                    flush=True,
                )

    finally:
        try:
            arbitrator.finish()
        finally:
            ann_db.close()
            loc_db.close()
            pin_ann.close()
            pin_istat.close()

    canonical = [
        {
            "pvp_id": x["pvp_id"],
            "old": x["old"],
            "new": x["new"],
            "safe_to_apply": x["safe_to_apply"],
            "safe_reason": x["safe_reason"],
            "input_source": x["input_source"],
            "locations": [
                {k: v for k, v in row.items() if k != "checked_at"}
                for row in x["locations"]
            ],
        }
        for x in cases
    ]
    plan_sha = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    # Recheck the exact frozen plan before the first write. Incomplete
    # previews or changes to parent/secondary data are NOT implicitly approved.
    if args.apply:
        if errors:
            raise RuntimeError(
                f"APPLY_ABORTED: {len(errors)} preflight errors; zero DB writes"
            )
        if plan_sha.lower() != args.expected_plan_sha.lower():
            raise RuntimeError(
                "APPLY_PLAN_SHA_MISMATCH: zero DB writes. Preview SHA="
                + plan_sha + "; supplied=" + args.expected_plan_sha
            )
        print(
            f"[BACKFILL APPLY] authorized={len(cases)} "
            f"eligible={sum(bool(c['safe_to_apply']) for c in cases)} "
            f"sha={plan_sha}",
            flush=True,
        )
        for index, case in enumerate(cases, 1):
            if not case["safe_to_apply"]:
                continue
            # Stop at the first failed apply; a failed rollback or concurrent
            # edit must not be silently followed by more writes.
            _apply_case(base, key, case)
            counters["applied"] += 1
            print(f"[BACKFILL APPLIED] {index}/{len(cases)} pvp={case['pvp_id']}", flush=True)

    summary = {
        "version": VERSION,
        "mode": "APPLY_SAFE_GUARDED" if args.apply else "PREVIEW_ZERO_DB_WRITES",
        "created_at": datetime.now().astimezone().isoformat(),
        "active_future_listings": len(all_active),
        "eligible_candidates": len(candidates),
        "skipped_already_modern": skipped_modern,
        "explicit_pvp_ids": explicit_ids,
        "max_rows": args.max_rows,
        "after_pvp_id": args.after_pvp_id,
        "batch_first_pvp_id": str(candidates[0]["pvp_id"]) if candidates else None,
        "batch_last_pvp_id": str(candidates[-1]["pvp_id"]) if candidates else None,
        "raw_stats": raw_stats,
        "plan_sha256": plan_sha,
        "counts": dict(counters),
        "db_writes": counters.get("applied", 0) if args.apply else 0,
        "errors": errors,
        "cases": cases,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "google_arbitration": arbitrator.summary(),
        "google_calls": arbitrator.summary()["network_attempts"],
    }

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "apply" if args.apply else "preview"
    out = REPORTS_DIR / f"historical_geocode_backfill_v2_{suffix}.json"
    out.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k != "cases"},
            ensure_ascii=False,
            indent=2,
        )
    )
    print("[OUT]", out)

    if args.apply and errors:
        raise RuntimeError(
            f"Backfill apply completed with {len(errors)} unresolved PVP/resolver errors; inspect report"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
