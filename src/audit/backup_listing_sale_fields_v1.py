#!/usr/bin/env python3
"""Read-only backup of current live sale fields before any historical repair.

Writes a timestamped, verified JSONL snapshot outside the Git repository:
runtime/backups/listing_sale_fields_<timestamp>.jsonl + .meta.json
Does not modify Supabase, historical RAW, or the pipeline. Never prints keys.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]


def _download_page(base, key, offset, limit):
    query = urlencode({
        "select": "pvp_id,lotto,esperimento_vendita",
        "order": "pvp_id.asc",
        "offset": str(offset),
        "limit": str(limit),
    })
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Prefer": "count=exact",
        "Accept": "application/json",
    }
    with urlopen(Request(base.rstrip("/") + "/rest/v1/listings?" + query, headers=headers), timeout=90) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        cr = resp.headers.get("Content-Range", "")
    if not isinstance(body, list):
        raise RuntimeError("Unexpected Supabase response (not array)")
    match = re.search(r"/(\d+)$", cr)
    if not match:
        raise RuntimeError("Missing exact Supabase table count (Content-Range)")
    return body, int(match.group(1))


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--page-size", type=int, default=500)
    args = p.parse_args(argv)
    if not 1 <= args.page_size <= 1000:
        p.error("--page-size must be 1..1000")

    base = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE", "").strip()
    if not base or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE must be set in Docker")

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    dest = ROOT / "runtime" / "backups" / ("listing_sale_fields_" + stamp + ".jsonl")
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".jsonl.tmp")
    count = 0
    expected = None
    sha = hashlib.sha256()
    seen_ids = set()
    try:
        with open(temp, "xb") as fh:
            while True:
                rows, total = _download_page(base, key, count, args.page_size)
                if expected is None:
                    expected = total
                if total != expected:
                    raise RuntimeError("Table changed during snapshot; aborting")
                for row in rows:
                    pid = str(row.get("pvp_id") or "")
                    if not pid or pid in seen_ids:
                        raise RuntimeError("Duplicate/missing PVP ID; aborting backup")
                    seen_ids.add(pid)
                    obj = {k: row.get(k) for k in ("pvp_id", "lotto", "esperimento_vendita")}
                    data = (json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
                    fh.write(data)
                    sha.update(data)
                    count += 1
                print(f"[BACKUP READ ONLY] {count}/{expected}", flush=True)
                if not rows:
                    break
                if count >= expected:
                    break
            fh.flush()
            os.fsync(fh.fileno())
        if count != expected:
            raise RuntimeError(f"Incomplete snapshot: {count} of {expected}")
        os.replace(temp, dest)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    manifest = {
        "status": "COMPLETE_READ_ONLY",
        "created_at": datetime.now().astimezone().isoformat(),
        "rows": count,
        "sha256": sha.hexdigest(),
        "file": str(dest),
        "columns": ["pvp_id", "lotto", "esperimento_vendita"],
        "warning": "This is an application-level snapshot, not a transactional DB dump.",
    }
    path = dest.with_suffix(".meta.json")
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
