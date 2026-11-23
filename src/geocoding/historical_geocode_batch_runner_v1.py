#!/usr/bin/env python3
"""Unattended, resumable RAW-only historical geocode backfill.

Uses the existing, approved v2 preview -> exact-SHA apply path without changing
the geocoder. Every batch is verified before the next begins. This is intended
for the local Docker environment holding the original historical PVP RAW file.
No Google network calls are enabled.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BACKFILL = ROOT / "src/geocoding/historical_geocode_backfill_v2.py"
REPORTS = ROOT / "runtime/reports"
STATE = ROOT / "runtime/state/historical_geocode_backfill_progress_v1.json"
PREVIEW = REPORTS / "historical_geocode_backfill_v2_preview.json"
APPLY = REPORTS / "historical_geocode_backfill_v2_apply.json"


def _read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _run(args, *, report):
    cmd = [sys.executable, str(BACKFILL), *args]
    started = time.monotonic()
    print("[AUTO] " + " ".join(cmd[2:]), flush=True)
    # Show progress rather than suppressing the whole child output for many
    # minutes. Keep only a bounded tail for error diagnosis.
    from collections import deque
    import re
    tail = deque(maxlen=60)
    with subprocess.Popen(
        cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, errors="replace",
    ) as proc:
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            tail.append(line)
            applied = re.search(r"^\\[BACKFILL APPLIED\\] (\\d+)/(\\d+) pvp=(\\d+)", line)
            preview = re.search(r"^\\[BACKFILL\\] (\\d+)/(\\d+) pvp=(\\d+)", line)
            if applied:
                i, total = int(applied.group(1)), int(applied.group(2))
                if i == 1 or i % 10 == 0 or i == total:
                    print(f"[AUTO APPLY] {i}/{total} saved & verified", flush=True)
            elif preview:
                i, total = int(preview.group(1)), int(preview.group(2))
                if i == 1 or i % 25 == 0 or i == total:
                    print(f"[AUTO PREVIEW] {i}/{total} processed", flush=True)
            elif line.startswith(("[BACKFILL ERROR]", "Traceback", "RuntimeError:")):
                print(line, flush=True)
        rc = proc.wait()
    if rc:
        print("[AUTO ERROR] Child process failed; later batches STOPPED.", file=sys.stderr)
        print("\n".join(tail)[-6500:], file=sys.stderr)
        raise RuntimeError("Historical backfill command failed (exit " + str(rc) + ")")
    if not report.is_file():
        raise RuntimeError("Expected report missing: " + str(report))
    obj = _read(report)
    print("[AUTO] completed in %.1fs" % (time.monotonic() - started), flush=True)
    return obj


def validate_preview(obj, *, batch_size, require_full_raw=True):
    if obj.get("mode") != "PREVIEW_ZERO_DB_WRITES" or obj.get("db_writes") != 0:
        raise RuntimeError("Unexpected preview mode/writes")
    if obj.get("google_calls", 0) != 0:
        raise RuntimeError("Unexpected Google API calls")
    n = int(obj.get("eligible_candidates") or 0)
    cases = obj.get("cases") or []
    errors = obj.get("errors") or []
    if not 0 <= n <= batch_size or n != len(cases) + len(errors):
        raise RuntimeError("Malformed/incomplete candidate count in preview")
    if n and not obj.get("batch_last_pvp_id"):
        raise RuntimeError("Missing progress cursor in preview")
    if n and not obj.get("plan_sha256"):
        raise RuntimeError("Missing full-plan SHA in preview")
    if require_full_raw:
        stats = obj.get("raw_stats") or {}
        if not stats.get("raw_file_present") or stats.get("selected_raw_batch") != n:
            raise RuntimeError("RAW source absent or batch includes non-RAW listings")
        if any(case.get("input_source") != "RAW_PVP_HISTORICAL" for case in cases):
            raise RuntimeError("Non-RAW listing in apply preview")
    safe = [str(case["pvp_id"]) for case in cases if case.get("safe_to_apply") is True]
    if len(safe) != len(set(safe)):
        raise RuntimeError("Duplicate safe pvp_id in preview")
    return safe


def verify_apply(obj, preview, *, expected_applied):
    if obj.get("mode") != "APPLY_SAFE_GUARDED":
        raise RuntimeError("Apply report has wrong mode")
    if obj.get("plan_sha256") != preview.get("plan_sha256"):
        raise RuntimeError("Apply plan SHA unexpectedly differs")
    counts = obj.get("counts") or {}
    if obj.get("errors") or int(counts.get("applied") or 0) != expected_applied:
        raise RuntimeError("Apply counts differ from approved batch")
    if obj.get("db_writes") != expected_applied or obj.get("google_calls", 0):
        raise RuntimeError("Unexpected DB write count/Google usage")


def main(argv=None):
    p = argparse.ArgumentParser(description="Finish all remaining RAW historical backfill batches unattended")
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--workers", type=int, default=3, help="Parallel independent Supabase writes (1..4)")
    p.add_argument("--max-batches", type=int, default=0, help="0 = until all original RAW candidates scanned")
    p.add_argument("--state-file", type=Path, default=STATE)
    args = p.parse_args(argv)
    if not 1 <= args.batch_size <= 250:
        p.error("--batch-size must be 1..250")
    if args.max_batches < 0:
        p.error("--max-batches cannot be negative")
    if not 1 <= args.workers <= 4:
        p.error("--workers must be 1..4")
    args.state_file.parent.mkdir(parents=True, exist_ok=True)

    progress = _read(args.state_file) if args.state_file.exists() else {
        "last_pvp_id": "",
        "batches_completed": 0,
        "raw_candidates_scanned": 0,
        "applied": 0,
        "blocked": 0,
        "resolver_errors_skipped": 0,
        "failed_pvp_ids": [],
        "finished": False,
    }
    if progress.get("finished"):
        print("[AUTO] Previous scan already finished. Progress: " + str(args.state_file))
        print(json.dumps(progress, ensure_ascii=False, indent=2))
        return 0

    batch_count = 0
    while args.max_batches == 0 or batch_count < args.max_batches:
        cursor = str(progress.get("last_pvp_id") or "")
        base = ["--require-raw", "--max-rows", str(args.batch_size)]
        if cursor:
            base += ["--after-pvp-id", cursor]

        # The original backfill writes a JSON preview. Freeze its SHA.
        preview = _run(base, report=PREVIEW)
        safe = validate_preview(preview, batch_size=args.batch_size)
        n = int(preview["eligible_candidates"])
        if not n:
            progress["finished"] = True
            _write_atomic(args.state_file, progress)
            break

        preview_errors = preview.get("errors") or []
        blocked = sum(not case.get("safe_to_apply") for case in preview["cases"])
        planned_apply = preview
        apply_args = list(base)

        if preview_errors and safe:
            # One malformed case must not block every valid auction in its batch.
            # Re-preview only the valid RAW IDs, then apply their exact new SHA.
            # This does not add a single new geocoding rule or source.
            targeted = [
                "--require-raw", "--max-rows", str(len(safe)),
                "--pvp-ids", ",".join(safe),
            ]
            planned_apply = _run(targeted, report=PREVIEW)
            confirmed = validate_preview(planned_apply, batch_size=len(safe))
            if planned_apply.get("errors") or len(confirmed) != len(safe) or set(confirmed) != set(safe):
                raise RuntimeError("Targeted safe-only preview changed; STOPPED with zero batch writes")
            apply_args = targeted

        if safe:
            apply_result = _run(
                [*apply_args, "--apply", "--apply-workers", str(args.workers),
                 "--expected-plan-sha", planned_apply["plan_sha256"]],
                report=APPLY,
            )
            verify_apply(apply_result, planned_apply, expected_applied=len(safe))

        # Advance only after all approved writes have been verified. On failure,
        # the cursor stays put and the next launch safely skips already-modern
        # listings, preventing silent loss of unprocessed rows.
        failures = [str(row.get("pvp_id")) for row in preview_errors]
        progress["last_pvp_id"] = str(preview["batch_last_pvp_id"])
        progress["batches_completed"] += 1
        progress["raw_candidates_scanned"] += n
        progress["applied"] += len(safe)
        progress["blocked"] += blocked
        progress["resolver_errors_skipped"] += len(preview_errors)
        progress["failed_pvp_ids"].extend(failures)
        _write_atomic(args.state_file, progress)
        batch_count += 1
        print(
            f"[AUTO] batch={progress['batches_completed']} scanned={n} "
            f"applied={len(safe)} blocked={blocked} errors={len(failures)} "
            f"cursor={progress['last_pvp_id']} | total applied={progress['applied']}",
            flush=True,
        )

    print("[AUTO] " + ("SCAN COMPLETE" if progress["finished"] else "BATCH LIMIT REACHED"))
    print(json.dumps(progress, ensure_ascii=False, indent=2))
    print("[AUTO] Results: " + str(args.state_file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
