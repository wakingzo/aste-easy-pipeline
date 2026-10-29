#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Aste Easy - PVP Incremental Importer v12.3.1

DEFAULT: DRY RUN
ZERO Google.

Principio:
- un pvp_id gia' presente in listings e' IMMUTABILE per l'importer;
- un nuovo esperimento PVP arriva con nuovo pvp_id -> INSERT completo solo per ID nuovi;
- sugli ID esistenti l'unico aggiornamento listing ammesso e' NOTE, e solo in forma conservativa;
- nessun campo geografico o geocode di un ID esistente viene mai scritto;
- soggetti: solo per ID nuovi, senza DELETE;
- allegati: solo per ID nuovi eleggibili;
- questo script NON scrive geocode_touched_v11.jsonl.

Dry-run:
  .\python\python.exe .\pvp_import_incremental_v12_3.py

Apply (SOLO dopo audit dry-run):
  .\python\python.exe .\pvp_import_incremental_v12_3.py --apply
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import runpy
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
SRC_ROOT = BASE_DIR.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from aste_paths import HANDOFF_DIR, RAW_DIR, REFERENCE_DIR, REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env as load_common_project_env

TIMEOUT = 120
FETCH_BATCH = 150
ISTAT_XLSX = REFERENCE_DIR / "Elenco-comuni-italiani.xlsx"

SUMMARY_PREFIX = "pvp_import_incremental_v12"
AUDIT_PREFIX = "pvp_import_incremental_v12"
NEW_ROWS_PREFIX = "pvp_new_rows_v12"

ORIGINAL_IMPORTER_CANDIDATES = [
    "pvp_import_from_api_raw_hybrid_FINAL.py",
    "pvp_import_from_api_raw_hybrid_final.py",
]

GEO_FIELDS_EXISTING_FORBIDDEN = {
    "regione", "comune", "provincia", "sigla_provincia", "cap",
    "indirizzo", "maps_link", "latitudine", "longitudine",
    "geocode_source", "geocode_location_type",
    "geocode_formatted_address", "geocode_query_used",
}


def clean(v):
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v).replace("\u00a0", " ")).strip()


def norm(v):
    return clean(v).casefold()


def load_pipeline_env():
    return load_common_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE"))


def find_original_importer():
    for name in ORIGINAL_IMPORTER_CANDIDATES:
        p = BASE_DIR / name
        if p.exists():
            return p
    raise SystemExit(
        "ERRORE: importer originale non trovato. Attesi: "
        + ", ".join(ORIGINAL_IMPORTER_CANDIDATES)
    )


def load_original_importer(path):
    spec = importlib.util.spec_from_file_location("asteeasy_original_importer_v12base", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"ERRORE: impossibile caricare {path.name}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def headers(key, content=False, prefer=None):
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    if content:
        h["Content-Type"] = "application/json"
    if prefer:
        h["Prefer"] = prefer
    return h


def fetch_existing(base, key, ids, listing_fields):
    fields = []
    for f in list(listing_fields) + [
        "geocode_source", "geocode_location_type",
        "geocode_formatted_address", "geocode_query_used",
    ]:
        if f not in fields:
            fields.append(f)

    out = {}
    ids = [str(x) for x in ids if clean(x)]
    for i in range(0, len(ids), FETCH_BATCH):
        batch = ids[i:i+FETCH_BATCH]
        vals = ",".join(batch)
        url = (
            f"{base.rstrip('/')}/rest/v1/listings"
            f"?select={quote(','.join(fields), safe=',')}"
            f"&pvp_id=in.({quote(vals, safe=',')})"
        )
        req = Request(url, method="GET", headers=headers(key))
        with urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace") or "[]")
        if not isinstance(data, list):
            raise RuntimeError("Risposta Supabase inattesa durante fetch existing")
        for r in data:
            pid = clean((r or {}).get("pvp_id"))
            if pid:
                out[pid] = dict(r or {})
    return out


def exact_pvp_address(mod, obj):
    detail = obj.get("detail_body") or {}
    search = obj.get("search_item") or {}
    beni = (detail.get("beni") or []) + ((detail.get("lotto") or {}).get("beni") or [])

    for bene in beni:
        addr = bene.get("indirizzo") or {}
        comune = clean(addr.get("descComune") or addr.get("citta") or addr.get("comune"))
        provincia = clean(addr.get("descProvincia") or addr.get("provincia"))
        sigla = clean(addr.get("siglaProvincia") or addr.get("sigla_provincia"))
        cap = re.sub(r"\D", "", clean(addr.get("cap") or ""))
        cap = cap.zfill(5) if cap else ""
        via = mod.clean_via_structured(
            addr.get("via") or addr.get("descVia"),
            comune, provincia, sigla, cap
        )
        nc = clean(addr.get("numeroCivico"))
        if nc and nc not in via:
            via = " ".join(x for x in [via, nc] if x).strip()
        composed = mod.compose_indirizzo(via, cap, comune, sigla or provincia)
        if composed:
            return composed

    addr = search.get("indirizzo") or {}
    comune = clean(addr.get("descComune") or addr.get("citta") or addr.get("comune"))
    provincia = clean(addr.get("descProvincia") or addr.get("provincia"))
    sigla = clean(addr.get("siglaProvincia") or addr.get("sigla_provincia"))
    cap = re.sub(r"\D", "", clean(addr.get("cap") or ""))
    cap = cap.zfill(5) if cap else ""
    via = mod.clean_via_structured(
        addr.get("via") or addr.get("descVia"),
        comune, provincia, sigla, cap
    )
    nc = clean(addr.get("numeroCivico"))
    if nc and nc not in via:
        via = " ".join(x for x in [via, nc] if x).strip()
    return mod.compose_indirizzo(via, cap, comune, sigla or provincia)


def derive_new_row_pvp_faithful(mod, obj, allegati_by_id):
    row = dict(mod.derive_listing_row(obj, allegati_by_id))

    detail = obj.get("detail_body") or {}
    search = obj.get("search_item") or {}
    beni = (detail.get("beni") or []) + ((detail.get("lotto") or {}).get("beni") or [])
    geo = mod.extract_geo_api(detail, search, beni)

    comune = clean(geo.get("comune"))
    provincia = clean(geo.get("provincia"))
    sigla = clean(geo.get("sigla_provincia")).upper()
    regione = clean(geo.get("regione"))
    cap = re.sub(r"\D", "", clean(geo.get("cap")))
    cap = cap.zfill(5) if cap else ""
    lat = clean(geo.get("lat"))
    lon = clean(geo.get("lon"))
    indirizzo = exact_pvp_address(mod, obj)

    row.update({
        "regione": regione,
        "comune": comune,
        "provincia": provincia,
        "sigla_provincia": sigla,
        "cap": cap,
        "indirizzo": indirizzo,
        "latitudine": lat,
        "longitudine": lon,
        "maps_link": mod.build_maps_link(lat, lon, indirizzo, comune, provincia),
    })
    return row


def note_decision(current_note, incoming_note):
    cur = clean(current_note)
    inc = clean(incoming_note)
    if not inc:
        return "NO_INCOMING_NOTE", None
    if norm(cur) == norm(inc):
        return "UNCHANGED", None
    if not cur:
        return "SET_EMPTY_TO_INCOMING", inc
    if norm(cur) in norm(inc):
        return "REPLACE_WITH_EXTENDED", inc
    return "CONFLICT_SKIP", None


def sanitize_new_rows(mod, rows):
    clean_rows = mod.sanitize_listing_rows(rows)
    out = []
    for r in clean_rows:
        rr = {k: v for k, v in r.items() if not str(k).startswith("geocode_")}
        out.append(rr)
    return out


def resolve_handoff_path(cli_value=""):
    raw = clean(cli_value) or clean(os.environ.get("PVP_GEOCODE_HANDOFF_MANIFEST"))
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = HANDOFF_DIR / p
    return p


def write_geocode_handoff(path, ids):
    """Write only IDs whose listing INSERT already succeeded.

    The row exists in Supabase at geocode time. We mark its geographic input as
    fresh so unified v11.6.1 uses the NEW/CHANGED PVP branch while re-reading the
    actual DB row (no hybrid/raw copy is required).
    """
    if path is None:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with tmp.open("w", encoding="utf-8") as f:
        for pid in ids:
            pid = clean(pid)
            if not pid:
                continue
            rec = {
                "pvp_id": pid,
                "existing": "YES",
                "geographic_input_changed": "YES",
                "pvp_coordinates_changed": "YES",
                "handoff_source": "pvp_import_incremental_v12_3",
                "fresh_inserted": "YES",
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count += 1
    tmp.replace(path)
    return count


def patch_note(base, key, pvp_id, note):
    payload = {"note": note}
    url = f"{base.rstrip('/')}/rest/v1/listings?pvp_id=eq.{quote(str(pvp_id), safe='')}"
    req = Request(
        url,
        method="PATCH",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers(key, content=True, prefer="return=minimal"),
    )
    with urlopen(req, timeout=TIMEOUT) as resp:
        resp.read()



def _norm_admin_text(v):
    s = clean(v)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold().replace("’", "'")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


CURRENT_COMUNE_ALIASES = {
    "presicce": "presicce acquarica",
    "molvena": "colceresa",
    "vermezzo": "vermezzo con zelo",
    "corigliano calabro": "corigliano rossano",
    "rossano": "corigliano rossano",
    "popoli": "popoli terme",
    "lusiana": "lusiana conco",
    "campospinoso": "campospinoso albaredo",
    "veruno": "gattico veruno",
    "tavarnelle val di pesa": "barberino tavarnelle",
    "montebello ionico": "montebello jonico",
    "tonengo": "moransengo tonengo",
    "vighizzolo d este": "borgo veneto",
    "gambugliano": "sovizzo",
    "piadena": "piadena drizzona",
}


def _pick_header_index(headers, predicates):
    for i, h in enumerate(headers):
        if all(p(h) for p in predicates):
            return i
    return None


def load_istat_admin(path):
    """
    Lookup amministrativo corrente da Elenco-comuni-italiani.xlsx.
    Fail-closed: se non si riesce a leggere il file, non inventa sigle.
    """
    if not path.exists():
        return {"ok": False, "reason": "istat_xlsx_missing", "by_comune": {}}
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        header = None
        header_row = None
        for ridx, row in enumerate(ws.iter_rows(values_only=True), start=1):
            norms = [_norm_admin_text(v) for v in row]
            if any("sigla automobilistica" in x for x in norms) and any("denominazione" in x for x in norms):
                header = norms
                header_row = ridx
                break
            if ridx >= 15:
                break
        if not header:
            return {"ok": False, "reason": "istat_header_not_found", "by_comune": {}}

        def first_idx(tests):
            for test in tests:
                for i, h in enumerate(header):
                    if test(h):
                        return i
            return None

        idx_comune = first_idx([
            lambda h: h == "denominazione in italiano",
            lambda h: "denominazione italiana e straniera" in h,
            lambda h: h.startswith("denominazione") and "unita territoriale" not in h and "regione" not in h,
        ])
        idx_sigla = first_idx([lambda h: "sigla automobilistica" in h])
        idx_prov = first_idx([
            lambda h: "denominazione dell unita territoriale sovracomunale" in h,
            lambda h: "denominazione provincia" in h,
            lambda h: h == "provincia",
        ])
        idx_reg = first_idx([
            lambda h: h == "regione",
            lambda h: "denominazione regione" in h,
        ])
        if idx_comune is None or idx_sigla is None or idx_prov is None or idx_reg is None:
            return {
                "ok": False,
                "reason": f"istat_columns_missing comune={idx_comune} sigla={idx_sigla} prov={idx_prov} reg={idx_reg}",
                "by_comune": {},
            }

        by_comune = {}
        for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
            def at(idx):
                return clean(row[idx]) if idx is not None and idx < len(row) else ""
            comune = at(idx_comune)
            sigla = at(idx_sigla).upper()
            provincia = at(idx_prov)
            regione = at(idx_reg)
            if not comune:
                continue
            rec = {
                "comune": comune,
                "sigla_provincia": sigla,
                "provincia": provincia,
                "regione": regione,
            }
            by_comune.setdefault(_norm_admin_text(comune), []).append(rec)
        return {"ok": True, "reason": "ok", "by_comune": by_comune}
    except Exception as e:
        return {"ok": False, "reason": f"istat_load_error:{type(e).__name__}", "by_comune": {}}


def canonical_admin(row, istat):
    comune_raw = clean(row.get("comune"))
    provincia_raw = clean(row.get("provincia"))
    sigla_raw = clean(row.get("sigla_provincia")).upper()
    regione_raw = clean(row.get("regione"))
    key = _norm_admin_text(comune_raw)
    key = CURRENT_COMUNE_ALIASES.get(key, key)

    cands = list((istat.get("by_comune") or {}).get(key, []))
    if not cands:
        return {
            "ok": False,
            "reason": "istat_comune_not_found",
            "comune": comune_raw,
            "provincia": provincia_raw,
            "sigla_provincia": sigla_raw,
            "regione": regione_raw,
        }

    if len(cands) > 1:
        pkey = _norm_admin_text(provincia_raw)
        skey = sigla_raw
        narrowed = [
            r for r in cands
            if (pkey and _norm_admin_text(r.get("provincia")) == pkey)
            or (skey and clean(r.get("sigla_provincia")).upper() == skey)
        ]
        if len(narrowed) == 1:
            cands = narrowed

    if len(cands) != 1:
        return {
            "ok": False,
            "reason": f"istat_comune_ambiguous:{len(cands)}",
            "comune": comune_raw,
            "provincia": provincia_raw,
            "sigla_provincia": sigla_raw,
            "regione": regione_raw,
        }

    r = cands[0]
    return {
        "ok": bool(clean(r.get("sigla_provincia"))),
        "reason": "istat_resolved" if clean(r.get("sigla_provincia")) else "istat_sigla_missing",
        "comune": clean(r.get("comune")) or comune_raw,
        "provincia": clean(r.get("provincia")) or provincia_raw,
        "sigla_provincia": clean(r.get("sigla_provincia")).upper(),
        "regione": clean(r.get("regione")) or regione_raw,
    }


def parse_auction_date(v):
    s = clean(v)
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None


def safe_maps_link(row):
    lat = clean(row.get("latitudine"))
    lon = clean(row.get("longitudine"))
    if lat and lon:
        return f"https://www.google.com/maps?q={lat},{lon}"
    indirizzo = clean(row.get("indirizzo"))
    return f"https://www.google.com/maps?q={quote(indirizzo)}" if indirizzo else ""


RETENTION_DAYS = 14
# Fail-closed su date d'asta palesemente anomale.
MAX_FUTURE_AUCTION_DAYS = 730


def classify_new_candidate(row, today, istat):
    """
    Lifecycle Aste Easy:
    - data asta oggi/futura: inseribile;
    - scaduta da 1..14 giorni: ancora inseribile, per la finestra di retention;
    - scaduta da oltre 14 giorni: non reinserire;
    - data asta non leggibile: review manuale, mai insert automatico.
    """
    d = parse_auction_date(row.get("data_asta"))
    if d is None:
        return "SKIP_DATE_UNKNOWN", "auction_date_missing_or_unparseable", None

    age_days = (today - d).days

    future_days = -age_days
    if future_days > MAX_FUTURE_AUCTION_DAYS:
        return (
            "SKIP_DATE_FAR_FUTURE_REVIEW",
            f"auction_{future_days}_days_in_future_gt_{MAX_FUTURE_AUCTION_DAYS}",
            None,
        )

    if age_days > RETENTION_DAYS:
        return (
            "SKIP_EXPIRED_BEYOND_RETENTION",
            f"auction_expired_{age_days}_days_gt_{RETENTION_DAYS}",
            None,
        )

    admin = canonical_admin(row, istat)
    if not admin.get("ok"):
        return "SKIP_ADMIN_REVIEW", clean(admin.get("reason")), admin

    if age_days > 0:
        return (
            "ELIGIBLE_EXPIRED_WITHIN_RETENTION",
            f"auction_expired_{age_days}_days_within_{RETENTION_DAYS}",
            admin,
        )

    return "ELIGIBLE_ACTIVE", "auction_today_or_future_and_admin_resolved", admin


def main():
    ensure_runtime_dirs()
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply = bool(args.apply)

    load_pipeline_env()
    importer_path = find_original_importer()
    mod = load_original_importer(importer_path)

    base = clean(os.environ.get("SUPABASE_URL"))
    key = clean(os.environ.get("SUPABASE_SERVICE_ROLE"))
    if not base or not key:
        raise SystemExit("ERRORE: SUPABASE_URL / SUPABASE_SERVICE_ROLE mancanti")

    print("[1/5] Carico input locali...", flush=True)
    raw_rows = mod.load_jsonl(mod.IN_RAW)
    allegati_rows = mod.load_jsonl(mod.IN_ALLEGATI)
    soggetti_rows = mod.load_jsonl(mod.IN_SOGGETTI)
    print(f"      raw={len(raw_rows)} allegati={len(allegati_rows)} soggetti={len(soggetti_rows)}", flush=True)

    allegati_by_id = defaultdict(list)
    for a in allegati_rows:
        allegati_by_id[str(a.get("pvp_id") or "")].append(a)

    raw_by_id = {}
    duplicate_input_ids = Counter()
    for obj in raw_rows:
        pid = clean(obj.get("pvp_id"))
        if not pid:
            continue
        duplicate_input_ids[pid] += 1
        raw_by_id[pid] = obj

    incoming_ids = sorted(raw_by_id, key=lambda x: int(x) if x.isdigit() else x)
    existing = fetch_existing(base, key, incoming_ids, mod.LISTINGS_FIELDS)
    existing_ids = set(existing)
    new_ids = [pid for pid in incoming_ids if pid not in existing_ids]

    derived = {}
    for pid in incoming_ids:
        obj = raw_by_id[pid]
        if pid in existing_ids:
            derived[pid] = mod.derive_listing_row(obj, allegati_by_id)
        else:
            derived[pid] = derive_new_row_pvp_faithful(mod, obj, allegati_by_id)

    note_actions = []
    note_counts = Counter()
    for pid in incoming_ids:
        if pid not in existing_ids:
            continue
        cur = existing[pid]
        cand = derived[pid]
        action, value = note_decision(cur.get("note"), cand.get("note"))
        note_counts[action] += 1
        note_actions.append({
            "pvp_id": pid,
            "action": action,
            "current_note": clean(cur.get("note")),
            "incoming_note": clean(cand.get("note")),
            "would_write_note": clean(value),
        })

    safe_note_map = {
        r["pvp_id"]: r["would_write_note"]
        for r in note_actions if r["would_write_note"]
    }

    projected_rows = []
    for pid in incoming_ids:
        if pid in existing_ids:
            rr = {k: existing[pid].get(k) for k in mod.LISTINGS_FIELDS if k in existing[pid]}
            if pid in safe_note_map:
                rr["note"] = safe_note_map[pid]
            projected_rows.append(rr)
        else:
            projected_rows.append(derived[pid])

    new_rows = [derived[pid] for pid in new_ids]
    new_rows_sanitized = sanitize_new_rows(mod, new_rows)

    new_id_set = set(new_ids)
    soggetti_norm = mod.normalize_soggetti_rows(soggetti_rows)
    soggetti_new = [r for r in soggetti_norm if clean(r.get("pvp_id")) in new_id_set]
    allegati_new = [r for r in allegati_rows if clean(r.get("pvp_id")) in new_id_set]
    allegati_existing = [r for r in allegati_rows if clean(r.get("pvp_id")) in existing_ids]

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    mode = "apply" if apply else "dryrun"
    summary_path = REPORTS_DIR / f"{SUMMARY_PREFIX}_{mode}_{stamp}.json"
    audit_path = REPORTS_DIR / f"{AUDIT_PREFIX}_{mode}_{stamp}.csv"
    new_path = RAW_DIR / f"{NEW_ROWS_PREFIX}_{mode}_{stamp}.jsonl"
    projected_path = RAW_DIR / f"hybrid_api_rows_projected_v12_{mode}_{stamp}.jsonl"

    with audit_path.open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["pvp_id","action","current_note","incoming_note","would_write_note"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(note_actions)

    with new_path.open("w", encoding="utf-8") as f:
        for r in new_rows_sanitized:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    with projected_path.open("w", encoding="utf-8") as f:
        for r in projected_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    apply_counts = Counter()
    if apply:
        confirm = input(
            f"Per applicare digita esattamente IMPORT_NEW_{len(new_ids)}: "
        ).strip()
        if confirm != f"IMPORT_NEW_{len(new_ids)}":
            raise SystemExit("ANNULLATO: conferma non corretta.")

        if new_rows_sanitized:
            mod.upsert_listings(new_rows_sanitized)
            apply_counts["NEW_LISTINGS_INSERTED"] = len(new_rows_sanitized)

        for rec in note_actions:
            val = clean(rec.get("would_write_note"))
            if not val:
                continue
            pid = rec["pvp_id"]
            fresh = fetch_existing(base, key, [pid], mod.LISTINGS_FIELDS).get(pid)
            if not fresh:
                apply_counts["NOTE_SKIP_ROW_MISSING"] += 1
                continue
            if clean(fresh.get("note")) != clean(existing[pid].get("note")):
                apply_counts["NOTE_SKIP_RACE"] += 1
                continue
            patch_note(base, key, pid, val)
            apply_counts["NOTE_PATCHED"] += 1

        if soggetti_new:
            mod.insert_soggetti(soggetti_new)
            apply_counts["NEW_SOGGETTI_INSERTED"] = len(soggetti_new)

        if allegati_new:
            mod.upsert_allegati(allegati_new)
            apply_counts["NEW_ALLEGATI_UPSERTED"] = len(allegati_new)

    summary = {
        "version": "12.0-incremental-immutable-existing",
        "generated_at": datetime.now().astimezone().isoformat(),
        "mode": "APPLY" if apply else "DRY_RUN_ZERO_WRITES",
        "original_importer": importer_path.name,
        "incoming_unique_ids": len(incoming_ids),
        "duplicate_input_id_count": sum(v-1 for v in duplicate_input_ids.values() if v > 1),
        "existing_ids": len(existing_ids),
        "new_ids": len(new_ids),
        "new_id_list": new_ids,
        "note_action_counts": dict(note_counts),
        "safe_note_writes": sum(1 for r in note_actions if clean(r.get("would_write_note"))),
        "new_listing_rows": len(new_rows_sanitized),
        "new_soggetti_rows": len(soggetti_new),
        "new_allegati_rows": len(allegati_new),
        "existing_allegati_seen_but_not_written_v12_0": len(allegati_existing),
        "existing_listing_fields_allowed_to_write": ["note"],
        "existing_listing_fields_forbidden": sorted(GEO_FIELDS_EXISTING_FORBIDDEN),
        "manifest_written": False,
        "google_calls": 0,
        "apply_counts": dict(apply_counts),
        "files": {
            "summary": summary_path.name,
            "note_audit_csv": audit_path.name,
            "new_rows_jsonl": new_path.name,
            "projected_hybrid_jsonl": projected_path.name,
        },
        "notes": [
            "Existing pvp_id are immutable for ordinary importer runs.",
            "No geographic/geocode field of an existing pvp_id is written.",
            "New rows use PVP/API geography without comuni_cap.csv normalization.",
            "No geocode_touched manifest is written by v12.0.",
            "Existing attachments are observed but not written in v12.0 pending explicit policy.",
        ],
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 96)
    print("ASTE EASY - PVP INCREMENTAL IMPORTER v12.0")
    print("MODE:", "APPLY" if apply else "DRY RUN - ZERO DB WRITES")
    print("=" * 96)
    print("incoming_unique_ids:", len(incoming_ids))
    print("existing_ids:", len(existing_ids))
    print("new_ids:", len(new_ids))
    print("note_action_counts:", dict(note_counts))
    print("safe_note_writes:", summary["safe_note_writes"])
    print("new_soggetti_rows:", len(soggetti_new))
    print("new_allegati_rows:", len(allegati_new))
    print("existing_allegati_seen_not_written:", len(allegati_existing))
    print("manifest_written: NO")
    print("google_calls: 0")
    print("apply_counts:", dict(apply_counts))
    print("[OUT]", summary_path.name)
    print("[OUT]", audit_path.name)
    print("[OUT]", new_path.name)
    print("[OUT]", projected_path.name)
    print("[DONE]")



def main_v121():
    ensure_runtime_dirs()
    print("=" * 96, flush=True)
    print("ASTE EASY - PVP INCREMENTAL IMPORTER v12.3", flush=True)
    print("Avvio | default DRY RUN | ZERO Google", flush=True)
    print("=" * 96, flush=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--handoff-manifest",
        default="",
        help="JSONL dei soli nuovi pvp_id da passare al geocoder dopo INSERT",
    )
    args = ap.parse_args()
    apply = bool(args.apply)
    handoff_path = resolve_handoff_path(args.handoff_manifest)
    handoff_written = 0

    load_pipeline_env()
    importer_path = find_original_importer()
    mod = load_original_importer(importer_path)

    base = clean(os.environ.get("SUPABASE_URL"))
    key = clean(os.environ.get("SUPABASE_SERVICE_ROLE"))
    if not base or not key:
        raise SystemExit("ERRORE: SUPABASE_URL / SUPABASE_SERVICE_ROLE mancanti")

    raw_rows = mod.load_jsonl(mod.IN_RAW)
    allegati_rows = mod.load_jsonl(mod.IN_ALLEGATI)
    soggetti_rows = mod.load_jsonl(mod.IN_SOGGETTI)

    allegati_by_id = defaultdict(list)
    for a in allegati_rows:
        allegati_by_id[str(a.get("pvp_id") or "")].append(a)

    raw_by_id = {}
    duplicate_input_ids = Counter()
    for obj in raw_rows:
        pid = clean(obj.get("pvp_id"))
        if not pid:
            continue
        duplicate_input_ids[pid] += 1
        raw_by_id[pid] = obj

    incoming_ids = sorted(raw_by_id, key=lambda x: int(x) if x.isdigit() else x)
    print(f"[2/5] Leggo Supabase per {len(incoming_ids)} ID (solo GET)...", flush=True)
    existing = fetch_existing(base, key, incoming_ids, mod.LISTINGS_FIELDS)
    existing_ids = set(existing)
    print(f"      esistenti={len(existing_ids)} assenti={len(incoming_ids)-len(existing_ids)}", flush=True)
    new_candidate_ids = [pid for pid in incoming_ids if pid not in existing_ids]

    # Existing: deriviamo solo per confrontare la nota; nessun altro campo e' scrivibile.
    derived_existing = {}
    for pid in incoming_ids:
        if pid in existing_ids:
            derived_existing[pid] = mod.derive_listing_row(raw_by_id[pid], allegati_by_id)

    note_actions = []
    note_counts = Counter()
    for pid in incoming_ids:
        if pid not in existing_ids:
            continue
        cur = existing[pid]
        cand = derived_existing[pid]
        action, value = note_decision(cur.get("note"), cand.get("note"))
        note_counts[action] += 1
        note_actions.append({
            "pvp_id": pid,
            "action": action,
            "current_note": clean(cur.get("note")),
            "incoming_note": clean(cand.get("note")),
            "would_write_note": clean(value),
        })

    print("[3/5] Carico ISTAT e classifico i soli ID assenti...", flush=True)
    istat = load_istat_admin(ISTAT_XLSX)
    today = datetime.now().astimezone().date()

    new_candidate_audit = []
    eligible_rows = []
    eligible_ids = []
    decision_counts = Counter()

    for pid in new_candidate_ids:
        row = derive_new_row_pvp_faithful(mod, raw_by_id[pid], allegati_by_id)
        source_snapshot = {
            "data_asta": clean(row.get("data_asta")),
            "comune": clean(row.get("comune")),
            "provincia": clean(row.get("provincia")),
            "sigla": clean(row.get("sigla_provincia")),
            "cap": clean(row.get("cap")),
            "indirizzo": clean(row.get("indirizzo")),
            "has_coords": bool(clean(row.get("latitudine")) and clean(row.get("longitudine"))),
        }
        decision, reason, admin = classify_new_candidate(row, today, istat)
        decision_counts[decision] += 1

        if decision in {"ELIGIBLE_ACTIVE", "ELIGIBLE_EXPIRED_WITHIN_RETENTION"}:
            row["comune"] = admin["comune"]
            row["provincia"] = admin["provincia"]
            row["sigla_provincia"] = admin["sigla_provincia"]
            row["regione"] = admin["regione"]
            # CAP: resta ESATTAMENTE quello PVP; mai riempito da fonti secondarie.
            row["maps_link"] = safe_maps_link(row)
            # Manteniamo il valore storico del parser: la UI/cleanup determina
            # la scadenza dalla data_asta. Non inventiamo nuovi enum DB qui.
            row["status"] = "active"
            eligible_rows.append(row)
            eligible_ids.append(pid)

        auction_d = parse_auction_date(source_snapshot["data_asta"])
        age_days = (today - auction_d).days if auction_d is not None else ""

        new_candidate_audit.append({
            "pvp_id": pid,
            "data_asta": source_snapshot["data_asta"],
            "age_days": age_days,
            "decision": decision,
            "reason": reason,
            "source_comune": source_snapshot["comune"],
            "source_provincia": source_snapshot["provincia"],
            "source_sigla": source_snapshot["sigla"],
            "source_cap": source_snapshot["cap"],
            "source_indirizzo": source_snapshot["indirizzo"],
            "has_pvp_coords": "YES" if source_snapshot["has_coords"] else "NO",
            "canonical_comune": clean((admin or {}).get("comune")),
            "canonical_provincia": clean((admin or {}).get("provincia")),
            "canonical_sigla": clean((admin or {}).get("sigla_provincia")),
            "canonical_regione": clean((admin or {}).get("regione")),
        })

    print(f"      decisioni={dict(decision_counts)}", flush=True)
    eligible_rows_sanitized = sanitize_new_rows(mod, eligible_rows)
    print(f"[4/5] Candidati inseribili={len(eligible_rows_sanitized)}", flush=True)
    eligible_set = set(eligible_ids)

    soggetti_norm = mod.normalize_soggetti_rows(soggetti_rows)
    soggetti_new = [r for r in soggetti_norm if clean(r.get("pvp_id")) in eligible_set]
    allegati_new = [r for r in allegati_rows if clean(r.get("pvp_id")) in eligible_set]
    allegati_existing = [r for r in allegati_rows if clean(r.get("pvp_id")) in existing_ids]

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    mode = "apply" if apply else "dryrun"
    summary_path = REPORTS_DIR / f"pvp_import_incremental_v12_3_{mode}_{stamp}.json"
    note_audit_path = REPORTS_DIR / f"pvp_import_incremental_v12_3_notes_{mode}_{stamp}.csv"
    new_audit_path = REPORTS_DIR / f"pvp_import_incremental_v12_3_new_candidates_{mode}_{stamp}.csv"
    new_path = RAW_DIR / f"pvp_new_rows_v12_3_{mode}_{stamp}.jsonl"

    with note_audit_path.open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["pvp_id","action","current_note","incoming_note","would_write_note"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(note_actions)

    with new_audit_path.open("w", encoding="utf-8-sig", newline="") as f:
        fields = [
            "pvp_id","data_asta","age_days","decision","reason",
            "source_comune","source_provincia","source_sigla","source_cap",
            "source_indirizzo","has_pvp_coords",
            "canonical_comune","canonical_provincia","canonical_sigla","canonical_regione",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(new_candidate_audit)

    with new_path.open("w", encoding="utf-8") as f:
        for r in eligible_rows_sanitized:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    apply_counts = Counter()
    if apply and handoff_path is not None:
        # Clear this run's manifest before any INSERT so stale IDs cannot leak.
        write_geocode_handoff(handoff_path, [])

    if apply:
        confirm = input(
            f"Per applicare digita esattamente IMPORT_NEW_{len(eligible_ids)}: "
        ).strip()
        if confirm != f"IMPORT_NEW_{len(eligible_ids)}":
            raise SystemExit("ANNULLATO: conferma non corretta.")

        if eligible_rows_sanitized:
            mod.upsert_listings(eligible_rows_sanitized)
            apply_counts["NEW_LISTINGS_INSERTED"] = len(eligible_rows_sanitized)
            if handoff_path is not None:
                handoff_written = write_geocode_handoff(handoff_path, eligible_ids)
                apply_counts["GEOCODE_HANDOFF_IDS"] = handoff_written

        for rec in note_actions:
            val = clean(rec.get("would_write_note"))
            if not val:
                continue
            pid = rec["pvp_id"]
            fresh = fetch_existing(base, key, [pid], mod.LISTINGS_FIELDS).get(pid)
            if not fresh:
                apply_counts["NOTE_SKIP_ROW_MISSING"] += 1
                continue
            if clean(fresh.get("note")) != clean(existing[pid].get("note")):
                apply_counts["NOTE_SKIP_RACE"] += 1
                continue
            patch_note(base, key, pid, val)
            apply_counts["NOTE_PATCHED"] += 1

        if soggetti_new:
            mod.insert_soggetti(soggetti_new)
            apply_counts["NEW_SOGGETTI_INSERTED"] = len(soggetti_new)

        if allegati_new:
            mod.upsert_allegati(allegati_new)
            apply_counts["NEW_ALLEGATI_UPSERTED"] = len(allegati_new)

    print("[5/5] Scrivo report locali...", flush=True)

    summary = {
        "version": "12.3.1-incremental-retention-geocode-handoff",
        "generated_at": datetime.now().astimezone().isoformat(),
        "mode": "APPLY" if apply else "DRY_RUN_ZERO_WRITES",
        "today_local": today.isoformat(),
        "original_importer": importer_path.name,
        "incoming_unique_ids": len(incoming_ids),
        "duplicate_input_id_count": sum(v-1 for v in duplicate_input_ids.values() if v > 1),
        "existing_ids": len(existing_ids),
        "new_candidates_absent_from_db": len(new_candidate_ids),
        "new_candidate_decisions": dict(decision_counts),
        "eligible_new_ids": eligible_ids,
        "eligible_new_count": len(eligible_ids),
        "note_action_counts": dict(note_counts),
        "safe_note_writes": sum(1 for r in note_actions if clean(r.get("would_write_note"))),
        "istat": {
            "file": ISTAT_XLSX.name,
            "loaded": bool(istat.get("ok")),
            "reason": clean(istat.get("reason")),
        },
        "new_soggetti_rows": len(soggetti_new),
        "new_allegati_rows": len(allegati_new),
        "existing_allegati_seen_but_not_written": len(allegati_existing),
        "existing_listing_fields_allowed_to_write": ["note"],
        "existing_listing_geo_fields_forbidden": sorted(GEO_FIELDS_EXISTING_FORBIDDEN),
        "cap_policy": "PVP_ONLY_PRESERVE_MISSING_NEVER_FILL",
        "expired_new_policy": "1_TO_14_DAYS_ELIGIBLE__OVER_14_DAYS_SKIP",
        "expired_retention_days": RETENTION_DAYS,
        "far_future_review_days": MAX_FUTURE_AUCTION_DAYS,
        "favorite_snapshot_policy": "existing_DB_cleanup_trigger_handles_snapshot_on_delete",
        "unknown_auction_date_policy": "SKIP_REVIEW",
        "legacy_geocode_touched_manifest_written": False,
        "geocode_handoff_manifest": handoff_path.name if handoff_path else "",
        "geocode_handoff_ids": handoff_written,
        "google_calls": 0,
        "apply_counts": dict(apply_counts),
        "files": {
            "summary": summary_path.name,
            "note_audit_csv": note_audit_path.name,
            "new_candidate_audit_csv": new_audit_path.name,
            "eligible_new_rows_jsonl": new_path.name,
            "geocode_handoff_manifest": handoff_path.name if handoff_path else "",
        },
        "notes": [
            "Existing pvp_id remain immutable except conservative note updates.",
            "The optional geocode handoff is written only after successful INSERT of eligible new listings.",
            "The handoff marks inserted DB rows as fresh geographic input for unified geocoder v11.6.1.",
            "Legacy geocode_touched_v11.jsonl is not written.",
        ],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 96)
    print("ASTE EASY - PVP INCREMENTAL IMPORTER v12.2")
    print("MODE:", "APPLY" if apply else "DRY RUN - ZERO DB WRITES")
    print("=" * 96)
    print("today_local:", today.isoformat())
    print("incoming_unique_ids:", len(incoming_ids))
    print("existing_ids:", len(existing_ids))
    print("new_candidates_absent_from_db:", len(new_candidate_ids))
    print("new_candidate_decisions:", dict(decision_counts))
    print("eligible_new_count:", len(eligible_ids))
    print("note_action_counts:", dict(note_counts))
    print("safe_note_writes:", summary["safe_note_writes"])
    print("istat:", summary["istat"])
    print("legacy_manifest_written: NO")
    print("geocode_handoff_manifest:", handoff_path.name if handoff_path else "")
    print("geocode_handoff_ids:", handoff_written)
    print("google_calls: 0")
    print("apply_counts:", dict(apply_counts))
    print("[OUT]", summary_path.name)
    print("[OUT]", note_audit_path.name)
    print("[OUT]", new_audit_path.name)
    print("[OUT]", new_path.name)
    print("[DONE]")

if __name__ == "__main__":
    main_v121()
