#!/usr/bin/env python3
"""Offline PVP API vs contextual filename parser comparison, read-only.

Reads complete raw sample JSONL created by pvp_sale_fields_raw_sample_v1.py
and writes a report under runtime/reports. No network or DB writes.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from importer.pvp_sale_attempt_parser_v1 import resolve_sale_attempt
from importer.pvp_sale_fields_v1 import pvp_lot_number


def compare_raw(path: Path):
    counts = Counter()
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            pid = str(raw.get("pvp_id") or "")
            search = raw.get("search_item") or {}
            detail_response = raw.get("detail_response") or {}
            detail = detail_response.get("body") or {}
            if not isinstance(search, dict) or not isinstance(detail, dict) or not detail:
                raise ValueError(f"Bad RAW schema on line {line_no}: missing PVP detail body")
            lot = detail.get("lotto") or {}
            if not isinstance(lot, dict):
                lot = {}
            descr = (search.get("descLotto") or lot.get("descLotto") or detail.get("descLotto") or "")
            attempt = resolve_sale_attempt(detail, search)
            counts["raw_examined"] += 1
            if attempt["api_value"]:
                counts["api_field_populated"] += 1
            if attempt["parsed_value"]:
                counts["filename_parser_populated"] += 1
            if attempt["api_parser_agree"] is not None:
                counts["api_filename_both_comparable"] += 1
                counts["api_filename_equal" if attempt["api_parser_agree"] else "api_filename_different"] += 1
            if attempt["source"] == "missing":
                counts["no_attempt_found"] += 1
            if attempt["multiple_filename_values"]:
                counts["multiple_filename_values"] += 1
            selected_lot = pvp_lot_number(detail, search, str(descr))
            if selected_lot:
                counts["lot_populated"] += 1
            if not selected_lot and (search.get("numeroLotto") or lot.get("codLotto")):
                counts["lot_suppressed_or_uninterpretable"] += 1

            rows.append({
                "pvp_id": pid,
                "api_attempt": attempt["api_value"],
                "filename_attempt": attempt["parsed_value"],
                "selected_attempt": attempt["value"],
                "source": attempt["source"],
                "api_parser_agree": attempt["api_parser_agree"],
                "multiple_filename_values": attempt["multiple_filename_values"],
                "filename_evidence": attempt["evidence"],
                "search_numeroLotto": search.get("numeroLotto"),
                "detail_lotto_codLotto": lot.get("codLotto"),
                "selected_lotto": selected_lot,
            })
    return {"counts": dict(counts), "rows": rows}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="Path to pvp_sale_fields_raw_*.jsonl, default latest local audit sample")
    args = ap.parse_args(argv)
    if args.input:
        inp = Path(args.input)
    else:
        candidates = sorted((ROOT / "runtime/raw/audits").glob("pvp_sale_fields_raw_*.jsonl"))
        if not candidates:
            ap.error("No RAW audit sample found; supply --input")
        inp = candidates[-1]
    if not inp.is_file():
        ap.error(f"Cannot open {inp}")
    data = compare_raw(inp)
    data["mode"] = "OFFLINE_READ_ONLY_NO_DB_WRITES"
    data["input_file"] = str(inp)
    data["created_at"] = datetime.now().astimezone().isoformat()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    report = ROOT / "runtime/reports" / f"pvp_sale_fields_compare_{stamp}.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"counts": data["counts"], "report": str(report)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
