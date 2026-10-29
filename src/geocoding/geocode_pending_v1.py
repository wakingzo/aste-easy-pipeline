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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from aste_paths import ANNCSU_DIR, REFERENCE_DIR, REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env

VERSION = "geocode_pending_offline_v1"
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
        for stem in ("STRAD_ITA_", "INDIR_ITA_"):
            if not any(ANNCSU_DIR.glob(stem + "*.csv")):
                raise RuntimeError("ANNCSU " + stem + " missing; add local files or opt out with --no-anncsu")
        # Loading local ANNCSU reuses parsers from the Google module, NOT its API.
        # Prevent optional prior usage-offset synchronization in this process.
        for key in ("MONTH_USAGE_OFFSET_GEOCODING", "MONTH_USAGE_OFFSET_AUTOCOMPLETE",
                    "MONTH_USAGE_OFFSET_ADDRESS_VALIDATION"):
            os.environ.pop(key, None)
        local = load_py(HERE / "anncsu_runtime_v11_2.py", "pending_anncsu_v1")
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
                return done(row, lat=civic["lat"], lon=civic["lon"], source="anncsu_civico_corretto",
                            precision="CIVICO", reason=f"PVP distante {dist:.0f} m dall'accesso civico ANNCSU univoco",
                            formatted=row.get("indirizzo"), query="ANNCSU:CIVICO")
            if dist > 250 and is_multi:
                return done(row, status="REVIEW", source="review_multi_bene_civico",
                            precision="NESSUNA", reason="Più beni e PVP discordante da accesso civico ANNCSU; verificare lotto")
        return done(row, lat=la, lon=lo, source="pvp_comune_compatibile",
                    precision="PVP_IN_COMUNE", reason="PVP entro Comune ISTAT; civico/strada non verificati",
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
        return done(row, lat=choice["lat"], lon=choice["lon"],
                    source="anncsu_" + choice["precision"].lower(), precision=choice["precision"],
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
    try:
        with urlopen(req, timeout=120) as response:
            return json.loads(response.read().decode("utf-8") or "[]")
    except HTTPError as exc:
        raise RuntimeError(f"Supabase HTTP {exc.code}: {exc.read().decode('utf-8',errors='replace')[:250]}") from exc


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
    return {"reviewed_without_db_writes": len(rows), "total_new": len(listings),
            "selection_truncated": len(rows) < len(listings),
            "decision_counts": dict(counts), "google_calls": 0}


def _old_field(name, val):
    if val is None:
        return (name, "is.null")
    return (name, "eq." + str(val))


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
    result = _json_request(base, key, "listings", params=params, method="PATCH", payload=payload)
    if not isinstance(result, list) or len(result) > 1:
        raise RuntimeError("PATCH response not unique; inspect DB")
    if not result:
        return False
    if str(result[0].get("pvp_id")) != pid:
        raise RuntimeError("PATCH response pvp_id mismatch")
    fresh = _json_request(base, key, "listings", params=[("pvp_id", "eq." + pid),
                          ("select", "pvp_id,geocode_status,geocode_source,geocode_precision,latitudine,longitudine,geocode_input_hash"),
                          ("limit", "2")])
    if not isinstance(fresh, list) or len(fresh) != 1:
        raise RuntimeError("Read-back after PATCH failed for " + pid)
    check = fresh[0]
    for name in ("geocode_status", "geocode_source", "geocode_precision", "geocode_input_hash"):
        expected = row["geocode_input_hash"] if name == "geocode_input_hash" else payload.get(name)
        if check.get(name) != expected:
            raise RuntimeError("Read-back mismatch " + name + " for " + pid)
    for name in ("latitudine", "longitudine"):
        old, new = fnum(check.get(name)), fnum(payload.get(name))
        if (old is None) != (new is None) or (old is not None and abs(old-new) > 1e-8):
            raise RuntimeError("Coordinate read-back mismatch for " + pid)
    return True


def run(*, apply=False, expected_pending=None, max_rows=2000, no_anncsu=False):
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
               "decision_counts": dict(counts), "input_digest": input_digest(rows),
               "google_calls": 0, "patched_verified": 0, "skipped_changed": 0}
    if apply:
        for row, payload in planned:
            if guarded_patch(base, key, row, payload):
                summary["patched_verified"] += 1
            else:
                summary["skipped_changed"] += 1
    ensure_runtime_dirs()
    path = REPORTS_DIR / ("geocode_pending_v1_" + datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f") + ".json")
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
