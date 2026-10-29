#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy - Unified geocoder v11.6.4 PLAN.

ZERO PATCH Supabase. ZERO Google API.

Fix cumulativi v11.1 + v11.2:
- scope=touched include anche i PVP nuovi non ancora presenti in Supabase,
  usando hybrid_api_rows_full.jsonl;
- per record nuovi/cambiati valuta il NUOVO input PVP (indirizzo/comune/CAP/
  coordinate PVP), non la vecchia riga DB;
- mantiene separato lo stato geocode corrente del DB;
- con coordinate PVP valide applica PVP-first: prima conferma locale, poi
  eventuale arbitraggio/validazione Google; ANNCSU non sovrascrive da solo
  un pin PVP valido solo perché esiste un civico ufficiale.
- v11.2 canonicalizza Comune/sigla con ISTAT corrente PRIMA di ANNCSU/Google;
- comuni_cap.csv viene usato solo come supporto CAP;
- la coda amministrativa sporca dell'indirizzo viene rimossa prima del matching;
- il report mostra il preview delle query Google senza chiamare alcuna API.
- v11.6.4: localita-only puo usare ANNCSU diretto; localita + strada/civico e specific-first.
- v11.6.4: localita esplicite ambigue/non confermate falliscono in modo conservativo.
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
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
SRC_ROOT = BASE_DIR.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from aste_paths import HANDOFF_DIR, RAW_DIR, REPORTS_DIR, ensure_runtime_dirs, env_path
from project_env import load_project_env as load_common_project_env

MANIFEST = env_path("UNIFIED_TOUCHED_MANIFEST", HANDOFF_DIR / "geocode_touched_v11.jsonl", relative_to=HANDOFF_DIR)
HYBRID = env_path("UNIFIED_HYBRID_FILE", RAW_DIR / "hybrid_api_rows_full.jsonl", relative_to=RAW_DIR)
SCOPE = os.environ.get("UNIFIED_SCOPE", "touched").strip().lower()
PVP_ID = os.environ.get("UNIFIED_PVP_ID", "").strip()
LIMIT = int(os.environ.get("UNIFIED_LIMIT", "0") or "0")
TOP_STREETS = int(os.environ.get("UNIFIED_TOP_STREETS", "6") or "6")
TIMEOUT = 60
MUNICIPALITY_GROSS_OUTSIDE_M = float(os.environ.get("UNIFIED_MUNICIPALITY_GROSS_OUTSIDE_M", "5000") or "5000")

SELECT_FIELDS = ",".join([
    "pvp_id", "indirizzo", "comune", "sigla_provincia", "cap",
    "latitudine", "longitudine", "maps_link",
    "geocode_location_type", "geocode_formatted_address",
    "geocode_source", "geocode_query_used",
])

PVP_AUDITED_TYPES = {"PVP_OK_CIVICO", "PVP_OK_STRADA", "PVP_SUSPECT", "PVP_UNCERTAIN"}

ADMIN_FILE = BASE_DIR / "geo_admin_canonical_v11_3.py"
GOOGLE_CORE_FILE = BASE_DIR / "google_geocode_core_v9_3_6.py"
BOUNDARY_FILE = BASE_DIR / "istat_boundary_runtime_v11_6.py"


def _load_module_file(path, name):
    if not path.exists():
        raise SystemExit(f"ERRORE: manca {path.name} nella cartella del progetto")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"ERRORE: impossibile caricare {path.name}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


local = _load_module_file(
    BASE_DIR / "anncsu_runtime_v11_2.py",
    "asteeasy_anncsu_runtime_v112",
)
admin = _load_module_file(
    ADMIN_FILE,
    "asteeasy_geo_admin_v112",
)
google_core = _load_module_file(
    GOOGLE_CORE_FILE,
    "asteeasy_google_core_v936_plan",
)
boundary = _load_module_file(
    BOUNDARY_FILE,
    "asteeasy_istat_boundary_runtime_v116",
)


def clean(v):
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v).replace("\u00a0", " ")).strip()


def fnum(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def valid_coords(row):
    lat, lon = fnum(row.get("latitudine")), fnum(row.get("longitudine"))
    return (
        lat is not None and lon is not None
        and -90 <= lat <= 90 and -180 <= lon <= 180
        and not (lat == 0 and lon == 0)
    )


def load_env():
    load_common_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE"))
    url = clean(os.environ.get("SUPABASE_URL")).rstrip("/")
    key = clean(os.environ.get("SUPABASE_SERVICE_ROLE"))
    return url, key


def headers(key):
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }


def get_json(url, key):
    req = Request(url, method="GET", headers=headers(key))
    with urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace") or "[]")


def read_jsonl(path):
    out = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def by_pvp(rows):
    out = {}
    for r in rows:
        pid = clean(r.get("pvp_id"))
        if pid:
            out[pid] = r
    return out


def touched_ids_and_meta():
    meta = by_pvp(read_jsonl(MANIFEST))
    if meta:
        return list(meta), meta, MANIFEST.name

    hybrid = by_pvp(read_jsonl(HYBRID))
    if hybrid:
        return list(hybrid), {}, HYBRID.name
    return [], {}, ""


def fetch_by_ids_map(base, key, ids):
    out = {}
    for i in range(0, len(ids), 100):
        batch = ids[i:i+100]
        vals = ",".join(re.sub(r"\D", "", x) for x in batch if re.sub(r"\D", "", x))
        if not vals:
            continue
        url = (
            f"{base}/rest/v1/listings"
            f"?select={quote(SELECT_FIELDS, safe=',')}"
            f"&pvp_id=in.({quote(vals, safe=',')})"
        )
        data = get_json(url, key)
        if not isinstance(data, list):
            raise RuntimeError("Risposta Supabase inattesa")
        for r in data:
            pid = clean(r.get("pvp_id"))
            if pid:
                out[pid] = r
    return out


def fetch_missing(base, key):
    out = []
    offset = 0
    while True:
        url = (
            f"{base}/rest/v1/listings?select={quote(SELECT_FIELDS, safe=',')}"
            f"&or=(latitudine.is.null,longitudine.is.null,and(latitudine.eq.0,longitudine.eq.0))"
            f"&order=pvp_id.asc.nullslast"
            f"&limit=1000&offset={offset}"
        )
        page = get_json(url, key)
        if not isinstance(page, list):
            raise RuntimeError("Risposta Supabase inattesa")
        out.extend(page)
        if len(page) < 1000:
            break
        offset += len(page)
        if LIMIT and len(out) >= LIMIT:
            break
    return out[:LIMIT] if LIMIT else out


def fetch_all(base, key):
    out = []
    offset = 0
    while True:
        url = (
            f"{base}/rest/v1/listings"
            f"?select={quote(SELECT_FIELDS, safe=',')}"
            f"&order=pvp_id.asc.nullslast"
            f"&limit=1000&offset={offset}"
        )
        page = get_json(url, key)
        if not isinstance(page, list):
            raise RuntimeError("Risposta Supabase inattesa")
        out.extend(page)
        if len(page) < 1000:
            break
        offset += len(page)
        if LIMIT and len(out) >= LIMIT:
            break
    return out[:LIMIT] if LIMIT else out


def assert_unique_pvp_rows(rows, label):
    """Fail closed if a paged Supabase read contains duplicate pvp_id values."""
    seen = set()
    dup = []
    for r in rows:
        pid = clean((r or {}).get("pvp_id"))
        if not pid:
            continue
        if pid in seen:
            dup.append(pid)
        else:
            seen.add(pid)
    if dup:
        sample = ", ".join(sorted(set(dup))[:20])
        raise SystemExit(
            f"ERRORE QA: {label} contiene {len(dup)} righe duplicate per pvp_id "
            f"({len(set(dup))} ID distinti; esempio: {sample}). "
            "Audit interrotto: nessun Google, nessun PATCH."
        )
    return rows


def incoming_candidate(incoming):
    """Create a clean PVP-source candidate, without stale geocode metadata."""
    r = dict(incoming or {})
    for k in (
        "geocode_source",
        "geocode_location_type",
        "geocode_formatted_address",
        "geocode_query_used",
    ):
        r.pop(k, None)
    return r


def load_targets(base, key):
    """Return target items: row used for local evaluation + current DB state."""
    if PVP_ID:
        current = fetch_by_ids_map(base, key, [PVP_ID])
        row = current.get(PVP_ID)
        return (
            [{
                "row": row,
                "current": row,
                "incoming": {},
                "meta": {},
                "row_basis": "CURRENT_DB",
            }] if row else [],
            "explicit_pvp_id",
            {},
        )

    if SCOPE == "touched":
        ids, meta, src = touched_ids_and_meta()
        if LIMIT:
            ids = ids[:LIMIT]
        if not ids:
            raise SystemExit(
                "ERRORE: nessun manifest/hybrid touched trovato. "
                "Esegui prima importer/manifest v11."
            )

        current_by = fetch_by_ids_map(base, key, ids)
        incoming_by = by_pvp(read_jsonl(HYBRID))

        items = []
        missing_everywhere = 0
        for pid in ids:
            m = meta.get(pid, {})
            current = current_by.get(pid, {})
            incoming = incoming_by.get(pid, {})
            existing_flag = clean(m.get("existing")).upper()
            changed = clean(m.get("geographic_input_changed")).upper() == "YES"

            if existing_flag == "NO" or not current:
                row = incoming_candidate(incoming)
                basis = "INCOMING_NEW"
            elif changed:
                if incoming:
                    row = incoming_candidate(incoming)
                    basis = "INCOMING_CHANGED"
                else:
                    row = dict(current)
                    basis = "CURRENT_DB_FALLBACK_CHANGED_NO_HYBRID"
            else:
                row = dict(current)
                basis = "CURRENT_DB_UNCHANGED"

            if not row:
                missing_everywhere += 1
                continue
            row["pvp_id"] = pid
            items.append({
                "row": row,
                "current": current,
                "incoming": incoming,
                "meta": m,
                "row_basis": basis,
            })

        load_meta = {
            "manifest_ids": len(ids),
            "db_existing_found": len(current_by),
            "incoming_found": sum(1 for pid in ids if pid in incoming_by),
            "target_items": len(items),
            "target_missing_everywhere": missing_everywhere,
            "row_basis": dict(Counter(x["row_basis"] for x in items)),
        }
        return items, src, load_meta

    if SCOPE == "missing":
        rows = assert_unique_pvp_rows(fetch_missing(base, key), "scope=missing")
        return [
            {"row": r, "current": r, "incoming": {}, "meta": {}, "row_basis": "CURRENT_DB"}
            for r in rows
        ], "supabase_missing", {}

    if SCOPE == "all":
        rows = assert_unique_pvp_rows(fetch_all(base, key), "scope=all")
        return [
            {"row": r, "current": r, "incoming": {}, "meta": {}, "row_basis": "CURRENT_DB"}
            for r in rows
        ], "supabase_all", {}

    raise SystemExit("UNIFIED_SCOPE valido: touched | missing | all")


def skip_reason(current, meta):
    if not meta or not current:
        return ""
    existing = clean(meta.get("existing")).upper() == "YES"
    changed = clean(meta.get("geographic_input_changed")).upper() == "YES"
    coord_changed = clean(meta.get("pvp_coordinates_changed")).upper() == "YES"
    src = clean(current.get("geocode_source"))
    lt = clean(current.get("geocode_location_type")).upper()

    if existing and src and not changed:
        return "SKIP_PROTECTED_UNCHANGED"
    if (
        existing and not src and lt in PVP_AUDITED_TYPES
        and not changed and not coord_changed
    ):
        return "SKIP_AUDITED_PVP_UNCHANGED"
    return ""


def is_fresh_geographic_input(meta):
    if not meta:
        return False
    return (
        clean(meta.get("existing")).upper() != "YES"
        or clean(meta.get("geographic_input_changed")).upper() == "YES"
    )


def ann_method(ev):
    s = clean(ev.get("anncsu_metodo"))
    return s[:-2] if s.endswith(".0") else s


def local_confirms_candidate(ev):
    return (
        clean(ev.get("anncsu_decision")).upper() == "LOCAL_ACCEPT"
        or clean(ev.get("candidate_tier")).upper() == "KEEP_CONFIRMED"
    )


def municipality_guard_status(ev):
    """Gross local territorial guard from ANNCSU municipality address bbox.

    It is not an administrative polygon:
    > threshold => strong conflict;
    0..threshold => review only;
    inside => no local territorial alarm.
    Google reverse remains the hard municipality verifier in LIVE.
    """
    bbox = fnum(ev.get("distance_outside_municipality_bbox_m"))
    if bbox is None:
        return "UNKNOWN", ""
    if bbox > MUNICIPALITY_GROSS_OUTSIDE_M:
        return "GROSS_OUTSIDE", f"{bbox:.1f}"
    if bbox > 0:
        return "OUTSIDE_BBOX_REVIEW", f"{bbox:.1f}"
    return "INSIDE_BBOX", f"{bbox:.1f}"


def gross_municipality_conflict(ev):
    return municipality_guard_status(ev)[0] == "GROSS_OUTSIDE"


def strong_conflict(ev):
    bbox = fnum(ev.get("distance_outside_municipality_bbox_m"))
    tier = clean(ev.get("candidate_tier")).upper()
    priority = str(ev.get("priority", "")).strip()
    audit = clean(ev.get("audit_result")).upper()

    if bbox is not None and bbox > 5000:
        return True
    if tier in {"A_STRONG_DIRECT", "C_TERRITORIAL_STRONG", "C_TERRITORIAL_REVIEW"}:
        return True
    if audit.startswith("CRITICAL_"):
        return True
    try:
        if float(priority) >= 90:
            return True
    except Exception:
        pass
    return False


def google_should_use_comune_only(row):
    """True when the address core has no meaningful geocodable place/street text."""
    raw = clean(row.get("indirizzo"))
    if not raw:
        return True

    n = raw.casefold().replace("’", "'")
    # If a real street/locality descriptor survives, let Google try the address.
    real_hints = (
        "via ", "viale ", "vicolo ", "piazza ", "corso ", "strada ",
        "contrada ", "c/da", "c.da", "località ", "localita ",
        "frazione ", "borgata ", "vocabolo ", "zona ", "ss ", "sp ",
    )
    if any(x in f" {n} " for x in real_hints):
        return False

    # Remove cadastral-only vocabulary, numbers and punctuation.
    s = re.sub(
        r"(?i)\b(catasto|catastale|foglio|fg\.?|part\.?|particella|mappale|"
        r"sub\.?|subalterno|numero|n\.?|nr\.?|al|alla|del|della|dei|delle)\b",
        " ",
        raw,
    )
    s = re.sub(r"\d+", " ", s)
    s = re.sub(r"[^A-Za-zÀ-ÿ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return len(s) < 3


def plan_one(row, ev, meta, current, istat_position='', admin_status='RESOLVED'):
    fresh = is_fresh_geographic_input(meta)
    valid = valid_coords(row)
    pos = clean(istat_position).upper()
    adm = clean(admin_status).upper()

    # Fail closed: without a canonical ISTAT municipality we do not spend Google
    # and we do not bless/write coordinates.
    if adm != "RESOLVED":
        return "REVIEW_ADMIN_UNRESOLVED", "canonical_municipality_unresolved"

    # NEW/CHANGED input: PVP first.
    if fresh:
        if valid:
            # v11.6: official ISTAT polygon is the territorial authority.
            if pos == "OUTSIDE":
                # Local authoritative data first: if ANNCSU already gives a
                # conservative civic/street inside the canonical municipality,
                # Google is unnecessary.
                if local.direct_civic_for_missing(ev):
                    return "LOCAL_BOUNDARY_CIVICO_READY", (
                        "istat_outside__exact_unique_anncsu_method_1_3"
                    )
                ok, why = local.street_for_missing(row, ev)
                if ok:
                    return "LOCAL_BOUNDARY_STRADA_READY", (
                        "istat_outside__" + clean(why)
                    )
                lok, lwhy = local.locality_for_missing(row, ev, allow_fallback=False)
                if lok:
                    return "LOCAL_BOUNDARY_LOCALITA_READY", "istat_outside__" + clean(lwhy)
                return "GOOGLE_ARBITRATION_NEW_PVP", "istat_boundary_outside"
            # ANNCSU bbox remains only a secondary diagnostic guard.
            if gross_municipality_conflict(ev):
                bbox = fnum(ev.get("distance_outside_municipality_bbox_m"))
                return "GOOGLE_ARBITRATION_NEW_PVP", (
                    f"gross_municipality_conflict_bbox_m={bbox:.1f}"
                    if bbox is not None else "gross_municipality_conflict"
                )
            if local_confirms_candidate(ev):
                return "KEEP_PVP_LOCAL_CONFIRMED", (
                    clean(ev.get("audit_result")) or "anncsu_local_accept"
                )
            if strong_conflict(ev):
                return "GOOGLE_ARBITRATION_NEW_PVP", (
                    clean(ev.get("audit_result")) or "strong_local_conflict"
                )
            return "GOOGLE_VALIDATE_NEW_PVP", (
                clean(ev.get("audit_result")) or "local_not_conclusive"
            )

        # No usable PVP coordinates: ANNCSU can supply the point directly.
        if local.direct_civic_for_missing(ev):
            return "LOCAL_CIVICO_READY", "exact_unique_anncsu_method_1_3"
        ok, why = local.street_for_missing(row, ev)
        if ok:
            return "LOCAL_STRADA_READY", why
        lok, lwhy = local.locality_for_missing(row, ev, allow_fallback=False)
        if lok:
            return "LOCAL_LOCALITA_READY", lwhy
        if google_should_use_comune_only(row):
            return "GOOGLE_COMUNE_ONLY", "no_geocodable_address_core"
        return "GOOGLE_NEEDED_MISSING", "local_not_sufficient"

    # Existing unchanged row without usable coordinates.
    if not valid:
        if local.direct_civic_for_missing(ev):
            return "LOCAL_CIVICO_READY", "exact_unique_anncsu_method_1_3"
        ok, why = local.street_for_missing(row, ev)
        if ok:
            return "LOCAL_STRADA_READY", why
        lok, lwhy = local.locality_for_missing(row, ev, allow_fallback=False)
        if lok:
            return "LOCAL_LOCALITA_READY", lwhy
        if google_should_use_comune_only(row):
            return "GOOGLE_COMUNE_ONLY", "no_geocodable_address_core"
        return "GOOGLE_NEEDED_MISSING", "local_not_sufficient"

    # Existing unchanged valid pin, not already skipped.
    if pos == "OUTSIDE":
        if local.direct_civic_for_missing(ev):
            return "LOCAL_BOUNDARY_CIVICO_READY", (
                "istat_outside__exact_unique_anncsu_method_1_3"
            )
        ok, why = local.street_for_missing(row, ev)
        if ok:
            return "LOCAL_BOUNDARY_STRADA_READY", (
                "istat_outside__" + clean(why)
            )
        lok, lwhy = local.locality_for_missing(row, ev, allow_fallback=False)
        if lok:
            return "LOCAL_BOUNDARY_LOCALITA_READY", "istat_outside__" + clean(lwhy)
        return "GOOGLE_ARBITRATION_EXISTING", "istat_boundary_outside"

    current_src = clean((current or row).get("geocode_source"))
    current_lt = clean((current or row).get("geocode_location_type")).upper()
    bbox = fnum(ev.get("distance_outside_municipality_bbox_m"))
    tier = clean(ev.get("candidate_tier")).upper()

    # Gross territorial conflict wins over local confirmation.
    if gross_municipality_conflict(ev):
        return "GOOGLE_ARBITRATION_EXISTING", (
            f"gross_municipality_conflict_bbox_m={bbox:.1f}"
            if bbox is not None else "gross_municipality_conflict"
        )

    if local_confirms_candidate(ev):
        return "KEEP_CURRENT_LOCAL_CONFIRMED", (
            clean(ev.get("audit_result")) or "anncsu_local_accept"
        )

    if (
        current_lt == "PVP_SUSPECT"
        or tier in {"A_STRONG_DIRECT", "C_TERRITORIAL_STRONG", "C_TERRITORIAL_REVIEW"}
    ):
        return "GOOGLE_ARBITRATION_EXISTING", (
            clean(ev.get("audit_result")) or "territorial_or_suspect"
        )

    if current_src:
        return "KEEP_PROTECTED_NEEDS_NO_CHANGE", "protected_same_input"
    return "GOOGLE_VALIDATE_PVP", (
        clean(ev.get("audit_result")) or "no_strong_local_confirmation"
    )


def main():
    print("=" * 92)
    print("ASTE EASY - UNIFIED GEOCODER v11.6.4 PLAN")
    print("ZERO Google API | ZERO Supabase PATCH | ANNCSU LOCALITA v11.6.3")
    print(f"scope={SCOPE} pvp_id={PVP_ID or '-'} limit={LIMIT or 'all'}")
    print("=" * 92)

    base, key = load_env()
    items, target_source, load_meta = load_targets(base, key)
    print(f"[TARGET] {len(items)} record da {target_source}")
    if load_meta:
        print(
            "[TOUCHED] "
            f"manifest={load_meta.get('manifest_ids',0)} "
            f"db={load_meta.get('db_existing_found',0)} "
            f"hybrid={load_meta.get('incoming_found',0)}"
        )
        print(f"[BASIS] {load_meta.get('row_basis',{})}")

    immediate = []
    eval_items = []

    # v11.6: canonicalizzazione + boundary guard PRIMA degli skip protetti.
    # In questo modo una coordinata fuori Comune non può essere saltata soltanto
    # perché ha già geocode_source.
    admin_ctx = admin.AdminContext()
    boundary_ctx = boundary.get_context(admin_ctx=admin_ctx)
    admin_counts = Counter()
    boundary_counts = Counter()

    for item in items:
        item["source_row"] = dict(item["row"])
        row_for_admin = dict(item["row"])
        row_for_admin["_v11_row_basis"] = item["row_basis"]
        canonical_row, admin_meta = admin_ctx.canonicalize_row(row_for_admin)
        canonical_row.pop("_v11_row_basis", None)
        item["row"] = canonical_row
        item["admin_meta"] = admin_meta

        admin_counts["RESOLVED" if admin_meta.get("status") == "RESOLVED" else "UNRESOLVED"] += 1
        if admin_meta.get("resolution") == "ALIAS":
            admin_counts["ALIAS"] += 1
        if admin_meta.get("province_changed") == "YES":
            admin_counts["PROVINCE_CHANGED"] += 1
        if admin_meta.get("comune_changed") == "YES":
            admin_counts["COMUNE_CHANGED"] += 1
        if admin_meta.get("cap_changed") == "YES":
            admin_counts["CAP_CHANGED"] += 1
        if admin_meta.get("address_tail_changed") == "YES":
            admin_counts["ADDRESS_TAIL_STRIPPED"] += 1
        admin_counts["CAP_SOURCE:" + clean(admin_meta.get("cap_source"))] += 1

        if valid_coords(canonical_row):
            btest = boundary_ctx.test(canonical_row, admin_meta=admin_meta)
        else:
            btest = {
                "position": "NO_COORDS",
                "safe": False,
                "canonical_code": clean(admin_meta.get("canonical_code")),
                "utm32_xy": "",
            }
        item["boundary_test"] = btest
        boundary_counts[clean(btest.get("position")) or "UNKNOWN"] += 1

        reason = skip_reason(item["current"], item["meta"])
        if reason and clean(btest.get("position")).upper() != "OUTSIDE":
            immediate.append({
                **item,
                "plan": reason,
                "plan_reason": "unchanged_previous_geocode_state",
            })
        else:
            eval_items.append(item)

    print(f"[ADMIN] {dict(admin_counts)}")
    print(f"[ISTAT BOUNDARY] {dict(boundary_counts)}")
    print(f"[SKIP] già chiusi/invariati: {len(immediate)}")
    print(f"[LOCAL EVAL] da valutare: {len(eval_items)}")

    eval_rows = [x["row"] for x in eval_items]
    evidence = []
    runtime_meta = {"rows": 0}
    if eval_rows:
        evidence, runtime_meta = local.build_local_evidence(
            eval_rows, top_streets=TOP_STREETS
        )
    ev_by = {clean(x.get("pvp_id")): x for x in evidence}

    planned = []
    for item in immediate:
        planned.append((
            item, {}, item["plan"], item["plan_reason"]
        ))

    for item in eval_items:
        pid = clean(item["row"].get("pvp_id"))
        ev = ev_by.get(pid, {})
        plan, why = plan_one(
            item["row"], ev, item["meta"], item["current"],
            istat_position=clean((item.get("boundary_test") or {}).get("position")),
            admin_status=clean((item.get("admin_meta") or {}).get("status")),
        )
        planned.append((item, ev, plan, why))

    order = {
        clean(item["row"].get("pvp_id")): i
        for i, item in enumerate(items)
    }
    planned.sort(
        key=lambda x: order.get(clean(x[0]["row"].get("pvp_id")), 999999999)
    )

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    ensure_runtime_dirs()
    csv_path = REPORTS_DIR / f"geocode_unified_plan_v11_6_4_{stamp}.csv"
    summary_path = REPORTS_DIR / f"geocode_unified_plan_summary_v11_6_4_{stamp}.json"

    fields = [
        "pvp_id","plan","plan_reason","row_basis",
        "existing","geographic_input_changed","pvp_coordinates_changed",
        "source_indirizzo","source_comune","source_sigla_provincia","source_cap",
        "admin_status","admin_resolution","admin_alias_target",
        "istat_boundary_position","istat_boundary_safe","istat_boundary_code",
        "candidate_indirizzo","candidate_comune","candidate_sigla_provincia","candidate_cap",
        "canonical_region","canonical_istat_code","canonical_catasto",
        "cap_source","cap_candidates","cap_embedded_in_address","trusted_incoming_cap","province_changed","comune_changed","cap_changed",
        "google_address_core","google_query_preview","google_free_query_preview","google_fallback_preview",
        "candidate_latitudine","candidate_longitudine",
        "current_latitudine","current_longitudine",
        "current_geocode_source","current_geocode_location_type",
        "audit_result","priority","candidate_tier","anncsu_decision",
        "street_score","strict_street_similarity","direct_locality_guard",
        "ambiguity","civic_relation","anncsu_odonimo","anncsu_civic",
        "anncsu_metodo","anncsu_lat","anncsu_lon",
        "municipality_guard","distance_outside_municipality_bbox_m",
        "matching_street_candidates","matching_access_candidates",
        "street_rep_lat","street_rep_lon",
        "street_rep_unique_coords","street_rep_p90_m",
        "runtime_anncsu_locality","runtime_locality_match_mode","runtime_locality_hint_reason",
        "runtime_locality_guard_reason","runtime_locality_source_hints_count",
        "runtime_locality_explicit_hints_count","runtime_locality_explicit_hints",
        "runtime_locality_source_specificity","runtime_locality_accesses","runtime_locality_p90_m",
        "runtime_locality_lat","runtime_locality_lon",
    ]

    records = []
    for item, ev, plan, why in planned:
        r = item["row"]
        source_r = item.get("source_row") or r
        current = item["current"] or {}
        m = item["meta"] or {}
        am = item.get("admin_meta") or {}
        rep = ev.get("street_representative") or {}

        constrained = google_core.constrained_address_variants(r) if plan.startswith("GOOGLE_") else []
        free_variants, fallback = google_core.build_query_variants(r) if plan.startswith("GOOGLE_") else ([], "")
        locality_preview = " ".join(
            x for x in [clean(r.get("cap")), clean(r.get("comune")), clean(r.get("sigla_provincia"))]
            if x
        )
        google_address_core = constrained[0] if constrained else clean(r.get("indirizzo"))
        google_query_preview = (
            f"{google_address_core} [vincoli: {locality_preview or 'Italia'}]"
            if plan.startswith("GOOGLE_") and google_address_core
            else (locality_preview if plan.startswith("GOOGLE_") else "")
        )

        records.append({
            "pvp_id": clean(r.get("pvp_id")),
            "plan": plan,
            "plan_reason": why,
            "row_basis": item["row_basis"],
            "existing": clean(m.get("existing")),
            "geographic_input_changed": clean(m.get("geographic_input_changed")),
            "pvp_coordinates_changed": clean(m.get("pvp_coordinates_changed")),
            "source_indirizzo": clean(source_r.get("indirizzo")),
            "source_comune": clean(source_r.get("comune")),
            "source_sigla_provincia": clean(source_r.get("sigla_provincia")),
            "source_cap": clean(source_r.get("cap")),
            "admin_status": clean(am.get("status")),
            "admin_resolution": clean(am.get("resolution")),
            "admin_alias_target": clean(am.get("alias_target")),
            "istat_boundary_position": clean((item.get("boundary_test") or {}).get("position")),
            "istat_boundary_safe": "YES" if (item.get("boundary_test") or {}).get("safe") else "NO",
            "istat_boundary_code": clean((item.get("boundary_test") or {}).get("canonical_code")),
            "candidate_indirizzo": clean(r.get("indirizzo")),
            "candidate_comune": clean(r.get("comune")),
            "candidate_sigla_provincia": clean(r.get("sigla_provincia")),
            "candidate_cap": clean(r.get("cap")),
            "canonical_region": clean(am.get("canonical_region")),
            "canonical_istat_code": clean(am.get("canonical_code")),
            "canonical_catasto": clean(am.get("canonical_catasto")),
            "cap_source": clean(am.get("cap_source")),
            "cap_candidates": clean(am.get("cap_candidates")),
            "cap_embedded_in_address": clean(am.get("cap_embedded_in_address")),
            "trusted_incoming_cap": clean(am.get("trusted_incoming_cap")),
            "province_changed": clean(am.get("province_changed")),
            "comune_changed": clean(am.get("comune_changed")),
            "cap_changed": clean(am.get("cap_changed")),
            "google_address_core": google_address_core if plan.startswith("GOOGLE_") else "",
            "google_query_preview": google_query_preview,
            "google_free_query_preview": free_variants[0] if free_variants else "",
            "google_fallback_preview": fallback if plan.startswith("GOOGLE_") else "",
            "candidate_latitudine": r.get("latitudine"),
            "candidate_longitudine": r.get("longitudine"),
            "current_latitudine": current.get("latitudine"),
            "current_longitudine": current.get("longitudine"),
            "current_geocode_source": clean(current.get("geocode_source")),
            "current_geocode_location_type": clean(current.get("geocode_location_type")),
            "audit_result": clean(ev.get("audit_result")),
            "priority": ev.get("priority", ""),
            "candidate_tier": clean(ev.get("candidate_tier")),
            "anncsu_decision": clean(ev.get("anncsu_decision")),
            "street_score": ev.get("street_score", ""),
            "strict_street_similarity": ev.get("strict_street_similarity", ""),
            "direct_locality_guard": clean(ev.get("direct_locality_guard")),
            "ambiguity": clean(ev.get("ambiguity")),
            "civic_relation": clean(ev.get("civic_relation")),
            "anncsu_odonimo": clean(ev.get("anncsu_odonimo")),
            "anncsu_civic": clean(ev.get("anncsu_civic")),
            "anncsu_metodo": ann_method(ev),
            "anncsu_lat": ev.get("anncsu_lat", ""),
            "anncsu_lon": ev.get("anncsu_lon", ""),
            "municipality_guard": municipality_guard_status(ev)[0],
            "distance_outside_municipality_bbox_m": ev.get(
                "distance_outside_municipality_bbox_m", ""
            ),
            "matching_street_candidates": ev.get("matching_street_candidates", ""),
            "matching_access_candidates": ev.get("matching_access_candidates", ""),
            "street_rep_lat": rep.get("lat", ""),
            "street_rep_lon": rep.get("lon", ""),
            "street_rep_unique_coords": rep.get("unique_coord_count", ""),
            "street_rep_p90_m": rep.get("p90_from_rep_m", ""),
            "runtime_anncsu_locality": clean(ev.get("runtime_anncsu_locality")),
            "runtime_locality_match_mode": clean(ev.get("runtime_locality_match_mode")),
            "runtime_locality_hint_reason": clean(ev.get("runtime_locality_hint_reason")),
            "runtime_locality_guard_reason": clean(ev.get("runtime_locality_guard_reason")),
            "runtime_locality_source_hints_count": ev.get("runtime_locality_source_hints_count", ""),
            "runtime_locality_explicit_hints_count": ev.get("runtime_locality_explicit_hints_count", ""),
            "runtime_locality_explicit_hints": clean(ev.get("runtime_locality_explicit_hints")),
            "runtime_locality_source_specificity": clean(ev.get("runtime_locality_source_specificity")),
            "runtime_locality_accesses": ev.get("runtime_locality_accesses", ""),
            "runtime_locality_p90_m": ev.get("runtime_locality_p90_m", ""),
            "runtime_locality_lat": ev.get("runtime_locality_lat", ""),
            "runtime_locality_lon": ev.get("runtime_locality_lon", ""),
        })

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(records)

    conflict_rows = [
        r for r in records
        if clean(r.get("municipality_guard")) == "GROSS_OUTSIDE"
    ]
    conflict_rows.sort(
        key=lambda r: float(r.get("distance_outside_municipality_bbox_m") or 0),
        reverse=True,
    )
    conflicts_path = REPORTS_DIR / f"geocode_municipality_conflicts_v11_5_{stamp}.csv"
    with conflicts_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(conflict_rows)

    counts = Counter(x["plan"] for x in records)
    google_plans = {
        k: v for k, v in counts.items()
        if k.startswith("GOOGLE_")
    }

    # Physical call estimate after cache/dedup, still without calling Google.
    reverse_keys = set()
    forward_keys = set()
    comune_keys = set()
    for rec in records:
        plan = clean(rec.get("plan"))
        if plan == "GOOGLE_VALIDATE_NEW_PVP":
            lat = fnum(rec.get("candidate_latitudine"))
            lon = fnum(rec.get("candidate_longitudine"))
            if lat is not None and lon is not None:
                reverse_keys.add((round(lat, 7), round(lon, 7)))
        elif plan in {"GOOGLE_NEEDED_MISSING", "GOOGLE_ARBITRATION_NEW_PVP", "GOOGLE_ARBITRATION_EXISTING", "GOOGLE_VALIDATE_PVP"}:
            q = clean(rec.get("google_query_preview"))
            if q:
                forward_keys.add(q.casefold())
        elif plan == "GOOGLE_COMUNE_ONLY":
            comune_keys.add((
                clean(rec.get("candidate_comune")).casefold(),
                clean(rec.get("candidate_sigla_provincia")).upper(),
                clean(rec.get("candidate_cap")),
            ))

    summary = {
        "version": "11.6.4-plan-locality-explicit-guard-specific-first",
        "generated_at": datetime.now().astimezone().isoformat(),
        "mode": "READ_ONLY_ZERO_GOOGLE_ZERO_PATCH",
        "scope": SCOPE,
        "target_source": target_source,
        "target_count": len(items),
        "load_meta": load_meta,
        "local_evaluated": len(eval_items),
        "skipped_without_local_scan": len(immediate),
        "plan_counts": dict(counts.most_common()),
        "would_need_google": sum(google_plans.values()),
        "google_plan_counts": google_plans,
        "google_unique_work_estimate": {
            "unique_reverse_coordinate_keys": len(reverse_keys),
            "unique_forward_query_previews": len(forward_keys),
            "unique_comune_only_keys": len(comune_keys),
            "minimum_distinct_google_jobs_before_retries": (
                len(reverse_keys) + len(forward_keys) + len(comune_keys)
            ),
            "note": "Estimate only; a live forward job can use more than one query variant/reverse verification.",
        },
        "google_api_calls": 0,
        "supabase_patches": 0,
        "municipality_guard": {
            "authority": "ISTAT 2026 official municipality polygon",
            "boundary_counts": dict(boundary_counts),
            "boundary_runtime": boundary_ctx.metadata(),
            "anncsu_bbox_gross_outside_threshold_m": MUNICIPALITY_GROSS_OUTSIDE_M,
            "anncsu_bbox_gross_conflicts": len(conflict_rows),
            "note": "ISTAT polygon is authoritative for municipality containment; ANNCSU bbox and Google reverse are diagnostic only.",
        },
        "admin_canonicalization": {
            "evaluated": len(eval_items),
            "counts": dict(admin_counts),
            "authority": "Elenco-comuni-italiani.xlsx",
            "cap_support": "comuni_cap.csv (validation only; never fills a missing CAP)",
        },
        "local_runtime": runtime_meta,
        "files": {
            "summary": summary_path.name,
            "plan": csv_path.name,
            "municipality_conflicts": conflicts_path.name,
        },
        "notes": [
            "Questo stage non chiama Google e non modifica Supabase.",
            "Per PVP nuovi/cambiati usa il nuovo input da hybrid_api_rows_full.jsonl.",
            "Lo stato geocode corrente del DB resta separato dal candidato PVP.",
            "I PVP nuovi non ancora presenti in Supabase vengono inclusi dal file hybrid.",
            "PVP-first: un pin PVP valido non viene sostituito automaticamente da ANNCSU.",
            "ANNCSU geocodifica direttamente solo quando il PVP non fornisce coordinate utilizzabili.",
            "Comune e sigla vengono canonicalizzati dall'ISTAT corrente prima di ANNCSU/Google.",
            "comuni_cap.csv valida solo CAP gia presenti; non compila mai un CAP mancante.",
            "La coda amministrativa PVP viene rimossa dall'indirizzo usato per il geocoding, ma resta visibile nei campi source_* del CSV.",
            "Le query Google nel CSV sono solo preview: nessuna API viene chiamata.",
            "I cambi geografici devono essere diagnosticati separatamente prima del live per evitare falsi reprocess.",
            "v11.5: gross municipality conflict precedes any semantic/local confirmation.",
            "v11.5: scope=all writes a separate municipality-conflicts CSV, still zero Google and zero PATCH.",
            "QA: paginazione Supabase ordinata stabilmente per pvp_id e fail-closed se compaiono pvp_id duplicati.",
            "v11.6.1: OUTSIDE usa ANNCSU civico/strada conservativo prima di Google.",
            "v11.6.1: ADMIN_UNRESOLVED va in REVIEW e non consuma Google.",
        ],
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n[PLAN]")
    for k, v in counts.most_common():
        print(f"  {k}: {v}")
    print(
        f"[GOOGLE FUTURO] record che richiederebbero Google: "
        f"{summary['would_need_google']}"
    )
    print("[GOOGLE OGGI] 0 chiamate")
    print(f"[MUNICIPALITY GUARD] gross conflicts > {MUNICIPALITY_GROSS_OUTSIDE_M:.0f} m: {len(conflict_rows)}")
    print(f"[OUT] {summary_path.name}")
    print(f"[OUT] {csv_path.name}")
    print(f"[OUT] {conflicts_path.name}")


if __name__ == "__main__":
    main()
