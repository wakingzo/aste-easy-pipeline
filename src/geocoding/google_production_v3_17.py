#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy production geocoder v3.12.

Production behavior validated by v3.9-v3.11.5:
- DB scope is an explicit list of PVP ids from the authorized importer plan;
- offline PVP/ISTAT/ANNCSU decision is computed for every PENDING row first;
- Google is attempted only for rows admitted by the v3.9 eligibility gate;
- network spend is ordered P1 -> P5 and protected by BOTH the legacy core hard
  cap and the independent Aste Easy ledger, reserved before HTTP;
- cached Google answers are free and may still be used after the network cap;
- if the cap is exhausted, the remaining listings retain their offline/map fallback;
- all decisions are computed before the first Supabase PATCH;
- PATCH is guarded by pvp_id + PENDING + input hash + original address/PVP fields,
  followed by read-back verification;
- a REVIEW with no precise coordinate receives a verified ISTAT municipality marker
  in latitudine/longitudine so it remains visible on the map, while status stays REVIEW.
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env
import geocode_pending_v2 as offline
import google_budget_v3_10 as budget
import google_live_guard_v3_11 as live
import google_policy_v3_9 as policy
import google_preview_v3 as preview
import locality_guard_runtime_v1 as locality_guard
from google_eligibility_v3_2 import eligibility, canonical

VERSION = "google_production_v3_17_locality_guard"
MAX_PRODUCTION_RUN_CAP = 9000


def _safe_int(v, default=0):
    try:
        return max(0, int(v))
    except Exception:
        return default


def _stamp():
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")


def _pid_sort_key(pid):
    s = str(pid or "")
    return (0, int(s)) if s.isdigit() else (1, s)


def _query_for(row, baseline, g):
    prio = preview.priority_bucket(row, baseline)
    if prio is None:
        return None
    rank, label, gate = prio
    variants = g.constrained_address_variants(row)
    if not variants:
        return None
    query = variants[0]
    name_canon = canonical(gate.get("address_name") or "")
    name_tokens = [t for t in re.findall(r"[a-z0-9]+", name_canon)
                   if t not in {"del","della","delle","degli","dei","di","via","viale","piazza","localita"}]
    query_tokens = set(re.findall(r"[a-z0-9]+", canonical(query)))
    if not query or not name_tokens or not set(name_tokens).issubset(query_tokens):
        return None
    civic = str(gate.get("address_civic") or "").strip()
    if civic:
        civic_pattern = re.escape(civic).replace("/", r"\s*/\s*")
        if not re.search(r"(?<!\d)" + civic_pattern + r"(?!\d)", query, re.I):
            return None
    return {"rank": rank, "label": label, "gate": gate, "query": query}


def _cache_key(g, query, row):
    address_part = g.clean(query).strip(" ,")
    cap = re.sub(r"\D", "", g.clean(row.get("cap")))
    comune = g.clean(row.get("comune"))
    components = ["country:IT"]
    if cap:
        components.append("postal_code:" + cap)
    if comune:
        components.append("locality:" + comune)
    return "FC|" + address_part + "|" + "|".join(components)


def _payload(row, baseline, final, map_data, *, evidence=None, query="", google_outcome="NOT_REQUESTED"):
    map_lat, map_lon = map_data.get("map_lat"), map_data.get("map_lon")
    if not offline.valid_pair(map_lat, map_lon):
        raise RuntimeError("Production invariant: row has no safe map point: " + str(row.get("pvp_id")))
    fallback = bool(map_data.get("map_is_fallback"))
    status = str(final.get("final_status") or baseline.get("geocode_status") or "REVIEW")
    source = (map_data.get("map_source") if fallback else final.get("final_source")) or baseline.get("geocode_source")
    if fallback and str(source or "").startswith("istat_comune_fallback_v39"):
        source = "istat_comune_fallback_v312"
    precision = (map_data.get("map_precision") if fallback else final.get("final_precision")) or baseline.get("geocode_precision")
    reason = str(final.get("final_reason") or baseline.get("geocode_decision_reason") or "")
    if fallback:
        reason = (reason + "; marker mappa indicativo nel Comune ISTAT; underlying_source=" +
                  str(final.get("final_source") or "NESSUNA") + "; underlying_precision=" +
                  str(final.get("final_precision") or "NESSUNA")).strip("; ")
    ev = evidence or {}
    locality_guarded = str(final.get("policy_action") or "") == "LOCALITY_GUARD_MOVE_TO_ISTAT_LOCALITA"
    formatted = (row.get("indirizzo") if locality_guarded else
                 ev.get("google_formatted_address") or baseline.get("geocode_formatted_address") or row.get("indirizzo"))
    location_type = ("LOCALITA" if locality_guarded else
                     (map_data.get("map_precision") if fallback else
                      ev.get("google_location_type") or final.get("final_precision") or baseline.get("geocode_location_type")))
    return {
        "latitudine": float(map_lat), "longitudine": float(map_lon),
        "maps_link": offline.maps_link(float(map_lat), float(map_lon)),
        "geocode_status": status,
        "geocode_source": source,
        "geocode_precision": precision,
        "geocode_version": VERSION,
        "geocode_checked_at": datetime.now(timezone.utc).isoformat(),
        "geocode_google_outcome": google_outcome,
        "geocode_decision_reason": reason,
        "geocode_location_type": location_type,
        "geocode_formatted_address": formatted,
        "geocode_query_used": query or baseline.get("geocode_query_used"),
    }


def _chunks(seq, n=100):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]


def fetch_rows_by_ids(base, key, pvp_ids: Iterable[str]):
    ids = sorted({str(x) for x in pvp_ids if str(x)}, key=_pid_sort_key)
    found = {}
    for batch_ids in _chunks(ids, 100):
        expr = "in.(" + ",".join(batch_ids) + ")"
        params = [("select", ",".join(offline.SELECT)), ("pvp_id", expr), ("limit", str(len(batch_ids)+2))]
        rows = offline._json_request(base, key, "listings", params=params)
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected Supabase response while reading production IDs")
        for row in rows:
            pid = str(row.get("pvp_id") or "")
            if not pid or pid in found:
                raise RuntimeError("Duplicate/invalid PVP id returned by Supabase: " + pid)
            found[pid] = row
    missing = [pid for pid in ids if pid not in found]
    if missing:
        raise RuntimeError(f"{len(missing)} authorized PVP ids are missing after atomic import; first={missing[:5]}")
    return [found[pid] for pid in ids]




def _load_google_core_production(run_cap):
    """Load the existing Google core with a production-sized run cap.

    The diagnostic preview loader intentionally caps samples at 500; production
    must not inherit that sampling limit. The core itself already supports larger
    MAX_RUN_GEOCODING_CALLS values.
    """
    if not 1 <= run_cap <= MAX_PRODUCTION_RUN_CAP:
        raise ValueError(f"production run cap must be 1..{MAX_PRODUCTION_RUN_CAP}")
    load_project_env(required=("GOOGLE_GEOCODING_API_KEY",))
    if not os.environ.get("GOOGLE_GEOCODING_API_KEY", "").strip():
        raise RuntimeError("GOOGLE_GEOCODING_API_KEY missing; zero requests")
    os.environ["MAX_RUN_GEOCODING_CALLS"] = str(run_cap)
    os.environ["PLACES_AUTOCOMPLETE_FALLBACK"] = "0"
    os.environ["ADDRESS_VALIDATION_FALLBACK"] = "0"
    os.environ["HTTP_RETRIES"] = "1"
    os.environ["API_USAGE_SAVE_EVERY"] = "1"
    path = offline.HERE / "google_geocode_core_v9_3_6.py"
    if not path.is_file():
        raise RuntimeError("Missing existing Google core v9.3.6: no requests made")
    spec = importlib.util.spec_from_file_location("aste_google_production_v312_core", path)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    for name in ("google_forward_constrained", "constrained_address_variants",
                 "score_forward_candidate", "address_specificity", "CacheStore",
                 "ApiUsageTracker", "API_USAGE"):
        if not hasattr(g, name):
            raise RuntimeError("Google core incompatible: missing " + name)
    if not g.CACHE_FILE.is_file() or not g.API_USAGE_FILE.is_file():
        raise RuntimeError("Existing Google cache and usage counter required; abort to avoid duplicate paid requests")
    if g.MAX_RUN_GEOCODING_CALLS != run_cap or g.HTTP_RETRIES != 1:
        raise RuntimeError("Google core ignored production run cap or retry settings")
    return g


def _load_core_and_reconcile(*, monthly_cap, run_cap, monthly_used_override=None):
    if monthly_cap < 1:
        raise ValueError("monthly_cap must be positive")
    if not 1 <= run_cap <= MAX_PRODUCTION_RUN_CAP:
        raise ValueError(f"production run cap must be 1..{MAX_PRODUCTION_RUN_CAP}")
    load_project_env(required=("GOOGLE_GEOCODING_API_KEY",))
    os.environ["MAX_MONTH_GEOCODING_CALLS"] = str(monthly_cap)
    os.environ["MAX_RUN_GEOCODING_CALLS"] = str(run_cap)
    os.environ.pop("MONTH_USAGE_OFFSET_GEOCODING", None)
    os.environ["PLACES_AUTOCOMPLETE_FALLBACK"] = "0"
    os.environ["ADDRESS_VALIDATION_FALLBACK"] = "0"
    os.environ["HTTP_RETRIES"] = "1"
    os.environ["API_USAGE_SAVE_EVERY"] = "1"
    g = _load_google_core_production(run_cap)
    usage_path = Path(g.API_USAGE_FILE).resolve()
    reconciliation = live.reconcile_usage_state(
        monthly_cap=monthly_cap, monthly_used_override=monthly_used_override,
        usage_path=usage_path)
    canonical = _safe_int(reconciliation.get("canonical_used_before"))
    if run_cap > max(0, monthly_cap - canonical):
        # Per-run cap may be larger than remaining, but requiring it to fit makes
        # operator intent explicit and prevents a misleading authorization value.
        raise RuntimeError(
            f"production run cap {run_cap} exceeds monthly remaining {max(0, monthly_cap-canonical)}; "
            "run preflight/reconcile and pass a cap <= remaining"
        )
    g.API_USAGE = g.ApiUsageTracker(g.API_USAGE_FILE)
    local, ext, effective = g.API_USAGE.month_usage("geocoding")
    if ext != 0 or effective != canonical:
        raise RuntimeError(
            f"Google core usage mismatch after reconciliation: local={local} offset={ext} effective={effective} expected={canonical}"
        )
    original_before = g.API_USAGE.before_network_attempt
    def guarded_before_network_attempt(sku, operation, attempt_index):
        if sku == "geocoding":
            res = budget.reserve_before_http(cap=monthly_cap, count=1, month=reconciliation["month"])
            if not res.get("ok"):
                raise g.ApiBudgetExceeded(
                    f"Aste Easy monthly ledger hard cap reached: {res.get('used')}/{monthly_cap}"
                )
        return original_before(sku, operation, attempt_index)
    g.API_USAGE.before_network_attempt = guarded_before_network_attempt
    return g, reconciliation


def preflight_usage(*, monthly_cap=9000, run_cap=1, monthly_used_override=None):
    """Network-free counter/core readiness check. It may repair only known historical counter bugs."""
    g, rec = _load_core_and_reconcile(monthly_cap=monthly_cap, run_cap=run_cap,
                                      monthly_used_override=monthly_used_override)
    local, ext, effective = g.API_USAGE.month_usage("geocoding")
    ledger = budget.read_ledger(month=rec["month"])
    delta = _safe_int(ledger.get("used")) - effective
    if delta != 0:
        raise RuntimeError(f"Google ledger/core mismatch before production: delta={delta}")
    return {"version": VERSION, "month": rec["month"], "monthly_cap": monthly_cap,
            "run_cap": run_cap, "effective_used": effective,
            "remaining": max(0, monthly_cap-effective), "ledger": ledger,
            "core_usage_path": str(Path(g.API_USAGE_FILE).resolve()), "reconciliation": rec}


def run_for_ids(pvp_ids, *, apply=False, max_rows=12000, monthly_cap=9000,
                run_cap=1, monthly_used_override=None):
    ids = sorted({str(x) for x in pvp_ids if str(x)}, key=_pid_sort_key)
    if not ids:
        return {"version": VERSION, "mode": "NO_IDS", "authorized_ids": 0,
                "pending_rows": 0, "google_calls": 0, "patched_verified": 0}
    if len(ids) > max_rows:
        raise RuntimeError(f"Authorized production ids {len(ids)} exceed --max-geocode {max_rows}")
    load_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE", "GOOGLE_GEOCODING_API_KEY"))
    base, key = os.environ["SUPABASE_URL"].rstrip("/"), os.environ["SUPABASE_SERVICE_ROLE"]
    all_rows = fetch_rows_by_ids(base, key, ids)
    pending = [r for r in all_rows if str(r.get("geocode_status") or "") == "PENDING"]
    already_done = [r for r in all_rows if str(r.get("geocode_status") or "") != "PENDING"]
    for r in pending:
        if r.get("latitudine") is not None or r.get("longitudine") is not None:
            raise RuntimeError("PENDING authorized row already has published coordinates: " + str(r.get("pvp_id")))
        if not r.get("geocode_input_hash"):
            raise RuntimeError("PENDING authorized row missing input hash: " + str(r.get("pvp_id")))

    ctx, territorial, local = offline.prepare_context(no_anncsu=False)
    planned = offline.plan_rows(pending, ctx, territorial, local)

    # Locality Guard pre-computation is offline-only. Reuse ANNCSU runtime evidence
    # only for rows that explicitly name a locality/frazione/contrada.
    locality_guard.validate_index(locality_guard.DEFAULT_INDEX)
    locality_rows = [r for r, _b in planned if locality_guard.explicit_hint(r.get("indirizzo"))]
    locality_ev_by_id = {}
    if locality_rows:
        loc_evidence, _loc_meta = local.build_local_evidence([offline.pvp_row(r) for r in locality_rows])
        locality_ev_by_id = {str(x.get("pvp_id")): x for x in loc_evidence}
    istat_locality_con = locality_guard.open_index(locality_guard.DEFAULT_INDEX)

    g, reconciliation = _load_core_and_reconcile(monthly_cap=monthly_cap, run_cap=run_cap,
                                                  monthly_used_override=monthly_used_override)
    cache = g.CacheStore(g.CACHE_FILE)

    candidates = []
    baseline_by_id = {}
    candidate_by_id = {}
    for row, baseline in planned:
        pid = str(row["pvp_id"])
        baseline_by_id[pid] = baseline
        c = _query_for(row, baseline, g)
        candidate_by_id[pid] = c
        if c:
            rank_hash = hashlib.sha256(pid.encode("utf-8")).hexdigest()
            candidates.append((c["rank"], rank_hash, row, baseline, c))
    candidates.sort(key=lambda x: (x[0], x[1]))

    evidence_by_id = {}
    outcome_by_id = {}
    google_source_by_id = {}
    google_error = None
    network_deferred = 0
    try:
        for _, _, row, baseline, c in candidates:
            pid = str(row["pvp_id"])
            query = c["query"]
            cache_key = _cache_key(g, query, row)
            cached = isinstance(cache.get(cache_key), dict)
            run_network = _safe_int(g.API_USAGE.run["geocoding"].get("network_attempts"))
            if not cached and run_network >= run_cap:
                outcome_by_id[pid] = "DEFERRED_RUN_CAP_OFFLINE_FALLBACK"
                google_source_by_id[pid] = "DEFERRED"
                network_deferred += 1
                continue
            ledger_now = budget.read_ledger(month=reconciliation["month"])
            if not cached and _safe_int(ledger_now.get("used")) >= monthly_cap:
                outcome_by_id[pid] = "DEFERRED_MONTHLY_CAP_OFFLINE_FALLBACK"
                google_source_by_id[pid] = "DEFERRED"
                network_deferred += 1
                continue
            try:
                results = g.google_forward_constrained(query, row, cache)
            except g.ApiBudgetExceeded:
                outcome_by_id[pid] = "DEFERRED_HARD_CAP_OFFLINE_FALLBACK"
                google_source_by_id[pid] = "DEFERRED"
                network_deferred += 1
                continue
            except Exception as exc:
                google_error = type(exc).__name__
                raise RuntimeError(
                    f"Google production error before any Supabase geocode PATCH; pvp_id={pid}; type={type(exc).__name__}"
                ) from exc
            ev = preview.compare_candidate(row, baseline, results, ctx, g)
            ev["priority_label"] = c["label"]
            ev["address_kind"] = c["gate"].get("address_kind") or ""
            evidence_by_id[pid] = ev
            outcome_by_id[pid] = str(ev.get("assessment") or "GOOGLE_COMPLETED")
            google_source_by_id[pid] = "CACHE" if cached else "NETWORK"
    finally:
        cache.save()
        g.API_USAGE.save()

    # Build every final payload before the first DB PATCH.
    municipality_cache = {}
    prepared = []
    details = []
    locality_guard_counts = Counter()
    try:
        for row, baseline in planned:
            pid = str(row["pvp_id"])
            ev = evidence_by_id.get(pid)
            final = policy.apply_policy(row, baseline, ev)
            map_data = policy.choose_map_point(row, final, ctx, municipality_cache)
            lg = locality_guard.evaluate(
                row, map_data, final, locality_ev_by_id.get(pid, {}), istat_locality_con)
            locality_guard_counts[str(lg.get("action") or "UNKNOWN")] += 1
            final, map_data = locality_guard.apply_override(row, final, map_data, lg)
            if not offline.valid_pair(map_data.get("map_lat"), map_data.get("map_lon")):
                raise RuntimeError("Map coverage invariant failed before DB writes for pvp_id=" + pid)
            c = candidate_by_id.get(pid)
            query = c.get("query") if c else ""
            outcome = outcome_by_id.get(pid) or (eligibility(row, baseline).get("google_reason") or "NOT_REQUESTED")
            payload = _payload(row, baseline, final, map_data, evidence=ev, query=query,
                               google_outcome=outcome)
            prepared.append((row, payload))
            details.append({
                "pvp_id": pid, "comune": row.get("comune"), "indirizzo": row.get("indirizzo"),
                "offline_source": baseline.get("geocode_source"), "offline_precision": baseline.get("geocode_precision"),
                "google_priority": c.get("label") if c else "", "google_query": query,
                "google_execution": google_source_by_id.get(pid) or "NOT_REQUESTED",
                "google_outcome": outcome,
                "policy_action": final.get("policy_action"), "final_status": final.get("final_status"),
                "final_source": final.get("final_source"), "final_precision": final.get("final_precision"),
                "map_source": map_data.get("map_source"), "map_precision": map_data.get("map_precision"),
                "map_is_fallback": map_data.get("map_is_fallback"),
                "db_status": payload.get("geocode_status"), "db_source": payload.get("geocode_source"),
                "db_precision": payload.get("geocode_precision"), "db_lat": payload.get("latitudine"),
                "db_lon": payload.get("longitudine"),
                "locality_guard_action": lg.get("action"), "locality_guard_hint": lg.get("hint"),
                "locality_guard_ref": lg.get("istat_name"), "locality_guard_distance_m": lg.get("distance_to_locality_m"),
            })
    
    finally:
        istat_locality_con.close()

    # Counter consistency must hold before DB writes.
    local_after, ext_after, effective_after = g.API_USAGE.month_usage("geocoding")
    ledger_after_calls = budget.read_ledger(month=reconciliation["month"])
    delta_before_patch = _safe_int(ledger_after_calls.get("used")) - effective_after
    if ext_after != 0 or delta_before_patch != 0:
        raise RuntimeError(
            f"Google counter invariant failed before DB PATCH: local={local_after} offset={ext_after} "
            f"ledger={ledger_after_calls.get('used')} delta={delta_before_patch}"
        )

    patched = skipped_changed = 0
    if apply:
        total_patch = len(prepared)
        print(f"[GEOCODE PATCH] starting guarded Supabase PATCH: {total_patch} PENDING rows", flush=True)
        for idx, (row, payload) in enumerate(prepared, 1):
            if offline.guarded_patch(base, key, row, payload):
                patched += 1
            else:
                skipped_changed += 1
            if idx == 1 or idx % 100 == 0 or idx == total_patch:
                print(f"[GEOCODE PATCH] {idx}/{total_patch} verified={patched} skipped_changed={skipped_changed}", flush=True)

    ensure_runtime_dirs()
    stamp = _stamp()
    csv_path = Path(REPORTS_DIR) / ("google_production_v3_17_" + stamp + ".csv")
    fields = ["pvp_id","comune","indirizzo","offline_source","offline_precision",
              "google_priority","google_query","google_execution","google_outcome",
              "policy_action","final_status","final_source","final_precision",
              "map_source","map_precision","map_is_fallback","db_status","db_source",
              "db_precision","db_lat","db_lon","locality_guard_action","locality_guard_hint",
              "locality_guard_ref","locality_guard_distance_m"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";", extrasaction="ignore")
        w.writeheader(); w.writerows(details)

    run_usage = g.API_USAGE.run["geocoding"]
    summary = {
        "version": VERSION, "mode": "APPLY" if apply else "PREVIEW",
        "authorized_ids": len(ids), "pending_rows": len(pending),
        "already_geocoded_or_concurrent": len(already_done),
        "google_eligible_rows": len(candidates),
        "google_calls": _safe_int(run_usage.get("network_attempts")),
        "google_cache_hits": _safe_int(run_usage.get("cache_hits")),
        "google_network_deferred": network_deferred,
        "google_error": google_error,
        "usage_reconciliation": reconciliation,
        "core_local_after": local_after, "core_external_offset_after": ext_after,
        "core_effective_after": effective_after, "ledger_after": ledger_after_calls,
        "counter_delta_ledger_minus_core": delta_before_patch,
        "remaining_after": max(0, monthly_cap - effective_after),
        "map_coverage": {"total": len(prepared), "mapped": len(prepared), "unmapped": 0,
                         "fallback_mapped": sum(bool(x.get("map_is_fallback")) for x in details)},
        "db_precision_counts": dict(Counter(str(x.get("db_precision") or "NESSUNA") for x in details)),
        "policy_action_counts": dict(Counter(str(x.get("policy_action") or "UNKNOWN") for x in details)),
        "google_execution_counts": dict(Counter(str(x.get("google_execution") or "UNKNOWN") for x in details)),
        "locality_guard_counts": dict(locality_guard_counts),
        "locality_guard_overrides": int(locality_guard_counts.get("OVERRIDE_TO_LOCALITA", 0)),
        "locality_guard_index": str(locality_guard.DEFAULT_INDEX),
        "patched_verified": patched, "skipped_changed": skipped_changed,
        "report_csv": str(csv_path),
        "safety_note": "All Google and locality-guard decisions are finalized before the first guarded Supabase PATCH. The locality guard never replaces verified precise coordinates; strong conflicts on coarse pins degrade to REVIEW/LOCALITA using an exact-unique ISTAT locality point.",
    }
    json_path = Path(REPORTS_DIR) / ("google_production_v3_17_" + stamp + ".json")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["report_json"] = str(json_path)
    return summary
