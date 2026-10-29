#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aste Easy - Unified Geocoder v11.6.4 LIVE

Pipeline local-first consolidata:
  PVP -> canonicalizzazione ISTAT -> ANNCSU -> Google solo se necessario.

DEFAULT:
  DRY RUN con Google REALE e cache persistente.
  ZERO PATCH Supabase.

APPLY:
  python\\python.exe geocode_unified_v11_6_4_live.py --apply

Sicurezze:
- usa lo stesso manifest geocode_touched_v11.jsonl dell'importer v11.1;
- ricalcola il piano sul DB corrente ad ogni avvio;
- canonicalizza Comune/sigla con ISTAT prima di ANNCSU/Google;
- comuni_cap.csv non inventa CAP;
- Google core v9.3.6: cache, parser civico, guard multi-segmento/semantico,
  safety cap mensili e per esecuzione;
- prima di ogni PATCH rilegge il record DB e verifica che non sia cambiato;
- se un PVP non esiste ancora in DB, APPLY lo salta;
- il dry-run salva cache e contatori Google, così APPLY può riusarli.
- v11.6.2: ultimo fallback COMUNE da punto interno al poligono ISTAT, ZERO Google.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import os
import re
import runpy
import sys
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
SRC_ROOT = BASE_DIR.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from aste_paths import CACHE_DIR, REPORTS_DIR, ensure_runtime_dirs, env_path
from project_env import load_project_env as load_common_project_env

PLANNER_FILE = BASE_DIR / "geocode_unified_v11_6_4_plan.py"

DEFAULT_MONTH_CAPS = {
    "MAX_MONTH_GEOCODING_CALLS": "49500",
    "MAX_MONTH_AUTOCOMPLETE_CALLS": "9000",
    "MAX_MONTH_ADDRESS_VALIDATION_CALLS": "4500",
}
DEFAULT_RUN_CAPS = {
    "MAX_RUN_GEOCODING_CALLS": "200",
    "MAX_RUN_AUTOCOMPLETE_CALLS": "40",
    "MAX_RUN_ADDRESS_VALIDATION_CALLS": "40",
}

TIMEOUT = 60


def clean(v):
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v).replace("\u00a0", " ")).strip()


def fnum(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def valid_pair(lat, lon):
    a, b = fnum(lat), fnum(lon)
    return (
        a is not None and b is not None
        and -90 <= a <= 90 and -180 <= b <= 180
        and not (a == 0 and b == 0)
    )


def load_project_env():
    load_common_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE", "GOOGLE_GEOCODING_API_KEY"))

    # Un solo usage file persistente; in cloud CACHE_DIR può essere un volume montato.
    usage = clean(os.environ.get("GOOGLE_API_USAGE_FILE"))
    if not usage:
        os.environ["GOOGLE_API_USAGE_FILE"] = str(CACHE_DIR / "google_api_usage_v9_2.json")
    elif not Path(usage).is_absolute():
        os.environ["GOOGLE_API_USAGE_FILE"] = str(CACHE_DIR / usage)

    for k, v in DEFAULT_MONTH_CAPS.items():
        os.environ.setdefault(k, v)
    for k, v in DEFAULT_RUN_CAPS.items():
        os.environ.setdefault(k, v)

    # Persistenza bilanciata: salvataggi periodici, senza riscrivere la cache a ogni chiamata.
    os.environ.setdefault("CACHE_SAVE_EVERY", "50")
    os.environ.setdefault("API_USAGE_SAVE_EVERY", "10")

    # Fallback Google disponibili ma limitati dai safety cap.
    os.environ.setdefault("ALLOW_LOCALITY_FALLBACK", "1")
    os.environ.setdefault("ADDRESS_VALIDATION_FALLBACK", "1")
    os.environ.setdefault("PLACES_AUTOCOMPLETE_FALLBACK", "1")
    os.environ.setdefault("REPAIR_MIN_SCORE", "82")
    os.environ.setdefault("GEOCODE_SLEEP_SECONDS", "0.05")

    for req in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE", "GOOGLE_GEOCODING_API_KEY"):
        if not clean(os.environ.get(req)):
            raise SystemExit(f"ERRORE: {req} mancante.")


def load_module(path: Path, name: str):
    if not path.exists():
        raise SystemExit(f"ERRORE: manca {path.name}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"ERRORE: impossibile caricare {path.name}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def db_headers(key, content=False, prefer=None):
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    if content:
        h["Content-Type"] = "application/json"
    if prefer:
        h["Prefer"] = prefer
    return h


def db_get_one(base, key, pvp_id):
    fields = (
        "pvp_id,indirizzo,comune,sigla_provincia,cap,"
        "latitudine,longitudine,maps_link,"
        "geocode_source,geocode_location_type,"
        "geocode_formatted_address,geocode_query_used"
    )
    url = (
        f"{base.rstrip('/')}/rest/v1/listings"
        f"?select={quote(fields, safe=',')}"
        f"&pvp_id=eq.{quote(str(pvp_id), safe='')}"
        f"&limit=1"
    )
    req = Request(url, method="GET", headers=db_headers(key))
    with urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace") or "[]")
    if not isinstance(data, list):
        raise RuntimeError("Risposta Supabase inattesa")
    return data[0] if data else None


def db_patch_one(base, key, pvp_id, payload):
    url = (
        f"{base.rstrip('/')}/rest/v1/listings"
        f"?pvp_id=eq.{quote(str(pvp_id), safe='')}"
    )
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        url,
        method="PATCH",
        data=body,
        headers=db_headers(key, content=True, prefer="return=representation"),
    )
    with urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace") or "[]")
    if not isinstance(data, list) or len(data) != 1:
        raise RuntimeError(f"PATCH pvp_id={pvp_id}: rows={len(data) if isinstance(data,list) else 'N/A'}")
    return data[0]


def text_norm(v):
    return clean(v).casefold().strip(" ,")


def cap_norm(v):
    d = re.sub(r"\D", "", clean(v))
    return d.zfill(5) if d else ""


def same_coord(a, b, tol=1e-7):
    x, y = fnum(a), fnum(b)
    if x is None or y is None:
        return x is None and y is None
    return abs(x-y) <= tol


def current_state_ok(item, now):
    """Race guard: verifica che il DB sia ancora quello usato per il piano."""
    if not now:
        return False, "db_row_missing"

    snapshot = item.get("current") or {}
    source_row = item.get("source_row") or item.get("row") or {}

    # Se il record esisteva quando abbiamo costruito il piano, geocode state e
    # coordinate devono essere ancora identici.
    if snapshot:
        for fld in ("geocode_source", "geocode_location_type", "geocode_query_used"):
            if clean(now.get(fld)) != clean(snapshot.get(fld)):
                return False, f"race_{fld}_changed"
        for fld in ("latitudine", "longitudine"):
            if not same_coord(now.get(fld), snapshot.get(fld)):
                return False, f"race_{fld}_changed"

    # I campi PVP non vuoti che hanno alimentato il piano devono coincidere.
    for fld in ("indirizzo", "comune", "sigla_provincia"):
        expected = clean(source_row.get(fld))
        if expected and text_norm(now.get(fld)) != text_norm(expected):
            return False, f"race_{fld}_changed"

    expected_cap = cap_norm(source_row.get("cap"))
    if expected_cap and cap_norm(now.get("cap")) != expected_cap:
        return False, "race_cap_changed"

    # Per pin PVP validati, anche il punto incoming deve essere ancora quello.
    if valid_pair(source_row.get("latitudine"), source_row.get("longitudine")):
        if not (
            same_coord(now.get("latitudine"), source_row.get("latitudine"))
            and same_coord(now.get("longitudine"), source_row.get("longitudine"))
        ):
            # Eccezione: se snapshot esisteva ed era protetto, il punto DB è
            # deliberatamente diverso dal PVP incoming.
            if not clean(snapshot.get("geocode_source")):
                return False, "race_pvp_coords_changed"

    return True, "ok"


def maps_link(lat, lon):
    return f"https://www.google.com/maps?q={float(lat):.7f},{float(lon):.7f}"


def input_fingerprint(row, label):
    raw = "|".join([
        clean(row.get("pvp_id")),
        clean(row.get("indirizzo")),
        clean(row.get("comune")),
        clean(row.get("sigla_provincia")),
        cap_norm(row.get("cap")),
        label,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def ann_method(ev):
    s = clean(ev.get("anncsu_metodo"))
    return s[:-2] if s.endswith(".0") else s


def full_address(row, street=None, civic=None):
    core = clean(street if street is not None else row.get("indirizzo")).strip(" ,")
    if civic:
        core = f"{core}, {clean(civic)}".strip(" ,")
    tail = " ".join(
        x for x in [cap_norm(row.get("cap")), clean(row.get("comune")), clean(row.get("sigla_provincia")).upper()]
        if x
    )
    return ", ".join(x for x in [core, tail, "Italia"] if x)


def make_local_civic_payload(row, ev):
    lat, lon = fnum(ev.get("anncsu_lat")), fnum(ev.get("anncsu_lon"))
    method = ann_method(ev)
    return {
        "latitudine": lat,
        "longitudine": lon,
        "maps_link": maps_link(lat, lon),
        "geocode_source": "anncsu_missing_civico",
        "geocode_location_type": f"ANNCSU_METHOD_{method}",
        "geocode_formatted_address": full_address(
            row,
            street=clean(ev.get("anncsu_odonimo")) or clean(row.get("indirizzo")),
            civic=clean(ev.get("anncsu_civic")),
        ),
        "geocode_query_used": (
            f"v11.4:anncsu_civico:"
            f"{input_fingerprint(row, clean(ev.get('anncsu_odonimo'))+'|'+clean(ev.get('anncsu_civic')))}"
        ),
    }


def make_local_street_payload(row, ev):
    rep = ev.get("street_representative") or {}
    lat, lon = fnum(rep.get("lat")), fnum(rep.get("lon"))
    return {
        "latitudine": lat,
        "longitudine": lon,
        "maps_link": maps_link(lat, lon),
        "geocode_source": "anncsu_missing_strada",
        "geocode_location_type": "ANNCSU_STREET_MEDOID",
        "geocode_formatted_address": full_address(
            row,
            street=clean(ev.get("anncsu_odonimo")) or clean(row.get("indirizzo")),
        ),
        "geocode_query_used": (
            f"v11.4:anncsu_strada:"
            f"{input_fingerprint(row, clean(ev.get('anncsu_odonimo')))}"
        ),
    }


def make_local_locality_payload(row, ev, *, boundary=False):
    lat, lon = fnum(ev.get("runtime_locality_lat")), fnum(ev.get("runtime_locality_lon"))
    if lat is None or lon is None:
        return None
    name = clean(ev.get("runtime_anncsu_locality")) or clean(row.get("indirizzo"))
    comune = clean(row.get("comune")); prov = clean(row.get("sigla_provincia")).upper()
    code = clean(ev.get("anncsu_code_used"))
    prog = clean(ev.get("runtime_locality_progressivo"))
    payload = {
        "latitudine": lat,
        "longitudine": lon,
        "maps_link": maps_link(lat, lon),
        "geocode_source": "anncsu_missing_localita",
        "geocode_location_type": "ANNCSU_LOCALITY_MEDOID",
        "geocode_formatted_address": f"Località {name}, {comune} {prov}, Italia".replace("  ", " ").strip(),
        "geocode_query_used": f"v11.6.3:anncsu_localita:{code}:{name}:{prog}",
    }
    if boundary:
        payload["geocode_source"] = "anncsu_corrected_boundary_localita"
    return payload


def make_boundary_civic_payload(row, ev):
    payload = make_local_civic_payload(row, ev)
    payload["geocode_source"] = "anncsu_corrected_boundary_civico"
    payload["geocode_query_used"] = (
        "v11.6.1:istat_boundary_anncsu_civico:"
        + input_fingerprint(
            row,
            clean(ev.get("anncsu_odonimo")) + "|" + clean(ev.get("anncsu_civic")),
        )
    )
    return payload


def make_boundary_street_payload(row, ev):
    payload = make_local_street_payload(row, ev)
    payload["geocode_source"] = "anncsu_corrected_boundary_strada"
    payload["geocode_query_used"] = (
        "v11.6.1:istat_boundary_anncsu_strada:"
        + input_fingerprint(row, clean(ev.get("anncsu_odonimo")))
    )
    return payload


def make_local_pvp_confirm_payload(row, ev, g):
    lat, lon = fnum(row.get("latitudine")), fnum(row.get("longitudine"))
    civic = clean(ev.get("civic_relation")).lower() == "exact"
    method = ann_method(ev)
    if civic and method:
        return {
            "latitudine": lat,
            "longitudine": lon,
            "maps_link": maps_link(lat, lon),
            "geocode_source": "pvp_verified_anncsu",
            "geocode_location_type": f"ANNCSU_METHOD_{method}",
            "geocode_formatted_address": full_address(row),
            "geocode_query_used": f"anncsu_checked:{lat:.7f},{lon:.7f}",
        }, "pvp_verified_anncsu_civico"

    # Conferma solo di strada: manteniamo source vuota, come il modello
    # storico PVP_OK_STRADA, così un futuro cambio PVP resta naturalmente eleggibile.
    return {
        "latitudine": lat,
        "longitudine": lon,
        "maps_link": maps_link(lat, lon),
        "geocode_location_type": "PVP_OK_STRADA",
        "geocode_formatted_address": full_address(row),
        "geocode_query_used": f"anncsu_checked:{lat:.7f},{lon:.7f}",
    }, "pvp_ok_strada_anncsu"



def _street_core(text):
    """Core lessicale conservativo della sola strada/toponimo."""
    s = clean(text).casefold().replace("’", "'")
    s = re.sub(
        r"(?i)\b(via|viale|vicolo|piazza|piazzale|corso|strada|contrada|c/?\.?da|"
        r"localit[aà]|frazione|borgata|vocabolo|zona|snc|s\.n\.c\.|n|nr)\b",
        " ", s,
    )
    s = re.sub(r"\d+[a-z]?(?:/\d+[a-z]?)?", " ", s)
    s = re.sub(r"(?i)\b(e|ed)\b", " ", s)
    s = re.sub(r"[^a-zà-ÿ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _road_segments(text):
    raw = clean(text)
    if not raw:
        return []
    pat = re.compile(r"(?i)\b(?:via|viale|vicolo|piazza|piazzale|corso|strada|largo|traversa|sentiero|salita|lungomare)\b")
    starts = [m.start() for m in pat.finditer(raw)]
    out = []
    for st in starts:
        tail = raw[st:]
        cut = len(tail)
        for sep in (",", ";"):
            i = tail.find(sep)
            if i >= 0:
                cut = min(cut, i)
        seg = clean(tail[:cut]).strip(" -/,.;")
        if seg:
            out.append(seg)
    return out


def strict_route_similarity(input_address, result, g):
    # v11.6.3: compare the Google route against the actual road segment, not
    # against locality/zone prefixes ("Zona Industriale - Via Corea ...").
    source_cores = []
    for text in _road_segments(input_address) + [clean(input_address)]:
        core = _street_core(text)
        if core and core not in source_cores:
            source_cores.append(core)
    vals = []
    for route in g.result_route_phrases(result):
        rcore = _street_core(route)
        if not rcore:
            continue
        for core in source_cores:
            vals.append(SequenceMatcher(None, core, rcore).ratio())
    return max(vals) if vals else 0.0


def _explicit_locality_phrases(text):
    raw = clean(text).replace("’", "'")
    out = []
    rx = re.compile(r"(?i)\b(?:localit[aà]|loc\.?|frazione(?:\s+di)?|fraz\.?|borgata|contrada|c/?\.?da|zona)\s+")
    road = re.compile(r"(?i)\b(?:via|viale|vicolo|piazza|piazzale|corso|strada|largo|traversa|sentiero|salita|lungomare)\b")
    for m in rx.finditer(raw):
        tail = raw[m.end():]
        cut = len(tail)
        for sep in (",", ";"):
            i = tail.find(sep)
            if i >= 0: cut = min(cut, i)
        rm = road.search(tail)
        if rm: cut = min(cut, rm.start())
        phrase = _street_core(tail[:cut])
        if phrase: out.append(phrase)
    return out


def locality_component_similarity(row, result, g):
    expected = _explicit_locality_phrases(row.get("indirizzo"))
    if not expected:
        return 0.0
    vals = g.component_values(result, {
        "neighborhood", "sublocality", "sublocality_level_1", "sublocality_level_2",
        "sublocality_level_3", "sublocality_level_4", "locality",
    })
    comune = _street_core(row.get("comune"))
    actual = []
    for v in vals:
        nv = _street_core(v)
        if nv and nv != comune:
            actual.append(nv)
    scores = [SequenceMatcher(None, e, a).ratio() for e in expected for a in actual]
    return max(scores) if scores else 0.0


def result_postal_codes(result):
    out = set()
    for comp in (result or {}).get("components", []) or []:
        types = set(comp.get("types") or [])
        if "postal_code" in types:
            for key in ("long_name", "short_name"):
                d = re.sub(r"\D", "", clean(comp.get(key)))
                if d:
                    out.add(d.zfill(5))
    return out


def locality_hint(text):
    return bool(re.search(
        r"(?i)\b(localit[aà]|frazione|contrada|c/?\.?da|borgata|vocabolo|zona)\b",
        clean(text),
    ))


def municipality_payload(row, result, query_label=""):
    if not result:
        return None
    lat, lon = fnum(result.get("lat")), fnum(result.get("lon"))
    if lat is None or lon is None:
        return None
    return {
        "latitudine": lat,
        "longitudine": lon,
        "maps_link": maps_link(lat, lon),
        "geocode_source": "google_missing_comune",
        "geocode_location_type": clean(result.get("location_type")) or "APPROXIMATE",
        "geocode_formatted_address": clean(result.get("formatted_address")),
        "geocode_query_used": query_label or clean(row.get("comune")),
    }


def google_missing_solution(row, g, cache, local_ev=None):
    """Trova la migliore precisione senza degradare una LOCALITA ANNCSU sicura a COMUNE."""
    def local_fallback(reason):
        if not local_ev:
            return None
        # Same compactness gate validated in the ANNCSU runtime/ISTAT audit.
        mode = clean(local_ev.get("runtime_locality_match_mode"))
        lat, lon = fnum(local_ev.get("runtime_locality_lat")), fnum(local_ev.get("runtime_locality_lon"))
        n = int(float(local_ev.get("runtime_locality_accesses") or 0))
        p90 = fnum(local_ev.get("runtime_locality_p90_m"))
        compact = mode == "EXACT" and lat is not None and lon is not None and p90 is not None and ((n >= 5 and p90 <= 250) or (n >= 10 and p90 <= 500))
        if not compact:
            return None
        return {"payload": make_local_locality_payload(row, local_ev), "level": "LOCALITA", "best": None, "reverse_check": None, "reason": "anncsu_locality_before_comune:" + clean(reason)}

    best = g.find_best_forward(row, cache)
    if not best:
        lf = local_fallback("google_no_specific_candidate")
        if lf:
            return lf
        center = g.municipality_center(row, cache)
        if center:
            return {
                "payload": municipality_payload(
                    row, center,
                    "COMUNE_ONLY:" + " ".join(x for x in [cap_norm(row.get("cap")), clean(row.get("comune")), clean(row.get("sigla_provincia"))] if x)
                ),
                "level": "COMUNE",
                "best": None,
                "reverse_check": None,
                "reason": "no_specific_candidate_municipality_fallback",
            }
        return {"payload": None, "level": "UNRESOLVED", "best": None, "reverse_check": None, "reason": "google_no_results"}

    result = best.get("result") or {}
    ev = best.get("evaluation") or {}
    score = float(ev.get("score") or 0.0)

    # Se il PVP fornisce un CAP esplicito e Google restituisce un CAP diverso,
    # il candidato specifico NON può essere usato né come CIVICO, né STRADA,
    # né LOCALITA'. Degradiamo al solo Comune.
    source_cap = cap_norm(row.get("cap"))
    google_caps = result_postal_codes(result)
    specific_cap_conflict = bool(
        source_cap and google_caps and source_cap not in google_caps
    )
    if specific_cap_conflict:
        lf = local_fallback("google_specific_cap_conflict")
        if lf:
            return lf
        center = g.municipality_center(row, cache)
        if center:
            return {
                "payload": municipality_payload(
                    row, center,
                    "COMUNE_ONLY:" + " ".join(
                        x for x in [
                            source_cap,
                            clean(row.get("comune")),
                            clean(row.get("sigla_provincia")),
                        ] if x
                    ),
                ),
                "level": "COMUNE",
                "best": best,
                "reverse_check": None,
                "reason": (
                    "specific_candidate_cap_conflict:"
                    + source_cap + "!=" + ",".join(sorted(google_caps))
                ),
            }
        return {
            "payload": None,
            "level": "UNRESOLVED",
            "best": best,
            "reverse_check": None,
            "reason": (
                "specific_candidate_cap_conflict_no_municipality_fallback:"
                + source_cap + "!=" + ",".join(sorted(google_caps))
            ),
        }

    if best.get("locality_fallback"):
        lf = local_fallback("google_locality_fallback")
        if lf:
            return lf
        return {
            "payload": municipality_payload(row, result, clean(best.get("query"))),
            "level": "COMUNE",
            "best": best,
            "reverse_check": None,
            "reason": "google_locality_fallback",
        }

    expected_nums = g.expected_house_numbers(row)
    result_nums = g.result_house_numbers(result)
    civic_rel = g.house_number_relation(expected_nums, result_nums)

    # CIVICO: richiede civico esatto + score alto + reverse del candidato sicuro.
    if expected_nums and civic_rel == "exact" and score >= 82:
        rc = g.verify_candidate_coordinates(row, result, cache)
        if rc.get("safe"):
            lat, lon = fnum(result.get("lat")), fnum(result.get("lon"))
            return {
                "payload": {
                    "latitudine": lat,
                    "longitudine": lon,
                    "maps_link": maps_link(lat, lon),
                    "geocode_source": "google_missing_civico",
                    "geocode_location_type": clean(result.get("location_type")),
                    "geocode_formatted_address": clean(result.get("formatted_address")),
                    "geocode_query_used": clean(best.get("query")),
                },
                "level": "CIVICO",
                "best": best,
                "reverse_check": rc,
                "reason": "exact_civic_google_reverse_verified",
            }

    # STRADA: la strada deve essere realmente riconosciuta, non solo il Comune.
    bm = g.best_address_match(row, result)
    route_phrases = g.result_route_phrases(result)
    strict_route = strict_route_similarity(row.get("indirizzo"), result, g)
    if (
        score >= 72
        and bm is not None and bm >= 0.55
        and route_phrases
        and strict_route >= 0.80
    ):
        lat, lon = fnum(result.get("lat")), fnum(result.get("lon"))
        return {
            "payload": {
                "latitudine": lat,
                "longitudine": lon,
                "maps_link": maps_link(lat, lon),
                "geocode_source": "google_missing_strada",
                "geocode_location_type": clean(result.get("location_type")),
                "geocode_formatted_address": clean(result.get("formatted_address")),
                "geocode_query_used": clean(best.get("query")),
            },
            "level": "STRADA",
            "best": best,
            "reverse_check": None,
            "reason": f"street_match_{bm:.3f}_strict_{strict_route:.3f}_score_{score:.1f}",
        }

    # LOCALITA/FRAZIONE: solo se il testo PVP lo dichiara e Google resta nel
    # Comune canonico. Non lo promuoviamo a strada/civico.
    locality_sim = locality_component_similarity(row, result, g)
    if (
        locality_hint(row.get("indirizzo"))
        and score >= 60
        and g.municipality_geo_matches(row, result)
        and locality_sim >= 0.80
    ):
        lat, lon = fnum(result.get("lat")), fnum(result.get("lon"))
        return {
            "payload": {
                "latitudine": lat,
                "longitudine": lon,
                "maps_link": maps_link(lat, lon),
                "geocode_source": "google_missing_localita",
                "geocode_location_type": clean(result.get("location_type")),
                "geocode_formatted_address": clean(result.get("formatted_address")),
                "geocode_query_used": clean(best.get("query")),
            },
            "level": "LOCALITA",
            "best": best,
            "reverse_check": None,
            "reason": f"locality_component_{locality_sim:.3f}_score_{score:.1f}",
        }

    # Candidato specifico insufficiente: a LOCALITA ANNCSU compatta e verificata
    # viene data precedenza rispetto al centro comunale.
    lf = local_fallback("specific_candidate_below_grade")
    if lf:
        return lf
    center = g.municipality_center(row, cache)
    if center:
        return {
            "payload": municipality_payload(
                row, center,
                "COMUNE_ONLY:" + " ".join(x for x in [cap_norm(row.get("cap")), clean(row.get("comune")), clean(row.get("sigla_provincia"))] if x)
            ),
            "level": "COMUNE",
            "best": best,
            "reverse_check": None,
            "reason": "specific_candidate_below_grade_municipality_fallback",
        }
    return {"payload": None, "level": "UNRESOLVED", "best": best, "reverse_check": None, "reason": "no_safe_specific_or_municipality_result"}


def google_comune_only_solution(row, g, cache, source_comune=""):
    center = g.municipality_center(row, cache)
    used_row = row
    if not center and clean(source_comune) and text_norm(source_comune) != text_norm(row.get("comune")):
        alt = dict(row)
        alt["comune"] = clean(source_comune)
        alt["cap"] = ""
        center = g.municipality_center(alt, cache)
        if center:
            used_row = alt
    if not center:
        return {"payload": None, "level": "UNRESOLVED", "best": None, "reverse_check": None, "reason": "municipality_center_not_found"}
    q = " ".join(x for x in [cap_norm(used_row.get("cap")), clean(used_row.get("comune")), clean(row.get("sigla_provincia"))] if x)
    return {
        "payload": municipality_payload(row, center, "COMUNE_ONLY:" + q),
        "level": "COMUNE",
        "best": None,
        "reverse_check": None,
        "reason": "explicit_comune_only",
    }


def google_validate_pvp(row, g, cache, force_forward=False):
    lat, lon = fnum(row.get("latitudine")), fnum(row.get("longitudine"))
    reverse_results = g.google_reverse(lat, lon, cache)
    ev = g.evaluate_existing_coords(row, reverse_results)
    status = clean(ev.get("status"))
    rr = ev.get("reverse_result") or {}

    if status == "PVP_OK_CIVICO" and not force_forward:
        return {
            "payload": {
                "latitudine": lat,
                "longitudine": lon,
                "maps_link": maps_link(lat, lon),
                "geocode_source": "pvp_verified",
                "geocode_location_type": clean(rr.get("location_type")) or "ROOFTOP",
                "geocode_formatted_address": clean(rr.get("formatted_address")),
                "geocode_query_used": f"reverse:{lat:.7f},{lon:.7f}",
            },
            "level": "PVP_CIVICO",
            "reverse_eval": ev,
            "best": None,
            "reverse_check": None,
            "reason": clean(ev.get("reason")) or "reverse_pvp_ok_civico",
        }

    if status == "PVP_OK_STRADA" and not force_forward:
        return {
            "payload": {
                "latitudine": lat,
                "longitudine": lon,
                "maps_link": maps_link(lat, lon),
                "geocode_location_type": "PVP_OK_STRADA",
                "geocode_formatted_address": clean(rr.get("formatted_address")),
                "geocode_query_used": f"reverse:{lat:.7f},{lon:.7f}",
            },
            "level": "PVP_STRADA",
            "reverse_eval": ev,
            "best": None,
            "reverse_check": None,
            "reason": clean(ev.get("reason")) or "reverse_pvp_ok_strada",
        }

    # Come il repair storico: correzione automatica solo con candidato forte
    # e reverse del candidato completamente sicuro.
    if status == "PVP_SUSPECT" or force_forward:
        best = g.find_best_forward(row, cache)
        if best and not best.get("locality_fallback"):
            score = float((best.get("evaluation") or {}).get("score") or 0.0)
            if score >= 82:
                r = best.get("result") or {}
                source_cap = cap_norm(row.get("cap"))
                google_caps = result_postal_codes(r)
                cap_conflict = bool(source_cap and google_caps and source_cap not in google_caps)
                rc = g.verify_candidate_coordinates(row, r, cache)
                if rc.get("safe") and not cap_conflict:
                    return {
                        "payload": {
                            "latitudine": float(r["lat"]),
                            "longitudine": float(r["lon"]),
                            "maps_link": maps_link(r["lat"], r["lon"]),
                            "geocode_source": "google_corrected_pvp",
                            "geocode_location_type": clean(r.get("location_type")),
                            "geocode_formatted_address": clean(r.get("formatted_address")),
                            "geocode_query_used": clean(best.get("query")),
                        },
                        "level": "PVP_CORRECTED",
                        "reverse_eval": ev,
                        "best": best,
                        "reverse_check": rc,
                        "reason": "google_forward_reverse_verified_correction",
                    }

    # v11.6: force_forward can be triggered by official ISTAT boundary OUTSIDE.
    # If no safe specific correction was found, never keep the outside point:
    # degrade to a municipality-level fallback and validate it again later.
    if force_forward:
        center = g.municipality_center(row, cache)
        if center:
            q = " ".join(
                x for x in [
                    cap_norm(row.get("cap")),
                    clean(row.get("comune")),
                    clean(row.get("sigla_provincia")),
                ] if x
            )
            return {
                "payload": municipality_payload(
                    row, center, "ISTAT_BOUNDARY_GUARD:" + q
                ),
                "level": "COMUNE",
                "reverse_eval": ev,
                "best": locals().get("best"),
                "reverse_check": locals().get("rc"),
                "reason": "istat_outside_degraded_to_comune",
            }
        return {
            "payload": None,
            "level": "PVP_SUSPECT",
            "reverse_eval": ev,
            "best": locals().get("best"),
            "reverse_check": locals().get("rc"),
            "reason": "istat_outside_no_safe_comune_fallback",
        }

    # HARD MUNICIPALITY GUARD v11.5.
    # A pin proven by reverse to be in the wrong municipality must never remain
    # displayed there. If no safe specific correction exists, degrade to the
    # correct municipality center.
    municipality_mismatch = clean(ev.get("reason")) in {
        "reverse_comune_mismatch",
        "reverse_same_comune_province_cap_mismatch",
    }
    if municipality_mismatch:
        center = g.municipality_center(row, cache)
        if center:
            q = " ".join(
                x for x in [
                    cap_norm(row.get("cap")),
                    clean(row.get("comune")),
                    clean(row.get("sigla_provincia")),
                ] if x
            )
            return {
                "payload": municipality_payload(
                    row, center, "MUNICIPALITY_GUARD:" + q
                ),
                "level": "COMUNE",
                "reverse_eval": ev,
                "best": locals().get("best"),
                "reverse_check": locals().get("rc"),
                "reason": "wrong_municipality_degraded_to_comune",
            }
        return {
            "payload": None,
            "level": "PVP_SUSPECT",
            "reverse_eval": ev,
            "best": locals().get("best"),
            "reverse_check": locals().get("rc"),
            "reason": "wrong_municipality_no_safe_comune_fallback",
        }

    # No safe correction and no proven municipality mismatch: keep only PVP status.
    marker = status if status in {"PVP_SUSPECT", "PVP_UNCERTAIN"} else "PVP_UNCERTAIN"
    payload = {
        "geocode_location_type": marker,
        "geocode_query_used": f"reverse:{lat:.7f},{lon:.7f}",
    }
    final_reason = clean(ev.get("reason")) or "pvp_not_safely_confirmed"
    if locals().get("cap_conflict"):
        final_reason = (
            "forward_candidate_cap_conflict:"
            + source_cap + "!=" + ",".join(sorted(google_caps))
        )
    if rr:
        payload["geocode_formatted_address"] = clean(rr.get("formatted_address"))
    return {
        "payload": payload,
        "level": marker,
        "reverse_eval": ev,
        "best": locals().get("best"),
        "reverse_check": None,
        "reason": final_reason,
    }


def build_fresh_plan(planner):
    base, key = planner.load_env()
    items, target_source, load_meta = planner.load_targets(base, key)

    admin_ctx = planner.admin.AdminContext()
    boundary_ctx = planner.boundary.get_context(admin_ctx=admin_ctx)

    immediate = []
    eval_items = []
    boundary_counts = Counter()

    for item in items:
        item["source_row"] = dict(item["row"])
        row_for_admin = dict(item["row"])
        row_for_admin["_v11_row_basis"] = item["row_basis"]
        canonical_row, admin_meta = admin_ctx.canonicalize_row(row_for_admin)
        canonical_row.pop("_v11_row_basis", None)
        item["row"] = canonical_row
        item["admin_meta"] = admin_meta

        if planner.valid_coords(canonical_row):
            btest = boundary_ctx.test(canonical_row, admin_meta=admin_meta)
        else:
            btest = {
                "position": "NO_COORDS",
                "safe": False,
                "canonical_code": clean(admin_meta.get("canonical_code")),
                "utm32_xy": "",
            }
        item["boundary_test"] = btest
        boundary_counts[clean(btest.get("position")) or "UNKNOWN"] += 1

        reason = planner.skip_reason(item["current"], item["meta"])
        if reason and clean(btest.get("position")).upper() != "OUTSIDE":
            immediate.append((item, {}, reason, "unchanged_previous_geocode_state"))
        else:
            eval_items.append(item)

    evidence, runtime_meta = planner.local.build_local_evidence(
        [x["row"] for x in eval_items],
        top_streets=planner.TOP_STREETS,
    ) if eval_items else ([], {"rows": 0})
    ev_by = {clean(x.get("pvp_id")): x for x in evidence}

    planned = list(immediate)
    for item in eval_items:
        pid = clean(item["row"].get("pvp_id"))
        ev = ev_by.get(pid, {})
        pl, why = planner.plan_one(
            item["row"], ev, item["meta"], item["current"],
            istat_position=clean((item.get("boundary_test") or {}).get("position")),
            admin_status=clean((item.get("admin_meta") or {}).get("status")),
        )
        planned.append((item, ev, pl, why))

    order = {clean(item["row"].get("pvp_id")): i for i, item in enumerate(items)}
    planned.sort(key=lambda x: order.get(clean(x[0]["row"].get("pvp_id")), 999999999))
    return base, key, planned, {
        "target_source": target_source,
        "load_meta": load_meta,
        "runtime_meta": runtime_meta,
        "targets": len(items),
        "local_evaluated": len(eval_items),
        "skipped": len(immediate),
        "boundary_counts": dict(boundary_counts),
        "boundary_runtime": boundary_ctx.metadata(),
    }

def execute_one(item, ev, plan_name, plan_reason, planner, cache):
    row = item["row"]
    g = planner.google_core

    if plan_name.startswith("SKIP_"):
        return {
            "payload": None, "level": "SKIP", "reason": plan_reason,
            "best": None, "reverse_check": None, "reverse_eval": None,
        }

    if plan_name.startswith("REVIEW_"):
        return {
            "payload": None, "level": "REVIEW", "reason": plan_reason,
            "best": None, "reverse_check": None, "reverse_eval": None,
        }

    if plan_name == "LOCAL_BOUNDARY_CIVICO_READY":
        return {
            "payload": make_boundary_civic_payload(row, ev),
            "level": "CIVICO", "reason": plan_reason,
            "best": None, "reverse_check": None, "reverse_eval": None,
        }

    if plan_name == "LOCAL_BOUNDARY_STRADA_READY":
        return {
            "payload": make_boundary_street_payload(row, ev),
            "level": "STRADA", "reason": plan_reason,
            "best": None, "reverse_check": None, "reverse_eval": None,
        }

    if plan_name == "KEEP_PVP_LOCAL_CONFIRMED":
        payload, level = make_local_pvp_confirm_payload(row, ev, g)
        return {
            "payload": payload, "level": level, "reason": plan_reason,
            "best": None, "reverse_check": None, "reverse_eval": None,
        }

    if plan_name == "LOCAL_CIVICO_READY":
        return {
            "payload": make_local_civic_payload(row, ev),
            "level": "CIVICO", "reason": plan_reason,
            "best": None, "reverse_check": None, "reverse_eval": None,
        }

    if plan_name == "LOCAL_STRADA_READY":
        return {
            "payload": make_local_street_payload(row, ev),
            "level": "STRADA", "reason": plan_reason,
            "best": None, "reverse_check": None, "reverse_eval": None,
        }

    if plan_name == "LOCAL_LOCALITA_READY":
        return {
            "payload": make_local_locality_payload(row, ev),
            "level": "LOCALITA", "reason": plan_reason,
            "best": None, "reverse_check": None, "reverse_eval": None,
        }

    if plan_name == "LOCAL_BOUNDARY_LOCALITA_READY":
        return {
            "payload": make_local_locality_payload(row, ev, boundary=True),
            "level": "LOCALITA", "reason": plan_reason,
            "best": None, "reverse_check": None, "reverse_eval": None,
        }

    if plan_name == "GOOGLE_COMUNE_ONLY":
        source_row = item.get("source_row") or {}
        return google_comune_only_solution(
            row, g, cache, source_comune=source_row.get("comune")
        )

    if plan_name == "GOOGLE_NEEDED_MISSING":
        return google_missing_solution(row, g, cache, ev)

    if plan_name in {
        "GOOGLE_VALIDATE_NEW_PVP", "GOOGLE_VALIDATE_PVP",
        "GOOGLE_ARBITRATION_NEW_PVP", "GOOGLE_ARBITRATION_EXISTING",
    }:
        return google_validate_pvp(
            row, g, cache,
            force_forward=plan_name.startswith("GOOGLE_ARBITRATION"),
        )

    if plan_name in {"KEEP_CURRENT_LOCAL_CONFIRMED", "KEEP_PROTECTED_NEEDS_NO_CHANGE"}:
        return {
            "payload": None, "level": "KEEP", "reason": plan_reason,
            "best": None, "reverse_check": None, "reverse_eval": None,
        }

    return {
        "payload": None, "level": "UNHANDLED", "reason": f"unhandled_plan:{plan_name}",
        "best": None, "reverse_check": None, "reverse_eval": None,
    }


def istat_comune_fallback_solution(row, planner, previous_reason=""):
    """Last-resort municipality-level pin from the official ISTAT polygon.

    Used only when the normal local/Google chain ends UNRESOLVED or when a
    proposed point is rejected by the municipality boundary guard. The point
    is explicitly marked as COMUNE precision and re-verified inside the same
    canonical municipality polygon before it can be returned.
    """
    bctx = planner.boundary.get_context()
    rep = bctx.representative_point(row)
    if not rep.get("ok"):
        return None
    lat, lon = fnum(rep.get("lat")), fnum(rep.get("lon"))
    if lat is None or lon is None:
        return None
    # Final fail-closed recheck, even though representative_point already does it.
    test = bctx.test(row, lat=lat, lon=lon)
    if not test.get("safe"):
        return None
    code = clean(rep.get("canonical_code"))
    comune = clean(row.get("comune"))
    sigla = clean(row.get("sigla_provincia")).upper()
    label = " ".join(x for x in [comune, f"({sigla})" if sigla else ""] if x)
    payload = {
        "latitudine": lat,
        "longitudine": lon,
        "maps_link": maps_link(lat, lon),
        "geocode_source": "istat_missing_comune",
        "geocode_location_type": "ISTAT_COMUNE_FALLBACK",
        "geocode_formatted_address": (f"Comune di {label}, Italia" if label else "Comune - fallback ISTAT"),
        "geocode_query_used": f"ISTAT_COMUNE:{code}",
    }
    reason = "istat_comune_final_fallback"
    if clean(previous_reason):
        reason += ":" + clean(previous_reason)
    return {
        "payload": payload,
        "level": "COMUNE",
        "reason": reason,
        "best": None,
        "reverse_check": None,
        "reverse_eval": None,
        "istat_candidate_position": clean(test.get("position")),
    }


def enforce_istat_boundary(result, row, planner, cache):
    """Never return a coordinate payload outside the canonical ISTAT municipality."""
    payload = result.get("payload") or {}
    if not (
        planner.valid_coords({
            "latitudine": payload.get("latitudine"),
            "longitudine": payload.get("longitudine"),
        })
    ):
        # v11.6.2: if every precise/local/Google attempt failed, keep the
        # listing visible on the map with an explicitly approximate ISTAT
        # municipality-level point. REVIEW/SUSPECT/UNCERTAIN are not touched.
        if clean(result.get("level")).upper() == "UNRESOLVED":
            fallback = istat_comune_fallback_solution(
                row, planner, clean(result.get("reason"))
            )
            if fallback:
                return fallback
        result["istat_candidate_position"] = ""
        return result

    bctx = planner.boundary.get_context()
    test = bctx.test(
        row,
        lat=payload.get("latitudine"),
        lon=payload.get("longitudine"),
        admin_meta=None,
    )
    result["istat_candidate_position"] = clean(test.get("position"))
    if test.get("safe"):
        return result

    # Reject an outside specific candidate and try municipality fallback once.
    if clean(result.get("level")).upper() != "COMUNE":
        fallback = google_comune_only_solution(row, planner.google_core, cache)
        fp = fallback.get("payload") or {}
        if planner.valid_coords({
            "latitudine": fp.get("latitudine"),
            "longitudine": fp.get("longitudine"),
        }):
            ft = bctx.test(
                row,
                lat=fp.get("latitudine"),
                lon=fp.get("longitudine"),
            )
            fallback["istat_candidate_position"] = clean(ft.get("position"))
            if ft.get("safe"):
                fallback["reason"] = (
                    "istat_rejected_specific_candidate__"
                    + clean(result.get("reason"))
                    + "__fallback_comune_inside"
                )
                return fallback

    rejected_reason = (
        "istat_boundary_rejected_candidate:"
        + clean(test.get("position"))
        + ":"
        + clean(result.get("reason"))
    )
    # v11.6.2 final no-network fallback: if Google/ANNCSU cannot give a safe
    # municipality point, derive one directly from the official ISTAT polygon.
    fallback = istat_comune_fallback_solution(row, planner, rejected_reason)
    if fallback:
        return fallback

    return {
        "payload": None,
        "level": "UNRESOLVED",
        "reason": rejected_reason,
        "best": result.get("best"),
        "reverse_check": result.get("reverse_check"),
        "reverse_eval": result.get("reverse_eval"),
        "istat_candidate_position": clean(test.get("position")),
    }


def audit_record(item, ev, plan_name, plan_reason, result, apply_status):
    row = item["row"]
    source_row = item.get("source_row") or row
    current = item.get("current") or {}
    payload = result.get("payload") or {}
    best = result.get("best") or {}
    bev = best.get("evaluation") or {}
    rc = result.get("reverse_check") or {}
    rev = result.get("reverse_eval") or {}
    rr = rev.get("reverse_result") or {}
    return {
        "pvp_id": clean(row.get("pvp_id")),
        "plan": plan_name,
        "plan_reason": plan_reason,
        "row_basis": item.get("row_basis", ""),
        "source_indirizzo": clean(source_row.get("indirizzo")),
        "source_comune": clean(source_row.get("comune")),
        "source_sigla_provincia": clean(source_row.get("sigla_provincia")),
        "source_cap": clean(source_row.get("cap")),
        "candidate_indirizzo": clean(row.get("indirizzo")),
        "candidate_comune": clean(row.get("comune")),
        "candidate_sigla_provincia": clean(row.get("sigla_provincia")),
        "candidate_cap": clean(row.get("cap")),
        "old_lat": current.get("latitudine"),
        "old_lon": current.get("longitudine"),
        "old_source": clean(current.get("geocode_source")),
        "old_location_type": clean(current.get("geocode_location_type")),
        "result_level": clean(result.get("level")),
        "result_reason": clean(result.get("reason")),
        "proposed_lat": payload.get("latitudine", ""),
        "proposed_lon": payload.get("longitudine", ""),
        "proposed_source": clean(payload.get("geocode_source")),
        "proposed_location_type": clean(payload.get("geocode_location_type")),
        "proposed_formatted_address": clean(payload.get("geocode_formatted_address")),
        "proposed_query_used": clean(payload.get("geocode_query_used")),
        "google_query": clean(best.get("query")),
        "google_score": bev.get("score", ""),
        "google_confidence": clean(bev.get("confidence")),
        "reverse_status": clean(rev.get("status")),
        "reverse_reason": clean(rev.get("reason")),
        "reverse_formatted_address": clean(rr.get("formatted_address")),
        "candidate_reverse_safe": rc.get("safe", ""),
        "candidate_reverse_reason": clean(rc.get("reason")),
        "current_istat_position": clean((item.get("boundary_test") or {}).get("position")),
        "candidate_istat_position": clean(result.get("istat_candidate_position")),
        "apply_status": apply_status,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="abilita PATCH Supabase")
    args = ap.parse_args()
    apply = bool(args.apply)

    load_project_env()

    # Il planner/core deve essere importato SOLO dopo aver caricato env e cap.
    planner = load_module(PLANNER_FILE, "asteeasy_unified_plan_v113_live")
    g = planner.google_core

    if not clean(g.GOOGLE_GEOCODING_API_KEY):
        raise SystemExit("ERRORE: Google key non caricata nel core.")

    print("=" * 96)
    print("ASTE EASY - UNIFIED GEOCODER v11.6.4 LIVE")
    print("MODE:", "APPLY (Google + PATCH)" if apply else "DRY RUN (Google REALE + ZERO PATCH)")
    print(f"scope={planner.SCOPE} pvp_id={planner.PVP_ID or '-'} limit={planner.LIMIT or 'all'}")
    print("=" * 96)

    for sku, label in [
        ("geocoding", "Geocoding"),
        ("places_autocomplete", "Autocomplete"),
        ("address_validation", "AddressValidation"),
    ]:
        local_u, off_u, eff_u = g.API_USAGE.month_usage(sku)
        cap = g.API_USAGE._month_limit_for(sku)
        print(f"[CAP] {label}: uso={eff_u} (locale={local_u}, offset={off_u}) cap={cap or 'OFF'}")

    base, key, planned, meta = build_fresh_plan(planner)
    counts = Counter(x[2] for x in planned)
    print(f"[PLAN FRESH] targets={meta['targets']} local_eval={meta['local_evaluated']} skip={meta['skipped']}")
    print("[PLAN FRESH]", dict(counts))

    cache = g.CacheStore(g.CACHE_FILE)
    out_rows = []
    result_counts = Counter()
    apply_counts = Counter()
    budget_stop = ""

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    ensure_runtime_dirs()
    csv_path = REPORTS_DIR / f"geocode_unified_live_v11_6_4_{'apply' if apply else 'dryrun'}_{stamp}.csv"
    summary_path = REPORTS_DIR / f"geocode_unified_live_summary_v11_6_4_{'apply' if apply else 'dryrun'}_{stamp}.json"

    try:
        for idx, (item, ev, plan_name, plan_reason) in enumerate(planned, start=1):
            pid = clean(item["row"].get("pvp_id"))

            # Gli skip non devono fare alcun lavoro.
            if plan_name.startswith("SKIP_"):
                continue

            try:
                result = execute_one(item, ev, plan_name, plan_reason, planner, cache)
                result = enforce_istat_boundary(result, item["row"], planner, cache)
            except g.ApiBudgetExceeded:
                raise
            except Exception as exc:
                result = {
                    "payload": None,
                    "level": "ERROR",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "best": None, "reverse_check": None, "reverse_eval": None,
                }

            result_counts[result.get("level") or "<EMPTY>"] += 1
            payload = result.get("payload")
            apply_status = "DRY_RUN"

            if apply and payload:
                now = db_get_one(base, key, pid)
                ok, why = current_state_ok(item, now)
                if not ok:
                    apply_status = f"SKIP_RECHECK:{why}"
                else:
                    db_patch_one(base, key, pid, payload)
                    apply_status = "PATCHED"
            elif apply and not payload:
                apply_status = "NO_PATCH"

            apply_counts[apply_status] += 1
            rec = audit_record(item, ev, plan_name, plan_reason, result, apply_status)
            out_rows.append(rec)

            if plan_name.startswith("GOOGLE_") or result.get("level") in {"ERROR", "UNRESOLVED", "PVP_SUSPECT", "PVP_UNCERTAIN"}:
                print(
                    f"[{idx}/{len(planned)}] pvp={pid} plan={plan_name} "
                    f"=> {result.get('level')} {apply_status} reason={result.get('reason')}"
                )

    except g.ApiBudgetExceeded as exc:
        budget_stop = str(exc)
        print(f"[STOP SAFETY] {budget_stop}")
    finally:
        cache.save()
        g.API_USAGE.save()

    fields = [
        "pvp_id","plan","plan_reason","row_basis",
        "source_indirizzo","source_comune","source_sigla_provincia","source_cap",
        "candidate_indirizzo","candidate_comune","candidate_sigla_provincia","candidate_cap",
        "old_lat","old_lon","old_source","old_location_type",
        "result_level","result_reason",
        "proposed_lat","proposed_lon","proposed_source","proposed_location_type",
        "proposed_formatted_address","proposed_query_used",
        "google_query","google_score","google_confidence",
        "reverse_status","reverse_reason","reverse_formatted_address",
        "candidate_reverse_safe","candidate_reverse_reason",
        "current_istat_position","candidate_istat_position",
        "apply_status",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)

    usage = {}
    for sku in g.API_USAGE.SKUS:
        local_u, off_u, eff_u = g.API_USAGE.month_usage(sku)
        usage[sku] = {
            "run": dict(g.API_USAGE.run[sku]),
            "month_local": local_u,
            "month_external_offset": off_u,
            "month_effective": eff_u,
            "month_cap": g.API_USAGE._month_limit_for(sku),
        }

    summary = {
        "version": "11.6.4-locality-explicit-guard-specific-first",
        "generated_at": datetime.now().astimezone().isoformat(),
        "mode": "APPLY" if apply else "DRY_RUN_GOOGLE_REAL_ZERO_PATCH",
        "scope": planner.SCOPE,
        "target_source": meta["target_source"],
        "target_count": meta["targets"],
        "local_evaluated": meta["local_evaluated"],
        "skipped_without_work": meta["skipped"],
        "istat_boundary_counts": meta.get("boundary_counts", {}),
        "istat_boundary_runtime": meta.get("boundary_runtime", {}),
        "fresh_plan_counts": dict(counts.most_common()),
        "result_counts": dict(result_counts.most_common()),
        "apply_counts": dict(apply_counts.most_common()),
        "budget_stop": budget_stop,
        "google_usage": usage,
        "cache_file": str(g.CACHE_FILE.name),
        "usage_file": str(g.API_USAGE.path.name),
        "files": {
            "summary": summary_path.name,
            "audit_csv": csv_path.name,
        },
        "notes": [
            "DRY RUN chiama Google realmente e salva la cache, ma non esegue PATCH.",
            "APPLY ricalcola il piano e rilegge ogni record prima del PATCH.",
            "PVP assenti da Supabase vengono saltati in APPLY.",
            "Comune/sigla sono canonicalizzati ISTAT solo per matching/geocoding; questo script non modifica i campi amministrativi del listing.",
            "comuni_cap.csv non inventa CAP.",
            "v11.6.4: locality-only ANNCSU puo essere diretto; con strada/civico si tenta prima il candidato specifico e solo poi locality fallback; explicit locality guard fail-closed.",
            "Google Cloud resta la fonte ufficiale del consumo fatturabile; google_api_usage_v9_2.json e' il contatore locale di sicurezza.",
        ],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("[RESULT]", dict(result_counts))
    print("[APPLY]", dict(apply_counts))
    g.API_USAGE.print_summary()
    print("[OUT]", summary_path.name)
    print("[OUT]", csv_path.name)
    print("[DONE PARZIALE - SAFETY CAP]" if budget_stop else "[DONE]")


if __name__ == "__main__":
    main()
