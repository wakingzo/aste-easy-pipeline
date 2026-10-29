#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aste Easy - ANNCSU API-FREE audit v2.4

Controllo indipendente delle coordinate esistenti usando:
- Supabase SOLO in lettura (GET)
- ANNCSU locale (STRAD + INDIR)
- elenco Comuni ISTAT locale

NON chiama API Google.
NON modifica Supabase.
NON applica correzioni.

Avvio:
    python\\python.exe audit_db_apifree_anncsu_v2.py

Default APIFREE_AUDIT_SCOPE=legacy:
- source vuoto con stato PVP_*
- pvp_verified
- google_corrected_pvp
- vecchio source=google

Sono esclusi dal default i risultati recenti local-first
anncsu_missing_*, google_missing_* e quelli gia' verificati ANNCSU.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import math
import os
import re
import runpy
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
SRC_ROOT = BASE_DIR.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from aste_paths import ANNCSU_DIR, REFERENCE_DIR, REPORTS_DIR, ensure_runtime_dirs, env_path
from project_env import load_project_env as load_common_project_env

ANN_ENGINE_FILE = BASE_DIR / "anncsu_local_engine_v10.py"

PAGE_SIZE = 1000
TIMEOUT = 60
TOP_STREETS = max(3, int(os.environ.get("APIFREE_TOP_STREETS", "6") or "6"))
AUDIT_SCOPE = os.environ.get("APIFREE_AUDIT_SCOPE", "legacy").strip().lower()

ANNCSU_STRAD_FILE = os.environ.get("ANNCSU_STRAD_FILE", "").strip()
ANNCSU_INDIR_FILE = os.environ.get("ANNCSU_INDIR_FILE", "").strip()
ISTAT_COMUNI_FILE = os.environ.get(
    "ISTAT_COMUNI_FILE", str(REFERENCE_DIR / "Elenco-comuni-italiani.xlsx")
).strip()

SELECT_FIELDS = ",".join([
    "pvp_id", "indirizzo", "comune", "sigla_provincia", "cap",
    "latitudine", "longitudine", "maps_link",
    "geocode_location_type", "geocode_formatted_address",
    "geocode_source", "geocode_query_used",
])

KM_RE = re.compile(
    r"\b(?:km|chilometr(?:o|ica|ico)?)\.?\s*\d+(?:\s*[,\.]\s*\d+)?",
    re.I,
)
NON_CIVIC_METADATA_PATTERNS = [
    re.compile(r"\blotto\s*(?:n(?:\.|umero)?\s*)?\d+[a-z]?\b", re.I),
    re.compile(
        r"\bpiano\s*(?:s?\d+|t(?:erra)?|primo|secondo|terzo|quarto|quinto|"
        r"seminterrato|rialzato)\b", re.I
    ),
    re.compile(r"\b(?:int\.?|interno)\s*\d+[a-z]?\b", re.I),
    re.compile(r"\bscala\s*[a-z0-9]+\b", re.I),
    re.compile(r"\b(?:sub\.?|subalterno)\s*\d+[a-z]?\b", re.I),
    re.compile(r"\b(?:edificio|palazzina)\s*[a-z0-9]+(?:[/\-]\d+)?\b", re.I),
]


def clean(v):
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v).replace("\u00a0", " ")).strip()


def safe_float(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def has_valid_coords(lat, lon):
    lat, lon = safe_float(lat), safe_float(lon)
    return (
        lat is not None and lon is not None
        and -90 <= lat <= 90 and -180 <= lon <= 180
        and not (lat == 0 and lon == 0)
    )


def normalize_text(v):
    s = clean(v).lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_locality_tail(indirizzo, comune, sigla, cap):
    s = clean(indirizzo).strip(" ,")
    s = re.sub(r",?\s*Italia\s*$", "", s, flags=re.I).strip(" ,")
    c = re.escape(clean(comune)) if clean(comune) else ""
    p = re.escape(clean(sigla)) if clean(sigla) else ""
    z = re.escape(clean(cap)) if clean(cap) else ""
    patterns = []
    if z and c and p:
        patterns.append(rf",?\s*{z}\s+{c}\s+{p}\s*$")
    if z and c:
        patterns.append(rf",?\s*{z}\s+{c}\s*$")
    if c and p:
        patterns.append(rf",?\s*{c}\s+{p}\s*$")
    for pat in patterns:
        s = re.sub(pat, "", s, flags=re.I).strip(" ,")
    return s


def strip_non_civic_metadata(text):
    s = clean(text)
    for pat in NON_CIVIC_METADATA_PATTERNS:
        s = pat.sub(" ", s)
    return clean(s)


def normalize_house_number(v):
    return normalize_text(v).replace(" ", "") or None


def expected_house_numbers(row):
    """Parser conservativo v9/v10: km, lotto, piano, interno ecc. non sono civici."""
    address = KM_RE.sub(" ", clean(row.get("indirizzo")))
    address = strip_non_civic_metadata(address)
    raw = strip_locality_tail(
        address, row.get("comune"), row.get("sigla_provincia"), row.get("cap")
    )
    if not raw:
        return set()

    nums = set()

    def add_num(v):
        n = normalize_house_number(v)
        if n:
            nums.add(n)

    atom = r"\d{1,4}(?:\s*[/\-]?\s*[A-Za-z](?![A-Za-z]))?"
    seq = rf"{atom}(?:\s*(?:[/\-]|\be\b|\s+)\s*{atom})*"

    def add_sequence(v):
        s = clean(v)
        s = re.sub(
            r"^(?:n\.?|nr\.?|numero|civico|civ)\s*", "", s, flags=re.I
        ).strip()
        s = re.sub(r"\s+\be\b\s+", "-", s, flags=re.I)
        s = re.split(
            r"\s*[/\-]?\s*\b(?:int\.?|interno|scala|piano|edificio|"
            r"sub\.?|subalterno)\b",
            s, maxsplit=1, flags=re.I
        )[0].strip()
        if not s or not re.fullmatch(seq, s, flags=re.I):
            return
        for m in re.finditer(atom, s, flags=re.I):
            mm = re.fullmatch(
                r"(\d{1,4})(?:\s*[/\-]?\s*([A-Za-z]))?",
                m.group(0), flags=re.I
            )
            if mm:
                add_num((mm.group(1) or "") + (mm.group(2) or ""))

    for m in re.finditer(
        r"\b(\d{1,4}(?:\s*[/\-]\s*[A-Za-z])?)\s*[/\-]?\s*"
        r"(?:int\.?|interno|scala|piano|edificio|sub\.?|subalterno)\s*\d+",
        raw, flags=re.I
    ):
        add_sequence(m.group(1))

    for m in re.finditer(
        rf"\b(?:n\.?|nr\.?|numero|civico|civ)\s*({seq})", raw, flags=re.I
    ):
        add_sequence(m.group(1))

    parts = [p.strip() for p in re.split(r"[,;]", raw) if p.strip()]
    for p in parts[1:]:
        add_sequence(p)

    m = re.search(rf"({seq})\s*$", raw, flags=re.I)
    if m:
        prefix = raw[:m.start(1)].strip()
        road_only = re.fullmatch(
            r"(?:ss|sp|sr|s p|s s|strada statale|strada provinciale)\s*\d{1,4}",
            normalize_text(raw),
        )
        internal_prefix = re.search(
            r"(?:[/\-]?\s*(?:int\.?|interno|scala|piano|edificio|"
            r"sub\.?|subalterno))\s*$",
            prefix, flags=re.I
        )
        if prefix and not road_only and not internal_prefix:
            add_sequence(m.group(1))

    for m in re.finditer(
        r",\s*(\d{1,4})(?:\s*[/\-]?\s*([A-Za-z]))?\s*,\s*\d{5}\b",
        raw, flags=re.I
    ):
        add_num((m.group(1) or "") + (m.group(2) or ""))

    cap = re.sub(r"\D", "", clean(row.get("cap")))
    if cap:
        nums.discard(normalize_house_number(cap))
    return nums


def load_module(path, name):
    if not path.exists():
        raise SystemExit(
            f"ERRORE: manca {path.name}. Metti questo file nella stessa cartella "
            "del pacchetto v10.2.11."
        )
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_supabase_env():
    load_common_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE"))
    return clean(os.environ.get("SUPABASE_URL")).rstrip("/"), clean(os.environ.get("SUPABASE_SERVICE_ROLE"))


def db_headers(key):
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }


def fetch_all_rows(url, key):
    out, offset = [], 0
    while True:
        endpoint = (
            f"{url}/rest/v1/listings"
            f"?select={quote(SELECT_FIELDS, safe=',')}"
            f"&order=pvp_id.asc.nullslast&limit={PAGE_SIZE}&offset={offset}"
        )
        req = Request(endpoint, method="GET", headers=db_headers(key))
        with urlopen(req, timeout=TIMEOUT) as resp:
            page = json.loads(resp.read().decode("utf-8", errors="replace") or "[]")
        if not isinstance(page, list):
            raise RuntimeError("Risposta Supabase inattesa.")
        out.extend(page)
        print(f"[DB] {len(out):,} record letti")
        if len(page) < PAGE_SIZE:
            break
        offset += len(page)
    return out


def pick_input(ann, explicit, patterns, label):
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = ANNCSU_DIR / p
        if p.exists():
            return p.resolve()
        raise SystemExit(f"ERRORE: {label} non trovato: {p}")
    p = ann.pick_existing(patterns, ANNCSU_DIR)
    if not p:
        raise SystemExit(f"ERRORE: {label} non trovato in {ANNCSU_DIR}")
    return p.resolve()


# v2.4: la fotografia completa resta identica alla v2.3 per confrontabilita'.
# La nuova vista OUTSTANDING separa invece i record ancora operativamente aperti
# da quelli gia' verificati/corretti. In questo modo non perdiamo la capacita'
# di auditare il pregresso ma le metriche residue non vengono gonfiate da source
# che hanno gia' superato una regola conservativa.
OUTSTANDING_OPEN_SOURCES = {"", "google"}
OUTSTANDING_PROTECTED_SOURCES = {
    "pvp_verified",
    "google_corrected_pvp",
    "pvp_verified_anncsu",
    "anncsu_corrected_google_reverse_verified",
    "anncsu_corrected_google_rough",
    "anncsu_corrected_pvp_street",
    "anncsu_corrected_pvp_suspect_direct",
    "anncsu_corrected_pvp_google_corroborated",
    "google_corrected_pvp_street",
}


def is_open_source_result(row):
    """Source non ancora protetta: vuota oppure vecchio legacy google."""
    src = clean(row.get("geocode_source")).lower()
    return src in OUTSTANDING_OPEN_SOURCES


def is_outstanding_result(row):
    """Residuo operativo: source aperta E non gia' KEEP_CONFIRMED localmente."""
    return (
        is_open_source_result(row)
        and clean(row.get("candidate_tier")) != "KEEP_CONFIRMED"
    )


def is_target(row):
    if not has_valid_coords(row.get("latitudine"), row.get("longitudine")):
        return False
    if AUDIT_SCOPE == "all":
        return True
    src = clean(row.get("geocode_source")).lower()
    if src.startswith(("anncsu_missing_", "google_missing_")):
        return False
    if src in {
        "pvp_verified_anncsu",
        "anncsu_corrected_google_reverse_verified",
    }:
        return False
    return True


def prepare_rows(rows):
    out = []
    for r in rows:
        x = dict(r)
        x["old_lat"] = safe_float(r.get("latitudine"))
        x["old_lon"] = safe_float(r.get("longitudine"))
        x["expected_house_numbers"] = "|".join(sorted(expected_house_numbers(r)))
        lt = clean(r.get("geocode_location_type")).upper()
        src = clean(r.get("geocode_source")).lower()
        x["status"] = lt if lt.startswith("PVP_") else ""
        x["reason"] = ""
        x["action"] = "corrected_from_pvp" if src == "google_corrected_pvp" else ""
        x["suggested_lat"] = None
        x["suggested_lon"] = None
        out.append(x)
    return out


def nearest_plausible_street_distance(ann, row, street_cands, accesses):
    """
    Minima distanza dal punto corrente a qualunque accesso di una strada
    semanticamente plausibile (score >= 0.72). SOLO diagnostica:
    non seleziona mai una coordinata da scrivere.
    """
    lat, lon = safe_float(row.get("old_lat")), safe_float(row.get("old_lon"))
    if lat is None or lon is None:
        return None
    best = None
    for sc, prog, _odo, _loc in street_cands:
        try:
            sc = float(sc)
        except Exception:
            continue
        if sc < 0.72:
            continue
        for _civ, alat, alon, _metodo in accesses.get(prog, []):
            if alat is None or alon is None:
                continue
            d = ann.haversine_m(lat, lon, float(alat), float(alon))
            if best is None or d < best:
                best = d
    return best



def cap5(value):
    digits = re.sub(r"\D", "", clean(value))
    if 1 <= len(digits) <= 5:
        return digits.zfill(5)
    return ""


def stored_reverse_evidence(row):
    """
    Usa SOLO il geocode_formatted_address gia' presente nel DB.
    Non esegue alcun reverse nuovo e non chiama Google.
    """
    formatted = clean(row.get("geocode_formatted_address"))
    comune = normalize_text(row.get("comune"))
    formatted_norm = normalize_text(formatted)

    if formatted and comune:
        comune_match = "YES" if comune in formatted_norm else "NO"
    else:
        comune_match = "UNKNOWN"

    expected_cap = cap5(row.get("cap"))
    formatted_caps = re.findall(r"\b\d{5}\b", formatted)
    if expected_cap and formatted_caps:
        cap_relation = "MATCH" if expected_cap in formatted_caps else "MISMATCH"
    else:
        cap_relation = "UNKNOWN"

    strong_conflict = (
        comune_match == "NO"
        and cap_relation == "MISMATCH"
    )
    strong_confirm = (
        comune_match == "YES"
        and cap_relation == "MATCH"
    )

    return {
        "stored_reverse_comune_match": comune_match,
        "stored_reverse_cap_relation": cap_relation,
        "stored_reverse_strong_conflict": "YES" if strong_conflict else "NO",
        "stored_reverse_strong_confirm": "YES" if strong_confirm else "NO",
    }


def assert_istat_sardinia_2026(comuni, ann):
    """
    ANNCSU 20260803 usa l'assetto statistico 2026 della Sardegna.
    Se l'xlsx ISTAT e' ancora quello pre-2026, i codici comunali sardi non
    coincidono e il matching STRAD fallisce (es. Tempio Pausania).

    Prefix ufficiali ISTAT 2026:
      112 Citta' metropolitana di Sassari
      113 Gallura Nord-Est Sardegna
      114 Nuoro
      115 Oristano
      116 Ogliastra
      117 Medio Campidano
      118 Citta' metropolitana di Cagliari
      119 Sulcis Iglesiente
    """
    expected = {
        "sassari": "112",
        "tempio pausania": "113",
        "nuoro": "114",
        "oristano": "115",
        "tortoli": "116",
        "sanluri": "117",
        "cagliari": "118",
        "carbonia": "119",
    }

    by_name = {}
    for c in comuni:
        by_name[ann.norm_name(c.get("name"))] = str(c.get("code") or "")

    problems = []
    for name, prefix in expected.items():
        code = by_name.get(ann.norm_name(name), "")
        if not code:
            problems.append(f"{name}: assente")
        elif not code.startswith(prefix):
            problems.append(f"{name}: codice {code}, atteso prefisso {prefix}")

    if problems:
        msg = "\n  - ".join(problems)
        raise SystemExit(
            "\nERRORE: Elenco-comuni-italiani.xlsx NON allineato "
            "all'assetto ISTAT Sardegna 2026.\n"
            "ANNCSU 20260803 e questo file ISTAT stanno usando codici comunali "
            "di epoche diverse.\n\n"
            "Controlli falliti:\n  - " + msg + "\n\n"
            "Scarica il permalink ISTAT aggiornato e sostituisci "
            "Elenco-comuni-italiani.xlsx:\n"
            "https://www.istat.it/storage/codici-unita-amministrative/"
            "Elenco-comuni-italiani.xlsx\n\n"
            "Nessun dato DB e' stato modificato."
        )

    print("[ISTAT CHECK] assetto Sardegna 2026: OK")



# Sigle osservate nel DB che rappresentano transizioni amministrative sarde
# e NON devono impedire il matching del Comune quando nome + codice catastale
# identificano senza ambiguita' lo stesso Comune.
SARDINIA_PROVINCE_COMPAT = {
    ("SS", "OT"),
    ("SU", "CI"),
    ("SU", "VS"),
    ("SU", "CA"),
    ("NU", "OG"),
}

GENERIC_STREET_TOKENS = {
    "via", "viale", "corso", "piazza", "piazzale", "largo", "vicolo",
    "strada", "str", "contrada", "cda", "localita", "frazione",
    "case", "sparse", "cascina", "vocabolo",
    "di", "del", "dello", "della", "dei", "degli", "delle",
    "da", "dal", "dallo", "dalla", "dai", "dagli", "dalle",
    "n", "nr", "numero", "civico", "snc",
}


def load_istat_comuni_plus(path, ann):
    """
    Come load_istat_comuni(), ma conserva anche il codice catastale del Comune.
    Il codice catastale e' usato come ponte stabile ISTAT <-> ANNCSU quando
    il CODICE_ISTAT e' cambiato per ricodifiche amministrative.
    """
    rows = ann.read_istat_rows(path)
    idx = {}
    header_seen = False
    out = []

    for row in rows:
        norms = [ann.norm_text(x) for x in row]
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
                header_seen = True
                continue
            continue

        required = {"name", "prov", "code", "catasto"}
        if not required.issubset(idx):
            raise SystemExit(
                "ERRORE: nell'Elenco-comuni-italiani.xlsx non trovo tutte "
                "le colonne richieste, compreso 'Codice Catastale'."
            )

        def cell(k):
            i = idx[k]
            return row[i].strip() if i < len(row) else ""

        name = cell("name")
        prov = cell("prov").upper()
        code = re.sub(r"\D", "", cell("code"))
        catasto = cell("catasto").upper().strip()

        if not name or not code or not catasto:
            continue

        out.append({
            "code": code.zfill(6)[-6:],
            "name": name,
            "prov": prov,
            "catasto": catasto,
            "name_norm": ann.norm_name(name),
        })

    if not out:
        raise SystemExit("ERRORE: elenco ISTAT+catastale vuoto/non riconosciuto.")

    print(f"[ISTAT+] Comuni caricati: {len(out):,}; codice catastale: OK")
    return out


def build_anncsu_catasto_crosswalk(strad_path, comuni, ann):
    """
    Costruisce dal file STRAD un crosswalk:
       CODICE_COMUNE (catastale/Agenzia Entrate) -> CODICE_ISTAT usato da ANNCSU.

    In questo modo non assumiamo che il CODICE_ISTAT dell'xlsx e quello presente
    nello snapshot ANNCSU coincidano dopo una ricodifica territoriale.
    """
    wanted = {c["catasto"] for c in comuni if c.get("catasto")}
    counts = {}
    fh, reader, delim = ann.sniff_csv(strad_path)
    n = kept = 0
    try:
        for row in reader:
            n += 1
            if n % 500000 == 0:
                print(f"[CROSSWALK] STRAD {n:,} righe; utili {kept:,}")
            cat = clean(row.get("CODICE_COMUNE")).upper()
            if cat not in wanted:
                continue
            code = re.sub(r"\D", "", clean(row.get("CODICE_ISTAT")))
            if not code:
                continue
            code = code.zfill(6)[-6:]
            kept += 1
            if cat not in counts:
                counts[cat] = Counter()
            counts[cat][code] += 1
    finally:
        fh.close()

    out = {}
    for cat, ctr in counts.items():
        ranked = ctr.most_common()
        total = sum(ctr.values())
        best_code, best_n = ranked[0]
        share = best_n / total if total else 0.0
        if len(ranked) == 1:
            status = "UNIQUE"
        elif share >= 0.90:
            status = "DOMINANT"
        else:
            status = "AMBIGUOUS"
        out[cat] = {
            "code": best_code,
            "status": status,
            "share": round(share, 6),
            "codes": "|".join(f"{c}:{n}" for c, n in ranked[:5]),
        }

    print(
        f"[CROSSWALK] catastali trovati in ANNCSU: {len(out):,}/{len(wanted):,}"
    )
    return out


def resolve_municipalities_v2_2(records, comuni, crosswalk, ann):
    """
    Risolve il Comune per nome/alias nell'ISTAT corrente, poi sceglie il
    CODICE_ISTAT effettivamente usato nello snapshot ANNCSU tramite codice
    catastale. La relazione di provincia grezza resta disponibile come flag.
    """
    by_name = {}
    for c in comuni:
        by_name.setdefault(c["name_norm"], []).append(c)

    resolved = {}
    meta = {}
    unresolved = []

    for r in records:
        pid = str(r.get("pvp_id"))
        source_name = ann.norm_name(r.get("comune"))
        source_prov = clean(r.get("sigla_provincia")).upper()
        target_name = source_name
        resolution = "EXACT"

        cand = by_name.get(target_name, [])
        if not cand and source_name in ann.MUNICIPALITY_ALIASES:
            target_name = ann.MUNICIPALITY_ALIASES[source_name]
            cand = by_name.get(target_name, [])
            resolution = "ALIAS"

        chosen = None
        if len(cand) == 1:
            chosen = cand[0]
        elif cand:
            exact = [c for c in cand if c["prov"] == source_prov]
            if len(exact) == 1:
                chosen = exact[0]

        if not chosen:
            unresolved.append(r)
            meta[pid] = {
                "municipality_resolution": "UNRESOLVED",
                "istat_comune": "",
                "istat_province": "",
                "province_relation": "UNKNOWN",
                "province_relation_raw": "UNKNOWN",
                "istat_code_current": "",
                "istat_catastale": "",
                "anncsu_code_used": "",
                "anncsu_code_source": "",
                "anncsu_crosswalk_status": "",
                "anncsu_crosswalk_codes": "",
            }
            continue

        raw_rel = (
            "MATCH"
            if (not source_prov or source_prov == chosen["prov"])
            else "MISMATCH"
        )
        compatible_transition = (source_prov, chosen["prov"]) in SARDINIA_PROVINCE_COMPAT
        eval_rel = "MATCH" if (raw_rel == "MATCH" or compatible_transition) else "MISMATCH"

        cw = crosswalk.get(chosen["catasto"], {})
        cw_status = cw.get("status", "")
        if cw_status in {"UNIQUE", "DOMINANT"}:
            ann_code = cw["code"]
            code_source = "CATASTAL_CROSSWALK"
        else:
            ann_code = chosen["code"]
            code_source = "CURRENT_ISTAT_FALLBACK"

        resolved[pid] = ann_code
        meta[pid] = {
            "municipality_resolution": resolution,
            "istat_comune": chosen["name"],
            "istat_province": chosen["prov"],
            # evaluate_record_v5 usa questo campo:
            "province_relation": eval_rel,
            "province_relation_raw": raw_rel,
            "province_transition_compatible": "YES" if compatible_transition else "NO",
            "istat_code_current": chosen["code"],
            "istat_catastale": chosen["catasto"],
            "anncsu_code_used": ann_code,
            "anncsu_code_source": code_source,
            "anncsu_crosswalk_status": cw_status,
            "anncsu_crosswalk_codes": cw.get("codes", ""),
        }

    print(
        f"[ISTAT+CROSSWALK] risolti {len(resolved):,}/{len(records):,}; "
        f"irrisolti {len(unresolved):,}"
    )
    return resolved, meta, unresolved


def scan_indir_plus(path, wanted_progs, target_codes, ann):
    """
    Un solo passaggio su INDIR:
    - conserva gli accessi delle strade candidate;
    - costruisce per ogni CODICE_ISTAT ANNCSU il bounding box di TUTTI gli
      accessi con coordinate valide.

    Il bounding box e' intenzionalmente conservativo: un outlier ANNCSU puo'
    allargarlo (falso negativo), ma non dovrebbe far apparire fuori Comune un
    punto che e' dentro il cloud ufficiale degli accessi.
    """
    accesses = {}
    bbox = {}
    fh, reader, delim = ann.sniff_csv(path)
    n = kept = coords = 0
    try:
        for row in reader:
            n += 1
            if n % 1000000 == 0:
                print(
                    f"[INDIR+] {n:,} righe; accessi strade {kept:,}; "
                    f"bbox comuni {len(bbox):,}"
                )

            code = re.sub(r"\D", "", clean(row.get("CODICE_ISTAT")))
            code = code.zfill(6)[-6:] if code else ""
            prog = clean(row.get("PROGRESSIVO_NAZIONALE"))

            need_bbox = code in target_codes
            need_access = prog in wanted_progs
            if not need_bbox and not need_access:
                continue

            # IMPORTANTE: ANNCSU usa valori decimali che possono contenere
            # la virgola. Il parser del motore v10 gestisce sia "," sia ".".
            lon = ann.safe_float(row.get("COORD_X_COMUNE"))
            lat = ann.safe_float(row.get("COORD_Y_COMUNE"))

            if (
                need_bbox
                and lat is not None and lon is not None
                and 35.0 <= lat <= 47.9 and 6.0 <= lon <= 19.0
            ):
                b = bbox.get(code)
                if b is None:
                    bbox[code] = {
                        "min_lat": lat, "max_lat": lat,
                        "min_lon": lon, "max_lon": lon,
                        "count": 1,
                    }
                else:
                    b["min_lat"] = min(b["min_lat"], lat)
                    b["max_lat"] = max(b["max_lat"], lat)
                    b["min_lon"] = min(b["min_lon"], lon)
                    b["max_lon"] = max(b["max_lon"], lon)
                    b["count"] += 1

            if need_access:
                civ = ann.normalize_civic(
                    clean(row.get("CIVICO")) + clean(row.get("ESPONENTE"))
                )
                metodo = clean(row.get("METODO"))
                accesses.setdefault(prog, []).append((civ, lat, lon, metodo))
                kept += 1
                if lat is not None and lon is not None:
                    coords += 1
    finally:
        fh.close()

    print(
        f"[INDIR+] fine: {n:,} righe; accessi utili {kept:,}; "
        f"con coordinate {coords:,}; bbox {len(bbox):,}"
    )
    return accesses, bbox


def distance_outside_bbox_m(ann, lat, lon, b):
    lat = safe_float(lat)
    lon = safe_float(lon)
    if lat is None or lon is None or not b:
        return None

    clamped_lat = min(max(lat, b["min_lat"]), b["max_lat"])
    clamped_lon = min(max(lon, b["min_lon"]), b["max_lon"])
    if clamped_lat == lat and clamped_lon == lon:
        return 0.0
    return ann.haversine_m(lat, lon, clamped_lat, clamped_lon)


def source_street_significant_tokens(row):
    s = normalize_text(row.get("indirizzo"))
    for value in (
        row.get("comune"),
        row.get("sigla_provincia"),
        row.get("cap"),
    ):
        v = normalize_text(value)
        if v:
            s = re.sub(r"\b" + re.escape(v) + r"\b", " ", s)

    for civic in clean(row.get("expected_house_numbers")).split("|"):
        v = normalize_text(civic)
        if v:
            s = re.sub(r"\b" + re.escape(v) + r"\b", " ", s)

    toks = []
    for t in normalize_text(s).split():
        if t in GENERIC_STREET_TOKENS:
            continue
        if t.isdigit() or len(t) <= 1:
            continue
        toks.append(t)
    return set(toks)


def anncsu_street_significant_tokens(odonimo):
    toks = []
    for t in normalize_text(odonimo).split():
        if t in GENERIC_STREET_TOKENS:
            continue
        if t.isdigit() or len(t) <= 1:
            continue
        toks.append(t)
    return set(toks)


def strict_street_similarity(row, odonimo):
    a = source_street_significant_tokens(row)
    b = anncsu_street_significant_tokens(odonimo)
    if not a or not b:
        return None
    inter = a & b
    p = len(inter) / len(b)
    r = len(inter) / len(a)
    if p + r == 0:
        return 0.0
    return round(2 * p * r / (p + r), 4)


def classify_v2_2(ev, nearest_street, reverse_ev, bbox_distance, strict_similarity, orig):
    """
    v2.2:
    1) ANNCSU LOCAL_ACCEPT prevale sul semplice conflitto di label reverse
       (frazioni/CAP diversi non devono diventare falsi HIGH).
    2) Il bbox di tutti gli accessi ANNCSU del Comune fornisce un controllo
       territoriale indipendente anche quando la strada non viene riconosciuta.
    3) LOCAL_CORRECT_ANNCSU + forte corrispondenza lessicale produce candidati
       forti, ma questo script NON applica alcuna correzione.
    """
    muni = clean(ev.get("municipality_resolution")).upper()
    ann_dec = clean(ev.get("anncsu_decision")).upper()
    ambiguity = clean(ev.get("ambiguity")).upper()
    civic = clean(ev.get("civic_relation")).lower()
    loc_guard = clean(ev.get("direct_locality_guard")).upper()
    reverse_conflict = reverse_ev["stored_reverse_strong_conflict"] == "YES"
    reverse_confirm = reverse_ev["stored_reverse_strong_confirm"] == "YES"

    try:
        d_selected = float(ev.get("distance_pvp_m"))
    except Exception:
        d_selected = None
    try:
        sc = float(ev.get("street_score"))
    except Exception:
        sc = None

    if muni == "UNRESOLVED":
        return "HIGH_MUNICIPALITY_UNRESOLVED", 90

    # Una conferma ANNCSU locale forte batte il falso conflitto generato da una
    # frazione/denominazione postale diversa.
    if ann_dec == "LOCAL_ACCEPT":
        if reverse_conflict:
            return "LOCAL_CONFIRM_ANNCSU_REVERSE_LABEL_DIFFERENT", 18
        if civic == "exact":
            return "LOCAL_CONFIRM_CIVICO_ANNCSU", 10
        return "LOCAL_CONFIRM_STRADA_ANNCSU", 15

    # Segnale territoriale piu' robusto: il pin e' fuori dal rettangolo che
    # contiene TUTTI gli accessi ANNCSU del Comune.
    if bbox_distance is not None and bbox_distance > 5000:
        if reverse_conflict:
            return "CRITICAL_OUTSIDE_MUNICIPALITY_BBOX_AND_REVERSE_CONFLICT", 100
        return "CRITICAL_OUTSIDE_MUNICIPALITY_BBOX_GT5KM", 99

    # Candidato diretto ANNCSU con controllo lessicale aggiuntivo.
    if (
        ann_dec == "LOCAL_CORRECT_ANNCSU"
        and strict_similarity is not None
        and strict_similarity >= 0.80
        and civic == "exact"
        and ambiguity == "NO"
        and loc_guard not in {"MISMATCH", "UNCONFIRMED"}
    ):
        src = clean(orig.get("geocode_source")).lower()
        lt = clean(orig.get("geocode_location_type")).upper()

        if src == "google" and lt in {"APPROXIMATE", "GEOMETRIC_CENTER"}:
            return "STRONG_ANNCSU_CIVIC_REPLACES_GOOGLE_ROUGH", 98

        if reverse_conflict and d_selected is not None and d_selected > 1000:
            return "STRONG_ANNCSU_CIVIC_AND_REVERSE_CONFLICT", 99

        if lt == "PVP_SUSPECT" and d_selected is not None and d_selected > 5000:
            return "STRONG_ANNCSU_CIVIC_FAR_FROM_PVP", 97

        if reverse_confirm:
            return "REVIEW_ANNCSU_DIRECT_BUT_REVERSE_TERRITORY_CONFIRMS", 66

        return "HIGH_ANNCSU_DIRECT_CIVIC_STRONG_LEXICAL", 88

    # Fuori bbox in misura minore: richiede corroborazione.
    if bbox_distance is not None and bbox_distance > 1000:
        if reverse_conflict:
            return "HIGH_OUTSIDE_MUNICIPALITY_BBOX_AND_REVERSE_CONFLICT", 94
        if clean(orig.get("geocode_location_type")).upper() == "PVP_SUSPECT":
            return "HIGH_OUTSIDE_MUNICIPALITY_BBOX_PVP_SUSPECT", 90
        return "REVIEW_OUTSIDE_MUNICIPALITY_BBOX_GT1KM", 68

    # Il solo reverse label/CAP conflict NON e' piu' un HIGH:
    # puo' essere una frazione dello stesso Comune.
    if reverse_conflict:
        return "REVIEW_REVERSE_LOCALITY_OR_CAP_DIFFERENCE", 60

    if loc_guard == "MISMATCH":
        return "HIGH_LOCALITY_CONFLICT", 82

    if ambiguity == "YES":
        return "REVIEW_ANNCSU_AMBIGUOUS", 60

    if sc is None or sc < 0.72:
        return "REVIEW_WEAK_OR_MISSING_STREET_MATCH", 50

    if civic == "exact" and d_selected is not None:
        if loc_guard == "UNCONFIRMED":
            return "REVIEW_EXACT_CIVIC_LOCALITY_UNCONFIRMED", 70
        if d_selected <= 25:
            return "LOCAL_CONFIRM_CIVICO_25M", 10
        if d_selected <= 50:
            return "REVIEW_EXACT_CIVIC_25_50M", 55
        if d_selected <= 100:
            return "HIGH_EXACT_CIVIC_50_100M", 78
        if reverse_confirm:
            return "REVIEW_CIVIC_DISTANCE_BUT_REVERSE_TERRITORY_CONFIRMS", 64
        if d_selected > 500:
            return "HIGH_EXACT_CIVIC_GT500M", 86
        return "HIGH_EXACT_CIVIC_100_500M", 80

    if nearest_street is not None:
        if nearest_street <= 50:
            return "LOCAL_CONFIRM_STRADA_50M", 15
        if nearest_street <= 100:
            return "REVIEW_STRADA_50_100M", 55
        if nearest_street <= 250:
            return "HIGH_STRADA_100_250M", 75
        if nearest_street > 1000 and reverse_confirm:
            return "REVIEW_STREET_FAR_BUT_REVERSE_TERRITORY_CONFIRMS", 62
        if nearest_street > 1000:
            return "HIGH_FAR_FROM_OFFICIAL_STREET_GT1KM", 86
        return "HIGH_STRADA_250M_1KM", 80

    if civic == "variant":
        return "REVIEW_CIVIC_VARIANT", 65
    if civic == "not_found":
        return "REVIEW_CIVIC_NOT_FOUND", 58
    return "REVIEW_OTHER", 50


def correction_tier_v2_2(ev, audit_result, strict_similarity, bbox_distance, orig):
    """
    Solo classificazione di candidati. Mai scrittura DB.
    A = evidenza locale molto forte, da ispezionare come prima lista.
    B = buon candidato ANNCSU ma necessita revisione.
    C = conflitto territoriale forte senza coordinata diretta sicura.
    """
    ann_dec = clean(ev.get("anncsu_decision")).upper()
    if audit_result.startswith("LOCAL_CONFIRM"):
        return "KEEP_CONFIRMED", "ANNCSU conferma il punto corrente"

    if (
        ann_dec == "LOCAL_CORRECT_ANNCSU"
        and strict_similarity is not None
        and strict_similarity >= 0.80
    ):
        if audit_result.startswith("STRONG_") or audit_result.startswith("CRITICAL_"):
            return "A_STRONG_DIRECT", "civico ANNCSU univoco/metodo 1-3 + forte match lessicale"
        return "B_DIRECT_REVIEW", "civico ANNCSU diretto ma manca una seconda evidenza forte"

    if audit_result.startswith("CRITICAL_OUTSIDE_MUNICIPALITY_BBOX"):
        return "C_TERRITORIAL_STRONG", "pin oltre 5 km fuori dal bbox di tutti gli accessi ANNCSU del Comune"

    if audit_result.startswith("HIGH_OUTSIDE_MUNICIPALITY_BBOX"):
        return "C_TERRITORIAL_REVIEW", "pin fuori dal bbox ANNCSU del Comune con corroborazione"

    return "", ""

def classify_v2_1(ev, nearest_street, reverse_ev):
    """
    Priorita' diagnostica v2.1.

    Principio:
    - il mismatch della sigla provincia e' un METADATA FLAG, non una prova
      di coordinata sbagliata;
    - un conflitto forte del reverse GIA' SALVATO (Comune assente + CAP diverso)
      pesa molto;
    - se il reverse salvato conferma Comune+CAP, una forte divergenza ANNCSU
      viene tenuta in REVIEW invece di autorizzare conclusioni automatiche.
    """
    metadata_flags = []

    prov = clean(ev.get("province_relation")).upper()
    muni = clean(ev.get("municipality_resolution")).upper()
    ambiguity = clean(ev.get("ambiguity")).upper()
    civic = clean(ev.get("civic_relation")).lower()
    loc_guard = clean(ev.get("direct_locality_guard")).upper()
    expected = bool(clean(ev.get("expected_house_numbers")))

    if prov == "MISMATCH":
        metadata_flags.append("SOURCE_PROVINCE_DIFFERS_FROM_ISTAT")

    try:
        sc = float(ev.get("street_score"))
    except Exception:
        sc = None
    try:
        d = float(ev.get("distance_pvp_m"))
    except Exception:
        d = None

    reverse_conflict = reverse_ev["stored_reverse_strong_conflict"] == "YES"
    reverse_confirm = reverse_ev["stored_reverse_strong_confirm"] == "YES"

    if muni == "UNRESOLVED":
        return "HIGH_MUNICIPALITY_UNRESOLVED", 90, "|".join(metadata_flags)

    # Il segnale piu' utile per i vecchi PVP_SUSPECT: il reverse gia' salvato
    # dice un altro territorio (Comune non presente + CAP differente).
    if reverse_conflict:
        if nearest_street is not None and nearest_street > 1000:
            return (
                "CRITICAL_STORED_REVERSE_CONFLICT_AND_ANNCSU_FAR",
                100,
                "|".join(metadata_flags),
            )
        return (
            "HIGH_STORED_REVERSE_TERRITORIAL_CONFLICT",
            94,
            "|".join(metadata_flags),
        )

    if loc_guard == "MISMATCH":
        return "HIGH_LOCALITY_CONFLICT", 88, "|".join(metadata_flags)

    if ambiguity == "YES":
        return "REVIEW_ANNCSU_AMBIGUOUS", 60, "|".join(metadata_flags)

    # Distanza da qualunque strada ufficiale semanticamente plausibile.
    if nearest_street is not None and sc is not None and sc >= 0.72:
        if nearest_street > 5000:
            if reverse_confirm:
                return (
                    "REVIEW_ANNCSU_FAR_BUT_STORED_REVERSE_CONFIRMS",
                    62,
                    "|".join(metadata_flags),
                )
            return (
                "CRITICAL_FAR_FROM_OFFICIAL_STREET_GT5KM",
                98,
                "|".join(metadata_flags),
            )
        if nearest_street > 1000:
            if reverse_confirm:
                return (
                    "REVIEW_ANNCSU_FAR_BUT_STORED_REVERSE_CONFIRMS",
                    62,
                    "|".join(metadata_flags),
                )
            return (
                "HIGH_FAR_FROM_OFFICIAL_STREET_GT1KM",
                90,
                "|".join(metadata_flags),
            )

    if sc is None or sc < 0.72:
        if prov == "MISMATCH":
            return (
                "REVIEW_METADATA_PROVINCE_MISMATCH_NO_STREET",
                58,
                "|".join(metadata_flags),
            )
        return (
            "REVIEW_WEAK_OR_MISSING_STREET_MATCH",
            50,
            "|".join(metadata_flags),
        )

    if civic == "exact" and d is not None:
        if loc_guard == "UNCONFIRMED":
            return (
                "REVIEW_EXACT_CIVIC_LOCALITY_UNCONFIRMED",
                70,
                "|".join(metadata_flags),
            )
        if d <= 25:
            return "LOCAL_CONFIRM_CIVICO_25M", 10, "|".join(metadata_flags)
        if d <= 50:
            return "REVIEW_EXACT_CIVIC_25_50M", 55, "|".join(metadata_flags)
        if d <= 100:
            return "HIGH_EXACT_CIVIC_50_100M", 78, "|".join(metadata_flags)
        if d <= 500:
            if reverse_confirm:
                return (
                    "REVIEW_ANNCSU_CIVIC_DISTANCE_BUT_REVERSE_CONFIRMS",
                    64,
                    "|".join(metadata_flags),
                )
            return "HIGH_EXACT_CIVIC_100_500M", 88, "|".join(metadata_flags)

        if reverse_confirm:
            return (
                "REVIEW_ANNCSU_CIVIC_CONFLICT_BUT_REVERSE_CONFIRMS",
                66,
                "|".join(metadata_flags),
            )
        return (
            "CRITICAL_EXACT_CIVIC_GT500M",
            97,
            "|".join(metadata_flags),
        )

    if not expected and nearest_street is not None:
        if nearest_street <= 50:
            return "LOCAL_CONFIRM_STRADA_50M", 15, "|".join(metadata_flags)
        if nearest_street <= 100:
            return "REVIEW_STRADA_50_100M", 55, "|".join(metadata_flags)
        if nearest_street <= 250:
            return "HIGH_STRADA_100_250M", 75, "|".join(metadata_flags)
        if nearest_street <= 1000:
            return "HIGH_STRADA_250M_1KM", 84, "|".join(metadata_flags)

    if civic == "variant":
        return "REVIEW_CIVIC_VARIANT", 65, "|".join(metadata_flags)

    if civic == "not_found":
        if nearest_street is not None and nearest_street > 250:
            return (
                "HIGH_CIVIC_NOT_FOUND_AND_STREET_FAR",
                82,
                "|".join(metadata_flags),
            )
        return "REVIEW_CIVIC_NOT_FOUND", 58, "|".join(metadata_flags)

    if clean(ev.get("anncsu_decision")).upper() == "LOCAL_ACCEPT":
        return "LOCAL_CONFIRM_OTHER", 20, "|".join(metadata_flags)

    if prov == "MISMATCH":
        return (
            "REVIEW_METADATA_PROVINCE_MISMATCH",
            55,
            "|".join(metadata_flags),
        )

    return "REVIEW_OTHER", 50, "|".join(metadata_flags)



def classify(ev, nearest_street):
    prov = clean(ev.get("province_relation")).upper()
    muni = clean(ev.get("municipality_resolution")).upper()
    ambiguity = clean(ev.get("ambiguity")).upper()
    civic = clean(ev.get("civic_relation")).lower()
    loc_guard = clean(ev.get("direct_locality_guard")).upper()
    expected = bool(clean(ev.get("expected_house_numbers")))
    try:
        sc = float(ev.get("street_score"))
    except Exception:
        sc = None
    try:
        d = float(ev.get("distance_pvp_m"))
    except Exception:
        d = None

    if prov == "MISMATCH":
        return "CRITICAL_METADATA_PROVINCE_MISMATCH", 100
    if muni == "UNRESOLVED":
        return "HIGH_MUNICIPALITY_UNRESOLVED", 90
    if loc_guard == "MISMATCH":
        return "HIGH_LOCALITY_CONFLICT", 88
    if ambiguity == "YES":
        return "REVIEW_ANNCSU_AMBIGUOUS", 60

    # Caso tipo Tempio Pausania -> pin a Sassari:
    # se il punto e' lontano da QUALUNQUE strada ANNCSU plausibile del Comune,
    # la distanza e' un forte segnale territoriale anche senza civico.
    if nearest_street is not None and sc is not None and sc >= 0.72:
        if nearest_street > 5000:
            return "CRITICAL_FAR_FROM_OFFICIAL_STREET_GT5KM", 99
        if nearest_street > 1000:
            return "HIGH_FAR_FROM_OFFICIAL_STREET_GT1KM", 92

    if sc is None or sc < 0.72:
        return "REVIEW_WEAK_OR_MISSING_STREET_MATCH", 50

    if civic == "exact" and d is not None:
        if loc_guard == "UNCONFIRMED":
            return "REVIEW_EXACT_CIVIC_LOCALITY_UNCONFIRMED", 70
        if d <= 25:
            return "LOCAL_CONFIRM_CIVICO_25M", 10
        if d <= 50:
            return "REVIEW_EXACT_CIVIC_25_50M", 55
        if d <= 100:
            return "HIGH_EXACT_CIVIC_50_100M", 80
        if d <= 500:
            return "HIGH_EXACT_CIVIC_100_500M", 90
        return "CRITICAL_EXACT_CIVIC_GT500M", 98

    if not expected and nearest_street is not None:
        if nearest_street <= 50:
            return "LOCAL_CONFIRM_STRADA_50M", 15
        if nearest_street <= 100:
            return "REVIEW_STRADA_50_100M", 55
        if nearest_street <= 250:
            return "HIGH_STRADA_100_250M", 75
        if nearest_street <= 1000:
            return "HIGH_STRADA_250M_1KM", 85

    if civic == "variant":
        return "REVIEW_CIVIC_VARIANT", 65
    if civic == "not_found":
        if nearest_street is not None and nearest_street > 250:
            return "HIGH_CIVIC_NOT_FOUND_AND_STREET_FAR", 82
        return "REVIEW_CIVIC_NOT_FOUND", 58

    if clean(ev.get("anncsu_decision")).upper() == "LOCAL_ACCEPT":
        return "LOCAL_CONFIRM_OTHER", 20
    return "REVIEW_OTHER", 50


def main():
    print("=" * 78)
    print("ASTE EASY - ANNCSU API-FREE AUDIT v2.4")
    print("ZERO Google API | SOLO GET Supabase | ZERO PATCH")
    print(f"scope={AUDIT_SCOPE}")
    print("=" * 78)

    if AUDIT_SCOPE not in {"legacy", "all"}:
        raise SystemExit("APIFREE_AUDIT_SCOPE deve essere legacy oppure all.")

    ann = load_module(ANN_ENGINE_FILE, "anncsu_local_engine_apifree_v2_4")

    # Alias storici/Comuni confluiti, solo per audit locale.
    ann.MUNICIPALITY_ALIASES.update({
        "molvena": "colceresa",
        "vermezzo": "vermezzo con zelo",
        "corigliano calabro": "corigliano rossano",
        "popoli": "popoli terme",
        "lusiana": "lusiana conco",
        "rossano": "corigliano rossano",
    })

    supabase_url, key = load_supabase_env()
    all_rows = fetch_all_rows(supabase_url, key)
    targets = [r for r in all_rows if is_target(r)]

    source_counts = Counter(clean(r.get("geocode_source")) or "<EMPTY>" for r in targets)
    type_counts = Counter(
        clean(r.get("geocode_location_type")) or "<EMPTY>" for r in targets
    )

    print(f"[TARGET] listings totali: {len(all_rows):,}")
    print(f"[TARGET] coordinate da controllare: {len(targets):,}")
    print("[TARGET] source:")
    for k, v in source_counts.most_common():
        print(f"  {k}: {v:,}")

    strad = pick_input(ann, ANNCSU_STRAD_FILE, ["STRAD_ITA_*.csv"], "STRAD ANNCSU")
    indir = pick_input(ann, ANNCSU_INDIR_FILE, ["INDIR_ITA_*.csv"], "INDIR ANNCSU")
    istat = Path(ISTAT_COMUNI_FILE)
    if not istat.is_absolute():
        istat = BASE_DIR / istat
    if not istat.exists():
        raise SystemExit(f"ERRORE: elenco ISTAT non trovato: {istat}")

    print(f"[INPUT] {strad.name} | {indir.name} | {istat.name}")

    live = prepare_rows(targets)
    comuni = load_istat_comuni_plus(istat, ann)
    assert_istat_sardinia_2026(comuni, ann)

    crosswalk = build_anncsu_catasto_crosswalk(strad, comuni, ann)
    resolved, muni_meta, unresolved = resolve_municipalities_v2_2(
        live, comuni, crosswalk, ann
    )

    streets = ann.scan_strad(strad, live, resolved, topn=TOP_STREETS)
    wanted = {cand[1] for arr in streets.values() for cand in arr}
    target_codes = set(resolved.values())
    accesses, municipality_bbox = scan_indir_plus(
        indir, wanted, target_codes, ann
    )

    coord_accesses = sum(
        1
        for arr in accesses.values()
        for _civ, lat, lon, _metodo in arr
        if lat is not None and lon is not None
    )
    if wanted and coord_accesses == 0:
        raise SystemExit(
            "ERRORE QA: sono state trovate strade candidate ma ZERO accessi "
            "ANNCSU con coordinate. Controllare formato decimali/INDIR. "
            "Nessun dato DB e' stato modificato."
        )
    print(
        f"[QA INDIR] accessi candidati con coordinate: {coord_accesses:,}; "
        f"bbox comuni: {len(municipality_bbox):,}"
    )

    originals = {clean(r.get("pvp_id")): r for r in targets}
    results = []

    for i, src in enumerate(live, 1):
        pid = clean(src.get("pvp_id"))
        ev = ann.evaluate_record_v5(
            src,
            resolved.get(pid),
            muni_meta.get(pid, {}),
            streets.get(pid, []),
            accesses,
        )
        near = nearest_plausible_street_distance(
            ann, src, streets.get(pid, []), accesses
        )
        orig = originals[pid]
        reverse_ev = stored_reverse_evidence(orig)
        meta = muni_meta.get(pid, {})
        bbox = municipality_bbox.get(resolved.get(pid))
        bbox_distance = distance_outside_bbox_m(
            ann, orig.get("latitudine"), orig.get("longitudine"), bbox
        )
        strict_similarity = strict_street_similarity(
            src, ev.get("anncsu_odonimo", "")
        )

        label, priority = classify_v2_2(
            ev, near, reverse_ev, bbox_distance, strict_similarity, orig
        )

        flags = []
        if meta.get("province_relation_raw") == "MISMATCH":
            flags.append("SOURCE_PROVINCE_DIFFERS_FROM_ISTAT")
        if meta.get("province_transition_compatible") == "YES":
            flags.append("PROVINCE_TRANSITION_COMPATIBLE")
        if meta.get("anncsu_code_source") == "CATASTAL_CROSSWALK":
            if meta.get("istat_code_current") != meta.get("anncsu_code_used"):
                flags.append("ANNCSU_ISTAT_CODE_BRIDGED_BY_CATASTAL")
        metadata_flags = "|".join(flags)

        candidate_tier, candidate_reason = correction_tier_v2_2(
            ev, label, strict_similarity, bbox_distance, orig
        )

        results.append({
            "priority": priority,
            "audit_result": label,
            "candidate_tier": candidate_tier,
            "candidate_reason": candidate_reason,
            "metadata_flags": metadata_flags,
            "pvp_id": pid,
            "indirizzo": clean(orig.get("indirizzo")),
            "comune": clean(orig.get("comune")),
            "sigla_provincia": clean(orig.get("sigla_provincia")).upper(),
            "cap": clean(orig.get("cap")),
            "current_lat": orig.get("latitudine"),
            "current_lon": orig.get("longitudine"),
            "geocode_source": clean(orig.get("geocode_source")),
            "geocode_location_type": clean(orig.get("geocode_location_type")),
            "geocode_formatted_address": clean(
                orig.get("geocode_formatted_address")
            ),
            "stored_reverse_comune_match": reverse_ev[
                "stored_reverse_comune_match"
            ],
            "stored_reverse_cap_relation": reverse_ev[
                "stored_reverse_cap_relation"
            ],
            "stored_reverse_strong_conflict": reverse_ev[
                "stored_reverse_strong_conflict"
            ],
            "stored_reverse_strong_confirm": reverse_ev[
                "stored_reverse_strong_confirm"
            ],
            "expected_house_numbers": ev.get("expected_house_numbers", ""),
            "municipality_resolution": ev.get("municipality_resolution", ""),
            "istat_comune": ev.get("istat_comune", ""),
            "istat_province": ev.get("istat_province", ""),
            "province_relation": ev.get("province_relation", ""),
            "province_relation_raw": meta.get("province_relation_raw", ""),
            "province_transition_compatible": meta.get(
                "province_transition_compatible", ""
            ),
            "istat_code_current": meta.get("istat_code_current", ""),
            "istat_catastale": meta.get("istat_catastale", ""),
            "anncsu_code_used": meta.get("anncsu_code_used", ""),
            "anncsu_code_source": meta.get("anncsu_code_source", ""),
            "anncsu_crosswalk_status": meta.get(
                "anncsu_crosswalk_status", ""
            ),
            "anncsu_crosswalk_codes": meta.get(
                "anncsu_crosswalk_codes", ""
            ),
            "municipality_bbox_access_count": (
                bbox.get("count", "") if bbox else ""
            ),
            "distance_outside_municipality_bbox_m": (
                "" if bbox_distance is None else round(bbox_distance, 2)
            ),
            "strict_street_similarity": (
                "" if strict_similarity is None else strict_similarity
            ),
            "anncsu_decision": ev.get("anncsu_decision", ""),
            "decision_reason": ev.get("decision_reason", ""),
            "direct_locality_guard": ev.get("direct_locality_guard", ""),
            "ambiguity": ev.get("ambiguity", ""),
            "ambiguity_reason": ev.get("ambiguity_reason", ""),
            "street_score": ev.get("street_score", ""),
            "anncsu_odonimo": ev.get("anncsu_odonimo", ""),
            "anncsu_localita": ev.get("anncsu_localita", ""),
            "civic_relation": ev.get("civic_relation", ""),
            "anncsu_civic": ev.get("anncsu_civic", ""),
            "anncsu_metodo": ev.get("anncsu_metodo", ""),
            "anncsu_lat": ev.get("anncsu_lat", ""),
            "anncsu_lon": ev.get("anncsu_lon", ""),
            "distance_to_selected_address_m": ev.get("distance_pvp_m", ""),
            "nearest_plausible_official_street_m": (
                "" if near is None else round(near, 2)
            ),
            "matching_street_candidates": ev.get(
                "matching_street_candidates", ""
            ),
            "matching_access_candidates": ev.get(
                "matching_access_candidates", ""
            ),
            "local_signal": ev.get("local_signal", ""),
        })

        if i % 500 == 0:
            print(f"[EVAL] {i:,}/{len(live):,}")

    results.sort(key=lambda r: (-int(r["priority"]), r["comune"], r["pvp_id"]))

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    all_path = REPORTS_DIR / f"db_apifree_anncsu_all_v2_4_{stamp}.csv"
    priority_path = REPORTS_DIR / f"db_apifree_anncsu_priority_v2_4_{stamp}.csv"
    summary_path = REPORTS_DIR / f"db_apifree_anncsu_summary_v2_4_{stamp}.json"

    fields = list(results[0].keys()) if results else ["priority", "audit_result", "pvp_id"]

    with all_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)

    priority_rows = [r for r in results if int(r["priority"]) >= 75]
    with priority_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(priority_rows)

    strong_path = REPORTS_DIR / f"db_apifree_anncsu_strong_v2_4_{stamp}.csv"
    strong_rows = [r for r in results if int(r["priority"]) >= 90]
    with strong_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(strong_rows)

    candidates_path = REPORTS_DIR / f"db_apifree_anncsu_candidates_v2_4_{stamp}.csv"
    candidate_rows = [r for r in results if clean(r.get("candidate_tier"))]
    candidate_rows.sort(
        key=lambda r: (
            {"A_STRONG_DIRECT": 0, "C_TERRITORIAL_STRONG": 1,
             "C_TERRITORIAL_REVIEW": 2, "B_DIRECT_REVIEW": 3,
             "KEEP_CONFIRMED": 4}.get(r["candidate_tier"], 9),
            -int(r["priority"]),
            r["comune"],
            r["pvp_id"],
        )
    )
    with candidates_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(candidate_rows)

    # v2.4: vista operativa separata. Non cambia i risultati legacy, serve solo
    # a contare/esportare cio' che e' ancora realmente aperto dopo i repair.
    open_source_rows = [r for r in results if is_open_source_result(r)]
    outstanding_rows = [r for r in results if is_outstanding_result(r)]
    outstanding_path = REPORTS_DIR / f"db_apifree_anncsu_outstanding_v2_4_{stamp}.csv"
    with outstanding_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(outstanding_rows)

    result_counts = Counter(r["audit_result"] for r in results)
    decision_counts = Counter(r["anncsu_decision"] for r in results)
    bands = Counter(
        "CRITICAL" if int(r["priority"]) >= 95 else
        "HIGH" if int(r["priority"]) >= 75 else
        "REVIEW" if int(r["priority"]) >= 40 else
        "LOCAL_CONFIRM"
        for r in results
    )

    summary = {
        "version": "2.4",
        "generated_at": datetime.now().astimezone().isoformat(),
        "mode": "READ_ONLY_ZERO_GOOGLE_API_ANNCSU",
        "scope": AUDIT_SCOPE,
        "total_listings": len(all_rows),
        "targets_with_valid_coords": len(targets),
        "municipality_unresolved": len(unresolved),
        "target_geocode_source": dict(source_counts.most_common()),
        "target_geocode_location_type": dict(type_counts.most_common()),
        "audit_result": dict(result_counts.most_common()),
        "priority_band": dict(bands),
        "anncsu_decision": dict(decision_counts.most_common()),
        "priority_rows_ge_75": len(priority_rows),
        "strong_rows_ge_90": len(strong_rows),
        "candidate_tier": dict(
            Counter(
                r.get("candidate_tier") or "<NONE>" for r in results
            ).most_common()
        ),
        "outstanding": {
            "definition": "source vuota oppure legacy source=google, esclusi i record gia' KEEP_CONFIRMED localmente; source verificate/corrette restano nella fotografia completa",
            "open_source_rows_before_keep_confirmed_exclusion": len(open_source_rows),
            "keep_confirmed_excluded": len(open_source_rows) - len(outstanding_rows),
            "rows": len(outstanding_rows),
            "geocode_source": dict(Counter(clean(r.get("geocode_source")) or "<EMPTY>" for r in outstanding_rows).most_common()),
            "geocode_location_type": dict(Counter(clean(r.get("geocode_location_type")) or "<EMPTY>" for r in outstanding_rows).most_common()),
            "audit_result": dict(Counter(r.get("audit_result") or "<EMPTY>" for r in outstanding_rows).most_common()),
            "candidate_tier": dict(Counter(r.get("candidate_tier") or "<NONE>" for r in outstanding_rows).most_common()),
            "priority_rows_ge_75": sum(int(r.get("priority") or 0) >= 75 for r in outstanding_rows),
            "strong_rows_ge_90": sum(int(r.get("priority") or 0) >= 90 for r in outstanding_rows),
            "rows_outside_bbox_gt1km": sum(
                safe_float(r.get("distance_outside_municipality_bbox_m")) is not None
                and safe_float(r.get("distance_outside_municipality_bbox_m")) > 1000
                for r in outstanding_rows
            ),
            "rows_outside_bbox_gt5km": sum(
                safe_float(r.get("distance_outside_municipality_bbox_m")) is not None
                and safe_float(r.get("distance_outside_municipality_bbox_m")) > 5000
                for r in outstanding_rows
            ),
        },
        "crosswalk": {
            "catastali_in_anncsu": len(crosswalk),
            "records_using_catastal_crosswalk": sum(
                muni_meta.get(clean(r.get("pvp_id")), {}).get(
                    "anncsu_code_source"
                ) == "CATASTAL_CROSSWALK"
                for r in live
            ),
            "records_code_bridged": sum(
                (
                    muni_meta.get(clean(r.get("pvp_id")), {}).get(
                        "anncsu_code_source"
                    ) == "CATASTAL_CROSSWALK"
                    and muni_meta.get(clean(r.get("pvp_id")), {}).get(
                        "istat_code_current"
                    )
                    != muni_meta.get(clean(r.get("pvp_id")), {}).get(
                        "anncsu_code_used"
                    )
                )
                for r in live
            ),
            "province_transition_compatible_rows": sum(
                muni_meta.get(clean(r.get("pvp_id")), {}).get(
                    "province_transition_compatible"
                ) == "YES"
                for r in live
            ),
        },
        "indir_qa": {
            "wanted_street_progressivi": len(wanted),
            "accesses_with_coordinates": coord_accesses,
        },
        "municipality_bbox": {
            "municipalities_with_bbox": len(municipality_bbox),
            "rows_outside_bbox_gt1km": sum(
                safe_float(r.get("distance_outside_municipality_bbox_m")) is not None
                and safe_float(r.get("distance_outside_municipality_bbox_m")) > 1000
                for r in results
            ),
            "rows_outside_bbox_gt5km": sum(
                safe_float(r.get("distance_outside_municipality_bbox_m")) is not None
                and safe_float(r.get("distance_outside_municipality_bbox_m")) > 5000
                for r in results
            ),
        },
        "reverse_conflict_rescued_by_anncsu_local_accept": sum(
            r.get("stored_reverse_strong_conflict") == "YES"
            and str(r.get("audit_result", "")).startswith("LOCAL_CONFIRM")
            for r in results
        ),
        "metadata_flags": dict(
            Counter(
                flag
                for r in results
                for flag in clean(r.get("metadata_flags")).split("|")
                if flag
            ).most_common()
        ),
        "stored_reverse": {
            "strong_conflict": sum(
                r.get("stored_reverse_strong_conflict") == "YES"
                for r in results
            ),
            "strong_confirm": sum(
                r.get("stored_reverse_strong_confirm") == "YES"
                for r in results
            ),
        },
        "inputs": {
            "strad": strad.name,
            "indir": indir.name,
            "istat": istat.name,
            "anncsu_engine": ANN_ENGINE_FILE.name,
        },
        "notes": [
            "Nessuna API Google viene chiamata.",
            "Nessun dato Supabase viene modificato: solo GET.",
            "Il CODICE_COMUNE catastale viene usato per colmare ricodifiche tra ISTAT corrente e snapshot ANNCSU.",
            "Il bbox comunale contiene tutti gli accessi ANNCSU con coordinate valide ed e' usato solo come segnale conservativo.",
            "Un reverse che mostra una frazione/CAP diverso non e' piu' considerato da solo prova di pin errato.",
            "Le coordinate ANNCSU restano proposte/diagnostica: questo script non applica correzioni.",
            "v2.4 aggiunge una vista outstanding separata senza cambiare l'universo legacy dell'audit.",
            "Outstanding = source vuota oppure vecchio source=google, esclusi i KEEP_CONFIRMED; pvp_verified e source corrected restano nel full audit ma non nel residuo operativo.",
        ],
        "files": {
            "summary": summary_path.name,
            "priority": priority_path.name,
            "strong": strong_path.name,
            "candidates": candidates_path.name,
            "outstanding": outstanding_path.name,
            "all": all_path.name,
        },
    }

    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print("=" * 78)
    print("RISULTATO")
    print("=" * 78)
    print(f"Target: {len(results):,}")
    print(f"Comuni non risolti: {len(unresolved):,}")
    print(f"CRITICAL: {bands.get('CRITICAL', 0):,}")
    print(f"HIGH: {bands.get('HIGH', 0):,}")
    print(f"REVIEW: {bands.get('REVIEW', 0):,}")
    print(f"LOCAL_CONFIRM: {bands.get('LOCAL_CONFIRM', 0):,}")
    print(f"Priority CSV (score >=75): {len(priority_rows):,}")
    print(f"Strong CSV (score >=90): {len(strong_rows):,}")
    print(f"Candidati/classificati: {len(candidate_rows):,}")
    print(f"Outstanding operativi: {len(outstanding_rows):,}")
    print(f"[OUT] {summary_path.name}")
    print(f"[OUT] {strong_path.name}")
    print(f"[OUT] {candidates_path.name}")
    print(f"[OUT] {outstanding_path.name}")
    print(f"[OUT] {priority_path.name}")
    print(f"[OUT] {all_path.name}")
    print("[DONE] Zero Google API. Zero modifiche DB.")


if __name__ == "__main__":
    main()
