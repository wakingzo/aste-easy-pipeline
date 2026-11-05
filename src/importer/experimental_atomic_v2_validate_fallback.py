#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
for _p in (SRC, HERE, SRC / "pipeline", SRC / "geocoding"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pvp_import_atomic_v2 as importer
import pvp_import_consolidated_preview as previous
from experimental_atomic_v2_validate_real import (
    VALIDATE_RPC, SYNTHETIC_PVP_ID, load_raw_by_id, load_attachments_for,
    load_subjects_for, rpc_validate
)
from project_env import load_project_env

def choose_real_pid(raw_by_id):
    # Prefer a row with a valid primary PVP coordinate so the fallback can be
    # validated as a resolved primary. Otherwise any RAW row is acceptable.
    for pid, raw_obj in raw_by_id.items():
        att_by_id = defaultdict(list)
        for a in load_attachments_for(pid):
            att_by_id[pid].append(a)
        preview_row, _ = previous.derive_new_consistent(raw_obj, att_by_id)
        if preview_row.get("latitudine") not in (None, "") and preview_row.get("longitudine") not in (None, ""):
            return pid, preview_row
    pid, raw_obj = next(iter(raw_by_id.items()))
    att_by_id = defaultdict(list)
    for a in load_attachments_for(pid):
        att_by_id[pid].append(a)
    preview_row, _ = previous.derive_new_consistent(raw_obj, att_by_id)
    return pid, preview_row

def main():
    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    raw_by_id=load_raw_by_id()
    pid, preview_row=choose_real_pid(raw_by_id)
    raw_obj=raw_by_id[pid]

    attachments=load_attachments_for(pid)
    subjects=load_subjects_for(pid)
    subjects, attachments=importer.prepare_children(pid, subjects, attachments)

    listing=importer.prepare_listing(preview_row, raw_obj)
    locations=importer.fallback_primary_location(preview_row)

    listing["pvp_id"]=SYNTHETIC_PVP_ID
    listing["geocode_input_hash"]=importer.fingerprint(listing)

    for row in subjects:
        row["pvp_id"]=SYNTHETIC_PVP_ID
    for row in attachments:
        row["pvp_id"]=SYNTHETIC_PVP_ID
    for row in locations:
        row["pvp_id"]=SYNTHETIC_PVP_ID

    result=rpc_validate(base,key,listing,subjects,attachments,locations)
    rpc_result=result.get("rpc_result") or {}

    if result.get("validated") is not True or result.get("rolled_back") is not True:
        raise RuntimeError("Validator returned unexpected response")
    if rpc_result.get("inserted") is not True:
        raise RuntimeError("Fallback payload was not inserted inside validator")
    if rpc_result.get("locations") != 1:
        raise RuntimeError("Fallback validator expected exactly one location")
    if sum(bool(x.get("is_primary")) for x in locations) != 1:
        raise RuntimeError("Fallback validator expected exactly one primary")

    loc=locations[0]
    summary={
        "validated":True,
        "rolled_back":True,
        "source_pvp_id":pid,
        "synthetic_pvp_id":SYNTHETIC_PVP_ID,
        "location_source":loc.get("source"),
        "location_status":loc.get("status"),
        "location_precision":loc.get("precision"),
        "warnings":loc.get("warnings"),
        "locations":1,
        "primary_locations":1,
        "soggetti":len(subjects),
        "allegati":len(attachments),
        "rpc_result":rpc_result,
    }
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
