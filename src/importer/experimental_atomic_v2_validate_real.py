#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
for _p in (SRC, HERE, SRC / "pipeline", SRC / "geocoding"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pvp_import_atomic_v2 as importer
import pvp_import_consolidated_preview as previous
from project_env import load_project_env

VALIDATE_RPC = "aste_validate_new_listing_v2"
SYNTHETIC_PVP_ID = "9999999999999999998"

def load_raw_by_id():
    out = {}
    with previous.IN_RAW.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            pid = previous.clean(obj.get("pvp_id"))
            if pid:
                out[pid] = obj
    return out

def load_attachments_for(pid):
    return importer._scan_jsonl_for_ids(previous.IN_ALLEGATI, {pid})

def load_subjects_for(pid):
    raw = importer._scan_jsonl_for_ids(previous.IN_SOGGETTI, {pid})
    out = []
    for original in raw:
        out.extend(previous.normalize_soggetti_rows([original]))
    return out

def choose_pid(raw_by_id, locations_by_id, explicit=None):
    if explicit:
        if explicit not in raw_by_id:
            raise RuntimeError("Requested pvp_id not found in RAW: " + explicit)
        if explicit not in locations_by_id:
            raise RuntimeError("Requested pvp_id has no location preview: " + explicit)
        return explicit

    # Prefer a real row with >1 location so the test exercises the new table well.
    candidates = sorted(
        (pid for pid in raw_by_id if pid in locations_by_id),
        key=lambda pid: (len(locations_by_id[pid]) <= 1, pid)
    )
    if not candidates:
        raise RuntimeError("No RAW row with listing_locations preview available")
    return candidates[0]

def build_payload(pid, raw_obj, locations):
    attachments = load_attachments_for(pid)
    att_by_id = defaultdict(list)
    for a in attachments:
        att_by_id[pid].append(a)

    preview_row, _source = previous.derive_new_consistent(raw_obj, att_by_id)
    listing = importer.prepare_listing(preview_row, raw_obj)

    subjects = load_subjects_for(pid)
    subjects, attachments = importer.prepare_children(pid, subjects, attachments)

    # Rewrite only the identity so the real existing listing does not conflict.
    listing["pvp_id"] = SYNTHETIC_PVP_ID
    # Keep legacy fingerprint internally coherent with the rewritten identity.
    listing["geocode_input_hash"] = importer.fingerprint(listing)

    new_subjects = []
    for row in subjects:
        x = dict(row)
        x["pvp_id"] = SYNTHETIC_PVP_ID
        new_subjects.append(x)

    new_attachments = []
    for row in attachments:
        x = dict(row)
        x["pvp_id"] = SYNTHETIC_PVP_ID
        new_attachments.append(x)

    new_locations = []
    for row in locations:
        x = dict(row)
        x["pvp_id"] = SYNTHETIC_PVP_ID
        new_locations.append(x)

    return listing, new_subjects, new_attachments, new_locations

def rpc_validate(base, key, listing, subjects, attachments, locations):
    url = base.rstrip("/") + "/rest/v1/rpc/" + VALIDATE_RPC
    payload = json.dumps({
        "p_listing": listing,
        "p_soggetti": subjects,
        "p_allegati": attachments,
        "p_locations": locations,
    }, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=payload, method="POST", headers={
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    with urlopen(req, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))

def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate one real RAW payload through atomic importer v2 with DB rollback")
    ap.add_argument("--pvp-id", help="Optional real PVP ID from current RAW")
    args = ap.parse_args(argv)

    load_project_env(required=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE"))
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE"]

    raw_by_id = load_raw_by_id()
    locations_by_id = importer.load_location_preview()
    pid = choose_pid(raw_by_id, locations_by_id, args.pvp_id)

    listing, subjects, attachments, locations = build_payload(
        pid, raw_by_id[pid], locations_by_id[pid]
    )

    result = rpc_validate(base, key, listing, subjects, attachments, locations)
    rpc_result = result.get("rpc_result") or {}

    if result.get("validated") is not True or result.get("rolled_back") is not True:
        raise RuntimeError("Validator returned unexpected response")
    if rpc_result.get("inserted") is not True:
        raise RuntimeError("Real payload was not inserted inside validator")
    if rpc_result.get("locations") != len(locations):
        raise RuntimeError("Location count mismatch")
    if rpc_result.get("soggetti") != len(subjects):
        raise RuntimeError("Subject count mismatch")
    if rpc_result.get("allegati") != len(attachments):
        raise RuntimeError("Attachment count mismatch")

    summary = {
        "validated": True,
        "rolled_back": True,
        "source_pvp_id": pid,
        "synthetic_pvp_id": SYNTHETIC_PVP_ID,
        "locations": len(locations),
        "primary_locations": sum(bool(x.get("is_primary")) for x in locations),
        "soggetti": len(subjects),
        "allegati": len(attachments),
        "rpc_result": rpc_result,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
