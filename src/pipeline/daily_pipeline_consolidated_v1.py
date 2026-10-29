#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Single entrypoint. By default NO build, NO DB writes, NO Google calls.

One explicit apply performs new-only atomic RPC import and DB-driven offline
geocoding. On interruption, --geocode-only resumes PENDING rows; no manifest.
"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "importer"))
sys.path.insert(0, str(SRC / "geocoding"))
from aste_paths import IMPORTER_DIR
import pvp_import_atomic_v1 as importer
import geocode_pending_v1 as geocoder


def main(argv=None):
    ap = argparse.ArgumentParser(description="Aste Easy: unico importer atomico + geocoder offline PENDING")
    ap.add_argument("--build", action="store_true", help="Opt-in to PVP search; requires explicit page/detail caps")
    ap.add_argument("--max-pages", type=int)
    ap.add_argument("--max-details", type=int)
    ap.add_argument("--apply", action="store_true", help="Real insert and geocoding; needs preview counts/hash")
    ap.add_argument("--expected-new", type=int)
    ap.add_argument("--expected-raw-sha256")
    ap.add_argument("--geocode-only", action="store_true", help="Resume DB PENDING; no RAW import/build")
    ap.add_argument("--max-geocode", type=int, default=2000)
    ap.add_argument("--no-anncsu", action="store_true", help="Explicitly skip ANNCSU and allow ISTAT municipality fallback")
    args = ap.parse_args(argv)
    if args.build and args.geocode_only:
        ap.error("--build and --geocode-only are incompatible")
    if args.build and (not args.max_pages or not args.max_details or args.max_pages < 1 or args.max_details < 1):
        ap.error("--build requires positive --max-pages and --max-details")
    if (args.max_pages is not None or args.max_details is not None) and not args.build:
        ap.error("page/detail limits require --build")
    if args.apply and not args.geocode_only and (args.expected_new is None or not args.expected_raw_sha256):
        ap.error("--apply requires --expected-new and --expected-raw-sha256 from preview")
    if args.apply and args.geocode_only and args.expected_new is not None:
        ap.error("--expected-new not relevant for --geocode-only")

    if args.build:
        env = dict(os.environ)
        env.update(MAX_PAGES=str(args.max_pages), MAX_DETAILS=str(args.max_details),
                   STOP_ON_OLD_PUBLICATION="1", AUTO_MIN_DATA_PUBBLICAZIONE_FROM_DB="1",
                   MIN_DATA_PUBBLICAZIONE_SAFETY_DAYS="2", STOP_ON_PAST="1")
        build = IMPORTER_DIR / "build_listings_from_pvp_api_RAW_ONLY.py"
        cp = subprocess.run([sys.executable, str(build)], env=env, check=False)
        if cp.returncode:
            raise RuntimeError("PVP build failed; no importer started")
    # Preflight geospatial resources BEFORE any insert. No hidden online download.
    # This is also needed on importer-only preview to prove geocoder readiness.
    geocoder.prepare_context(no_anncsu=args.no_anncsu)
    if args.geocode_only:
        # No importer, no raw reading: pending rows are the persistent queue.
        if args.apply:
            from project_env import load_project_env
            load_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE"))
            pending = geocoder.fetch_pending(os.environ["SUPABASE_URL"],
                                             os.environ["SUPABASE_SERVICE_ROLE"], args.max_geocode)
            return 0 if geocoder.run(apply=True, expected_pending=len(pending),
                                     max_rows=args.max_geocode, no_anncsu=args.no_anncsu) else 1
        geocoder.run(max_rows=args.max_geocode, no_anncsu=args.no_anncsu)
        return 0
    preview_geo = None if args.apply else (lambda rows: geocoder.preview_new_rows(
        rows, max_rows=args.max_geocode, no_anncsu=args.no_anncsu))
    imp = importer.run(apply=args.apply, expected_new=args.expected_new,
                       expected_sha=args.expected_raw_sha256, preview_geo=preview_geo)
    if args.apply:
        # Pending rows in DB, not a handoff manifest. Includes retries from a prior interrupted run.
        from project_env import load_project_env
        load_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE"))
        pending = geocoder.fetch_pending(os.environ["SUPABASE_URL"],
                                         os.environ["SUPABASE_SERVICE_ROLE"], args.max_geocode)
        geocoder.run(apply=True, expected_pending=len(pending),
                     max_rows=args.max_geocode, no_anncsu=args.no_anncsu)
    else:
        print("[PREVIEW] Importer new-only + decisioni geografiche simulate sui nuovi record: zero scritture, Google=0.", flush=True)
        print("[NEXT] After installing the RPC SQL, use --apply with expected-new and raw SHA from this preview.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
