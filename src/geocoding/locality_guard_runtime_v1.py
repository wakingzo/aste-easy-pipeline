#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime locality guard for Aste Easy daily production v3.17.

This module is intentionally narrower than the historical audit/repair tools.
It only auto-overrides a FINAL coarse map point when all of these hold:
- the auction address contains an explicit locality marker;
- the locality resolves to one unique exact ISTAT 2021 locality point in the
  same municipality (PRO_COM supplied by ANNCSU municipality resolution);
- the final candidate is coarse (Comune / PVP-in-Comune style), not a verified
  civic/street coordinate;
- the candidate is a strong locality conflict under the v3.17 audit logic;
- distance to the unique ISTAT locality point is >= 2 km;
- there is no ANNCSU/ISTAT reference divergence warning.

The replacement is deliberately indicative: REVIEW + LOCALITA. It never claims
an exact auctioned-property coordinate and never calls Google.
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

VERSION = "locality_guard_runtime_v1"
MIN_AUTO_DISTANCE_M = 2000.0
DEFAULT_INDEX = Path(os.environ.get("ISTAT_LOCALITY_INDEX", "") or (Path(__file__).resolve().parents[2] / "data" / "istat" / "istat_localita_2021.sqlite3")).expanduser().resolve()
LOCALITY_RE = re.compile(r"(?i)\b(?:localit[aà]|loc\.?|frazione(?:\s+di)?|fraz\.?|borgata|contrada|c\s*/?\s*da|c\.\s*da)\s+")
ROAD_RE = re.compile(r"(?i)\b(?:via|viale|corso|piazza|piazzale|strada|vicolo|vico|largo|traversa|sentiero|rotonda|salita|lungomare|s\.?p\.?|s\.?s\.?|s\.?r\.?)\b")
COARSE_PRECISIONS = {"COMUNE", "PVP_IN_COMUNE", "PVP_INDICATIVO_ALTRO_COMUNE", "NESSUNA"}
COARSE_SOURCES = {
    "pvp_comune_compatibile", "istat_comune_indicativo", "istat_comune_fallback_v312",
    "pvp_localita_non_conclusiva", "pvp_strada_non_conclusiva",
}


def clean(v):
    return "" if v is None else re.sub(r"\s+", " ", str(v).replace("\u00a0", " ")).strip()


def norm(v):
    s = unicodedata.normalize("NFKD", clean(v).replace("’", "'").replace("`", "'"))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def fnum(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def hav(a, b, c, d):
    if None in (a, b, c, d):
        return None
    r = 6371008.8
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c-a), math.radians(d-b)
    z = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(min(1, math.sqrt(z)))


def explicit_hint(address):
    raw = clean(address)
    m = LOCALITY_RE.search(raw.replace("’", "'"))
    if not m:
        return ""
    tail = raw[m.end():]
    stop = len(tail)
    for sep in (",", ";"):
        x = tail.find(sep)
        if x >= 0:
            stop = min(stop, x)
    rm = ROAD_RE.search(tail)
    if rm:
        stop = min(stop, rm.start())
    s = clean(tail[:stop]).strip(" -/,.;")
    # PVP form: "Loc. Capo Comino - Podere 18".
    sm = re.search(r"\s+[–—-]\s+(?=(?:podere|poderi|via|viale|piazza|strada|s\.?p\.?|s\.?s\.?|civ(?:ico)?\.?|n\.?|numero|km|snc)\b)", s, re.I)
    if sm:
        s = clean(s[:sm.start()]).strip(" -/,.;")
    return s


def validate_index(path: Path):
    path = Path(path).resolve()
    if not path.is_file():
        raise RuntimeError("ISTAT locality SQLite index missing: " + str(path))
    con = sqlite3.connect("file:" + str(path).replace("\\", "/") + "?mode=ro", uri=True)
    try:
        row = con.execute("select value from meta where key='version'").fetchone()
        if not row or row[0] != "istat_locality_index_v1":
            raise RuntimeError("Unsupported ISTAT locality index version: " + str(row[0] if row else None))
        qc = con.execute("PRAGMA quick_check").fetchone()[0]
        if qc != "ok":
            raise RuntimeError("ISTAT locality SQLite quick_check failed: " + str(qc))
        n = int(con.execute("select count(*) from localities").fetchone()[0])
        if n < 10000:
            raise RuntimeError("ISTAT locality index unexpectedly small: " + str(n))
        return {"version": row[0], "rows": n, "path": str(path)}
    finally:
        con.close()


def open_index(path: Path):
    validate_index(path)
    con = sqlite3.connect("file:" + str(Path(path).resolve()).replace("\\", "/") + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def istat_match(con, pro_com, hint):
    if con is None or not pro_com or not hint:
        return None
    nh = norm(hint)
    exact = list(con.execute(
        "select loc21_id,tipo_loc,nome,lat,lon from localities where pro_com=? and nome_norm=?",
        (str(pro_com), nh)))
    if len(exact) == 1:
        r = exact[0]
        return {"mode": "EXACT_UNIQUE", "score": 1.0, "margin": 1.0, **dict(r)}
    if len(exact) > 1:
        return {"mode": "EXACT_AMBIGUOUS", "score": 1.0, "margin": 0.0,
                "matches": len(exact), "best_name": exact[0]["nome"]}
    # Fuzzy matches are useful for diagnostics only. Runtime AUTO override requires EXACT_UNIQUE.
    rows = list(con.execute(
        "select loc21_id,tipo_loc,nome,nome_norm,lat,lon from localities where pro_com=?",
        (str(pro_com),)))
    if not rows:
        return None
    scores = [(SequenceMatcher(None, nh, r["nome_norm"]).ratio(), r) for r in rows]
    scores.sort(key=lambda x: x[0], reverse=True)
    best = scores[0]
    second = scores[1][0] if len(scores) > 1 else 0
    return {"mode": "NO_SAFE_MATCH", "score": round(best[0], 4),
            "margin": round(best[0]-second, 4), "best_name": best[1]["nome"]}


def _evnum(ev, name):
    return fnum((ev or {}).get(name))


def evaluate(row, map_data, final, ann_ev, istat_con):
    """Return a diagnostic/override decision for one row; ZERO network/DB writes."""
    hint = clean((ann_ev or {}).get("runtime_locality_source_hint")) or explicit_hint(row.get("indirizzo"))
    if not hint:
        return {"action": "NO_EXPLICIT_LOCALITY", "override": False}
    pro = clean((ann_ev or {}).get("anncsu_code_used"))
    im = istat_match(istat_con, pro, hint)
    if not im or im.get("mode") != "EXACT_UNIQUE":
        return {"action": "NO_UNIQUE_ISTAT_LOCALITY", "override": False,
                "hint": hint, "anncsu_code": pro, "istat_match_mode": clean((im or {}).get("mode"))}

    ilat, ilon = fnum(im.get("lat")), fnum(im.get("lon"))
    mlat, mlon = fnum(map_data.get("map_lat")), fnum(map_data.get("map_lon"))
    if None in (ilat, ilon, mlat, mlon):
        return {"action": "INVALID_COORDS", "override": False, "hint": hint}

    map_precision = clean(map_data.get("map_precision") or final.get("final_precision")).upper()
    map_source = clean(map_data.get("map_source") or final.get("final_source")).lower()
    coarse = map_precision in COARSE_PRECISIONS or map_source in COARSE_SOURCES
    dist = hav(mlat, mlon, ilat, ilon)

    ann_mode = clean((ann_ev or {}).get("runtime_locality_match_mode"))
    ann_lat, ann_lon = _evnum(ann_ev, "runtime_locality_lat"), _evnum(ann_ev, "runtime_locality_lon")
    ann_p90 = _evnum(ann_ev, "runtime_locality_p90_m")
    cross = hav(ann_lat, ann_lon, ilat, ilon) if ann_mode == "EXACT" and None not in (ann_lat, ann_lon) else None
    reference_warning = "ANNCSU_ISTAT_REFERENCE_DIVERGENCE" if cross is not None and cross > 5000 else ""

    # Runtime auto-override is deliberately restricted to coarse candidates.
    # A verified CIVICO/STRADA/Google precise result is never displaced by a representative locality point.
    strong_conflict = bool(coarse and dist is not None and dist > 6000)
    eligible = bool(strong_conflict and dist >= MIN_AUTO_DISTANCE_M and not reference_warning)
    result = {
        "action": "OVERRIDE_TO_LOCALITA" if eligible else ("STRONG_CONFLICT_NO_AUTO" if strong_conflict else "KEEP"),
        "override": eligible,
        "hint": hint,
        "anncsu_code": pro,
        "istat_match_mode": im.get("mode"),
        "istat_name": clean(im.get("nome")),
        "istat_lat": ilat, "istat_lon": ilon,
        "distance_to_locality_m": round(dist, 1) if dist is not None else None,
        "map_source_before": map_data.get("map_source"),
        "map_precision_before": map_data.get("map_precision"),
        "coarse_candidate": coarse,
        "anncsu_istat_ref_distance_m": round(cross, 1) if cross is not None else None,
        "reference_warning": reference_warning,
        "anncsu_locality_p90_m": ann_p90,
    }
    return result


def apply_override(row, final, map_data, decision):
    if not decision.get("override"):
        return final, map_data
    name = decision.get("istat_name") or decision.get("hint") or "località"
    dist = decision.get("distance_to_locality_m")
    new_final = dict(final)
    new_final.update({
        "final_status": "REVIEW",
        "final_source": "locality_guard_istat_localita_2021",
        "final_precision": "LOCALITA",
        "final_lat": decision["istat_lat"],
        "final_lon": decision["istat_lon"],
        "final_reason": (
            f"Località esplicita {decision.get('hint')!r}; pin finale coarse incompatibile "
            f"({dist:.0f} m dal riferimento ISTAT 2021 {name!r}); sostituito con punto "
            "indicativo della località. Non prova l'immobile."),
        "policy_action": "LOCALITY_GUARD_MOVE_TO_ISTAT_LOCALITA",
    })
    new_map = dict(map_data)
    new_map.update({
        "map_lat": decision["istat_lat"],
        "map_lon": decision["istat_lon"],
        "map_source": "locality_guard_istat_localita_2021",
        "map_precision": "LOCALITA",
        "map_is_fallback": False,
    })
    return new_final, new_map
