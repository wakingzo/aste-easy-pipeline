#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Central path layout for Aste Easy.

Local default layout (PROJECT_ROOT = tools/):
  src/                  code
  data/reference/       ISTAT/CAP reference data
  data/anncsu/          large ANNCSU datasets
  runtime/cache/        persistent caches/counters
  runtime/raw/          PVP raw/intermediate JSONL
  runtime/handoff/      touched manifests
  runtime/reports/      CSV/JSON run reports

Every root can be overridden through environment variables for cloud/container use.
"""
from __future__ import annotations

import os
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent


def _resolve(raw: str | None, default: Path, *, relative_to: Path | None = None) -> Path:
    if raw and str(raw).strip():
        p = Path(str(raw).strip()).expanduser()
        if not p.is_absolute():
            p = (relative_to or default.parent) / p
        return p.resolve()
    return default.resolve()


PROJECT_ROOT = _resolve(os.environ.get("ASTE_ROOT"), SRC_ROOT.parent, relative_to=SRC_ROOT.parent)
DATA_DIR = _resolve(os.environ.get("ASTE_DATA_DIR"), PROJECT_ROOT / "data", relative_to=PROJECT_ROOT)
REFERENCE_DIR = _resolve(os.environ.get("ASTE_REFERENCE_DIR"), DATA_DIR / "reference", relative_to=PROJECT_ROOT)
ANNCSU_DIR = _resolve(os.environ.get("ASTE_ANNCSU_DIR"), DATA_DIR / "anncsu", relative_to=PROJECT_ROOT)
RUNTIME_DIR = _resolve(os.environ.get("ASTE_RUNTIME_DIR"), PROJECT_ROOT / "runtime", relative_to=PROJECT_ROOT)
CACHE_DIR = _resolve(os.environ.get("ASTE_CACHE_DIR"), RUNTIME_DIR / "cache", relative_to=PROJECT_ROOT)
RAW_DIR = _resolve(os.environ.get("ASTE_RAW_DIR"), RUNTIME_DIR / "raw", relative_to=PROJECT_ROOT)
HANDOFF_DIR = _resolve(os.environ.get("ASTE_HANDOFF_DIR"), RUNTIME_DIR / "handoff", relative_to=PROJECT_ROOT)
REPORTS_DIR = _resolve(os.environ.get("ASTE_REPORTS_DIR"), RUNTIME_DIR / "reports", relative_to=PROJECT_ROOT)
LEGACY_CONFIG_FILE = _resolve(os.environ.get("ASTE_LEGACY_CONFIG_FILE"), PROJECT_ROOT / "start_pipeline.py", relative_to=PROJECT_ROOT)

GEOCODING_DIR = SRC_ROOT / "geocoding"
IMPORTER_DIR = SRC_ROOT / "importer"
PIPELINE_DIR = SRC_ROOT / "pipeline"


def ensure_runtime_dirs() -> None:
    for p in (RUNTIME_DIR, CACHE_DIR, RAW_DIR, HANDOFF_DIR, REPORTS_DIR):
        p.mkdir(parents=True, exist_ok=True)


def env_path(name: str, default: Path, *, relative_to: Path | None = None) -> Path:
    """Resolve an optional env path, preserving absolute paths and making relative paths deterministic."""
    return _resolve(os.environ.get(name), default, relative_to=relative_to or PROJECT_ROOT)
