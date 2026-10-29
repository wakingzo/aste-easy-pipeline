#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy - canonicalizzazione amministrativa locale v11.3.

Fonti:
- Elenco-comuni-italiani.xlsx = autorità per Comune/sigla/regione/codice ISTAT.
- comuni_cap.csv = SOLO validazione diagnostica di CAP già presenti; non inventa CAP mancanti.
- alias legacy = ponte da denominazioni PVP storiche a Comuni ISTAT correnti.

ZERO Google. ZERO DB.
"""
from __future__ import annotations

import csv
import importlib.util
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SRC_ROOT = BASE_DIR.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from aste_paths import REFERENCE_DIR

ISTAT_FILE = REFERENCE_DIR / "Elenco-comuni-italiani.xlsx"
ANN_FILE = BASE_DIR / "anncsu_local_engine_v10.py"
CAP_FILE = REFERENCE_DIR / "comuni_cap.csv"

LEGACY_ALIASES = {
    "veruno": "gattico veruno",
    "tavarnelle val di pesa": "barberino tavarnelle",
    "montebello ionico": "montebello jonico",
    "presicce": "presicce acquarica",
    "tonengo": "moransengo tonengo",
    "vighizzolo d este": "borgo veneto",
    "molvena": "colceresa",
    "vermezzo": "vermezzo con zelo",
    "corigliano calabro": "corigliano rossano",
    "rossano": "corigliano rossano",
    "popoli": "popoli terme",
    "lusiana": "lusiana conco",
    "campospinoso": "campospinoso albaredo",
    "gambugliano": "sovizzo",
    "piadena": "piadena drizzona",
}


def _load(path: Path, name: str):
    if not path.exists():
        raise SystemExit(f"ERRORE: manca {path.name}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"ERRORE: impossibile caricare {path.name}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def clean(v):
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v).replace("\u00a0", " ")).strip()


def cap_norm(v):
    d = re.sub(r"\D", "", clean(v))
    return d.zfill(5) if d else ""


def _strip_tail_once(s, comune="", sigla="", cap=""):
    s = clean(s).strip(" ,")
    c = re.escape(clean(comune)) if clean(comune) else ""
    p = re.escape(clean(sigla)) if clean(sigla) else ""
    z = re.escape(cap_norm(cap)) if cap_norm(cap) else ""
    pats = []
    if z and c and p:
        pats.append(rf",?\s*{z}\s+{c}\s+{p}\s*$")
    if z and c:
        pats.append(rf",?\s*{z}\s+{c}\s*$")
    if c and p:
        pats.append(rf",?\s*{c}\s+{p}\s*$")
    if c:
        pats.append(rf",?\s*{c}\s*$")
    for pat in pats:
        ns = re.sub(pat, "", s, flags=re.I).strip(" ,")
        if ns != s:
            return ns
    return s


def strip_admin_tail(indirizzo, source_comune="", source_sigla="", source_cap="",
                     canonical_comune="", canonical_sigla="", canonical_cap=""):
    s = clean(indirizzo).strip(" ,")
    s = re.sub(r",?\s*Italia\s*$", "", s, flags=re.I).strip(" ,")
    # Prima rimuove la coda così come arrivava dal PVP/DB, anche se errata.
    prev = None
    while prev != s:
        prev = s
        s = _strip_tail_once(s, source_comune, source_sigla, source_cap)
    # Poi elimina anche una eventuale coda già canonica.
    prev = None
    while prev != s:
        prev = s
        s = _strip_tail_once(s, canonical_comune, canonical_sigla, canonical_cap)
    s = clean(s).strip(" ,")
    # Piccole normalizzazioni conservative del core stradale.
    s = re.sub(
        r"(?i)^\s*(via|viale|vicolo|piazza|corso|strada)\s+\1\b",
        r"\1",
        s,
    )
    return clean(s).strip(" ,")


class AdminContext:
    def __init__(self):
        self.ann = _load(ANN_FILE, "aste_ann_admin_v112")
        self.by_name = {}
        self.cap_by_name = {}
        self.aliases = {
            self.ann.norm_name(k): self.ann.norm_name(v)
            for k, v in LEGACY_ALIASES.items()
        }
        for k, v in getattr(self.ann, "MUNICIPALITY_ALIASES", {}).items():
            self.aliases[self.ann.norm_name(k)] = self.ann.norm_name(v)
        self._load_istat()
        self._load_caps()

    def _load_istat(self):
        if not ISTAT_FILE.exists():
            raise SystemExit(f"ERRORE: manca {ISTAT_FILE.name}")
        rows = self.ann.read_istat_rows(ISTAT_FILE)
        idx = {}
        header_seen = False
        loaded = 0

        for row in rows:
            norms = [self.ann.norm_text(x) for x in row]
            if not header_seen:
                if (
                    any("denominazione in italiano" in x for x in norms)
                    and any("codice comune formato alfanumerico" in x for x in norms)
                ):
                    for i, h in enumerate(norms):
                        if "denominazione in italiano" in h:
                            idx["name"] = i
                        elif "sigla automobilistica" in h:
                            idx["prov"] = i
                        elif "codice comune formato alfanumerico" in h:
                            idx["code"] = i
                        elif "codice catastale" in h:
                            idx["catasto"] = i
                        elif "denominazione regione" in h:
                            idx["region"] = i
                    header_seen = True
                    continue
                continue

            if not {"name", "prov", "code"}.issubset(idx):
                raise SystemExit(
                    "ERRORE: colonne Comune/sigla/codice ISTAT non riconosciute "
                    f"in {ISTAT_FILE.name}"
                )

            def cell(key):
                i = idx.get(key)
                return clean(row[i]) if i is not None and i < len(row) else ""

            name = cell("name")
            prov = cell("prov").upper()
            code = re.sub(r"\D", "", cell("code"))
            if not name or not prov or not code:
                continue
            rec = {
                "name": name,
                "prov": prov,
                "code": code.zfill(6)[-6:],
                "catasto": cell("catasto").upper(),
                "region": cell("region"),
            }
            self.by_name.setdefault(self.ann.norm_name(name), []).append(rec)
            loaded += 1

        if not loaded:
            raise SystemExit(f"ERRORE: nessun Comune caricato da {ISTAT_FILE.name}")
        print(f"[ADMIN ISTAT] Comuni caricati: {loaded:,}")

    def _load_caps(self):
        if not CAP_FILE.exists():
            print(f"[ADMIN CAP] {CAP_FILE.name} assente: fallback CAP disattivato")
            return
        with CAP_FILE.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
            first = f.readline()
            f.seek(0)
            delim = ";" if first.count(";") >= first.count(",") else ","
            reader = csv.DictReader(f, delimiter=delim)
            n = 0
            for row in reader:
                comune = clean(row.get("comune"))
                cap = cap_norm(row.get("cap"))
                if not comune or not cap:
                    continue
                rec = {
                    "cap": cap,
                    "sigla": clean(row.get("sigla_provincia")).upper(),
                    "provincia": clean(row.get("provincia")),
                    "regione": clean(row.get("regione")),
                }
                self.cap_by_name.setdefault(self.ann.norm_name(comune), []).append(rec)
                n += 1
        print(f"[ADMIN CAP] righe utili: {n:,} (solo supporto CAP)")

    def resolve(self, row):
        src_name = clean(row.get("comune"))
        src_prov = clean(row.get("sigla_provincia")).upper()
        src_cap = cap_norm(row.get("cap"))
        n = self.ann.norm_name(src_name)

        candidates = self.by_name.get(n, [])
        resolution = "EXACT"
        alias_target = ""
        if not candidates and n in self.aliases:
            alias_target = self.aliases[n]
            candidates = self.by_name.get(alias_target, [])
            resolution = "ALIAS"

        chosen = None
        if len(candidates) == 1:
            chosen = candidates[0]
        elif candidates:
            same = [x for x in candidates if x["prov"] == src_prov]
            if len(same) == 1:
                chosen = same[0]
                resolution = "EXACT_PROVINCE_DISAMBIG"

        if not chosen:
            return {
                "status": "UNRESOLVED",
                "resolution": "UNRESOLVED",
                "source_comune": src_name,
                "source_sigla": src_prov,
                "source_cap": src_cap,
                "canonical_comune": src_name,
                "canonical_sigla": src_prov,
                "canonical_region": "",
                "canonical_code": "",
                "canonical_catasto": "",
                "canonical_cap": src_cap,
                "cap_source": "SOURCE_UNRESOLVED",
                "province_changed": "NO",
                "comune_changed": "NO",
                "cap_changed": "NO",
                "alias_target": alias_target,
            }

        canon_name = chosen["name"]
        canon_prov = chosen["prov"]
        canon_region = chosen.get("region", "")
        src_rel = "MATCH" if (not src_prov or src_prov == canon_prov) else "MISMATCH"

        # CAP: il CSV dell'utente è intenzionalmente semplificato e NON può
        # dimostrare che un Comune abbia un solo CAP. Perciò:
        # - non riempiamo MAI un CAP mancante;
        # - un CAP già presente si conserva se arriva dal nuovo PVP, è scritto
        #   nell'indirizzo, oppure è confermato dal CSV per nome storico/canonico;
        # - altrimenti, se i metadati amministrativi erano corrotti, lo togliamo
        #   dai vincoli Google invece di sostituirlo con un CAP inventato.
        cap_rows_canon = [
            x for x in self.cap_by_name.get(self.ann.norm_name(canon_name), [])
            if not x["sigla"] or x["sigla"] == canon_prov
        ]
        cap_rows_source = [
            x for x in self.cap_by_name.get(self.ann.norm_name(src_name), [])
            if not x["sigla"] or x["sigla"] == canon_prov
        ] if src_name else []

        cap_set = sorted({
            x["cap"]
            for x in (cap_rows_canon + cap_rows_source)
            if x.get("cap")
        })

        basis = clean(row.get("_v11_row_basis")).upper()
        trusted_incoming = basis in {"INCOMING_NEW", "INCOMING_CHANGED"}
        embedded = bool(
            src_cap
            and re.search(rf"(?<!\d){re.escape(src_cap)}(?!\d)", clean(row.get("indirizzo")))
        )
        validated = bool(src_cap and src_cap in cap_set)

        if src_cap and trusted_incoming:
            canon_cap = src_cap
            cap_source = "SOURCE_PVP"
        elif src_cap and embedded:
            canon_cap = src_cap
            cap_source = "SOURCE_EMBEDDED_IN_ADDRESS"
        elif src_cap and validated:
            canon_cap = src_cap
            cap_source = "SOURCE_VALIDATED_BY_CAP_TABLE"
        elif src_cap and src_rel == "MATCH" and resolution.startswith("EXACT"):
            # DB storico con Comune/sigla già coerenti: non abbiamo un segnale
            # sufficiente per cancellare un CAP esistente.
            canon_cap = src_cap
            cap_source = "SOURCE_ADMIN_COHERENT"
        elif src_cap:
            canon_cap = ""
            cap_source = "BLANKED_UNVERIFIED_SOURCE_CAP"
        else:
            canon_cap = ""
            cap_source = "MISSING_NOT_FILLED"

        return {
            "status": "RESOLVED",
            "resolution": resolution,
            "source_comune": src_name,
            "source_sigla": src_prov,
            "source_cap": src_cap,
            "source_province_relation": src_rel,
            "canonical_comune": canon_name,
            "canonical_sigla": canon_prov,
            "canonical_region": canon_region,
            "canonical_code": chosen["code"],
            "canonical_catasto": chosen.get("catasto", ""),
            "canonical_cap": canon_cap,
            "cap_source": cap_source,
            "province_changed": "YES" if src_prov and src_prov != canon_prov else "NO",
            "comune_changed": "YES" if self.ann.norm_name(src_name) != self.ann.norm_name(canon_name) else "NO",
            "cap_changed": "YES" if src_cap != canon_cap else "NO",
            "alias_target": alias_target,
            "cap_candidates": "|".join(cap_set),
            "cap_embedded_in_address": "YES" if embedded else "NO",
            "trusted_incoming_cap": "YES" if trusted_incoming else "NO",
        }

    def canonicalize_row(self, row):
        src = dict(row or {})
        meta = self.resolve(src)
        out = dict(src)

        if meta["status"] == "RESOLVED":
            out["comune"] = meta["canonical_comune"]
            out["sigla_provincia"] = meta["canonical_sigla"]
            out["cap"] = meta["canonical_cap"]

        core = strip_admin_tail(
            src.get("indirizzo"),
            src.get("comune"), src.get("sigla_provincia"), src.get("cap"),
            out.get("comune"), out.get("sigla_provincia"), out.get("cap"),
        )
        out["indirizzo"] = core
        meta["address_core"] = core
        meta["address_tail_changed"] = (
            "YES" if clean(src.get("indirizzo")).strip(" ,") != core else "NO"
        )
        return out, meta
