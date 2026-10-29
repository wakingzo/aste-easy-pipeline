#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.9 offline policy dry-run with truncated-locality Google bypass.

ZERO Google requests. ZERO DB writes.

Adds the validated P4/P5 behavior to v3.7:
- P4 (no usable PVP, named street/locality): Google may improve COMUNE to
  STRADA/LOCALITA with a coherent center point; a matched representative civic
  may become CIVICO when Google is ROOFTOP.
- P5 (PVP present but toponym unconfirmed): keep the PVP coordinate whenever
  possible. Google can confirm STRADA/LOCALITA only when distinct and close;
  <=1 m is not independent evidence. A representative civic ROOFTOP within
  150 m may promote to CIVICO; a far civic conflict goes to REVIEW.
- Multi-civic source addresses remain untouched. One matched civic is enough
  for the single map marker.
- Map visibility remains independent from stored geocode precision; verified
  municipality fallback prevents safely resolvable listings from disappearing.
"""
from __future__ import annotations

import csv
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
import geocode_pending_v2 as offline
from google_eligibility_v3_2 import address_parts, canonical, eligibility

VERSION = "google_policy_v3_9_locality_bypass_dryrun"
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
    """Load newest diagnostic row per PVP id; older pre-priority CSVs are ignored."""
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
            evidence[pid] = row
            usable_rows += 1
    if not report_files:
        raise RuntimeError("No google_comparison_v3_*.csv reports found; policy dry-run needs prior diagnostics")
    return evidence, report_files, usable_rows


def _reason_has(ev, token):
    return token in str((ev or {}).get("google_reason") or "").split("|")


def _address_match(ev):
    reason = str((ev or {}).get("google_reason") or "")
    m = re.search(r"(?:^|\|)indirizzo_match=([0-9.]+)(?:\||$)", reason)
    return _num(m.group(1)) if m else None


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


def _strong_toponym(ev):
    """Conservative street/locality evidence for P4/P5, never CIVICO by itself."""
    if not ev or not _valid(ev.get("google_lat"), ev.get("google_lon")):
        return False
    lt = str(ev.get("google_location_type") or "").upper()
    kind = str(ev.get("address_kind") or "").upper()
    if lt not in {"GEOMETRIC_CENTER", "APPROXIMATE"}:
        return False
    # APPROXIMATE is accepted only for a named locality/frazione, never a road.
    if lt == "APPROXIMATE" and kind != "LOCALITA":
        return False
    return (
        str(ev.get("google_istat_position") or "") == "INSIDE"
        and _reason_has(ev, "comune_ok")
        and _reason_has(ev, "provincia_ok")
        and (_address_match(ev) or 0) >= 0.90
        and not _reason_has(ev, "multi_segment_incomplete=1/2")
        and not any(part.startswith("multi_segment_incomplete=")
                    for part in str(ev.get("google_reason") or "").split("|"))
    )


def _norm_civic(value):
    return re.sub(r"[^0-9a-z]+", "", canonical(value))


def _representative_civic(row):
    """Return one source civic suitable for one marker; source address is not changed."""
    civic = str(address_parts(row).get("civic") or "").strip()
    if civic:
        return civic
    # Fallback for old reports generated before the representative multi-civic
    # parser existed. Prefer a number at the beginning of a comma-separated
    # address piece; this avoids treating SS/SP route ids as house numbers.
    address = str(row.get("indirizzo") or "")
    for piece in address.split(",")[1:3]:
        p = piece.strip()
        p = re.sub(r"\b\d{5}\b", " ", p)
        m = re.match(r"^(?:n(?:um(?:ero)?)?\s*\.?\s*|civ(?:ico)?\s*\.?\s*)?(\d{1,4}[a-z]?)", p, re.I)
        if m:
            return m.group(1)
    return ""


def _formatted_representative_civic(formatted):
    text = str(formatted or "")
    for piece in text.split(",")[1:3]:
        p = re.sub(r"\b\d{5}\b", " ", piece).strip()
        m = re.match(r"^(\d{1,4}[a-z]?)(?:\s*[/\-]\s*\d{1,4}[a-z]?)?", p, re.I)
        if m:
            return m.group(1)
    return ""


def _representative_civic_matches(row, ev):
    """True when Google matched at least one civic actually present in the source."""
    if not ev:
        return False
    if _reason_has(ev, "civico_ok"):
        return True
    expected = _norm_civic(_representative_civic(row))
    got = _norm_civic(_formatted_representative_civic(ev.get("google_formatted_address")))
    if not expected or not got:
        return False
    if expected == got:
        return True
    # A plain representative number (e.g. source "9 e 29") may be rendered
    # by Google as 9/29. Do not drop a source suffix: 24A is not the same civic
    # as Google's 24.
    if expected.isdigit():
        gm = re.match(r"^(\d+)", got)
        return bool(gm and expected == gm.group(1))
    return False


def _representative_rooftop(row, ev):
    return (
        str((ev or {}).get("google_location_type") or "").upper() == "ROOFTOP"
        and str((ev or {}).get("google_istat_position") or "") == "INSIDE"
        and _valid((ev or {}).get("google_lat"), (ev or {}).get("google_lon"))
        and _reason_has(ev, "comune_ok")
        and _reason_has(ev, "provincia_ok")
        and _representative_civic_matches(row, ev)
    )


def _representative_range(row, ev):
    return (
        str((ev or {}).get("google_location_type") or "").upper() == "RANGE_INTERPOLATED"
        and str((ev or {}).get("google_istat_position") or "") == "INSIDE"
        and _valid((ev or {}).get("google_lat"), (ev or {}).get("google_lon"))
        and _reason_has(ev, "comune_ok")
        and _reason_has(ev, "provincia_ok")
        and _representative_civic_matches(row, ev)
    )


def _toponym_precision(ev):
    return "LOCALITA" if str((ev or {}).get("address_kind") or "").upper() == "LOCALITA" else "STRADA"


def _pvp_toponym_precision(ev):
    return "PVP_LOCALITA_COMPATIBILE" if _toponym_precision(ev) == "LOCALITA" else "PVP_STRADA_COMPATIBILE"


def apply_policy(row, baseline, ev):
    """Return proposed stored decision. Pure function; no network/DB."""
    out = _copy_baseline(baseline)
    if not ev:
        return out

    parsed = address_parts(row)
    if parsed.get("kind") == "LOCALITA" and not parsed.get("civic"):
        out["policy_action"] = "BYPASS_GOOGLE_LOCALITA_TRONCATA_KEEP_OFFLINE"
        out["final_reason"] = (str(out.get("final_reason") or "") +
                               "; indirizzo solo località/frazione senza civico: Google non necessario").strip("; ")
        return out

    label = str(ev.get("priority_label") or "")
    assessment = str(ev.get("assessment") or "")
    lt = str(ev.get("google_location_type") or "").upper()

    # P1: three-source disagreement; no automatic winner.
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

    # P2: no PVP. Exact structured civic can be used; interpolated stays STRADA.
    if label == "P2_SENZA_PVP_CON_CIVICO":
        if assessment == "GOOGLE_CIVICO_ESATTO_DA_VALIDARE" and _strict_google_civic(ev):
            out.update(
                final_status="DONE", final_source="google_civico_rooftop_v38",
                final_precision="CIVICO", final_lat=_num(ev.get("google_lat")),
                final_lon=_num(ev.get("google_lon")),
                final_reason="Google ROOFTOP con civico strutturato esatto, strada coerente e punto INSIDE nel Comune ISTAT",
                policy_action="P2_PROMOTE_GOOGLE_CIVICO",
            )
        elif (assessment == "GOOGLE_CIVICO_ESATTO_DA_VALIDARE"
              and lt == "RANGE_INTERPOLATED" and _strict_google_civic(ev, allow_range=True)):
            out.update(
                final_status="DONE", final_source="google_civico_interpolato_v38",
                final_precision="STRADA", final_lat=_num(ev.get("google_lat")),
                final_lon=_num(ev.get("google_lon")),
                final_reason="Google ha il civico esatto ma la coordinata e' RANGE_INTERPOLATED: posizione stradale indicativa, non CIVICO",
                policy_action="P2_USE_GOOGLE_INTERPOLATED_AS_STRADA",
            )
        return out

    # P3: PVP present + civic. Only a distinct nearby exact ROOFTOP can replace it.
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
                final_status="DONE", final_source="google_civico_rooftop_v38",
                final_precision="CIVICO", final_lat=_num(ev.get("google_lat")),
                final_lon=_num(ev.get("google_lon")),
                final_reason=f"Google ROOFTOP con civico esatto e distinto dal PVP, compatibile entro {delta:.0f} m",
                policy_action="P3_PROMOTE_GOOGLE_CIVICO",
            )
        else:
            out.update(
                final_status="REVIEW", final_source="review_google_pvp_civico_discordante_v38",
                final_precision="NESSUNA", final_lat=None, final_lon=None,
                final_reason=f"Google ROOFTOP con civico esatto ma distante {delta:.0f} m dal PVP: conflitto da verificare",
                policy_action="P3_REVIEW_GOOGLE_PVP_DIVERGENCE",
            )
        return out

    # P4: no usable PVP, no civic recognized by the old parser. A representative
    # multi-civic match can safely use one ROOFTOP marker. Otherwise only improve
    # COMUNE to STRADA/LOCALITA with a strong toponym result.
    if label == "P4_SENZA_PVP_TOPONIMO":
        if _representative_rooftop(row, ev):
            rep = _representative_civic(row)
            out.update(
                final_status="DONE", final_source="google_civico_rappresentativo_v38",
                final_precision="CIVICO", final_lat=_num(ev.get("google_lat")),
                final_lon=_num(ev.get("google_lon")),
                final_reason=("Google ROOFTOP coerente con uno dei civici dell'indirizzo; "
                              f"usato un solo civico rappresentativo ({rep}) per il marker"),
                policy_action="P4_PROMOTE_GOOGLE_CIVICO_RAPPRESENTATIVO",
            )
        elif _strong_toponym(ev):
            precision = _toponym_precision(ev)
            out.update(
                final_status="DONE", final_source="google_toponimo_indicativo_v38",
                final_precision=precision, final_lat=_num(ev.get("google_lat")),
                final_lon=_num(ev.get("google_lon")),
                final_reason=f"Google localizza il toponimo nel Comune ISTAT: posizione indicativa a livello {precision}, non CIVICO",
                policy_action="P4_USE_GOOGLE_TOPONIMO_" + precision,
            )
        return out

    # P5: PVP exists but toponym was not independently confirmed. Prefer keeping
    # the PVP coordinate. Representative civic ROOFTOP follows the P3 distance
    # discipline; RANGE/center results can only confirm STRADA/LOCALITA.
    if label == "P5_PVP_NON_CONFERMATO_TOPONIMO":
        delta = _num(ev.get("google_distance_from_pvp_m"))
        baseline_has_pin = (str(baseline.get("geocode_precision") or "").startswith("PVP_")
                            and _valid(out.get("final_lat"), out.get("final_lon")))

        if _representative_rooftop(row, ev) and delta is not None:
            rep = _representative_civic(row)
            if delta <= 1:
                out["policy_action"] = "P5_KEEP_PVP_GOOGLE_NON_INDIPENDENTE"
                out["final_reason"] = (str(out.get("final_reason") or "") +
                                       "; Google ROOFTOP coincide col PVP <=1 m: non usato come conferma indipendente").strip("; ")
            elif delta <= 150:
                out.update(
                    final_status="DONE", final_source="google_civico_rappresentativo_v38",
                    final_precision="CIVICO", final_lat=_num(ev.get("google_lat")),
                    final_lon=_num(ev.get("google_lon")),
                    final_reason=(f"Google ROOFTOP su civico rappresentativo {rep}, distinto dal PVP "
                                  f"ma compatibile entro {delta:.0f} m"),
                    policy_action="P5_PROMOTE_GOOGLE_CIVICO_RAPPRESENTATIVO",
                )
            else:
                out.update(
                    final_status="REVIEW", final_source="review_google_pvp_civico_rappresentativo_v38",
                    final_precision="NESSUNA", final_lat=None, final_lon=None,
                    final_reason=(f"Google ROOFTOP su civico rappresentativo {rep} ma distante {delta:.0f} m "
                                  "dal PVP: conflitto da verificare"),
                    policy_action="P5_REVIEW_GOOGLE_PVP_CIVICO_DIVERGENCE",
                )
            return out

        if _representative_range(row, ev) and baseline_has_pin and delta is not None and 1 < delta <= 150:
            precision = _pvp_toponym_precision(ev)
            out.update(
                final_status="DONE", final_source="pvp_google_toponimo_compatibile_v38",
                final_precision=precision,
                final_reason=(f"Google RANGE_INTERPOLATED coerente con il toponimo entro {delta:.0f} m; "
                              "mantenuta la coordinata PVP"),
                policy_action="P5_KEEP_PVP_CONFIRM_TOPONIMO_INTERPOLATED",
            )
            return out

        if _strong_toponym(ev):
            if baseline_has_pin and delta is not None:
                if delta <= 1:
                    out["policy_action"] = "P5_KEEP_PVP_GOOGLE_NON_INDIPENDENTE"
                    out["final_reason"] = (str(out.get("final_reason") or "") +
                                           "; Google localizza lo stesso toponimo ma coincide col PVP <=1 m: non indipendente").strip("; ")
                elif delta <= 150:
                    precision = _pvp_toponym_precision(ev)
                    out.update(
                        final_status="DONE", final_source="pvp_google_toponimo_compatibile_v38",
                        final_precision=precision,
                        final_reason=(f"Google conferma il toponimo nel Comune e il suo punto indicativo e' entro {delta:.0f} m; "
                                      "mantenuta la coordinata PVP"),
                        policy_action="P5_KEEP_PVP_CONFIRM_TOPONIMO_" + _toponym_precision(ev),
                    )
                # A distant GEOMETRIC_CENTER is not a civic conflict: a long road
                # can have a center far from the property. Keep the PVP baseline.
                else:
                    out["policy_action"] = "P5_KEEP_PVP_TOPONIMO_DISTANTE_NON_CONCLUSIVO"
            else:
                precision = _toponym_precision(ev)
                out.update(
                    final_status="DONE", final_source="google_toponimo_indicativo_v38",
                    final_precision=precision, final_lat=_num(ev.get("google_lat")),
                    final_lon=_num(ev.get("google_lon")),
                    final_reason=f"PVP non pubblicabile; Google localizza il toponimo nel Comune a livello {precision}",
                    policy_action="P5_USE_GOOGLE_TOPONIMO_SENZA_PVP_" + precision,
                )
        return out

    return out


def municipality_fallback(row, ctx, cache):
    """Verified point inside declared ISTAT municipality, for map display only."""
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
        "map_lat": lat, "map_lon": lon, "map_source": "istat_comune_fallback_v39",
        "map_precision": "COMUNE", "map_is_fallback": True,
        "map_reason": "Marker indicativo nel Comune ISTAT; non rappresenta la posizione esatta dell'immobile",
    }, ""


def choose_map_point(row, final, ctx, municipality_cache):
    if _valid(final.get("final_lat"), final.get("final_lon")):
        return {
            "map_lat": _num(final.get("final_lat")), "map_lon": _num(final.get("final_lon")),
            "map_source": final.get("final_source"), "map_precision": final.get("final_precision"),
            "map_is_fallback": False, "map_reason": "Usa la coordinata proposta dalla policy",
        }
    fallback, failure = municipality_fallback(row, ctx, municipality_cache)
    if fallback:
        return fallback
    return {
        "map_lat": None, "map_lon": None, "map_source": "unmappable_review_v39",
        "map_precision": "NESSUNA", "map_is_fallback": True,
        "map_reason": failure or "Nessun fallback cartografico sicuro",
    }


def preview_new_rows(listings, *, max_rows):
    """Evaluate every new row using existing diagnostics only. ZERO Google calls."""
    if max_rows < 1:
        raise RuntimeError("max_rows must be positive")
    if len(listings) > max_rows:
        raise RuntimeError("Incomplete offline batch: raise --max-geocode before v3.8 policy dry-run")

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
    csv_path = REPORTS_DIR / ("google_policy_v3_9_" + stamp + ".csv")
    fields = (
        "pvp_id", "comune", "sigla_provincia", "indirizzo",
        "offline_status", "offline_source", "offline_precision", "offline_lat", "offline_lon",
        "google_report", "google_priority", "google_assessment", "google_address_kind",
        "google_location_type", "google_formatted_address", "google_istat_position", "google_reason",
        "google_civic_relation", "google_score", "google_distance_from_pvp_m", "representative_civic",
        "policy_action", "final_status", "final_source", "final_precision", "final_lat", "final_lon", "final_reason",
        "future_google_eligible", "future_google_group", "future_google_reason",
        "map_lat", "map_lon", "map_source", "map_precision", "map_is_fallback", "map_reason",
    )

    action_counts = Counter()
    final_precision_counts = Counter()
    map_precision_counts = Counter()
    map_source_counts = Counter()
    mapped = fallback_mapped = unmapped = rows_with_evidence = 0
    future_google_eligible = 0
    future_google_group_counts = Counter()
    future_google_skip_reason_counts = Counter()
    municipality_cache = {}
    detail_rows = []

    for row, baseline in planned:
        pid = str(row.get("pvp_id") or "")
        ev = evidence.get(pid)
        if ev:
            rows_with_evidence += 1
        gate = eligibility(row, baseline)
        if gate.get("google_eligible"):
            future_google_eligible += 1
            future_google_group_counts[str(gate.get("google_group") or "UNKNOWN")] += 1
        else:
            future_google_skip_reason_counts[str(gate.get("google_reason") or "UNKNOWN")] += 1
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
            "pvp_id": pid, "comune": row.get("comune"), "sigla_provincia": row.get("sigla_provincia"),
            "indirizzo": row.get("indirizzo"), "offline_status": baseline.get("geocode_status"),
            "offline_source": baseline.get("geocode_source"), "offline_precision": baseline.get("geocode_precision"),
            "offline_lat": baseline.get("latitudine"), "offline_lon": baseline.get("longitudine"),
            "google_report": ev.get("_report_file") if ev else "", "google_priority": ev.get("priority_label") if ev else "",
            "google_assessment": ev.get("assessment") if ev else "", "google_address_kind": ev.get("address_kind") if ev else "",
            "google_location_type": ev.get("google_location_type") if ev else "",
            "google_formatted_address": ev.get("google_formatted_address") if ev else "",
            "google_istat_position": ev.get("google_istat_position") if ev else "", "google_reason": ev.get("google_reason") if ev else "",
            "google_civic_relation": ev.get("google_civic_relation") if ev else "", "google_score": ev.get("google_score_diagnostico") if ev else "",
            "google_distance_from_pvp_m": ev.get("google_distance_from_pvp_m") if ev else "",
            "representative_civic": _representative_civic(row),
            "future_google_eligible": bool(gate.get("google_eligible")),
            "future_google_group": gate.get("google_group") or "",
            "future_google_reason": gate.get("google_reason") or "",
            **final, **map_data,
        })

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, delimiter=";", extrasaction="ignore")
        writer.writeheader(); writer.writerows(detail_rows)

    return {
        "reviewed_without_db_writes": len(rows), "total_new": len(listings),
        "selection_truncated": False, "google_calls": 0,
        "policy_preview": {
            "version": VERSION,
            "mode": "OFFLINE_EXISTING_GOOGLE_EVIDENCE_NO_DB_WRITES_NO_GOOGLE_CALLS",
            "google_reports_seen": len(report_files), "google_evidence_rows_loaded": usable_rows,
            "rows_with_google_evidence": rows_with_evidence, "policy_action_counts": dict(action_counts),
            "final_precision_counts": dict(final_precision_counts), "map_precision_counts": dict(map_precision_counts),
            "map_source_counts": dict(map_source_counts),
            "future_google_gate": {
                "eligible_rows": future_google_eligible,
                "skipped_rows": len(rows) - future_google_eligible,
                "eligible_group_counts": dict(future_google_group_counts),
                "skip_reason_counts": dict(future_google_skip_reason_counts),
            },
            "map_coverage": {"total": len(rows), "mapped": mapped, "fallback_mapped": fallback_mapped, "unmapped": unmapped},
            "report_csv": str(csv_path),
        },
    }
