#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy - compact ANNCSU SQLite index v1.

Builds a persistent read-only runtime index from the official ANNCSU massive
STRAD/INDIR CSV or ZIP downloads.  The index deliberately stores only fields
used by the validated Aste Easy geocoder.

No Supabase writes. No Google calls. No ANNCSU API calls.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

VERSION = "anncsu_sqlite_index_v1"
EXPECTED_STRAD = {
    "CODICE_COMUNE", "CODICE_ISTAT", "PROGRESSIVO_NAZIONALE", "ODONIMO", "LOCALITA'",
}
EXPECTED_INDIR = {
    "CODICE_ISTAT", "PROGRESSIVO_NAZIONALE", "CIVICO", "ESPONENTE",
    "COORD_X_COMUNE", "COORD_Y_COMUNE", "METODO",
}

try:
    HERE = Path(__file__).resolve().parent
    SRC_ROOT = HERE.parent
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    from aste_paths import ANNCSU_DIR  # type: ignore
except Exception:
    ANNCSU_DIR = Path.cwd() / "reference" / "anncsu"


def clean(v):
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v).replace("\u00a0", " ")).strip()


def safe_float(v):
    s = clean(v).replace(",", ".")
    if not s:
        return None
    try:
        x = float(s)
    except Exception:
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


def normalize_civic(v):
    import unicodedata
    s = clean(v).lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def _sha256(path: Path, chunk=8 * 1024 * 1024):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _candidate_files():
    roots = [Path.cwd(), Path(ANNCSU_DIR)]
    home = Path.home()
    for p in (home / "Downloads", home / "Scaricati"):
        if p.exists():
            roots.append(p)
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        for pat in ("*.zip", "*.csv"):
            for p in root.glob(pat):
                try:
                    key = str(p.resolve()).lower()
                except Exception:
                    key = str(p).lower()
                if key not in seen:
                    seen.add(key)
                    yield p


def _classify(path: Path):
    name = path.name.upper()
    if path.suffix.lower() == ".csv":
        if "STRAD" in name:
            return "STRAD"
        if "INDIR" in name:
            return "INDIR"
        return None
    if path.suffix.lower() != ".zip":
        return None
    try:
        with zipfile.ZipFile(path) as z:
            names = [Path(x).name.upper() for x in z.namelist() if x.lower().endswith(".csv")]
    except Exception:
        return None
    has_s = any("STRAD" in x for x in names)
    has_i = any("INDIR" in x for x in names)
    if has_s and not has_i:
        return "STRAD"
    if has_i and not has_s:
        return "INDIR"
    return None


def auto_find(kind: str):
    hits = [p for p in _candidate_files() if _classify(p) == kind]
    if not hits:
        return None
    hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0]


def _open_csv_source(path: Path, kind: str):
    """Return (text_stream, closer, source_member). UTF-8-SIG, as official dumps."""
    if path.suffix.lower() == ".zip":
        z = zipfile.ZipFile(path)
        members = [x for x in z.namelist() if x.lower().endswith(".csv") and kind in Path(x).name.upper()]
        if not members:
            z.close()
            raise RuntimeError(f"{path.name}: nessun CSV {kind} trovato nello ZIP")
        # Prefer national file and then largest member.
        members.sort(key=lambda x: (("ITA" not in Path(x).name.upper()), -z.getinfo(x).file_size))
        member = members[0]
        raw = z.open(member, "r")
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        def close():
            try: text.close()
            finally: z.close()
        return text, close, member
    text = path.open("r", encoding="utf-8-sig", newline="")
    return text, text.close, path.name


def _dict_reader(path: Path, kind: str):
    text, closer, member = _open_csv_source(path, kind)
    sample = text.read(131072)
    text.seek(0)
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except Exception:
        delim = ";"
    reader = csv.DictReader(text, delimiter=delim)
    return reader, closer, member, delim


def _validate_fields(fieldnames, expected, label):
    got = {clean(x) for x in (fieldnames or [])}
    missing = sorted(expected - got)
    if missing:
        raise RuntimeError(f"{label}: schema inatteso, colonne mancanti: {missing}")


def _create_schema(con):
    con.executescript("""
    PRAGMA page_size=4096;
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

    CREATE TABLE streets(
      source_row INTEGER PRIMARY KEY,
      codice_comune TEXT NOT NULL,
      codice_istat TEXT NOT NULL,
      progressivo_nazionale TEXT NOT NULL,
      odonimo TEXT NOT NULL,
      localita TEXT NOT NULL,
      dizione_lingua1 TEXT NOT NULL,
      dizione_lingua2 TEXT NOT NULL
    );

    CREATE TABLE crosswalk_counts(
      codice_comune TEXT NOT NULL,
      codice_istat TEXT NOT NULL,
      n INTEGER NOT NULL,
      PRIMARY KEY(codice_comune, codice_istat)
    ) WITHOUT ROWID;

    CREATE TABLE accesses(
      source_row INTEGER PRIMARY KEY,
      codice_istat TEXT NOT NULL,
      progressivo_nazionale TEXT NOT NULL,
      civic_norm TEXT NOT NULL,
      lat REAL,
      lon REAL,
      metodo TEXT NOT NULL
    );

    CREATE TABLE municipality_bbox(
      codice_istat TEXT PRIMARY KEY,
      min_lat REAL NOT NULL,
      max_lat REAL NOT NULL,
      min_lon REAL NOT NULL,
      max_lon REAL NOT NULL,
      count INTEGER NOT NULL
    ) WITHOUT ROWID;
    """)


def _build_strad(con, path: Path):
    reader, closer, member, delim = _dict_reader(path, "STRAD")
    _validate_fields(reader.fieldnames, EXPECTED_STRAD, "STRAD")
    rows = 0
    batch = []
    cross = Counter()
    t0 = time.perf_counter()
    try:
        for row in reader:
            rows += 1
            code = re.sub(r"\D", "", clean(row.get("CODICE_ISTAT")))
            code = code.zfill(6)[-6:] if code else ""
            cat = clean(row.get("CODICE_COMUNE")).upper()
            prog = clean(row.get("PROGRESSIVO_NAZIONALE"))
            odo = clean(row.get("ODONIMO"))
            if not code or not prog or not odo:
                continue
            batch.append((rows, cat, code, prog, odo, clean(row.get("LOCALITA'")),
                          clean(row.get("DIZIONE_LINGUA1")), clean(row.get("DIZIONE_LINGUA2"))))
            if cat:
                cross[(cat, code)] += 1
            if len(batch) >= 20000:
                con.executemany("INSERT INTO streets VALUES(?,?,?,?,?,?,?,?)", batch)
                batch.clear()
            if rows % 500000 == 0:
                print(f"[INDEX STRAD] {rows:,} righe", flush=True)
        if batch:
            con.executemany("INSERT INTO streets VALUES(?,?,?,?,?,?,?,?)", batch)
        con.executemany("INSERT INTO crosswalk_counts VALUES(?,?,?)",
                        [(cat, code, n) for (cat, code), n in cross.items()])
    finally:
        closer()
    return {"rows": rows, "member": member, "delimiter": delim, "seconds": round(time.perf_counter()-t0,3)}


def _build_indir(con, path: Path):
    reader, closer, member, delim = _dict_reader(path, "INDIR")
    _validate_fields(reader.fieldnames, EXPECTED_INDIR, "INDIR")
    rows = valid_coords = 0
    batch = []
    bbox = {}
    t0 = time.perf_counter()
    try:
        for row in reader:
            rows += 1
            code = re.sub(r"\D", "", clean(row.get("CODICE_ISTAT")))
            code = code.zfill(6)[-6:] if code else ""
            prog = clean(row.get("PROGRESSIVO_NAZIONALE"))
            if not code or not prog:
                continue
            civic = normalize_civic(clean(row.get("CIVICO")) + clean(row.get("ESPONENTE")))
            lon = safe_float(row.get("COORD_X_COMUNE"))
            lat = safe_float(row.get("COORD_Y_COMUNE"))
            method = clean(row.get("METODO")).removesuffix(".0")
            batch.append((rows, code, prog, civic, lat, lon, method))
            if lat is not None and lon is not None and 35.0 <= lat <= 47.9 and 6.0 <= lon <= 19.0:
                valid_coords += 1
                b = bbox.get(code)
                if b is None:
                    bbox[code] = [lat, lat, lon, lon, 1]
                else:
                    b[0] = min(b[0], lat); b[1] = max(b[1], lat)
                    b[2] = min(b[2], lon); b[3] = max(b[3], lon); b[4] += 1
            if len(batch) >= 50000:
                con.executemany("INSERT INTO accesses VALUES(?,?,?,?,?,?,?)", batch)
                batch.clear()
            if rows % 1000000 == 0:
                print(f"[INDEX INDIR] {rows:,} righe; coordinate valide {valid_coords:,}", flush=True)
        if batch:
            con.executemany("INSERT INTO accesses VALUES(?,?,?,?,?,?,?)", batch)
        con.executemany("INSERT INTO municipality_bbox VALUES(?,?,?,?,?,?)",
                        [(c, *v) for c, v in bbox.items()])
    finally:
        closer()
    return {"rows": rows, "valid_coords": valid_coords, "municipalities_bbox": len(bbox),
            "member": member, "delimiter": delim, "seconds": round(time.perf_counter()-t0,3)}


def build(strad: Path, indir: Path, out: Path, *, overwrite=False):
    strad = strad.resolve(); indir = indir.resolve(); out = out.resolve()
    if not strad.exists() or not indir.exists():
        raise RuntimeError("File STRAD/INDIR non trovato")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".building")
    if tmp.exists():
        tmp.unlink()
    if out.exists() and not overwrite:
        raise RuntimeError(f"Indice già presente: {out}. Usa --overwrite solo per ricostruirlo.")

    print(f"[INDEX] STRAD: {strad}", flush=True)
    print(f"[INDEX] INDIR: {indir}", flush=True)
    print(f"[INDEX] OUT  : {out}", flush=True)
    t0 = time.perf_counter()
    con = sqlite3.connect(tmp)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA cache_size=-262144")
        _create_schema(con)
        con.execute("BEGIN")
        s = _build_strad(con, strad)
        con.commit()
        con.execute("BEGIN")
        i = _build_indir(con, indir)
        con.commit()
        print("[INDEX] creo indici SQLite...", flush=True)
        con.executescript("""
        CREATE INDEX ix_streets_code_row ON streets(codice_istat, source_row);
        CREATE INDEX ix_streets_prog ON streets(progressivo_nazionale);
        CREATE INDEX ix_streets_code_localita ON streets(codice_istat, localita);
        CREATE INDEX ix_access_prog_row ON accesses(progressivo_nazionale, source_row);
        CREATE INDEX ix_access_code ON accesses(codice_istat);
        """)
        meta = {
            "version": VERSION,
            "built_at": datetime.now().astimezone().isoformat(),
            "strad_path": str(strad), "indir_path": str(indir),
            "strad_size": str(strad.stat().st_size), "indir_size": str(indir.stat().st_size),
            "strad_sha256": _sha256(strad), "indir_sha256": _sha256(indir),
            "strad_member": s["member"], "indir_member": i["member"],
            "strad_rows": str(s["rows"]), "indir_rows": str(i["rows"]),
            "indir_valid_coords": str(i["valid_coords"]),
        }
        con.executemany("INSERT INTO meta(key,value) VALUES(?,?)", meta.items())
        con.commit()
        q = con.execute("PRAGMA quick_check").fetchone()[0]
        if q != "ok":
            raise RuntimeError("SQLite quick_check failed: " + str(q))
        con.execute("ANALYZE")
        con.commit()
    except Exception:
        con.close()
        try: tmp.unlink()
        except Exception: pass
        raise
    con.close()
    if out.exists():
        out.unlink()
    tmp.replace(out)
    result = {
        "version": VERSION, "result": "PASS", "path": str(out),
        "size_bytes": out.stat().st_size, "strad": s, "indir": i,
        "total_seconds": round(time.perf_counter()-t0, 3),
    }
    report = out.with_suffix(".build.json")
    report.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[INDEX] PASS size={out.stat().st_size/1024/1024:.1f} MiB total={result['total_seconds']:.1f}s", flush=True)
    print(f"[INDEX] report: {report}", flush=True)
    return result


def main():
    ap = argparse.ArgumentParser(description="Build compact ANNCSU SQLite runtime index")
    ap.add_argument("--strad", type=Path, help="STRAD national .zip or .csv")
    ap.add_argument("--indir", type=Path, help="INDIR national .zip or .csv")
    ap.add_argument("--out", type=Path, default=Path(ANNCSU_DIR) / "anncsu_runtime.sqlite3")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--auto", action="store_true", help="auto-detect newest STRAD/INDIR in project or Downloads")
    args = ap.parse_args()
    strad = args.strad
    indir = args.indir
    if args.auto or not strad:
        strad = strad or auto_find("STRAD")
    if args.auto or not indir:
        indir = indir or auto_find("INDIR")
    if not strad or not indir:
        raise SystemExit("Non trovo entrambi i file. Usa --strad <zip> --indir <zip> oppure --auto.")
    build(Path(strad), Path(indir), args.out, overwrite=args.overwrite)

if __name__ == "__main__":
    main()
