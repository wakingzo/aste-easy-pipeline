#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-closed Linux/container readiness probe for Aste Easy v3.18.

ZERO Google calls, ZERO Supabase calls/writes. It validates only local files,
environment variables, imports, writable persistent directories and SQLite indexes.
"""
from __future__ import annotations

import importlib
import json
import os
import platform
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from aste_paths import ANNCSU_DIR, REFERENCE_DIR, REPORTS_DIR, RAW_DIR, ensure_runtime_dirs

VERSION = "container_preflight_v3_18"


def _sqlite_check(path: Path, *, expected_version: str | None = None):
    if not path.is_file():
        raise RuntimeError(f"SQLite missing: {path}")
    con = sqlite3.connect("file:" + str(path.resolve()).replace("\\", "/") + "?mode=ro", uri=True)
    try:
        qc = con.execute("PRAGMA quick_check").fetchone()[0]
        if qc != "ok":
            raise RuntimeError(f"SQLite quick_check failed ({qc}): {path}")
        version = None
        try:
            row = con.execute("SELECT value FROM meta WHERE key='version'").fetchone()
            version = row[0] if row else None
        except sqlite3.Error:
            pass
        if expected_version and version != expected_version:
            raise RuntimeError(f"SQLite version mismatch {path.name}: {version!r} != {expected_version!r}")
        return {"path": str(path), "bytes": path.stat().st_size, "quick_check": qc, "version": version}
    finally:
        con.close()


def _require_file(path: Path):
    if not path.is_file():
        raise RuntimeError(f"Required file missing: {path}")
    return {"path": str(path), "bytes": path.stat().st_size}


def _require_env(name: str):
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"Required environment variable missing: {name}")
    return "SET"


def _writable_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".container_write_probe"
    probe.write_text("ok\n", encoding="utf-8")
    probe.unlink()
    return str(path)


def run_preflight(*, write_report=True, fail=True):
    ensure_runtime_dirs()
    try:
        for mod in ("requests", "openpyxl"):
            importlib.import_module(mod)

        ann = Path(os.environ.get("ANNCSU_SQLITE_INDEX", "") or (Path(ANNCSU_DIR) / "anncsu_runtime.sqlite3")).expanduser().resolve()
        istat_loc = Path(os.environ.get("ISTAT_LOCALITY_INDEX", "") or (ROOT / "data" / "istat" / "istat_localita_2021.sqlite3")).expanduser().resolve()
        state_dir = Path(os.environ.get("GOOGLE_STATE_DIR", "") or (Path(REPORTS_DIR).parent / "cache")).expanduser().resolve()
        g_cache = Path(os.environ.get("GOOGLE_GEOCODE_CACHE_FILE", "") or (state_dir / "google_geocode_cache_v8.json")).expanduser().resolve()
        g_usage = Path(os.environ.get("GOOGLE_API_USAGE_FILE", "") or (state_dir / "google_api_usage_v9_2.json")).expanduser().resolve()

        result = {
            "version": VERSION,
            "status": "PASS",
            "created_at": datetime.now().astimezone().isoformat(),
            "platform": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
            "env": {
                "SUPABASE_URL": _require_env("SUPABASE_URL"),
                "SUPABASE_SERVICE_ROLE": _require_env("SUPABASE_SERVICE_ROLE"),
                "GOOGLE_GEOCODING_API_KEY": _require_env("GOOGLE_GEOCODING_API_KEY"),
            },
            "paths": {
                "root": str(ROOT), "raw": str(RAW_DIR), "reports": str(REPORTS_DIR),
                "reference": str(REFERENCE_DIR), "anncsu": str(ANNCSU_DIR), "google_state": str(state_dir),
            },
            "required_code": {
                "google_core": _require_file(SRC / "geocoding" / "google_geocode_core_v9_3_6.py"),
                "istat_boundary": _require_file(SRC / "geocoding" / "istat_boundary_runtime_v11_6.py"),
            },
            "required_reference": {
                "istat_comuni": _require_file(Path(REFERENCE_DIR) / "Elenco-comuni-italiani.xlsx"),
                "comuni_cap": _require_file(Path(REFERENCE_DIR) / "comuni_cap.csv"),
            },
            "indexes": {
                "anncsu": _sqlite_check(ann, expected_version="anncsu_sqlite_index_v1"),
                "istat_locality": _sqlite_check(istat_loc, expected_version="istat_locality_index_v1"),
            },
            "google_state": {
                "cache": _require_file(g_cache),
                "usage": _require_file(g_usage),
            },
            "writable": {
                "raw": _writable_dir(Path(RAW_DIR)),
                "reports": _writable_dir(Path(REPORTS_DIR)),
                "state": _writable_dir(Path(REPORTS_DIR).parent / "state"),
                "cache": _writable_dir(state_dir),
            },
        }
        if os.environ.get("ASTE_EASY_CONTAINER", "").strip() == "1" and platform.system() != "Linux":
            raise RuntimeError("ASTE_EASY_CONTAINER=1 but platform is not Linux")
    except Exception as exc:
        result = {
            "version": VERSION, "status": "FAIL", "created_at": datetime.now().astimezone().isoformat(),
            "error_type": type(exc).__name__, "error": str(exc),
        }
        if write_report:
            try:
                p = Path(REPORTS_DIR) / ("container_preflight_v3_18_FAILED_" + datetime.now().astimezone().strftime("%Y%m%d-%H%M%S") + ".json")
                p.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                result["report"] = str(p)
            except Exception:
                pass
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        if fail:
            raise
        return result

    if write_report:
        p = Path(REPORTS_DIR) / ("container_preflight_v3_18_" + datetime.now().astimezone().strftime("%Y%m%d-%H%M%S") + ".json")
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["report"] = str(p)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


def main():
    run_preflight(write_report=True, fail=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
