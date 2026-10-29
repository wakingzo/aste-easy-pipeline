#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prepare persistent mutable state before the first Docker build/run.

Safe local operation only: copies legacy Google cache/usage into runtime/cache
IF the destination does not already exist. Never overwrites a newer state file,
never calls Google, never touches Supabase.
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from aste_paths import REPORTS_DIR, ANNCSU_DIR, ensure_runtime_dirs

VERSION = "container_prepare_state_v3_18"
ROOT = SRC.parent
GEO = SRC / "geocoding"
RUNTIME = Path(REPORTS_DIR).parent
CACHE = RUNTIME / "cache"


def _copy_once(src: Path, dst: Path):
    if dst.exists():
        return {"source": str(src), "dest": str(dst), "action": "KEEP_EXISTING", "bytes": dst.stat().st_size}
    if not src.is_file():
        raise RuntimeError(f"Required legacy state missing: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {"source": str(src), "dest": str(dst), "action": "COPIED", "bytes": dst.stat().st_size}


def main():
    ensure_runtime_dirs()
    CACHE.mkdir(parents=True, exist_ok=True)
    result = {
        "version": VERSION,
        "created_at": datetime.now().astimezone().isoformat(),
        "google_cache": _copy_once(GEO / "google_geocode_cache_v8.json", CACHE / "google_geocode_cache_v8.json"),
        "google_usage": _copy_once(GEO / "google_api_usage_v9_2.json", CACHE / "google_api_usage_v9_2.json"),
        "anncsu_index": str(Path(ANNCSU_DIR) / "anncsu_runtime.sqlite3"),
        "istat_locality_index": str(ROOT / "data" / "istat" / "istat_localita_2021.sqlite3"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
