#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DB-driven offline geocoder for NEW PENDING rows. No Google API, no manifests.

Default: GET-only plan. --apply PATCHes only the SAME PENDING input, with guarded
filters and read-back verification. Historical rows with NULL status are excluded.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import sys
import socket
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from aste_paths import ANNCSU_DIR, REFERENCE_DIR, REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env

VERSION = "geocode_pending_offline_v2_2_locality_bypass"
SELECT = ("pvp_id", "comune", "sigla_provincia", "provincia", "regione", "cap", "indirizzo",
          "pvp_latitudine", "pvp_longitudine", "pvp_geo_source", "latitudine", "longitudine",
          "geocode_status", "geocode_input_hash", "geocode_source", "geocode_precision")
COORD_EPS = 0.0000001
_CONTEXT_CACHE = {}


def load_py(path, name):
    if not path.is_file():
        raise RuntimeError("Required local module missing: " + str(path))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def prepare_context(*, no_anncsu=False):
    if no_anncsu in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[no_anncsu]
    # Without the ZIP the historic boundary module would download it implicitly.
    boundary = load_py(HERE / "istat_boundary_runtime_v11_6.py", "pending_boundary_v1")
    if not boundary.ISTAT_ZIP.is_file():
        raise RuntimeError("ISTAT local boundary ZIP missing: " + str(boundary.ISTAT_ZIP))
    if not (REFERENCE_DIR / "Elenco-comuni-italiani.xlsx").is_file():
        raise RuntimeError("ISTAT municipality workbook missing")
    ctx = boundary.get_context()
    territorial = load_py(HERE / "geocode_territorial_offline_v1.py", "pending_territorial_v1")
    local = None
    if not no_anncsu:
        sqlite_path = Path(os.environ.get("ANNCSU_SQLITE_INDEX", "") or (ANNCSU_DIR / "anncsu_runtime.sqlite3"))
        if not sqlite_path.is_file():
            for stem in ("STRAD_ITA_", "INDIR_ITA_"):
                if not any(ANNCSU_DIR.glob(stem + "*.csv")):
                    raise RuntimeError(
                        "ANNCSU SQLite index missing and legacy " + stem +
                        " CSV missing; build the index or opt out with --no-anncsu")
        # Loading local ANNCSU reuses parsers from the Google module, NOT its API.
        # Prevent optional prior usage-offset synchronization in this process.
        for key in ("MONTH_USAGE_OFFSET_GEOCODING", "MONTH_USAGE_OFFSET_AUTOCOMPLETE",
                    "MONTH_USAGE_OFFSET_ADDRESS_VALIDATION"):
            os.environ.pop(key, None)
        local = load_py(HERE / "anncsu_runtime_consolidated_v2.py", "pending_anncsu_v2")
    _CONTEXT_CACHE[no_anncsu] = (ctx, territorial, local)
    return _CONTEXT_CACHE[no_anncsu]


def fnum(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def valid_pair(lat, lon):
    lat, lon = fnum(lat), fnum(lon)
    return (lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180
            and not (lat == 0 and lon == 0))


def distance_m(a, b, c, d):
    rad = math.pi / 180
    x = math.sin((c-a)*rad/2)**2 + math.cos(a*rad)*math.cos(c*rad)*math.sin((d-b)*rad/2)**2
    return 12742000 * math.asin(min(1, math.sqrt(x)))


def pvp_row(row):
    r = dict(row)
    r["latitudine"], r["longitudine"] = row.get("pvp_latitudine"), row.get("pvp_longitudine")
    return r


def input_digest(rows):
    data = [{k: row.get(k) for k in SELECT} for row in rows]
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def maps_link(lat, lon):
    return "https://www.google.com/maps?q=" + quote(f"{lat:.8f},{lon:.8f}")


def done(row, *, lat=None, lon=None, source=None, precision=None, reason=None,
         status="DONE", formatted=None, query=None):
    payload = {
        "latitudine": lat, "longitudine": lon,
        "maps_link": maps_link(lat, lon) if valid_pair(lat, lon) else None,
        "geocode_status": status,
        "geocode_source": source,
        "geocode_precision": precision,
        "geocode_version": VERSION,
        "geocode_checked_at": datetime.now(timezone.utc).isoformat(),
        "geocode_google_outcome": "NOT_REQUESTED",
        "geocode_decision_reason": reason,
        "geocode_location_type": precision,
        "geocode_formatted_address": formatted,
        "geocode_query_used": query,
    }
    return payload



def _ev_num(ev, name):
    return fnum(ev.get(name)) if ev else None


def _pvp_street_or_locality(row, la, lo, ev, local, is_multi):
    """Corroborate PVP using matched ANNCSU access clouds, not road centroids.

    No automatic coordinate replacement: a distant cluster is only a review
    signal when compact, well-covered and not a rural/compound address.
    """
    if not ev or local is None:
        return None
    street_score = _ev_num(ev, "street_score")
    strict = _ev_num(ev, "strict_street_similarity")
    nstreet = _ev_num(ev, "matching_street_candidates")
    near = _ev_num(ev, "selected_street_nearest_high_accuracy_access_m")
    high = _ev_num(ev, "selected_street_high_accuracy_accesses") or 0
    rep = ev.get("street_representative") or {}
    guard = str(ev.get("direct_locality_guard") or "").upper().strip()
    matched = (
        str(ev.get("province_relation") or "").upper().strip() == "MATCH"
        and str(ev.get("ambiguity") or "").upper().strip() == "NO"
        and guard in {"NO_HINT", "CONFIRMED"}
        and street_score is not None and street_score >= .96
        and strict is not None and strict >= .90 and nstreet == 1
        and ev.get("street_representative_status") == "UNIQUE"
        and high >= 2 and near is not None
    )
    if matched:
        name = str(ev.get("anncsu_odonimo") or "strada").strip()
        if near <= 150:
            return done(row, lat=la, lon=lo,
                        source="pvp_anncsu_strada_compatibile",
                        precision="PVP_STRADA_COMPATIBILE",
                        reason=f"Odonimo ANNCSU {name} univoco; accesso 1/3 più vicino a {near:.0f} m. Non prova il civico.",
                        formatted=row.get("indirizzo"), query="PVP+ANNCSU:STRADA_ACCESSI")
        # A compact and well-covered street provides a meaningful contradiction.
        # Exclude multi-bene, rural roads, km markers and incomplete geometries.
        raw = " " + str(row.get("indirizzo") or "").casefold() + " "
        rural = any(s in raw for s in (
            "contrada", "c/da", "c.da", "località", "localita", "frazione",
            "borgata", "vicinale", "strada statale", "strada provinciale",
            " ss ", " sp ", " km "))
        max_extent = fnum(rep.get("max_from_rep_m"))
        if (not is_multi and not rural and high >= 8 and max_extent is not None
                and max_extent <= 500 and near > 1500):
            return done(row, status="REVIEW", source="review_pvp_strada_discordante",
                        precision="NESSUNA",
                        reason=f"PVP a {near:.0f} m dal più vicino accesso 1/3 ANNCSU di {name}; strada compatta e coperta: verifica manuale.",
                        formatted=row.get("indirizzo"), query="PVP+ANNCSU:STRADA_ACCESSI")
        return done(row, lat=la, lon=lo, source="pvp_strada_non_conclusiva",
                    precision="PVP_IN_COMUNE",
                    reason=f"Odonimo ANNCSU {name} plausibile, ma accesso 1/3 più vicino a {near:.0f} m: dati non conclusivi, pin non verificato oltre il Comune.",
                    formatted=row.get("indirizzo"), query="PVP+ANNCSU:STRADA_ACCESSI")

    # A locality is evaluated only if the source does not also identify a
    # specific road/civic and its official access cloud is compact/unambiguous.
    if local.locality_for_missing(row, ev, allow_fallback=False)[0]:
        near = _ev_num(ev, "matched_locality_nearest_high_accuracy_access_m")
        n = _ev_num(ev, "matched_locality_high_accuracy_accesses") or 0
        if near is not None and n >= 5:
            loc = str(ev.get("runtime_anncsu_locality") or "località").strip()
            if near <= 200:
                return done(row, lat=la, lon=lo,
                            source="pvp_anncsu_localita_compatibile",
                            precision="PVP_LOCALITA_COMPATIBILE",
                            reason=f"Località ANNCSU {loc} esatta e compatta; accesso 1/3 più vicino a {near:.0f} m. Non prova l'immobile.",
                            formatted=row.get("indirizzo"), query="PVP+ANNCSU:LOCALITA_ACCESSI")
            radius = _ev_num(ev, "runtime_locality_max_m")
            if (not is_multi and n >= 10 and radius is not None and radius <= 600
                    and near > 1500):
                return done(row, status="REVIEW", source="review_pvp_localita_discordante",
                            precision="NESSUNA",
                            reason=f"PVP a {near:.0f} m dalla località ANNCSU {loc}, compatta e con almeno 10 accessi 1/3: verifica manuale.",
                            formatted=row.get("indirizzo"), query="PVP+ANNCSU:LOCALITA_ACCESSI")
            return done(row, lat=la, lon=lo, source="pvp_localita_non_conclusiva",
                        precision="PVP_IN_COMUNE",
                        reason=f"Località ANNCSU {loc} identificata ma accesso 1/3 più vicino a {near:.0f} m: confronto non conclusivo.",
                        formatted=row.get("indirizzo"), query="PVP+ANNCSU:LOCALITA_ACCESSI")
    return None

def decision(row, ctx, territorial, local=None, ev=None, rep_cache=None):
    """Strict precedence: coherent PVP -> safe ANNCSU -> ISTAT indicative -> REVIEW."""
    rep_cache = rep_cache if rep_cache is not None else {}
    if row.get("geocode_status") != "PENDING":
        raise RuntimeError("Non-PENDING row was submitted to the geocoder")
    if row.get("latitudine") is not None or row.get("longitudine") is not None:
        raise RuntimeError("PENDING row already has published coordinates; manual review required")
    if not row.get("geocode_input_hash"):
        raise RuntimeError("PENDING row missing input hash")
    sample = pvp_row(row)
    meta = ctx.resolve_admin(sample)
    if meta.get("status") != "RESOLVED" or not meta.get("canonical_code"):
        return done(row, status="REVIEW", source="review_admin", precision="NESSUNA",
                    reason="Comune non risolto univocamente con ISTAT")
    la, lo = fnum(row.get("pvp_latitudine")), fnum(row.get("pvp_longitudine"))
    if (la is None) != (lo is None) or (la is not None and not valid_pair(la, lo)):
        return done(row, status="REVIEW", source="review_pvp_invalid", precision="NESSUNA",
                    reason="Coordinate PVP incomplete o non valide")
    pos = ctx.test(sample, lat=la, lon=lo, admin_meta=meta).get("position") if la is not None else "NO_COORDS"
    is_multi = "multi" in str(row.get("pvp_geo_source") or "").lower()
    if pos in ("INSIDE", "BOUNDARY"):
        # Municipality containment is NOT verification of a street number.
        civic = territorial.local_option(sample, ev, local, ctx, meta) if local is not None else None
        if civic and civic["precision"] == "CIVICO":
            dist = distance_m(la, lo, civic["lat"], civic["lon"])
            if dist <= 100:
                return done(row, lat=la, lon=lo, source="pvp_anncsu_civico_compatibile",
                            precision="PVP_CIVICO_COMPATIBILE",
                            reason=f"PVP nel Comune e accesso civico ANNCSU a {dist:.0f} m",
                            formatted=row.get("indirizzo"), query="PVP+ANNCSU")
            if dist > 250 and not is_multi:
                # ANNCSU locates an access, not necessarily the auctioned unit.
                # Never automatically replace a discordant PVP coordinate.
                return done(row, status="REVIEW", source="review_pvp_civico_discordante",
                            precision="NESSUNA", reason=f"PVP distante {dist:.0f} m dall'accesso civico ANNCSU univoco: confronto aggiuntivo necessario, nessuna sostituzione automatica",
                            formatted=row.get("indirizzo"), query="PVP+ANNCSU:CIVICO")
            if dist > 250 and is_multi:
                return done(row, status="REVIEW", source="review_multi_bene_civico",
                            precision="NESSUNA", reason="Più beni e PVP discordante da accesso civico ANNCSU; verificare lotto")
            if 100 < dist <= 250:
                return done(row, lat=la, lon=lo, source="pvp_civico_non_conclusivo",
                            precision="PVP_IN_COMUNE",
                            reason=f"Civico univoco ANNCSU a {dist:.0f} m: distanza intermedia, non verifica del civico.",
                            formatted=row.get("indirizzo"), query="PVP+ANNCSU:CIVICO")
        refined = _pvp_street_or_locality(row, la, lo, ev, local, is_multi)
        if refined is not None:
            return refined
        return done(row, lat=la, lon=lo, source="pvp_comune_compatibile",
                    precision="PVP_IN_COMUNE", reason="PVP entro Comune ISTAT; civico/strada/località non verificati",
                    formatted=row.get("indirizzo"), query="PVP+ISTAT")
    if pos == "OUTSIDE":
        alt = territorial.alternative_comune_for_pin(sample, ctx, la, lo)
        if alt:
            # An explicit alternative municipality is an informative but unresolved pin.
            return done(row, lat=la, lon=lo, status="REVIEW", source="pvp_comune_alternativo",
                        precision="PVP_INDICATIVO_ALTRO_COMUNE",
                        reason=f"Pin nel Comune alternativo dichiarato nell'indirizzo: {alt}; verificare i beni",
                        formatted=row.get("indirizzo"), query="PVP+ISTAT:ALTRO_COMUNE")
        if is_multi:
            return done(row, status="REVIEW", source="review_multi_bene_pvp",
                        precision="NESSUNA", reason="Pin PVP esterno al Comune principale con più beni: non ricollocare automaticamente")
    if pos not in ("NO_COORDS", "OUTSIDE"):
        return done(row, status="REVIEW", source="review_istat_boundary",
                    precision="NESSUNA", reason="Test territoriale non risolto: " + str(pos))
    choice = territorial.local_option(sample, ev, local, ctx, meta) if local is not None else None
    if choice:
        source = str(choice.get("source") or ("anncsu_" + choice["precision"].lower()))
        return done(row, lat=choice["lat"], lon=choice["lon"],
                    source=source, precision=choice["precision"],
                    reason="Posizione ANNCSU nel Comune; " + str(choice["reason"]),
                    formatted=row.get("indirizzo"), query="ANNCSU:" + choice["precision"])
    code = str(meta["canonical_code"])
    if code not in rep_cache:
        rep_cache[code] = ctx.representative_point(sample, admin_meta=meta)
    rep = rep_cache[code]
    if rep.get("ok") and valid_pair(rep.get("lat"), rep.get("lon")) and ctx.test(
        sample, lat=rep["lat"], lon=rep["lon"], admin_meta=meta).get("position") == "INSIDE":
        return done(row, lat=rep["lat"], lon=rep["lon"],
                    source="istat_comune_indicativo", precision="COMUNE",
                    reason="Punto INDICATIVO nel Comune, non posizione dell'immobile; Google non richiesto",
                    formatted="Comune di " + str(row.get("comune") or ""), query="ISTAT_COMUNE:" + code)
    return done(row, status="REVIEW", source="review_no_comune_point", precision="NESSUNA",
                reason="Nessun punto rappresentativo verificato all'interno del Comune")


def _is_transient_transport(exc):
    if isinstance(exc, (ConnectionResetError, TimeoutError, socket.timeout, URLError)):
        return True
    if isinstance(exc, OSError):
        # Windows connection reset / aborted / timed out and analogous POSIX errors.
        return getattr(exc, "winerror", None) in {10053, 10054, 10060} or getattr(exc, "errno", None) in {54, 60, 104, 110}
    return False


def _json_request(base, key, route, params=None, payload=None, method="GET"):
    url = base.rstrip("/") + "/rest/v1/" + route
    if params:
        url += "?" + urlencode(params)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = Request(url, data=data, method=method, headers={
        "apikey": key, "Authorization": "Bearer " + key,
        "Accept": "application/json", "Content-Type": "application/json",
        **({"Prefer": "return=representation"} if method == "PATCH" else {})
    })
    # GET is safe to retry automatically. PATCH is deliberately one-shot here:
    # guarded_patch() handles its unknown-commit window with a read-back before retrying.
    attempts = 3 if method == "GET" else 1
    for attempt in range(attempts):
        try:
            with urlopen(req, timeout=120) as response:
                return json.loads(response.read().decode("utf-8") or "[]")
        except HTTPError as exc:
            raise RuntimeError(f"Supabase HTTP {exc.code}: {exc.read().decode('utf-8',errors='replace')[:250]}") from exc
        except Exception as exc:
            if method == "GET" and _is_transient_transport(exc) and attempt < attempts - 1:
                print(f"[SUPABASE RETRY] GET transport error {type(exc).__name__}; retry {attempt+1}/{attempts-1}", flush=True)
                time.sleep(1.5 * (attempt + 1))
                continue
            raise


def fetch_pending(base, key, max_rows):
    rows, cursor = [], None
    while len(rows) < max_rows:
        params = [("select", ",".join(SELECT)), ("geocode_status", "eq.PENDING"),
                  ("order", "pvp_id.asc"), ("limit", str(min(500, max_rows - len(rows))))]
        if cursor:
            params.append(("pvp_id", "gt." + cursor))
        batch = _json_request(base, key, "listings", params=params)
        if not isinstance(batch, list):
            raise RuntimeError("Unexpected pending listings response")
        if not batch:
            break
        if any(not isinstance(x, dict) for x in batch):
            raise RuntimeError("Invalid pending listing row")
        pids = [str(x.get("pvp_id")) for x in batch]
        if pids != sorted(set(pids)) or (cursor and pids[0] <= cursor):
            raise RuntimeError("Unsorted/duplicate pending listings: stop")
        rows.extend(batch)
        cursor = pids[-1]
    return rows


def plan_rows(rows, ctx, territorial, local):
    ev_by_id = {}
    if local is not None and rows:
        print(f"[ANNCSU] Analisi locale di {len(rows)} righe; nessuna chiamata Google", flush=True)
        evidence, _summary = local.build_local_evidence([pvp_row(r) for r in rows])
        ev_by_id = {str(ev.get("pvp_id")): ev for ev in evidence}
    rep_cache = {}
    return [(row, decision(row, ctx, territorial, local, ev_by_id.get(str(row["pvp_id"])), rep_cache))
            for row in rows]


def preview_new_rows(listings, *, max_rows=2000, no_anncsu=False):
    """Offline geographic preview of NEW importer rows, before a single DB INSERT."""
    ctx, territorial, local = prepare_context(no_anncsu=no_anncsu)
    if max_rows < 1:
        raise RuntimeError("max_rows must be positive")
    rows = []
    for listing in listings[:max_rows]:
        row = {k: listing.get(k) for k in SELECT}
        row["latitudine"] = None
        row["longitudine"] = None
        if row.get("geocode_status") != "PENDING":
            raise RuntimeError("Importer preview row is not PENDING")
        rows.append(row)
    planned = plan_rows(rows, ctx, territorial, local)
    counts = Counter(p["geocode_status"] + ":" + str(p["geocode_precision"]) for _, p in planned)
    sources = Counter(str(p["geocode_source"] or "NESSUNA") for _, p in planned)
    return {"reviewed_without_db_writes": len(rows), "total_new": len(listings),
            "selection_truncated": len(rows) < len(listings),
            "decision_counts": dict(counts), "source_counts": dict(sources), "google_calls": 0}


def _old_field(name, val):
    if val is None:
        return (name, "is.null")
    return (name, "eq." + str(val))


def _readback_patch_row(base, key, pid):
    fresh = _json_request(base, key, "listings", params=[("pvp_id", "eq." + pid),
                          ("select", "pvp_id,geocode_status,geocode_source,geocode_precision,latitudine,longitudine,geocode_input_hash"),
                          ("limit", "2")])
    if not isinstance(fresh, list) or len(fresh) != 1:
        raise RuntimeError("Read-back after PATCH failed for " + pid)
    return fresh[0]


def _patch_result_matches(check, row, payload):
    for name in ("geocode_status", "geocode_source", "geocode_precision", "geocode_input_hash"):
        expected = row["geocode_input_hash"] if name == "geocode_input_hash" else payload.get(name)
        if check.get(name) != expected:
            return False
    for name in ("latitudine", "longitudine"):
        old, new = fnum(check.get(name)), fnum(payload.get(name))
        if (old is None) != (new is None) or (old is not None and abs(old-new) > 1e-8):
            return False
    return True


def guarded_patch(base, key, row, payload):
    pid = str(row["pvp_id"])
    params = []
    params.extend([("pvp_id", "eq." + pid), ("geocode_status", "eq.PENDING"),
                   ("geocode_input_hash", "eq." + str(row["geocode_input_hash"])),
                   ("latitudine", "is.null"), ("longitudine", "is.null")])
    for name in ("comune", "sigla_provincia", "indirizzo", "pvp_geo_source"):
        params.append(_old_field(name, row.get(name)))
    for name in ("pvp_latitudine", "pvp_longitudine"):
        v = fnum(row.get(name))
        if v is None:
            params.append((name, "is.null"))
        else:
            params.extend([(name, "gte." + format(v-COORD_EPS, ".10f")),
                           (name, "lte." + format(v+COORD_EPS, ".10f"))])

    max_attempts = 4
    for attempt in range(max_attempts):
        try:
            result = _json_request(base, key, "listings", params=params, method="PATCH", payload=payload)
        except Exception as exc:
            if not _is_transient_transport(exc):
                raise
            print(f"[SUPABASE RETRY] PATCH pvp_id={pid} transport error {type(exc).__name__}; checking commit state", flush=True)
            # The server may have committed even if the TCP response was lost.
            try:
                check = _readback_patch_row(base, key, pid)
            except Exception as read_exc:
                if attempt >= max_attempts - 1:
                    raise RuntimeError(f"PATCH transport/read-back failed after {max_attempts} attempts for {pid}") from read_exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if _patch_result_matches(check, row, payload):
                print(f"[SUPABASE RETRY] pvp_id={pid} was already committed; read-back verified", flush=True)
                return True
            # If it is still exactly PENDING with no published coords, retry the guarded PATCH.
            if (str(check.get("geocode_status") or "") == "PENDING" and
                    check.get("latitudine") is None and check.get("longitudine") is None and
                    str(check.get("geocode_input_hash") or "") == str(row.get("geocode_input_hash") or "")):
                if attempt < max_attempts - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
            # Any other state means the guard no longer owns this row.
            return False

        if not isinstance(result, list) or len(result) > 1:
            raise RuntimeError("PATCH response not unique; inspect DB")
        if not result:
            # Guard miss can also mean an earlier lost-response PATCH committed. Verify once.
            check = _readback_patch_row(base, key, pid)
            if _patch_result_matches(check, row, payload):
                return True
            return False
        if str(result[0].get("pvp_id")) != pid:
            raise RuntimeError("PATCH response pvp_id mismatch")
        check = _readback_patch_row(base, key, pid)
        if not _patch_result_matches(check, row, payload):
            raise RuntimeError("Read-back mismatch after PATCH for " + pid)
        return True

    raise RuntimeError("Unreachable PATCH retry state for " + pid)


def run(*, apply=False, expected_pending=None, max_rows=2000, no_anncsu=False):
    # Direct entrypoint is preview-only too; do not write without real-data review.
    if apply:
        raise RuntimeError("v2 criterio ANNCSU: --apply non disponibile nella release di anteprima")
    if max_rows < 1 or max_rows > 5000:
        raise RuntimeError("max_rows must be 1..5000")
    if apply and expected_pending is None:
        raise RuntimeError("--apply requires expected pending count from this pipeline run")
    load_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE"))
    base, key = os.environ["SUPABASE_URL"].rstrip("/"), os.environ["SUPABASE_SERVICE_ROLE"]
    # Preflight local assets BEFORE PATCHing any row; no silent Google fallback.
    ctx, territorial, local = prepare_context(no_anncsu=no_anncsu)
    rows = fetch_pending(base, key, max_rows)
    if apply and expected_pending != len(rows):
        raise RuntimeError("Pending count changed since authorization; no PATCH")
    planned = plan_rows(rows, ctx, territorial, local)
    counts = Counter(p["geocode_status"] + ":" + str(p["geocode_precision"]) for _, p in planned)
    summary = {"version": VERSION, "mode": "APPLY" if apply else "GET_ONLY_PREVIEW",
               "pending_selected": len(rows), "max_rows": max_rows, "anncsu_enabled": bool(local),
               "decision_counts": dict(counts),
               "source_counts": dict(Counter(str(p["geocode_source"] or "NESSUNA") for _, p in planned)),
               "input_digest": input_digest(rows),
               "google_calls": 0, "patched_verified": 0, "skipped_changed": 0}
    if apply:
        for row, payload in planned:
            if guarded_patch(base, key, row, payload):
                summary["patched_verified"] += 1
            else:
                summary["skipped_changed"] += 1
    ensure_runtime_dirs()
    path = REPORTS_DIR / ("geocode_pending_v2_" + datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f") + ".json")
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print("[OUT]", path, flush=True)
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="PENDING geocoder offline; GET-only by default; Google always zero")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--expected-pending", type=int)
    ap.add_argument("--max-rows", type=int, default=2000)
    ap.add_argument("--no-anncsu", action="store_true", help="Explicitly skip ANNCSU; fallback only to municipality")
    args = ap.parse_args(argv)
    run(apply=args.apply, expected_pending=args.expected_pending, max_rows=args.max_rows,
        no_anncsu=args.no_anncsu)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
