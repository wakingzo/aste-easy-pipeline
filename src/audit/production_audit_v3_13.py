#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only production audit for Aste Easy daily v3.13."""
from __future__ import annotations

import json
import math
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

VERSION = "production_audit_v3_13"
TIMEOUT = 90


def _clean(v):
    return "" if v is None else str(v).strip()


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _valid_coords(row):
    lat, lon = _num(row.get("latitudine")), _num(row.get("longitudine"))
    return lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def _headers(key, count=False):
    h = {"apikey": key, "Authorization": "Bearer " + key, "Accept": "application/json"}
    if count:
        h["Prefer"] = "count=exact"
    return h


def _request(url, key, *, count=False):
    last = None
    for attempt in range(3):
        req = Request(url, method="GET", headers=_headers(key, count=count))
        try:
            with urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body or "[]"), dict(resp.headers.items())
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"Audit Supabase HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            last = exc
            if attempt < 2:
                print(f"[AUDIT RETRY] {type(exc).__name__}; retry {attempt+2}/3", flush=True)
                time.sleep(1.5 * (attempt + 1))
                continue
    raise RuntimeError(f"Audit Supabase network error after retries: {last}") from last


def _url(base, route, params):
    return base.rstrip("/") + "/rest/v1/" + route + "?" + urlencode(params)


def _count_exact(base, key, route, filters=None):
    params = [("select", "pvp_id"), ("limit", "1")]
    if filters:
        params.extend(filters)
    _data, headers = _request(_url(base, route, params), key, count=True)
    cr = headers.get("Content-Range") or headers.get("content-range") or ""
    if "/" not in cr or cr.rsplit("/", 1)[1] == "*":
        raise RuntimeError(f"Audit exact count unavailable for {route}: {cr!r}")
    return int(cr.rsplit("/", 1)[1])


def _count_for_ids(base, key, route, ids, batch_size=200):
    total = 0
    for i in range(0, len(ids), batch_size):
        vals = ",".join(ids[i:i+batch_size])
        total += _count_exact(base, key, route, [("pvp_id", f"in.({vals})")])
    return total


def _fetch_listings(base, key, ids, batch_size=200):
    fields = "pvp_id,geocode_status,geocode_precision,geocode_source,latitudine,longitudine,maps_link"
    rows = []
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i+batch_size]
        params = [("select", fields), ("pvp_id", f"in.({','.join(batch)})"), ("limit", str(len(batch)))]
        data, _ = _request(_url(base, "listings", params), key)
        if not isinstance(data, list):
            raise RuntimeError("Audit unexpected listings response")
        rows.extend(data)
    return rows


def run(ids, *, expected_subjects=None, expected_attachments=None, raw_sha256=None, report_path=None):
    ids = [str(x).strip() for x in ids if str(x).strip()]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Audit authorized ids contain duplicates")
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE"]

    rows = _fetch_listings(base, key, ids) if ids else []
    by_id = {_clean(r.get("pvp_id")): r for r in rows}
    missing = [pid for pid in ids if pid not in by_id]
    invalid_coords = [pid for pid, r in by_id.items() if not _valid_coords(r)]
    missing_maps = [pid for pid, r in by_id.items() if not _clean(r.get("maps_link"))]
    pending = [pid for pid, r in by_id.items() if _clean(r.get("geocode_status")) == "PENDING"]

    subjects = _count_for_ids(base, key, "listing_soggetti", ids) if ids else 0
    attachments = _count_for_ids(base, key, "listing_allegati", ids) if ids else 0
    global_pending = _count_exact(base, key, "listings", [("geocode_status", "eq.PENDING")])
    total_listings = _count_exact(base, key, "listings")

    checks = {
        "all_authorized_listings_present": len(by_id) == len(ids) and not missing,
        "no_batch_pending": not pending,
        "all_batch_rows_have_valid_coords": not invalid_coords,
        "all_batch_rows_have_maps_link": not missing_maps,
        "global_pending_zero": global_pending == 0,
    }
    if expected_subjects is not None:
        checks["subject_count_matches_plan"] = subjects == int(expected_subjects)
    if expected_attachments is not None:
        checks["attachment_count_matches_plan"] = attachments == int(expected_attachments)

    result = "PASS" if all(checks.values()) else "FAIL"
    report = {
        "version": VERSION,
        "mode": "READ_ONLY_ZERO_DB_WRITES_ZERO_GOOGLE",
        "created_at": datetime.now().astimezone().isoformat(),
        "result": result,
        "raw_sha256": raw_sha256,
        "authorized_ids": len(ids),
        "checks": checks,
        "supabase_global": {"total_listings": total_listings, "pending_listings": global_pending},
        "batch": {
            "found": len(by_id), "missing": len(missing), "missing_ids_first_50": missing[:50],
            "pending": len(pending), "pending_ids_first_50": pending[:50],
            "invalid_coords": len(invalid_coords), "invalid_coords_first_50": invalid_coords[:50],
            "missing_maps_link": len(missing_maps), "missing_maps_first_50": missing_maps[:50],
            "status_counts": dict(Counter(_clean(r.get("geocode_status")) or "NULL" for r in by_id.values())),
            "precision_counts": dict(Counter(_clean(r.get("geocode_precision")) or "NULL" for r in by_id.values())),
            "source_counts": dict(Counter(_clean(r.get("geocode_source")) or "NULL" for r in by_id.values())),
        },
        "children": {
            "listing_soggetti_actual": subjects, "listing_soggetti_expected": expected_subjects,
            "listing_allegati_actual": attachments, "listing_allegati_expected": expected_attachments,
        },
    }
    if report_path:
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[AUDIT] RESULT={result}; listings={len(by_id)}/{len(ids)}; batch_pending={len(pending)}; global_pending={global_pending}", flush=True)
    print(f"[AUDIT] soggetti={subjects}/{expected_subjects}; allegati={attachments}/{expected_attachments}; invalid_coords={len(invalid_coords)}; missing_maps={len(missing_maps)}", flush=True)
    return report
