#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.7 offline policy dry-run over the already collected Google evidence.

ZERO Google requests. ZERO DB writes.

Goals:
- turn the validated P1/P2/P3 diagnostics into an explicit proposed decision;
- keep Google promotions conservative (ROOFTOP+exact for true CIVICO);
- keep RANGE_INTERPOLATED useful but never call it a true CIVICO;
- never let a safely resolvable listing disappear from the map merely because
  its property position is imprecise: when the final decision has no publishable
  pin, use a verified ISTAT municipality representative point for *map display*.

The map fallback is deliberately separate from the geocode precision. A COMUNE
fallback is not a claim about the property's exact location.
"""
from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime
from pathlib import Path

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
import geocode_pending_v2 as offline

VERSION = "google_policy_v3_7_dryrun"
GOOGLE_REPORT_GLOB = "google_comparison_v3_*.csv"


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid(lat, lon):
    return offline.valid_pair(_num(lat), _num(lon))


def _copy_baseline(baseline):
    return {
        "final_status": baseline.get("geocode_status"),
        "final_source": baseline.get("geocode_source"),
        "final_precision": baseline.get("geocode_precision"),
        "final_lat": _num(baseline.get("latitudine")),
        "final_lon": _num(baseline.get("longitudine")),
        "final_reason": baseline.get("geocode_decision_reason"),
        "policy_action": "KEEP_OFFLINE_BASELINE",
    }


def load_google_evidence(report_dir=REPORTS_DIR):
    """Load the newest diagnostic row per PVP id.

    Older v3 CSVs are allowed. Rows without priority_label are ignored because
    they predate the P1/P2/P3 policy and are unsafe to interpret as such.
    """
    evidence = {}
    report_files = []
    usable_rows = 0
    for path in sorted(Path(report_dir).glob(GOOGLE_REPORT_GLOB)):
        try:
            with path.open(encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f, delimiter=";")
                fields = set(reader.fieldnames or ())
                if not {"pvp_id", "assessment"}.issubset(fields):
                    continue
                rows = list(reader)
        except (OSError, UnicodeError, csv.Error) as exc:
            raise RuntimeError("Cannot read Google diagnostic report: " + path.name) from exc
        report_files.append(path.name)
        for row in rows:
            pid = str(row.get("pvp_id") or "").strip()
            label = str(row.get("priority_label") or "").strip()
            if not pid or not label:
                continue
            row = dict(row)
            row["_report_file"] = path.name
            evidence[pid] = row  # sorted filenames: newest compatible row wins
            usable_rows += 1
    if not report_files:
        raise RuntimeError("No google_comparison_v3_*.csv reports found; policy dry-run needs prior diagnostics")
    return evidence, report_files, usable_rows


def _strict_google_civic(ev, *, allow_range=False):
    if not ev:
        return False
    lt = str(ev.get("google_location_type") or "").upper()
    allowed = {"ROOFTOP", "RANGE_INTERPOLATED"} if allow_range else {"ROOFTOP"}
    return (
        lt in allowed
        and str(ev.get("google_civic_relation") or "") == "exact"
        and str(ev.get("google_istat_position") or "") == "INSIDE"
        and (_num(ev.get("google_score_diagnostico")) or 0) >= 82
        and _valid(ev.get("google_lat"), ev.get("google_lon"))
    )


def apply_policy(row, baseline, ev):
    """Return the proposed stored decision. Pure function; no network/DB."""
    out = _copy_baseline(baseline)
    if not ev:
        return out

    label = str(ev.get("priority_label") or "")
    assessment = str(ev.get("assessment") or "")
    lt = str(ev.get("google_location_type") or "").upper()

    # P1 is a three-source disagreement. Google is evidence only, never an
    # automatic winner. Do not publish one of the conflicting detailed pins.
    if label == "P1_DISCORDANZA_PVP_ANNCSU":
        out.update(
            final_status="REVIEW",
            final_source="review_pvp_anncsu_google_discordanza",
            final_precision="NESSUNA",
            final_lat=None,
            final_lon=None,
            final_reason="Discordanza PVP/ANNCSU: Google resta terza evidenza, nessuna sostituzione automatica",
            policy_action="P1_REVIEW_NO_AUTO_REPLACEMENT",
        )
        return out

    # P2 has no PVP pin. Exact ROOFTOP can become a true civic result. Exact
    # RANGE_INTERPOLATED is useful for display/search but deliberately labelled
    # STRADA, not CIVICO, because Google interpolated the street segment.
    if label == "P2_SENZA_PVP_CON_CIVICO":
        if assessment == "GOOGLE_CIVICO_ESATTO_DA_VALIDARE" and _strict_google_civic(ev):
            out.update(
                final_status="DONE",
                final_source="google_civico_rooftop_v37",
                final_precision="CIVICO",
                final_lat=_num(ev.get("google_lat")),
                final_lon=_num(ev.get("google_lon")),
                final_reason="Google ROOFTOP con civico strutturato esatto, strada coerente e punto INSIDE nel Comune ISTAT",
                policy_action="P2_PROMOTE_GOOGLE_CIVICO",
            )
        elif (assessment == "GOOGLE_CIVICO_ESATTO_DA_VALIDARE"
              and lt == "RANGE_INTERPOLATED" and _strict_google_civic(ev, allow_range=True)):
            out.update(
                final_status="DONE",
                final_source="google_civico_interpolato_v37",
                final_precision="STRADA",
                final_lat=_num(ev.get("google_lat")),
                final_lon=_num(ev.get("google_lon")),
                final_reason="Google ha il civico esatto ma la coordinata e' RANGE_INTERPOLATED: usata come posizione stradale indicativa, non CIVICO",
                policy_action="P2_USE_GOOGLE_INTERPOLATED_AS_STRADA",
            )
        return out

    # P3 already has a PVP point inside the municipality, but the address was
    # not independently confirmed. A distinct exact ROOFTOP within 150 m may
    # improve it to CIVICO. Coincidence <=1 m is not treated as independent.
    if label == "P3_PVP_NON_CONFERMATO_CON_CIVICO" and _strict_google_civic(ev):
        delta = _num(ev.get("google_distance_from_pvp_m"))
        if delta is None:
            return out
        if delta <= 1:
            out["policy_action"] = "P3_KEEP_PVP_GOOGLE_NON_INDIPENDENTE"
            out["final_reason"] = (str(out.get("final_reason") or "") +
                                   "; Google coincide col PVP <=1 m: non usato come conferma indipendente").strip("; ")
        elif delta <= 150:
            out.update(
                final_status="DONE",
                final_source="google_civico_rooftop_v37",
                final_precision="CIVICO",
                final_lat=_num(ev.get("google_lat")),
                final_lon=_num(ev.get("google_lon")),
                final_reason=f"Google ROOFTOP con civico esatto e distinto dal PVP, compatibile entro {delta:.0f} m",
                policy_action="P3_PROMOTE_GOOGLE_CIVICO",
            )
        else:
            out.update(
                final_status="REVIEW",
                final_source="review_google_pvp_civico_discordante_v37",
                final_precision="NESSUNA",
                final_lat=None,
                final_lon=None,
                final_reason=f"Google ROOFTOP con civico esatto ma distante {delta:.0f} m dal PVP: conflitto da verificare",
                policy_action="P3_REVIEW_GOOGLE_PVP_DIVERGENCE",
            )
    return out


def municipality_fallback(row, ctx, cache):
    """Verified point inside the declared ISTAT municipality, for map display only."""
    sample = offline.pvp_row(row)
    meta = ctx.resolve_admin(sample)
    if meta.get("status") != "RESOLVED" or not meta.get("canonical_code"):
        return None, "ADMIN_NON_RISOLTO"
    code = str(meta["canonical_code"])
    if code not in cache:
        rep = ctx.representative_point(sample, admin_meta=meta)
        if (rep.get("ok") and _valid(rep.get("lat"), rep.get("lon"))
                and ctx.test(sample, lat=_num(rep.get("lat")), lon=_num(rep.get("lon")),
                             admin_meta=meta).get("position") == "INSIDE"):
            cache[code] = (_num(rep.get("lat")), _num(rep.get("lon")))
        else:
            cache[code] = None
    if not cache[code]:
        return None, "NESSUN_PUNTO_COMUNE_VERIFICATO"
    lat, lon = cache[code]
    return {
        "map_lat": lat,
        "map_lon": lon,
        "map_source": "istat_comune_fallback_v37",
        "map_precision": "COMUNE",
        "map_is_fallback": True,
        "map_reason": "Marker indicativo nel Comune ISTAT; non rappresenta la posizione esatta dell'immobile",
    }, ""


def choose_map_point(row, final, ctx, municipality_cache):
    """Map placement is independent from precision/status.

    If a final publishable coordinate exists, use it. Otherwise fall back to a
    municipality representative point. Never silently relabel a fallback as a
    precise property coordinate.
    """
    if _valid(final.get("final_lat"), final.get("final_lon")):
        return {
            "map_lat": _num(final.get("final_lat")),
            "map_lon": _num(final.get("final_lon")),
            "map_source": final.get("final_source"),
            "map_precision": final.get("final_precision"),
            "map_is_fallback": False,
            "map_reason": "Usa la coordinata proposta dalla policy",
        }
    fallback, failure = municipality_fallback(row, ctx, municipality_cache)
    if fallback:
        return fallback
    return {
        "map_lat": None,
        "map_lon": None,
        "map_source": "unmappable_review_v37",
        "map_precision": "NESSUNA",
        "map_is_fallback": True,
        "map_reason": failure or "Nessun fallback cartografico sicuro",
    }


def preview_new_rows(listings, *, max_rows):
    """Evaluate every new row using existing diagnostics only. ZERO Google calls."""
    if max_rows < 1:
        raise RuntimeError("max_rows must be positive")
    if len(listings) > max_rows:
        raise RuntimeError("Incomplete offline batch: raise --max-geocode before v3.7 policy dry-run")

    ctx, territorial, local = offline.prepare_context(no_anncsu=False)
    rows = []
    for listing in listings:
        row = {k: listing.get(k) for k in offline.SELECT}
        row["latitudine"] = row["longitudine"] = None
        if row.get("geocode_status") != "PENDING":
            raise RuntimeError("Non-PENDING listing: stop")
        rows.append(row)
    planned = offline.plan_rows(rows, ctx, territorial, local)
    evidence, report_files, usable_rows = load_google_evidence(REPORTS_DIR)

    ensure_runtime_dirs()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    csv_path = REPORTS_DIR / ("google_policy_v3_7_" + stamp + ".csv")
    fields = (
        "pvp_id", "comune", "sigla_provincia", "indirizzo",
        "offline_status", "offline_source", "offline_precision", "offline_lat", "offline_lon",
        "google_report", "google_priority", "google_assessment", "google_location_type",
        "google_civic_relation", "google_score", "google_distance_from_pvp_m",
        "policy_action", "final_status", "final_source", "final_precision", "final_lat", "final_lon", "final_reason",
        "map_lat", "map_lon", "map_source", "map_precision", "map_is_fallback", "map_reason",
    )

    action_counts = Counter()
    final_precision_counts = Counter()
    map_precision_counts = Counter()
    map_source_counts = Counter()
    mapped = 0
    fallback_mapped = 0
    unmapped = 0
    rows_with_evidence = 0
    municipality_cache = {}
    detail_rows = []

    for row, baseline in planned:
        pid = str(row.get("pvp_id") or "")
        ev = evidence.get(pid)
        if ev:
            rows_with_evidence += 1
        final = apply_policy(row, baseline, ev)
        map_data = choose_map_point(row, final, ctx, municipality_cache)
        if _valid(map_data.get("map_lat"), map_data.get("map_lon")):
            mapped += 1
            if map_data.get("map_is_fallback"):
                fallback_mapped += 1
        else:
            unmapped += 1
        action_counts[str(final.get("policy_action") or "UNKNOWN")] += 1
        final_precision_counts[str(final.get("final_precision") or "NESSUNA")] += 1
        map_precision_counts[str(map_data.get("map_precision") or "NESSUNA")] += 1
        map_source_counts[str(map_data.get("map_source") or "NESSUNA")] += 1
        detail_rows.append({
            "pvp_id": pid,
            "comune": row.get("comune"),
            "sigla_provincia": row.get("sigla_provincia"),
            "indirizzo": row.get("indirizzo"),
            "offline_status": baseline.get("geocode_status"),
            "offline_source": baseline.get("geocode_source"),
            "offline_precision": baseline.get("geocode_precision"),
            "offline_lat": baseline.get("latitudine"),
            "offline_lon": baseline.get("longitudine"),
            "google_report": ev.get("_report_file") if ev else "",
            "google_priority": ev.get("priority_label") if ev else "",
            "google_assessment": ev.get("assessment") if ev else "",
            "google_location_type": ev.get("google_location_type") if ev else "",
            "google_civic_relation": ev.get("google_civic_relation") if ev else "",
            "google_score": ev.get("google_score_diagnostico") if ev else "",
            "google_distance_from_pvp_m": ev.get("google_distance_from_pvp_m") if ev else "",
            **final,
            **map_data,
        })

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(detail_rows)

    return {
        "reviewed_without_db_writes": len(rows),
        "total_new": len(listings),
        "selection_truncated": False,
        "google_calls": 0,
        "policy_preview": {
            "version": VERSION,
            "mode": "OFFLINE_EXISTING_GOOGLE_EVIDENCE_NO_DB_WRITES_NO_GOOGLE_CALLS",
            "google_reports_seen": len(report_files),
            "google_evidence_rows_loaded": usable_rows,
            "rows_with_google_evidence": rows_with_evidence,
            "policy_action_counts": dict(action_counts),
            "final_precision_counts": dict(final_precision_counts),
            "map_precision_counts": dict(map_precision_counts),
            "map_source_counts": dict(map_source_counts),
            "map_coverage": {
                "total": len(rows),
                "mapped": mapped,
                "fallback_mapped": fallback_mapped,
                "unmapped": unmapped,
            },
            "report_csv": str(csv_path),
        },
    }
