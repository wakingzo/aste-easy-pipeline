#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy: general, read-only territorial fallback, using current DB + local ISTAT/ANNCSU.

No Google API, no DB PATCH, no internet data retrieval. The only network requests are
Supabase GETs for current listings. This is a *proposal* generator, not a repair.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from aste_paths import REFERENCE_DIR, REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env

ADMIN_TAIL = re.compile(r"(?<!\d)(\d{5})\s+([A-Za-zÀ-ÿ'’\- ]{3,55}?)\s+([A-Z]{2})\b")
OUTPUT_COLUMNS = (
    "pvp_id", "decision", "precision", "reason", "comune", "sigla_provincia",
    "indirizzo", "istat_code", "current_boundary", "original_lat", "original_lon",
    "proposed_lat", "proposed_lon", "alternative_comune", "anncsu_odonimo",
    "anncsu_civico", "anncsu_street_score", "anncsu_strict_street_similarity",
    "old_geocode_source", "old_geocode_location_type", "indicator_label",
)


def mod(path: Path, name: str):
    if not path.is_file():
        raise RuntimeError(f"Modulo necessario mancante: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    obj = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(obj)
    return obj


def flt(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def alternative_comune_for_pin(row, ctx, lat, lon):
    """Protect PVP pin if the address explicitly has a CAP+other comune+sigla.

    Example: '09024 Nuraminis CA, Muravera ...' with marker inside Nuraminis.
    Only a municipality resolved by ISTAT *and* containing the actual pin qualifies.
    Does not infer multi-lot status from proximity or Google.
    """
    if lat is None or lon is None:
        return ""
    address = str(row.get("indirizzo") or "")
    current_code = str(ctx.resolve_admin(row).get("canonical_code") or "")
    seen = set()
    for cap, commune, province in ADMIN_TAIL.findall(address):
        alt_row = {"comune": commune.strip(), "sigla_provincia": province, "cap": cap}
        meta = ctx.resolve_admin(alt_row)
        code = str(meta.get("canonical_code") or "")
        if meta.get("status") != "RESOLVED" or not code or code == current_code or code in seen:
            continue
        seen.add(code)
        t = ctx.test(alt_row, lat=lat, lon=lon, admin_meta=meta)
        if t.get("position") == "INSIDE":
            return str(meta.get("canonical_comune") or commune.strip())
    return ""


def local_option(row, ev, local, ctx, meta):
    """Reuse strict ANNCSU guards, plus LOCALITA-only indicative map fallback."""
    if not ev:
        return None
    candidates = []
    if local.direct_civic_for_missing(ev):
        candidates.append(("CIVICO", "anncsu_lat", "anncsu_lon", "exact_unique_anncsu", "anncsu_civico"))
    street_ok, street_reason = local.street_for_missing(row, ev)
    if street_ok:
        candidates.append(("STRADA", "rep_lat", "rep_lon", street_reason, "anncsu_strada"))
    locality_ok, locality_reason = local.locality_for_missing(row, ev, allow_fallback=False)
    if locality_ok:
        candidates.append(("LOCALITA", "runtime_locality_lat", "runtime_locality_lon",
                           locality_reason, "anncsu_localita"))
    elif hasattr(local, "locality_for_indicative_map"):
        locality_ok, locality_reason = local.locality_for_indicative_map(row, ev)
        if locality_ok:
            candidates.append(("LOCALITA", "runtime_locality_lat", "runtime_locality_lon",
                               locality_reason, "anncsu_localita_indicativa"))
    for precision, lat_key, lon_key, why, source in candidates:
        rep = ev.get("street_representative") or {}
        la = flt(rep.get("lat") if lat_key == "rep_lat" else ev.get(lat_key))
        lo = flt(rep.get("lon") if lon_key == "rep_lon" else ev.get(lon_key))
        if la is None or lo is None:
            continue
        if ctx.test(row, lat=la, lon=lo, admin_meta=meta).get("position") != "INSIDE":
            continue
        return {"precision": precision, "lat": la, "lon": lo, "reason": why, "source": source}
    return None


def evaluate(row, ctx, local, ev=None, rep_cache=None, *, include_missing=False):
    """Pure decision logic (ctx/local injected for fast deterministic tests)."""
    rep_cache = rep_cache if rep_cache is not None else {}
    lat = flt(row.get("latitudine"))
    lon = flt(row.get("longitudine"))
    meta = ctx.resolve_admin(row)
    code = str(meta.get("canonical_code") or "")
    result = {"pvp_id": str(row.get("pvp_id") or ""),
              "decision": "", "precision": "", "reason": "", "comune": str(row.get("comune") or ""),
              "sigla_provincia": str(row.get("sigla_provincia") or ""),
              "indirizzo": str(row.get("indirizzo") or ""), "istat_code": code,
              "current_boundary": "", "original_lat": lat if lat is not None else "",
              "original_lon": lon if lon is not None else "", "proposed_lat": "", "proposed_lon": "",
              "alternative_comune": "", "anncsu_odonimo": str((ev or {}).get("anncsu_odonimo") or ""),
              "anncsu_civico": str((ev or {}).get("anncsu_civic") or ""),
              "anncsu_street_score": (ev or {}).get("street_score", ""),
              "anncsu_strict_street_similarity": (ev or {}).get("strict_street_similarity", ""),
              "old_geocode_source": str(row.get("geocode_source") or ""),
              "old_geocode_location_type": str(row.get("geocode_location_type") or ""),
              "indicator_label": ""}
    if meta.get("status") != "RESOLVED" or not code:
        result.update(decision="REVIEW_ADMIN", reason="ISTAT municipality not resolved")
        return result
    old = ctx.test(row, lat=lat, lon=lon, admin_meta=meta)
    pos = str(old.get("position") or "")
    result["current_boundary"] = pos
    if pos in ("INSIDE", "BOUNDARY"):
        result.update(decision="KEEP_PVP_IN_COMUNE", precision="PVP", reason="existing pin inside canonical municipality")
        return result
    if pos == "NO_COORDS" and not include_missing:
        result.update(decision="NOT_TARGETED_MISSING", reason="use --include-missing for missing coordinates")
        return result
    if pos not in ("OUTSIDE", "NO_COORDS"):
        result.update(decision="REVIEW_BOUNDARY", reason=f"cannot prove municipality containment: {pos}")
        return result
    alt = alternative_comune_for_pin(row, ctx, lat, lon) if pos == "OUTSIDE" else ""
    if alt:
        result.update(decision="KEEP_ALTERNATIVE_COMUNE_REVIEW", precision="PVP",
                      reason="PVP inside other municipality explicitly given in address", alternative_comune=alt,
                      indicator_label="Posizione indicativa: verificare avviso e perizia")
        return result
    choice = local_option(row, ev, local, ctx, meta)
    if choice:
        result.update(decision="PROPOSE_ANNCSU_" + choice["precision"], precision=choice["precision"],
                      reason=choice["reason"], proposed_lat=choice["lat"], proposed_lon=choice["lon"],
                      indicator_label="Posizione indicativa" if choice["precision"] != "CIVICO" else "")
        return result
    if code not in rep_cache:
        rep_cache[code] = ctx.representative_point(row, admin_meta=meta)
    rep = rep_cache[code]
    if not rep.get("ok"):
        result.update(decision="REVIEW_NO_SAFE_REP", reason="no verified internal representative for municipality")
        return result
    la, lo = flt(rep.get("lat")), flt(rep.get("lon"))
    if la is None or lo is None or ctx.test(row, lat=la, lon=lo, admin_meta=meta).get("position") != "INSIDE":
        result.update(decision="REVIEW_NO_SAFE_REP", reason="representative failed strict recheck")
        return result
    result.update(decision="PROPOSE_ISTAT_COMUNE_INDICATIVE", precision="COMUNE",
                  reason="no reliable local civic/street/locality; point strictly inside ISTAT polygon",
                  proposed_lat=la, proposed_lon=lo,
                  indicator_label="Posizione indicativa nel Comune: verificare avviso e perizia")
    return result


def main():
    parser = argparse.ArgumentParser(description="GET Supabase + ISTAT/ANNCSU locali; zero Google e zero PATCH")
    parser.add_argument("--scope", choices=("all", "touched"), default="all")
    parser.add_argument("--manifest", type=Path, help="required for --scope touched; manifest of pvp_id")
    parser.add_argument("--pvp-id", help="inspect just this pvp_id (for focused smoke test)")
    parser.add_argument("--include-missing", action="store_true", help="also propose fallback for rows without coordinates")
    parser.add_argument("--no-anncsu", action="store_true", help="skip local ANNCSU scan; municipality fallback only")
    args = parser.parse_args()
    if args.scope == "touched" and not args.manifest and not args.pvp_id:
        parser.error("--scope touched requires --manifest (or --pvp-id)")
    if not (REFERENCE_DIR / "Elenco-comuni-italiani.xlsx").is_file():
        raise SystemExit("ERRORE: ISTAT municipality workbook missing locally; no download")
    # Important: boundary.ensure_boundary_files() normally downloads if missing.
    # Abort before calling it, to guarantee this command has no web requests.
    load_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE"))
    boundary = mod(HERE / "istat_boundary_runtime_v11_6.py", "offline_istat_boundaries_v1")
    if not boundary.ISTAT_ZIP.is_file():
        raise SystemExit("ERRORE: ISTAT boundary ZIP missing locally; no download")
    ctx = boundary.get_context()
    # Importing the existing Google parser also instantiates its usage tracker.
    # Suppress optional offset synchronization in THIS process: this offline
    # audit must not rewrite the shared Google usage file either.
    for name in ("MONTH_USAGE_OFFSET_GEOCODING", "MONTH_USAGE_OFFSET_AUTOCOMPLETE",
                 "MONTH_USAGE_OFFSET_ADDRESS_VALIDATION"):
        os.environ.pop(name, None)
    planner = mod(HERE / "geocode_unified_v11_6_4_plan.py", "offline_source_planner_v1")
    local = planner.local
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE"]
    if args.pvp_id:
        rows = list(planner.fetch_by_ids_map(base, key, [str(args.pvp_id)]).values())
        if len(rows) != 1:
            raise SystemExit("ERRORE: pvp_id not found uniquely in Supabase")
    elif args.scope == "touched":
        if not args.manifest.is_file():
            raise SystemExit("ERRORE: manifest not found")
        ids = []
        for ln in args.manifest.read_text(encoding="utf-8-sig").splitlines():
            if not ln.strip():
                continue
            data = json.loads(ln)
            pid = str(data.get("pvp_id") or "")
            if not pid or not pid.isdigit():
                raise SystemExit("ERRORE: manifest contains invalid pvp_id")
            ids.append(pid)
        if len(set(ids)) != len(ids):
            raise SystemExit("ERRORE: duplicate IDs in manifest")
        rows = list(planner.fetch_by_ids_map(base, key, ids).values())
        if len(rows) != len(ids):
            raise SystemExit("ERRORE: missing DB IDs from manifest")
    else:
        rows = planner.assert_unique_pvp_rows(planner.fetch_all(base, key), "offline territorial all")
    rows.sort(key=lambda x: str(x.get("pvp_id")))
    target = []
    positions = Counter()
    for row in rows:
        status = ctx.test(row)
        pos = status.get("position", "UNKNOWN")
        positions[pos] += 1
        if pos == "OUTSIDE" or (args.include_missing and pos == "NO_COORDS"):
            target.append(row)
    print(f"[GET ONLY] records={len(rows)} ISTAT={dict(positions)} candidate_rows={len(target)}")
    evidence = {}
    if target and not args.no_anncsu:
        # One shared STRAD pass + one shared INDIR pass for just the problematic rows.
        ev, _ = local.build_local_evidence(target)
        evidence = {str(x.get("pvp_id")): x for x in ev}
    rep_cache = {}
    results = [evaluate(row, ctx, local, evidence.get(str(row.get("pvp_id"))), rep_cache,
                        include_missing=args.include_missing) for row in target]
    counts = Counter(r["decision"] for r in results)
    ensure_runtime_dirs()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    out_csv = REPORTS_DIR / f"geocode_territorial_offline_v1_{stamp}.csv"
    out_json = REPORTS_DIR / f"geocode_territorial_offline_v1_summary_{stamp}.json"
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(results)
    summary = {
        "mode": "SUPABASE_GET_ONLY_ISTAT_ANNCSU_LOCAL_ZERO_GOOGLE_ZERO_PATCH",
        "scope": "one_id" if args.pvp_id else args.scope,
        "db_rows_read": len(rows), "boundary_counts": dict(positions),
        "target_rows": len(target), "decision_counts": dict(counts),
        "alternative_municipality_held": counts["KEEP_ALTERNATIVE_COMUNE_REVIEW"],
        "google_api_calls": 0, "supabase_patches": 0,
        "network": "Supabase GET only; ISTAT and ANNCSU from local files",
        "csv": out_csv.name, "json": out_json.name,
        "notes": "Proposals ONLY. Municipality fallback is indicative, not a property or SS km position. No apply command in this script.",
    }
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[DECISIONS]", dict(counts))
    print("[OUT]", out_csv)
    print("[OUT]", out_json)
    print("[DONE] 0 Google; 0 PATCH")


if __name__ == "__main__":
    main()
