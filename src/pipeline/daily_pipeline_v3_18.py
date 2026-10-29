#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy v3.18 - publication-sorted one-command safe daily production update.

Flow:
  RPC probe -> PVP discovery build -> exact new-only plan -> safety gate ->
  asset readiness -> atomic import -> production geocode -> read-only audit.

The daily discovery requests PVP pages ordered by dataPubblicazione desc and stops
only after the publication threshold is crossed and monotonic ordering has been verified.
If ordering cannot be verified, the builder falls back to a conservative full scan.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import sqlite3
import time
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
for p in (SRC, SRC / "pipeline", SRC / "importer", SRC / "geocoding", SRC / "audit"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from aste_paths import IMPORTER_DIR, RAW_DIR, REPORTS_DIR, ANNCSU_DIR, ensure_runtime_dirs
from project_env import load_project_env
import pvp_import_atomic_v1 as importer
import geocode_pending_v2 as geocoder
import google_production_v3_18 as prod
import locality_guard_runtime_v1 as locality_guard
import production_audit_v3_13 as audit
import container_preflight_v3_18 as container_preflight

VERSION = "daily_pipeline_v3_18"
MANIFEST_VERSION = "daily_manifest_v3_18"


def _stamp():
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")


def _atomic_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _write_staging(path: Path, plan: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for listing, subjects, attachments in plan["records"]:
            f.write(json.dumps({"listing": listing, "subjects": subjects, "attachments": attachments}, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _load_staging(path: Path):
    records = []
    with path.open("r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            records.append((obj["listing"], obj.get("subjects") or [], obj.get("attachments") or []))
    return records


def _plan_from_manifest_staging(manifest: dict):
    staging = Path(manifest.get("staging_file") or "")
    if not staging.is_file():
        raise RuntimeError("Daily staging payload missing; cannot safely auto-resume import: " + str(staging))
    records = _load_staging(staging)
    if len(records) != int(manifest.get("eligible_new") or -1):
        raise RuntimeError("Daily staging row count differs from authorized manifest")
    ids = [str(r[0].get("pvp_id")) for r in records]
    if ids != [str(x) for x in manifest.get("pvp_ids") or []]:
        raise RuntimeError("Daily staging pvp_id order/content differs from authorized manifest")
    meta = manifest.get("plan_meta") or {}
    return {
        "records": records, "raw_sha256": manifest["raw_sha256"],
        "incoming_unique": meta.get("incoming_unique", 0), "existing": meta.get("existing", 0),
        "new_candidates": meta.get("new_candidates", len(records)), "decisions": meta.get("decisions", {}),
        "primary_sources": meta.get("primary_sources", {}),
        "subjects": manifest.get("expected_soggetti", sum(len(r[1]) for r in records)),
        "attachments": manifest.get("expected_allegati", sum(len(r[2]) for r in records)),
    }


def _seconds(t0):
    return round(time.perf_counter() - t0, 3)


def _fmt(sec):
    sec = float(sec or 0)
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{int(m)}m {s:.0f}s"
    h, m = divmod(m, 60)
    return f"{int(h)}h {int(m)}m {s:.0f}s"


def _read_new_jsonl(path: Path, old_size: int):
    if not path.exists() or path.stat().st_size <= old_size:
        return []
    with path.open("rb") as f:
        f.seek(old_size)
        raw = f.read().decode("utf-8", errors="replace")
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            out.append({"stage": "UNPARSEABLE", "error": line[:300]})
    return out


def _latest_unfinished_manifest():
    # Recover both native v3.17 runs and a v3.16 run interrupted just before upgrade.
    candidates = list(Path(REPORTS_DIR).glob("daily_manifest_v3_18_*.json"))
    candidates += list(Path(REPORTS_DIR).glob("daily_manifest_v3_17_*.json"))
    candidates += list(Path(REPORTS_DIR).glob("daily_manifest_v3_16_*.json"))
    candidates = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            obj = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        status = str(obj.get("status") or "")
        if status not in {"COMPLETED", "NO_NEW"}:
            return path, obj
    return None, None


def _audit_and_finish(path, manifest):
    report_path = Path(REPORTS_DIR) / ("production_audit_v3_13_" + _stamp() + ".json")
    t = time.perf_counter()
    a = audit.run(
        manifest.get("pvp_ids") or [],
        expected_subjects=manifest.get("expected_soggetti"),
        expected_attachments=manifest.get("expected_allegati"),
        raw_sha256=manifest.get("raw_sha256"),
        report_path=report_path,
    )
    manifest.setdefault("timings_seconds", {})["audit"] = _seconds(t)
    manifest["audit_summary"] = a
    manifest["audit_report"] = str(report_path)
    if a.get("result") != "PASS":
        manifest["status"] = "AUDIT_FAILED"
        _atomic_json(path, manifest)
        raise RuntimeError("Daily audit FAILED; inspect " + str(report_path))
    manifest["status"] = "COMPLETED"
    manifest["completed_at"] = datetime.now().astimezone().isoformat()
    old_error = manifest.pop("last_error_type", None)
    if old_error:
        manifest["previous_error_type"] = old_error
    _atomic_json(path, manifest)
    return a


def _resume_if_needed(args):
    path, manifest = _latest_unfinished_manifest()
    if not path:
        return False
    status = str(manifest.get("status") or "")
    print(f"[DAILY v3.18] unfinished manifest found: {path.name} status={status}", flush=True)
    if status in {"IMPORT_RUNNING", "IMPORT_INTERRUPTED"}:
        print("[DAILY v3.18] auto-resume atomic import from authorized staging payload", flush=True)
        plan = _plan_from_manifest_staging(manifest)
        manifest["status"] = "IMPORT_RESUME_RUNNING"
        _atomic_json(path, manifest)
        t = time.perf_counter()
        try:
            imp = importer.run(apply=True, expected_new=len(plan["records"]), expected_sha=plan["raw_sha256"], prebuilt_plan=plan)
            manifest.setdefault("timings_seconds", {})["resume_import"] = _seconds(t)
            manifest["import_resume_summary"] = imp
            manifest["status"] = "IMPORTED_PENDING_GEOCODE"
            _atomic_json(path, manifest)
            status = "IMPORTED_PENDING_GEOCODE"
        except Exception as exc:
            manifest["status"] = "IMPORT_INTERRUPTED"
            manifest["last_error_type"] = type(exc).__name__
            _atomic_json(path, manifest)
            raise

    if status in {"IMPORT_RESUME_RUNNING", "IMPORTED_PENDING_GEOCODE", "GEOCODE_INTERRUPTED", "RESUME_GEOCODE_RUNNING"}:
        # IMPORT_RESUME_RUNNING may mean the process died after all idempotent RPCs but before manifest update.
        if status == "IMPORT_RESUME_RUNNING":
            plan = _plan_from_manifest_staging(manifest)
            imp = importer.run(apply=True, expected_new=len(plan["records"]), expected_sha=plan["raw_sha256"], prebuilt_plan=plan)
            manifest["import_resume_summary"] = imp
            manifest["status"] = "IMPORTED_PENDING_GEOCODE"
            _atomic_json(path, manifest)
        print("[DAILY v3.18] auto-resume geocoding before discovering new auctions", flush=True)
        manifest["status"] = "RESUME_GEOCODE_RUNNING"
        _atomic_json(path, manifest)
        t = time.perf_counter()
        try:
            geo = prod.run_for_ids(
                manifest["pvp_ids"], apply=True, max_rows=max(args.max_geocode, len(manifest["pvp_ids"])),
                monthly_cap=args.google_monthly_cap, run_cap=args.google_daily_run_cap,
                monthly_used_override=None,
            )
            manifest.setdefault("timings_seconds", {})["resume_geocode"] = _seconds(t)
            manifest["geocode_summary"] = geo
            manifest["status"] = "GEOCODED_PENDING_AUDIT"
            _atomic_json(path, manifest)
            _audit_and_finish(path, manifest)
            print(f"[DAILY v3.18] recovered previous run: {path}", flush=True)
            return True
        except Exception as exc:
            manifest["status"] = "GEOCODE_INTERRUPTED"
            manifest["last_error_type"] = type(exc).__name__
            _atomic_json(path, manifest)
            raise
    if status == "GEOCODED_PENDING_AUDIT":
        _audit_and_finish(path, manifest)
        print(f"[DAILY v3.18] completed pending audit: {path}", flush=True)
        return True
    raise RuntimeError(
        f"Unfinished daily manifest requires inspection before a new run: {path} status={status}. "
        "No new build/import was started."
    )


def _run_builder(args):
    build = Path(IMPORTER_DIR) / "build_listings_daily_v3_16.py"
    if not build.is_file():
        raise RuntimeError("Missing v3.16 PVP builder: " + str(build))
    errors = Path(RAW_DIR) / "api_errors.jsonl"
    old_error_size = errors.stat().st_size if errors.exists() else 0
    env = dict(os.environ)
    env.update({
        "PAGE_SIZE": str(args.page_size),
        "START_PAGE": "0",
        # 0 = no arbitrary page/detail ceiling. Publication threshold is the normal stop.
        "MAX_PAGES": "0",
        "MAX_DETAILS": "0",
        "STOP_ON_PAST": "0",
        # PVP API ordering is dataPubblicazione,desc; builder verifies monotonicity before stopping.
        "STOP_ON_OLD_PUBLICATION": "1",
        "AUTO_MIN_DATA_PUBBLICAZIONE_FROM_DB": "1",
        "MIN_DATA_PUBBLICAZIONE": "",
        "MIN_DATA_PUBBLICAZIONE_SAFETY_DAYS": str(args.safety_days),
    })
    print("[DAILY v3.18] PVP discovery: dataPubblicazione desc + overlap stop; detail only new/recent IDs", flush=True)
    cp = subprocess.run([sys.executable, str(build)], env=env, check=False)
    if cp.returncode:
        raise RuntimeError(f"PVP builder failed with exit code {cp.returncode}; zero importer writes")
    new_errors = _read_new_jsonl(errors, old_error_size)
    search_errors = [x for x in new_errors if str(x.get("stage") or "").lower() == "search"]
    detail_errors = [x for x in new_errors if str(x.get("stage") or "").lower() == "detail"]
    if search_errors:
        raise RuntimeError(f"PVP discovery had {len(search_errors)} search-page error(s); zero importer writes")
    if detail_errors:
        print(f"[DAILY v3.18] warning: {len(detail_errors)} detail error(s); those IDs will be retried on a later run", flush=True)
    return {"new_error_rows": len(new_errors), "search_errors": len(search_errors), "detail_errors": len(detail_errors)}


def _verify_anncsu_index(path: Path):
    path = Path(path).resolve()
    if not path.is_file():
        raise RuntimeError(
            "ANNCSU SQLite index missing: " + str(path) + "\n"
            "Build it first with: python src/geocoding/anncsu_sqlite_index_v1.py --auto")
    uri = "file:" + str(path).replace("\\", "/") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        meta = {k:v for k,v in con.execute("SELECT key,value FROM meta")}
        if meta.get("version") != "anncsu_sqlite_index_v1":
            raise RuntimeError("Unsupported ANNCSU SQLite index version: " + str(meta.get("version")))
        qc = con.execute("PRAGMA quick_check").fetchone()[0]
        if qc != "ok":
            raise RuntimeError("ANNCSU SQLite quick_check failed: " + str(qc))
    finally:
        con.close()
    return meta


def run_daily(args):
    ensure_runtime_dirs()
    total_t = time.perf_counter()
    if (os.environ.get("ASTE_EASY_CONTAINER", "").strip() == "1"
            and os.environ.get("ASTE_EASY_PREFLIGHT_DONE", "").strip() != "1"):
        container_preflight.run_preflight(write_report=True, fail=True)
    load_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE", "GOOGLE_GEOCODING_API_KEY"))

    ann_index = Path(args.anncsu_index).resolve()
    ann_meta = _verify_anncsu_index(ann_index)
    os.environ["ANNCSU_SQLITE_INDEX"] = str(ann_index)
    print(f"[DAILY v3.18] ANNCSU SQLite ready: {ann_index.name}; built={ann_meta.get('built_at','?')}", flush=True)

    locality_meta = locality_guard.validate_index(locality_guard.DEFAULT_INDEX)
    print(f"[DAILY v3.18] ISTAT locality index ready: rows={locality_meta['rows']:,}", flush=True)

    # Any previous daily geocode interruption is finished first, using its own authorization manifest.
    if _resume_if_needed(args):
        print("[DAILY v3.18] previous interrupted run recovered. Starting today's discovery now.", flush=True)

    t = time.perf_counter()
    importer.assert_rpc_available()
    rpc_sec = _seconds(t)

    t = time.perf_counter()
    build_summary = _run_builder(args)
    build_sec = _seconds(t)

    t = time.perf_counter()
    plan = importer.build_plan()
    plan_sec = _seconds(t)
    n = len(plan["records"])
    print(f"[DAILY v3.18] plan: new eligible={n}; soggetti={plan['subjects']}; allegati={plan['attachments']}", flush=True)
    print(f"[DAILY v3.18] RAW SHA={plan['raw_sha256']}", flush=True)

    if n == 0:
        stamp = _stamp()
        audit_path = Path(REPORTS_DIR) / ("production_audit_v3_13_" + stamp + ".json")
        t = time.perf_counter()
        a = audit.run([], expected_subjects=0, expected_attachments=0, raw_sha256=plan["raw_sha256"], report_path=audit_path)
        audit_sec = _seconds(t)
        if a.get("result") != "PASS":
            raise RuntimeError("NO_NEW but global read-only audit failed: " + str(audit_path))
        out = Path(REPORTS_DIR) / ("daily_run_v3_18_" + stamp + ".json")
        obj = {
            "version": VERSION, "status": "NO_NEW", "created_at": datetime.now().astimezone().isoformat(),
            "raw_sha256": plan["raw_sha256"], "eligible_new": 0, "build_summary": build_summary, "planner_stats": plan.get("planner_stats", {}),
            "anncsu_index": {"path": str(ann_index), "built_at": ann_meta.get("built_at"),
                             "strad_rows": ann_meta.get("strad_rows"), "indir_rows": ann_meta.get("indir_rows")},
            "istat_locality_index": locality_meta,
            "audit_report": str(audit_path), "audit_summary": a,
            "timings_seconds": {"rpc_check": rpc_sec, "pvp_build": build_sec, "plan": plan_sec, "audit": audit_sec, "total": _seconds(total_t)},
        }
        _atomic_json(out, obj)
        print(f"[DAILY v3.18] NO_NEW; global_pending=0; total={_fmt(obj['timings_seconds']['total'])}; report={out}", flush=True)
        return 0

    if n > args.daily_max_new:
        raise RuntimeError(
            f"DAILY SAFETY GATE: {n} eligible new rows exceed --daily-max-new {args.daily_max_new}; zero writes. "
            "Review the plan before treating this as a normal daily update."
        )
    if n > args.max_geocode:
        raise RuntimeError(f"Daily plan {n} exceeds --max-geocode {args.max_geocode}; zero writes")

    t = time.perf_counter()
    readiness = prod.preflight_usage(monthly_cap=args.google_monthly_cap, run_cap=args.google_daily_run_cap)
    google_preflight_sec = _seconds(t)
    if readiness.get("remaining", 0) <= 0:
        print("[DAILY v3.18] Google monthly app cap exhausted: all uncached Google candidates will use offline fallback", flush=True)

    # Validate all local geocoding assets before the first write. Heavy ANNCSU scanning happens later,
    # only now that we know there is real new work to do.
    t = time.perf_counter()
    geocoder.prepare_context(no_anncsu=False)
    asset_sec = _seconds(t)

    run_stamp = _stamp()
    staging_path = Path(REPORTS_DIR) / ("daily_import_payload_v3_18_" + run_stamp + ".jsonl")
    _write_staging(staging_path, plan)
    path = Path(REPORTS_DIR) / ("daily_manifest_v3_18_" + run_stamp + ".json")
    manifest = {
        "version": MANIFEST_VERSION,
        "status": "IMPORT_RUNNING",
        "created_at": datetime.now().astimezone().isoformat(),
        "raw_sha256": plan["raw_sha256"],
        "eligible_new": n,
        "pvp_ids": [str(r[0]["pvp_id"]) for r in plan["records"]],
        "expected_soggetti": plan["subjects"],
        "expected_allegati": plan["attachments"],
        "staging_file": str(staging_path),
        "plan_meta": {
            "incoming_unique": plan["incoming_unique"], "existing": plan["existing"],
            "new_candidates": plan["new_candidates"], "decisions": plan["decisions"],
            "primary_sources": plan["primary_sources"],
            "planner_stats": plan.get("planner_stats", {}),
        },
        "build_summary": build_summary,
        "anncsu_index": {"path": str(ann_index), "built_at": ann_meta.get("built_at"),
                         "strad_rows": ann_meta.get("strad_rows"), "indir_rows": ann_meta.get("indir_rows"),
                         "strad_sha256": ann_meta.get("strad_sha256"), "indir_sha256": ann_meta.get("indir_sha256")},
        "istat_locality_index": locality_meta,
        "google_readiness_before_import": readiness,
        "timings_seconds": {
            "rpc_check": rpc_sec, "pvp_build": build_sec, "plan": plan_sec,
            "google_preflight": google_preflight_sec, "asset_readiness": asset_sec,
        },
    }
    _atomic_json(path, manifest)
    print(f"[DAILY v3.18] manifest: {path}", flush=True)

    t = time.perf_counter()
    try:
        imp = importer.run(apply=True, expected_new=n, expected_sha=plan["raw_sha256"], prebuilt_plan=plan)
        manifest["timings_seconds"]["import"] = _seconds(t)
        manifest["import_summary"] = imp
        manifest["status"] = "IMPORTED_PENDING_GEOCODE"
        _atomic_json(path, manifest)
    except Exception as exc:
        manifest["timings_seconds"]["import"] = _seconds(t)
        manifest["status"] = "IMPORT_INTERRUPTED"
        manifest["last_error_type"] = type(exc).__name__
        _atomic_json(path, manifest)
        raise

    t = time.perf_counter()
    try:
        geo = prod.run_for_ids(
            manifest["pvp_ids"], apply=True, max_rows=args.max_geocode,
            monthly_cap=args.google_monthly_cap, run_cap=args.google_daily_run_cap,
        )
        manifest["timings_seconds"]["geocode"] = _seconds(t)
        manifest["geocode_summary"] = geo
        manifest["status"] = "GEOCODED_PENDING_AUDIT"
        _atomic_json(path, manifest)
    except Exception as exc:
        manifest["timings_seconds"]["geocode"] = _seconds(t)
        manifest["status"] = "GEOCODE_INTERRUPTED"
        manifest["last_error_type"] = type(exc).__name__
        _atomic_json(path, manifest)
        print(f"[DAILY v3.18] geocode interrupted; next --run auto-resumes manifest {path}", flush=True)
        raise

    _audit_and_finish(path, manifest)
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    manifest["timings_seconds"]["total"] = _seconds(total_t)
    _atomic_json(path, manifest)

    tms = manifest["timings_seconds"]
    print("", flush=True)
    print("[DAILY v3.18] COMPLETED", flush=True)
    print(f"[DAILY v3.18] new={n}; Google HTTP={manifest['geocode_summary'].get('google_calls')} cache={manifest['geocode_summary'].get('google_cache_hits')}", flush=True)
    print(f"[DAILY v3.18] times: build={_fmt(tms.get('pvp_build'))} plan={_fmt(tms.get('plan'))} import={_fmt(tms.get('import'))} geocode={_fmt(tms.get('geocode'))} audit={_fmt(tms.get('audit'))} TOTAL={_fmt(tms.get('total'))}", flush=True)
    print(f"[DAILY v3.18] manifest: {path}", flush=True)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Aste Easy v3.18 container-ready daily + locality guard")
    ap.add_argument("--run", action="store_true", help="Run complete daily production flow")
    ap.add_argument("--daily-max-new", type=int, default=1500, help="Hard safety gate before any import write")
    ap.add_argument("--max-geocode", type=int, default=2000)
    ap.add_argument("--google-monthly-cap", type=int, default=9000)
    ap.add_argument("--google-daily-run-cap", type=int, default=1000, help="Max NEW Google HTTP attempts in one daily run")
    ap.add_argument("--safety-days", type=int, default=2, help="Publication-date overlap used by PVP detail filter")
    ap.add_argument("--page-size", type=int, default=200)
    ap.add_argument("--anncsu-index", type=Path, default=Path(ANNCSU_DIR) / "anncsu_runtime.sqlite3",
                    help="Persistent ANNCSU SQLite index (required in v3.16)")
    args = ap.parse_args(argv)
    if not args.run:
        ap.error("Use --run")
    if args.daily_max_new < 1 or args.max_geocode < 1 or args.google_monthly_cap < 1 or args.google_daily_run_cap < 1:
        ap.error("caps must be positive")
    if args.max_geocode < args.daily_max_new:
        ap.error("--max-geocode must be >= --daily-max-new")
    if not 0 <= args.safety_days <= 14:
        ap.error("--safety-days must be 0..14")
    if not 10 <= args.page_size <= 500:
        ap.error("--page-size must be 10..500")
    return run_daily(args)


if __name__ == "__main__":
    raise SystemExit(main())
