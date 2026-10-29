#!/usr/bin/env python3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from aste_paths import ANNCSU_DIR, CACHE_DIR, RAW_DIR, REFERENCE_DIR, REPORTS_DIR

required = [
    REFERENCE_DIR / "Elenco-comuni-italiani.xlsx",
    REFERENCE_DIR / "comuni_cap.csv",
    REFERENCE_DIR / "Limiti01012026.zip",
    ANNCSU_DIR / "STRAD_ITA_20260803.csv",
    ANNCSU_DIR / "INDIR_ITA_20260803.csv",
]
missing = [str(p) for p in required if not p.exists()]
if missing:
    raise SystemExit("MISSING LAYOUT FILES:\n" + "\n".join(missing))
print("OK - cloud-ready layout paths")
print("REFERENCE:", REFERENCE_DIR)
print("ANNCSU   :", ANNCSU_DIR)
print("CACHE    :", CACHE_DIR)
print("RAW      :", RAW_DIR)
print("REPORTS  :", REPORTS_DIR)
