#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy daily pipeline: importer + fast, offline territorial guard.

Default DRY RUN: build + importer preview, no DB writes or Google calls.
--apply: importer INSERT -> territorial guard on actual INSERT handoff, NO Google.
--with-google is the ONLY way this entrypoint calls the paid unified geocoder.
--skip-geocode skips BOTH territorial and Google stages, preserving legacy semantics.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from aste_paths import GEOCODING_DIR, HANDOFF_DIR, IMPORTER_DIR, ensure_runtime_dirs
from project_env import load_project_env

BUILD = IMPORTER_DIR / 'build_listings_from_pvp_api_RAW_ONLY.py'
IMPORTER = IMPORTER_DIR / 'pvp_import_incremental_v12_3.py'
TERRITORIAL = GEOCODING_DIR / 'territorial_touched_daily_v1.py'
GEOCODER = GEOCODING_DIR / 'geocode_unified_v11_6_4_live.py'


def run_step(title: str, script: Path, *, args=None, env=None) -> None:
    print('\n' + '=' * 72, flush=True)
    print(title, flush=True)
    print('=' * 72, flush=True)
    if not script.is_file():
        raise SystemExit(f'ERRORE: script mancante: {script}')
    cmd = [sys.executable, str(script)] + [str(x) for x in (args or [])]
    cp = subprocess.run(cmd, env=env if env is not None else os.environ.copy())
    if cp.returncode:
        raise SystemExit(cp.returncode)


def handoff_path() -> Path:
    stamp = datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')
    return HANDOFF_DIR / f'geocode_handoff_daily_{stamp}.jsonl'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='PVP importer + local territorial correction; Google opt-in')
    ap.add_argument('--apply', action='store_true', help='INSERT + offline territorial correction, no Google by default')
    ap.add_argument('--skip-build', action='store_true')
    ap.add_argument('--skip-geocode', action='store_true', help='skip ALL geocoding, retaining only INSERT handoff')
    ap.add_argument('--with-google', action='store_true', help='EXPLICIT OPT IN to paid unified Google geocoder')
    ap.add_argument('--with-anncsu', action='store_true', help='opt in to large local ANNCSU scan for outside pins')
    ap.add_argument('--territorial-include-missing', action='store_true',
                    help='opt in to indicative municipality points for NEW rows without coordinates')
    args = ap.parse_args(argv)
    if args.with_google and args.skip_geocode:
        ap.error('--with-google and --skip-geocode are incompatible')
    if args.skip_geocode and (args.with_anncsu or args.territorial_include_missing):
        ap.error('--skip-geocode is incompatible with territorial stage options')
    if not args.apply and (args.with_google or args.with_anncsu or args.territorial_include_missing):
        ap.error('Geocoding options require --apply; daily dry-run only previews importer')

    ensure_runtime_dirs()
    required = ('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE') + (
        ('GOOGLE_GEOCODING_API_KEY',) if args.apply and args.with_google else ())
    load_project_env(required=required)
    env = os.environ.copy()
    env.setdefault('STOP_ON_OLD_PUBLICATION', '1')
    env.setdefault('AUTO_MIN_DATA_PUBBLICAZIONE_FROM_DB', '1')
    env.setdefault('MIN_DATA_PUBBLICAZIONE_SAFETY_DAYS', '2')
    env.setdefault('STOP_ON_PAST', '1')
    env.setdefault('MAX_PAGES', '0')
    env.setdefault('MAX_DETAILS', '0')

    if not args.skip_build:
        run_step('STEP 1 - BUILD PVP RAW', BUILD, env=env)
    if not args.apply:
        run_step('STEP 2 - IMPORTER v12.3 DRY RUN', IMPORTER, env=env)
        print('\n[DRY RUN] Nessuna scrittura DB e nessuna chiamata Google.', flush=True)
        return 0

    handoff = handoff_path()
    run_step('STEP 2 - IMPORTER v12.3 APPLY', IMPORTER,
             args=['--apply', '--handoff-manifest', str(handoff)], env=env)
    if args.skip_geocode:
        print(f'\n[OK] Import completato; geocoding saltato. Handoff conservato: {handoff}', flush=True)
        return 0

    if not handoff.is_file():
        raise SystemExit('ERRORE: importer non ha scritto il manifest; stop prima del geocoder')
    local_env = dict(env)
    local_env['ASTE_PIPELINE_TERRITORIAL_AUTHORIZED'] = '1'
    local_args = ['--manifest', str(handoff), '--apply', '--pipeline-authorized']
    if args.with_anncsu:
        local_args.append('--with-anncsu')
    if args.territorial_include_missing:
        local_args.append('--include-missing')
    run_step('STEP 3 - TERRITORIAL OFFLINE (SOLO INSERT HANDOFF; GOOGLE ZERO)',
             TERRITORIAL, args=local_args, env=local_env)

    if args.with_google:
        geo_env = dict(env)
        geo_env['UNIFIED_SCOPE'] = 'touched'
        geo_env['UNIFIED_TOUCHED_MANIFEST'] = str(handoff)
        geo_env['UNIFIED_LIMIT'] = '0'
        run_step('STEP 4 - UNIFIED GEOCODER GOOGLE v11.6.4 (OPT-IN)',
                 GEOCODER, args=['--apply'], env=geo_env)
    print(f'\n[OK] DAILY COMPLETATA. Handoff: {handoff}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
