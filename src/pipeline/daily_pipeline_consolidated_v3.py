#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy consolidated pipeline v3.12.3.

Default remains preview-only. Production writes require the explicit
--production-apply-v312 mode plus expected new-count and RAW SHA from a preflight.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "importer"))
sys.path.insert(0, str(SRC / "geocoding"))
from aste_paths import IMPORTER_DIR, REPORTS_DIR, ensure_runtime_dirs
import pvp_import_atomic_v1 as importer
import geocode_pending_v2 as geocoder


def _atomic_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _stamp():
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")


def _manifest(plan):
    ensure_runtime_dirs()
    path = Path(REPORTS_DIR) / ("production_manifest_v3_12_" + _stamp() + ".json")
    obj = {
        "version": "production_manifest_v3_12", "status": "AUTHORIZED_NOT_STARTED",
        "created_at": datetime.now().astimezone().isoformat(),
        "raw_sha256": plan["raw_sha256"], "eligible_new": len(plan["records"]),
        "pvp_ids": [str(r[0]["pvp_id"]) for r in plan["records"]],
    }
    _atomic_json(path, obj)
    return path, obj


def _load_manifest(path):
    p = Path(path)
    obj = json.loads(p.read_text(encoding="utf-8-sig"))
    if obj.get("version") != "production_manifest_v3_12" or not isinstance(obj.get("pvp_ids"), list):
        raise RuntimeError("Invalid v3.12 production manifest")
    return p, obj


def main(argv=None):
    ap = argparse.ArgumentParser(description="Aste Easy v3.12: preview by default; explicit guarded production apply")
    ap.add_argument("--build", action="store_true", help="Opt-in to PVP search; requires explicit page/detail caps")
    ap.add_argument("--max-pages", type=int)
    ap.add_argument("--max-details", type=int)
    ap.add_argument("--apply", action="store_true", help="Legacy apply remains blocked; use --production-apply-v312")
    ap.add_argument("--expected-new", type=int)
    ap.add_argument("--expected-raw-sha256")
    ap.add_argument("--geocode-only", action="store_true", help="Legacy offline geocode-only preview")
    ap.add_argument("--max-geocode", type=int, default=2000)
    ap.add_argument("--google-preview", action="store_true")
    ap.add_argument("--google-plan", action="store_true")
    ap.add_argument("--google-policy-v37", action="store_true")
    ap.add_argument("--google-policy-v38", action="store_true")
    ap.add_argument("--google-policy-v39", action="store_true")
    ap.add_argument("--google-production-preflight", action="store_true", help="v3.10 budget dry-run")
    ap.add_argument("--production-preflight-v312", action="store_true", help="FINAL v3.12 preflight: zero Google HTTP, zero DB writes")
    ap.add_argument("--production-apply-v312", action="store_true", help="REAL import + Google production geocode + guarded DB PATCH")
    ap.add_argument("--production-resume-v312", metavar="MANIFEST", help="Resume geocoding for a completed v3.12 import manifest; no importer writes")
    ap.add_argument("--google-live-guard-smoke", action="store_true")
    ap.add_argument("--google-live-max-requests", type=int)
    ap.add_argument("--google-monthly-cap", type=int, default=9000)
    ap.add_argument("--google-monthly-used", type=int)
    ap.add_argument("--google-production-run-cap", type=int, help="Explicit max NEW Google HTTP attempts for this production run")
    ap.add_argument("--google-max-requests", type=int)
    ap.add_argument("--google-max-cases", type=int)
    ap.add_argument("--google-priority-only", choices=("P1_DISCORDANZA_PVP_ANNCSU","P2_SENZA_PVP_CON_CIVICO","P3_PVP_NON_CONFERMATO_CON_CIVICO","P4_SENZA_PVP_TOPONIMO","P5_PVP_NON_CONFERMATO_TOPONIMO"))
    ap.add_argument("--no-anncsu", action="store_true")
    args = ap.parse_args(argv)

    production_modes = sum(bool(x) for x in (args.production_preflight_v312, args.production_apply_v312, args.production_resume_v312))
    if production_modes > 1:
        ap.error("Choose only one v3.12 production mode")
    if args.apply:
        ap.error("Legacy --apply is intentionally blocked. Use --production-apply-v312 with SHA/count guards")
    if args.production_apply_v312:
        if args.build or args.geocode_only or args.no_anncsu:
            ap.error("v3.12 production apply requires existing RAW + ANNCSU; no build/geocode-only/no-anncsu")
        if args.expected_new is None or not args.expected_raw_sha256:
            ap.error("--production-apply-v312 requires --expected-new and --expected-raw-sha256 from final preflight")
        if args.google_production_run_cap is None or args.google_production_run_cap < 1:
            ap.error("--production-apply-v312 requires positive --google-production-run-cap")
    if args.production_resume_v312:
        if args.expected_new is not None or args.expected_raw_sha256 or args.build or args.geocode_only or args.no_anncsu:
            ap.error("resume uses the manifest authorization; do not pass build/expected/no-anncsu")
        if args.google_production_run_cap is None or args.google_production_run_cap < 1:
            ap.error("--production-resume-v312 requires positive --google-production-run-cap")
    if args.production_preflight_v312 and (args.build or args.geocode_only or args.no_anncsu or args.google_production_run_cap is not None):
        ap.error("v3.12 preflight is new-only/offline and does not use a production run cap")
    if args.google_monthly_cap < 1:
        ap.error("--google-monthly-cap must be positive")
    if args.google_monthly_used is not None and args.google_monthly_used < 0:
        ap.error("--google-monthly-used cannot be negative")
    if args.max_geocode < 1:
        ap.error("--max-geocode must be positive")

    diagnostic_modes = sum(bool(x) for x in (args.google_preview, args.google_plan, args.google_policy_v37,
                           args.google_policy_v38, args.google_policy_v39, args.google_production_preflight,
                           args.google_live_guard_smoke, args.production_preflight_v312))
    if diagnostic_modes > 1:
        ap.error("Choose only one preview/diagnostic mode")
    if production_modes and diagnostic_modes and not args.production_preflight_v312:
        ap.error("Production apply/resume cannot be combined with diagnostic Google modes")

    if args.google_live_guard_smoke and (args.google_live_max_requests is None or not 1 <= args.google_live_max_requests <= 50):
        ap.error("--google-live-guard-smoke requires --google-live-max-requests 1..50")
    if not args.google_live_guard_smoke and args.google_live_max_requests is not None:
        ap.error("--google-live-max-requests requires --google-live-guard-smoke")
    if args.google_preview and (args.google_max_requests is None or args.google_max_cases is None or
                                not 1 <= args.google_max_requests <= 500 or not 1 <= args.google_max_cases <= 500):
        ap.error("--google-preview requires explicit --google-max-requests/--google-max-cases 1..500")
    if not args.google_preview and (args.google_max_requests is not None or args.google_max_cases is not None or args.google_priority_only is not None):
        ap.error("Google diagnostic limits require --google-preview")
    if args.build and args.geocode_only:
        ap.error("--build and --geocode-only are incompatible")
    if args.build and (not args.max_pages or not args.max_details or args.max_pages < 1 or args.max_details < 1):
        ap.error("--build requires positive --max-pages and --max-details")
    if (args.max_pages is not None or args.max_details is not None) and not args.build:
        ap.error("page/detail limits require --build")

    if args.build:
        env = dict(os.environ)
        env.update(MAX_PAGES=str(args.max_pages), MAX_DETAILS=str(args.max_details),
                   STOP_ON_OLD_PUBLICATION="1", AUTO_MIN_DATA_PUBBLICAZIONE_FROM_DB="1",
                   MIN_DATA_PUBBLICAZIONE_SAFETY_DAYS="2", STOP_ON_PAST="1")
        build = IMPORTER_DIR / "build_listings_from_pvp_api_RAW_ONLY.py"
        cp = subprocess.run([sys.executable, str(build)], env=env, check=False)
        if cp.returncode:
            raise RuntimeError("PVP build failed; no importer started")

    # Production apply requires the atomic SQL RPC. Probe it BEFORE the expensive
    # ANNCSU/ISTAT scan. The probe intentionally sends an invalid empty listing and
    # must fail inside the SQL function with 22023, so it performs zero writes.
    if args.production_apply_v312:
        importer.assert_rpc_available()

    # Asset readiness before any possible write.
    geocoder.prepare_context(no_anncsu=args.no_anncsu)

    if args.production_resume_v312:
        import google_production_v3_12 as prod
        path, manifest = _load_manifest(args.production_resume_v312)
        readiness = prod.preflight_usage(monthly_cap=args.google_monthly_cap,
                                         run_cap=args.google_production_run_cap,
                                         monthly_used_override=args.google_monthly_used)
        manifest["resume_readiness"] = readiness
        manifest["status"] = "RESUME_GEOCODE_RUNNING"
        _atomic_json(path, manifest)
        try:
            geo = prod.run_for_ids(manifest["pvp_ids"], apply=True, max_rows=args.max_geocode,
                                   monthly_cap=args.google_monthly_cap,
                                   run_cap=args.google_production_run_cap,
                                   monthly_used_override=args.google_monthly_used)
            manifest["geocode_summary"] = geo
            manifest["status"] = "COMPLETED"
            _atomic_json(path, manifest)
        except Exception as exc:
            manifest["status"] = "GEOCODE_INTERRUPTED"
            manifest["last_error_type"] = type(exc).__name__
            _atomic_json(path, manifest)
            raise
        print("[PRODUCTION v3.12] resume completato; manifest:", path, flush=True)
        return 0

    if args.production_apply_v312:
        import google_production_v3_12 as prod
        # Build/validate the exact importer plan once, before any write.
        plan = importer.build_plan()
        if len(plan["records"]) != args.expected_new or plan["raw_sha256"] != args.expected_raw_sha256:
            raise RuntimeError("FINAL AUTHORIZATION FAILED: eligible count or RAW SHA changed; zero writes. Run v3.12 preflight again")
        if len(plan["records"]) > args.max_geocode:
            raise RuntimeError("--max-geocode is below authorized new rows; zero writes")
        readiness = prod.preflight_usage(monthly_cap=args.google_monthly_cap,
                                         run_cap=args.google_production_run_cap,
                                         monthly_used_override=args.google_monthly_used)
        path, manifest = _manifest(plan)
        manifest["google_readiness_before_import"] = readiness
        manifest["status"] = "IMPORT_RUNNING"
        _atomic_json(path, manifest)
        print("[PRODUCTION v3.12] authorization manifest:", path, flush=True)
        try:
            imp = importer.run(apply=True, expected_new=args.expected_new,
                               expected_sha=args.expected_raw_sha256,
                               preview_geo=None, prebuilt_plan=plan)
            manifest["import_summary"] = imp
            manifest["status"] = "IMPORTED_PENDING_GEOCODE"
            _atomic_json(path, manifest)
        except Exception as exc:
            manifest["status"] = "IMPORT_INTERRUPTED"
            manifest["last_error_type"] = type(exc).__name__
            _atomic_json(path, manifest)
            raise
        try:
            geo = prod.run_for_ids(manifest["pvp_ids"], apply=True, max_rows=args.max_geocode,
                                   monthly_cap=args.google_monthly_cap,
                                   run_cap=args.google_production_run_cap,
                                   monthly_used_override=args.google_monthly_used)
            manifest["geocode_summary"] = geo
            manifest["status"] = "COMPLETED"
            _atomic_json(path, manifest)
        except Exception as exc:
            manifest["status"] = "GEOCODE_INTERRUPTED"
            manifest["last_error_type"] = type(exc).__name__
            _atomic_json(path, manifest)
            print("[RESUME] Import completato ma geocoding interrotto. Riprendi con --production-resume-v312", path, flush=True)
            raise
        print("[PRODUCTION v3.12] COMPLETED; manifest:", path, flush=True)
        return 0

    if args.geocode_only:
        geocoder.run(max_rows=args.max_geocode, no_anncsu=args.no_anncsu)
        return 0

    if args.google_live_guard_smoke:
        import google_live_guard_v3_11
        preview_geo = lambda rows: google_live_guard_v3_11.preview_new_rows(
            rows, max_rows=args.max_geocode, monthly_cap=args.google_monthly_cap,
            max_requests=args.google_live_max_requests, monthly_used_override=args.google_monthly_used)
    elif args.production_preflight_v312:
        import google_budget_v3_10
        import google_production_v3_12 as prod
        def preview_geo(rows):
            result = google_budget_v3_10.preview_new_rows(
                rows, max_rows=args.max_geocode, monthly_cap=args.google_monthly_cap,
                monthly_used_override=args.google_monthly_used)
            result["policy_preview"]["production_v312_readiness"] = prod.preflight_usage(
                monthly_cap=args.google_monthly_cap, run_cap=1,
                monthly_used_override=args.google_monthly_used)
            result["policy_preview"]["version"] = "production_preflight_v3_12"
            result["policy_preview"]["mode"] = "ZERO_GOOGLE_HTTP_ZERO_DB_WRITES_FINAL_AUTHORIZATION_PREVIEW"
            return result
    elif args.google_production_preflight:
        import google_budget_v3_10
        preview_geo = lambda rows: google_budget_v3_10.preview_new_rows(
            rows, max_rows=args.max_geocode, monthly_cap=args.google_monthly_cap,
            monthly_used_override=args.google_monthly_used)
    elif args.google_policy_v39:
        import google_policy_v3_9
        preview_geo = lambda rows: google_policy_v3_9.preview_new_rows(rows, max_rows=args.max_geocode)
    elif args.google_policy_v38:
        import google_policy_v3_8
        preview_geo = lambda rows: google_policy_v3_8.preview_new_rows(rows, max_rows=args.max_geocode)
    elif args.google_policy_v37:
        import google_policy_v3_7
        preview_geo = lambda rows: google_policy_v3_7.preview_new_rows(rows, max_rows=args.max_geocode)
    elif args.google_plan:
        import google_policy_plan_v3_2
        preview_geo = lambda rows: google_policy_plan_v3_2.preview_new_rows(rows, max_rows=args.max_geocode)
    elif args.google_preview:
        import google_preview_v3
        preview_geo = lambda rows: google_preview_v3.preview_new_rows(
            rows, max_rows=args.max_geocode, max_requests=args.google_max_requests,
            max_cases=args.google_max_cases, priority_only=args.google_priority_only)
    else:
        preview_geo = lambda rows: geocoder.preview_new_rows(rows, max_rows=args.max_geocode, no_anncsu=args.no_anncsu)

    importer.run(apply=False, preview_geo=preview_geo)
    print("[PREVIEW] zero scritture DB.", flush=True)
    if args.production_preflight_v312:
        print("[NEXT] Se SHA/count e readiness sono invariati, usa --production-apply-v312 con gli stessi valori.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
