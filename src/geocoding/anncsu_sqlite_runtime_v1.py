#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only query adapter for anncsu_runtime.sqlite3.

The functions intentionally reproduce the data structures returned by the
legacy full-CSV scanners, so validated policy code remains unchanged.
"""
from __future__ import annotations
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


def open_index(path: Path):
    uri = "file:" + str(path.resolve()).replace("\\", "/") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def verify_index(con):
    row = con.execute("SELECT value FROM meta WHERE key='version'").fetchone()
    if not row or row[0] != "anncsu_sqlite_index_v1":
        raise RuntimeError("ANNCSU SQLite index version unsupported")
    return {r[0]: r[1] for r in con.execute("SELECT key,value FROM meta")}


def load_crosswalk(con):
    counts = defaultdict(Counter)
    for cat, code, n in con.execute("SELECT codice_comune,codice_istat,n FROM crosswalk_counts"):
        counts[cat][code] = int(n)
    out = {}
    for cat, ctr in counts.items():
        ranked = ctr.most_common()
        total = sum(ctr.values())
        best_code, best_n = ranked[0]
        share = best_n / total if total else 0.0
        status = "UNIQUE" if len(ranked) == 1 else ("DOMINANT" if share >= .90 else "AMBIGUOUS")
        out[cat] = {"code": best_code, "status": status, "share": round(share,6),
                    "codes": "|".join(f"{c}:{n}" for c,n in ranked[:5])}
    return out


def street_rows_by_code(con, codes):
    out = defaultdict(list)
    for code in sorted(set(codes)):
        for r in con.execute(
            "SELECT source_row,codice_istat,progressivo_nazionale,odonimo,localita,dizione_lingua1,dizione_lingua2 "
            "FROM streets WHERE codice_istat=? ORDER BY source_row", (code,)
        ):
            out[code].append({
                "CODICE_ISTAT": r["codice_istat"], "PROGRESSIVO_NAZIONALE": r["progressivo_nazionale"],
                "ODONIMO": r["odonimo"], "LOCALITA'": r["localita"],
                "DIZIONE_LINGUA1": r["dizione_lingua1"], "DIZIONE_LINGUA2": r["dizione_lingua2"],
            })
    return out


def scan_streets(con, records, resolved, ann, topn=6):
    by_code, token_index, source_info = ann.prepare_record_indexes(records, resolved)
    rows_by_code = street_rows_by_code(con, by_code.keys())
    top = defaultdict(list)
    for code, rows in rows_by_code.items():
        for row in rows:
            prog = str(row.get("PROGRESSIVO_NAZIONALE") or "").strip()
            odo = str(row.get("ODONIMO") or "").strip()
            if not prog or not odo:
                continue
            variants = ann.street_variants(row)
            street_toks = set()
            for v in variants:
                street_toks |= set(ann.tokens(v))
            candidate_idx = set()
            for tok in street_toks:
                candidate_idx |= token_index[code].get(tok, set())
            for idx in candidate_idx:
                rec = records[idx]
                pid = str(rec.get("pvp_id"))
                src_norm, src_toks = source_info[pid]
                best_v = max(variants, key=lambda v: ann.street_score(src_norm, src_toks, v))
                sc = ann.street_score(src_norm, src_toks, best_v)
                if sc < .35:
                    continue
                arr = top[pid]
                arr.append((sc, prog, odo, str(row.get("LOCALITA'") or "").strip()))
                arr.sort(key=lambda x: x[0], reverse=True)
                dedup=[]; seen=set()
                for item in arr:
                    if item[1] in seen: continue
                    seen.add(item[1]); dedup.append(item)
                    if len(dedup) >= topn: break
                top[pid] = dedup
    return top, rows_by_code


def load_accesses(con, wanted_progs, ann):
    out = defaultdict(list)
    progs = sorted(set(str(x) for x in wanted_progs if str(x)))
    for i in range(0, len(progs), 500):
        chunk = progs[i:i+500]
        q = ",".join("?" for _ in chunk)
        sql = ("SELECT source_row,progressivo_nazionale,civic_norm,lat,lon,metodo FROM accesses "
               f"WHERE progressivo_nazionale IN ({q}) ORDER BY source_row")
        for r in con.execute(sql, chunk):
            out[r["progressivo_nazionale"]].append((r["civic_norm"], r["lat"], r["lon"], r["metodo"]))
    return out


def load_bboxes(con, codes):
    out={}
    codes=sorted(set(codes))
    for i in range(0,len(codes),500):
        chunk=codes[i:i+500]
        q=",".join("?" for _ in chunk)
        for r in con.execute(
            f"SELECT codice_istat,min_lat,max_lat,min_lon,max_lon,count FROM municipality_bbox WHERE codice_istat IN ({q})",
            chunk):
            out[r["codice_istat"]] = {"min_lat":r["min_lat"],"max_lat":r["max_lat"],
                                      "min_lon":r["min_lon"],"max_lon":r["max_lon"],"count":r["count"]}
    return out
