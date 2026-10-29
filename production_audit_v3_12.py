#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy - audit finale production v3.12 (SOLO LETTURA).

Controlla direttamente Supabase:
- presenza di tutti i pvp_id autorizzati nel manifest;
- stato geocoding / coordinate / maps_link del lotto;
- conteggi soggetti e allegati del lotto;
- PENDING globali;
- campione deterministico di record.

ZERO scritture Supabase. ZERO Google.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

VERSION = "production_audit_v3_12_1"
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
for p in (SRC, SRC / "pipeline", SRC / "importer", SRC / "geocoding"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    from project_env import load_project_env
except Exception as exc:
    raise SystemExit(f"ERRORE: impossibile importare project_env dal progetto: {exc}")

REPORTS_DIR = ROOT / "runtime" / "reports"
TIMEOUT = 90

LISTING_SELECT = ",".join([
    "pvp_id", "indirizzo", "comune", "sigla_provincia",
    "latitudine", "longitudine", "maps_link",
    "geocode_status", "geocode_precision", "geocode_source",
])


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
    return (
        lat is not None and lon is not None
        and -90 <= lat <= 90 and -180 <= lon <= 180
        and not (abs(lat) < 1e-15 and abs(lon) < 1e-15)
    )


def _headers(key, *, count=False):
    h = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    }
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
                data = json.loads(body or "[]")
                return data, dict(resp.headers.items())
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Supabase HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            last = exc
            if attempt < 2:
                print(f"[AUDIT RETRY] {type(exc).__name__}; tentativo {attempt+2}/3", flush=True)
                time.sleep(1.5 * (attempt + 1))
                continue
    raise RuntimeError(f"Errore rete Supabase dopo 3 tentativi: {last}") from last


def _url(base, route, params):
    return base.rstrip("/") + "/rest/v1/" + route + "?" + urlencode(params)


def _fetch_listing_batch(base, key, ids):
    vals = ",".join(ids)
    params = [
        ("select", LISTING_SELECT),
        ("pvp_id", f"in.({vals})"),
        ("limit", str(max(1, len(ids)))),
    ]
    data, _ = _request(_url(base, "listings", params), key)
    if not isinstance(data, list):
        raise RuntimeError("Risposta listings inattesa")
    return data


def _count_exact(base, key, route, filters=None):
    params = [("select", "pvp_id"), ("limit", "1")]
    if filters:
        params.extend(filters)
    _data, headers = _request(_url(base, route, params), key, count=True)
    content_range = headers.get("Content-Range") or headers.get("content-range") or ""
    if "/" not in content_range:
        raise RuntimeError(f"Content-Range assente per {route}: {content_range!r}")
    tail = content_range.rsplit("/", 1)[1]
    if tail == "*":
        raise RuntimeError(f"Conteggio esatto non disponibile per {route}")
    return int(tail)


def _count_for_ids(base, key, route, ids, batch_size):
    total = 0
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i+batch_size]
        vals = ",".join(batch)
        total += _count_exact(base, key, route, [("pvp_id", f"in.({vals})")])
    return total


def _count_one(base, key, route, pid):
    return _count_exact(base, key, route, [("pvp_id", "eq." + pid)])


def main():
    ap = argparse.ArgumentParser(description="Audit finale read-only Aste Easy production v3.12")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--batch-size", type=int, default=200)
    ap.add_argument("--sample-size", type=int, default=12)
    args = ap.parse_args()

    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.is_file():
        raise SystemExit(f"Manifest non trovato: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))

    ids = [_clean(x) for x in manifest.get("pvp_ids", []) if _clean(x)]
    if not ids:
        raise SystemExit("Manifest senza pvp_ids")
    if len(ids) != len(set(ids)):
        raise SystemExit("Manifest contiene pvp_id duplicati")

    expected_n = int(manifest.get("eligible_new") or len(ids))
    import_summary = manifest.get("import_summary") or {}
    expected_subjects = import_summary.get("new_soggetti")
    expected_attachments = import_summary.get("new_allegati")

    load_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE"))
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE"]

    print(f"[AUDIT] manifest={manifest_path.name}", flush=True)
    print(f"[AUDIT] lotto autorizzato={len(ids)}; ZERO write; ZERO Google", flush=True)

    rows = []
    for i in range(0, len(ids), args.batch_size):
        batch = ids[i:i+args.batch_size]
        rows.extend(_fetch_listing_batch(base, key, batch))
        done = min(i + len(batch), len(ids))
        if done % 1000 < args.batch_size or done == len(ids):
            print(f"[AUDIT] listings letti {done}/{len(ids)}", flush=True)

    by_id = {}
    duplicate_returned = []
    for row in rows:
        pid = _clean(row.get("pvp_id"))
        if pid in by_id:
            duplicate_returned.append(pid)
        by_id[pid] = row

    missing_ids = [pid for pid in ids if pid not in by_id]
    unexpected_ids = sorted(set(by_id) - set(ids))

    status_counts = Counter(_clean(r.get("geocode_status")) or "NULL" for r in by_id.values())
    precision_counts = Counter(_clean(r.get("geocode_precision")) or "NULL" for r in by_id.values())
    source_counts = Counter(_clean(r.get("geocode_source")) or "NULL" for r in by_id.values())

    invalid_coords = []
    missing_maps = []
    bad_maps = []
    pending_ids = []
    for pid, row in by_id.items():
        if _clean(row.get("geocode_status")) == "PENDING":
            pending_ids.append(pid)
        if not _valid_coords(row):
            invalid_coords.append(pid)
        maps = _clean(row.get("maps_link"))
        if not maps:
            missing_maps.append(pid)
        elif not maps.startswith("https://www.google.com/maps?q="):
            bad_maps.append(pid)

    print("[AUDIT] conteggio soggetti/allegati del lotto...", flush=True)
    subject_count = _count_for_ids(base, key, "listing_soggetti", ids, args.batch_size)
    attachment_count = _count_for_ids(base, key, "listing_allegati", ids, args.batch_size)

    print("[AUDIT] conteggi globali...", flush=True)
    total_listings = _count_exact(base, key, "listings")
    global_pending = _count_exact(base, key, "listings", [("geocode_status", "eq.PENDING")])

    rng = random.Random(3123)
    sample_ids = rng.sample(ids, min(args.sample_size, len(ids)))
    samples = []
    for pid in sample_ids:
        r = by_id.get(pid) or {}
        samples.append({
            "pvp_id": pid,
            "comune": r.get("comune"),
            "indirizzo": r.get("indirizzo"),
            "geocode_status": r.get("geocode_status"),
            "geocode_precision": r.get("geocode_precision"),
            "geocode_source": r.get("geocode_source"),
            "latitudine": r.get("latitudine"),
            "longitudine": r.get("longitudine"),
            "maps_link": r.get("maps_link"),
            "soggetti": _count_one(base, key, "listing_soggetti", pid),
            "allegati": _count_one(base, key, "listing_allegati", pid),
        })

    checks = {
        "manifest_status_completed": manifest.get("status") == "COMPLETED",
        "manifest_id_count_matches_expected": len(ids) == expected_n,
        "all_authorized_listings_present": len(missing_ids) == 0 and len(by_id) == len(ids),
        "no_duplicate_listing_rows": not duplicate_returned,
        "no_unexpected_listing_rows": not unexpected_ids,
        "no_batch_pending": len(pending_ids) == 0,
        "all_batch_rows_have_valid_coords": len(invalid_coords) == 0,
        "all_batch_rows_have_maps_link": len(missing_maps) == 0,
        "all_maps_links_expected_format": len(bad_maps) == 0,
    }
    if expected_subjects is not None:
        checks["subject_count_matches_import"] = subject_count == int(expected_subjects)
    if expected_attachments is not None:
        checks["attachment_count_matches_import"] = attachment_count == int(expected_attachments)

    critical_ok = all(checks.values())
    report = {
        "version": VERSION,
        "mode": "READ_ONLY_ZERO_DB_WRITES_ZERO_GOOGLE",
        "created_at": datetime.now().astimezone().isoformat(),
        "result": "PASS" if critical_ok else "FAIL",
        "manifest": {
            "path": str(manifest_path),
            "status": manifest.get("status"),
            "raw_sha256": manifest.get("raw_sha256"),
            "eligible_new": expected_n,
            "authorized_ids": len(ids),
        },
        "checks": checks,
        "supabase_global": {
            "total_listings": total_listings,
            "pending_listings": global_pending,
        },
        "authorized_batch": {
            "found": len(by_id),
            "missing": len(missing_ids),
            "missing_ids_first_50": missing_ids[:50],
            "unexpected_ids": unexpected_ids[:50],
            "duplicate_returned_ids": duplicate_returned[:50],
            "status_counts": dict(sorted(status_counts.items())),
            "precision_counts": dict(sorted(precision_counts.items())),
            "source_counts": dict(source_counts.most_common()),
            "invalid_coords": len(invalid_coords),
            "invalid_coords_first_50": invalid_coords[:50],
            "missing_maps_link": len(missing_maps),
            "missing_maps_first_50": missing_maps[:50],
            "bad_maps_link_format": len(bad_maps),
            "bad_maps_first_50": bad_maps[:50],
            "pending": len(pending_ids),
            "pending_ids_first_50": pending_ids[:50],
        },
        "children": {
            "listing_soggetti_actual": subject_count,
            "listing_soggetti_expected": expected_subjects,
            "listing_allegati_actual": attachment_count,
            "listing_allegati_expected": expected_attachments,
        },
        "samples": samples,
    }

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    out = REPORTS_DIR / f"production_audit_v3_12_{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("", flush=True)
    print(f"[AUDIT] RESULT={report['result']}", flush=True)
    print(f"[AUDIT] lotto presente: {len(by_id)}/{len(ids)}; PENDING lotto={len(pending_ids)}", flush=True)
    print(f"[AUDIT] coordinate valide: {len(by_id)-len(invalid_coords)}/{len(by_id)}; maps_link mancanti={len(missing_maps)}", flush=True)
    print(f"[AUDIT] soggetti: {subject_count} (attesi {expected_subjects})", flush=True)
    print(f"[AUDIT] allegati: {attachment_count} (attesi {expected_attachments})", flush=True)
    print(f"[AUDIT] DB globale: listings={total_listings}; PENDING={global_pending}", flush=True)
    print(f"[OUT] {out}", flush=True)
    return 0 if critical_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
