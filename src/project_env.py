#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Environment loader shared by local and future cloud execution.

Environment variables always win. For the current local installation only, missing
values may be filled from tools/start_pipeline.py so the migration does not require
moving secrets immediately. Cloud jobs should set ASTE_ALLOW_LEGACY_CONFIG=0 and
provide secrets through their secret/environment manager.
"""
from __future__ import annotations

import os
import runpy

from aste_paths import LEGACY_CONFIG_FILE


def _clean(v) -> str:
    return "" if v is None else str(v).strip()


def legacy_defaults() -> dict:
    if os.environ.get("ASTE_ALLOW_LEGACY_CONFIG", "1") != "1":
        return {}
    if not LEGACY_CONFIG_FILE.exists():
        return {}
    ns = runpy.run_path(str(LEGACY_CONFIG_FILE))
    fn = ns.get("build_default_env")
    if not callable(fn):
        return {}
    try:
        return dict(fn() or {})
    except Exception:
        return {}


def load_project_env(*, required=()) -> dict:
    defaults = legacy_defaults()
    for k, v in defaults.items():
        if _clean(v):
            os.environ.setdefault(str(k), str(v))

    missing = [k for k in required if not _clean(os.environ.get(k))]
    if missing:
        raise SystemExit(
            "ERRORE: variabili ambiente mancanti: " + ", ".join(missing) +
            ". In locale puoi mantenere temporaneamente start_pipeline.py; "
            "in cloud usa environment/secrets."
        )
    return {k: v for k, v in os.environ.items()}
