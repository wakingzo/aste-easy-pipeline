#!/usr/bin/env python3
"""Very selective Google cross-check for the active v4 offline geocoder.

A disagreement is not a reason to promote a result. Google is consulted only
for a SINGLE physical bene with a structured CIVICO already matched by ANNCSU,
a valid PVP coordinate, and >1 km between the two. Even a Google ROOFTOP is
only corroborating evidence: the original ANNCSU or PVP coordinate is kept.

No Google API, cache or budget module is loaded until an explicitly enabled,
eligible case is encountered. By default the entire module is offline.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from math import asin, cos, radians, sin, sqrt

MIN_CONFLICT_M = 1000.0
CONFIRM_RADIUS_M = 150.0


def _number(x):
    try:
        v = float(x)
        return v if abs(v) < float("inf") else None
    except (TypeError, ValueError):
        return None


def _distance(a, b, c, d):
    nums = tuple(_number(x) for x in (a, b, c, d))
    if None in nums:
        return None
    a, b, c, d = nums
    p, q = radians(a), radians(c)
    z = sin(radians(c-a)/2)**2 + cos(p)*cos(q)*sin(radians(d-b)/2)**2
    return 12742017.6 * asin(min(1.0, sqrt(max(0.0, z))))


def eligible(loc, *, number_of_beni=1, min_conflict_m=MIN_CONFLICT_M):
    """Pure filter; intentionally excludes multi-bene, incomplete and weak matches."""
    if number_of_beni != 1 or not loc.get("is_primary"):
        return False, "MULTI_OR_NONPRIMARY"
    if not (
        str(loc.get("source") or "").upper() == "ANNCSU"
        and str(loc.get("precision") or "").upper() == "CIVICO"
        and str(loc.get("status") or "").upper() == "REVIEW"
        and "PVP_ANNCSU_CIVIC_CONFLICT" in (loc.get("warnings") or [])
        and str(loc.get("anncsu_status") or "").upper() in ("MATCH_EXACT", "MATCH_ALIAS_HIGH")
    ):
        return False, "NOT_CIVIC_CONFLICT"
    if not loc.get("street") or not loc.get("civic") or loc.get("snc") or loc.get("is_intersection"):
        return False, "NOT_SINGLE_STREET_CIVIC"
    d = _distance(
        loc.get("pvp_latitudine"), loc.get("pvp_longitudine"),
        loc.get("latitudine"), loc.get("longitudine"),
    )
    if d is None:
        return False, "MISSING_COORDINATES"
    if d <= min_conflict_m:
        return False, "BELOW_DISTANCE_THRESHOLD"
    return True, "ELIGIBLE"


def _query_row(loc):
    # A query is built from the original structured physical-bene address, NOT
    # the existing map marker or a DB-synthetic address.
    return {
        "indirizzo": f'{loc["street"]} {loc["civic"]}',
        "comune": loc.get("comune_pvp") or "",
        "sigla_provincia": loc.get("sigla_provincia_pvp") or "",
        "cap": loc.get("cap_pvp") or "",
    }


def _admin_row(loc, listing):
    return {
        "comune": loc.get("comune_pvp") or "",
        "provincia": loc.get("provincia_pvp") or listing.get("provincia"),
        "sigla_provincia": loc.get("sigla_provincia_pvp") or listing.get("sigla_provincia"),
        "regione": listing.get("regione"),
    }


def judge(loc, listing, results, core, boundary, *, proximity_m=CONFIRM_RADIUS_M):
    """Pure assessment with an injected Google parser and territory validator.

    Do not use Google coordinates as published coordinates. Multiple strong
    results pointing to opposite candidates must NOT select either one.
    """
    row = _query_row(loc)
    expected = core.expected_house_numbers(row)
    if len(expected) != 1:
        return None, "MULTIPLE_OR_UNKNOWN_CIVICS"
    adr = _admin_row(loc, listing)
    hits = []
    for result in results or []:
        if not result.get("ok") or result.get("partial_match"):
            continue
        if str(result.get("location_type") or "").upper() != "ROOFTOP":
            continue
        if not core.municipality_geo_matches(row, result):
            continue
        if core.house_number_relation(expected, core.result_house_numbers(result)) != "exact":
            continue
        match = core.best_address_match(row, result)
        if match is None or match < .85:
            continue
        score = core.score_forward_candidate(row, result)
        if score is None or score.get("score", 0) < 82:
            continue
        google_lat, google_lon = result.get("lat"), result.get("lon")
        if boundary.test(adr, lat=google_lat, lon=google_lon).get("position") not in ("INSIDE", "BOUNDARY"):
            continue
        d_ann = _distance(google_lat, google_lon, loc["latitudine"], loc["longitudine"])
        d_pvp = _distance(google_lat, google_lon, loc["pvp_latitudine"], loc["pvp_longitudine"])
        if d_ann is None or d_pvp is None:
            continue
        near_ann = d_ann <= proximity_m and d_pvp > 500
        near_pvp = d_pvp <= proximity_m and d_ann > 500
        if near_ann and boundary.test(
            adr, lat=loc["latitudine"], lon=loc["longitudine"]
        ).get("position") in ("INSIDE", "BOUNDARY"):
            hits.append(("ANNCSU", d_ann))
        elif near_pvp and boundary.test(
            adr, lat=loc["pvp_latitudine"], lon=loc["pvp_longitudine"]
        ).get("position") in ("INSIDE", "BOUNDARY"):
            hits.append(("PVP", d_pvp))

    if not hits:
        return None, "NO_STRICT_GOOGLE_AGREEMENT"
    sides = {side for side, _ in hits}
    if len(sides) != 1:
        return None, "CONTRADICTORY_GOOGLE_RESULTS"

    side = next(iter(sides))
    matched_m = min(d for _, d in hits)
    changed = deepcopy(loc)
    if side == "ANNCSU":
        changed.update(
            status="RESOLVED", confidence="HIGH",
            google_status="CIVIC_ARBITRATION_CONFIRMS_ANNCSU",
            resolution_reason=(
                "Civico ANNCSU confermato da Google ROOFTOP con strada, civico "
                f"e comune coerenti (distanza {matched_m:.0f} m); PVP discordante."
            ),
        )
    else:
        changed.update(
            latitudine=float(loc["pvp_latitudine"]),
            longitudine=float(loc["pvp_longitudine"]),
            source="PVP", precision="PVP_COORD",
            status="RESOLVED", confidence="MEDIUM_HIGH",
            google_status="CIVIC_ARBITRATION_CONFIRMS_PVP",
            resolution_reason=(
                "Coordinate PVP confermate da Google ROOFTOP con strada, civico "
                f"e comune coerenti (distanza {matched_m:.0f} m); ANNCSU discordante."
            ),
        )
    warnings = list(changed.get("warnings") or [])
    tag = "GOOGLE_CIVIC_ARBITRATION_" + side
    if tag not in warnings:
        warnings.append(tag)
    changed["warnings"] = warnings
    return changed, side + "_CONFIRMED"


class ConflictArbitrator:
    def __init__(self, *, max_http=0, monthly_cap=9000, min_conflict_m=MIN_CONFLICT_M):
        if not 0 <= max_http <= 3:
            raise ValueError("max_http must be 0..3; explicitly opt in to network calls")
        if monthly_cap < 1:
            raise ValueError("monthly_cap must be positive")
        self.max_http = max_http
        self.monthly_cap = monthly_cap
        self.min_conflict_m = min_conflict_m
        self.core = None
        self.cache = None
        self.prod = None
        self.counts = Counter()

    def _init_live(self):
        if self.core is not None:
            return
        # Reuse the proven production reconciliation, atomic ledger reservation,
        # existing cache and per-attempt cap. No separate paid Google client.
        import google_production_v3_18 as prod
        self.core, _ = prod._load_core_and_reconcile(
            monthly_cap=self.monthly_cap, run_cap=self.max_http
        )
        self.prod = prod
        self.cache = self.core.CacheStore(self.core.CACHE_FILE)

    def resolve(self, location, number_of_beni, listing, boundary):
        ok, why = eligible(
            location, number_of_beni=number_of_beni,
            min_conflict_m=self.min_conflict_m,
        )
        if not ok:
            return location
        self.counts["eligible_gt_1km"] += 1
        if self.max_http == 0:
            self.counts["disabled_no_google"] += 1
            return location

        self._init_live()
        row = _query_row(location)
        variants = self.core.constrained_address_variants(row)
        if not variants or len(self.core.expected_house_numbers(row)) != 1:
            self.counts["no_single_google_query"] += 1
            return location
        query = variants[0]
        key = self.prod._cache_key(self.core, query, row)
        cached = isinstance(self.cache.get(key), dict)
        calls = self.core.API_USAGE.run["geocoding"]["network_attempts"]
        if not cached and calls >= self.max_http:
            self.counts["run_cap_exhausted"] += 1
            return location

        # A single constrained forward-geocoding query: cached answers first,
        # otherwise at most ONE reserved HTTP attempt (no retries).
        results = self.core.google_forward_constrained(query, row, self.cache)
        self.counts["cache_lookups" if cached else "network_queries"] += 1
        corrected, outcome = judge(location, listing, results, self.core, boundary)
        self.counts[outcome] += 1
        return corrected if corrected is not None else location

    def finish(self):
        if self.core is not None:
            try:
                self.cache.save()
            finally:
                self.core.API_USAGE.save()

    def summary(self):
        calls = (
            self.core.API_USAGE.run["geocoding"]["network_attempts"]
            if self.core is not None else 0
        )
        return {
            "enabled": self.max_http > 0,
            "threshold_m": self.min_conflict_m,
            "max_http": self.max_http,
            "network_attempts": calls,
            "counts": dict(self.counts),
        }
