#!/usr/bin/env python3
"""Sample genuine PVP search/detail API RAW without touching the production importer.

Default --latest 20 reads the newest advertised entries (published-date order).
Extra --ids fetches explicit PVP listings, even when absent from the local RAW.
Full unmodified API responses are written to runtime/raw/audits/ only.
Summary shows raw campo vs selected value, including ad-code suppressions.
No Supabase/Google API calls, no DB writes, no production RAW modifications.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from importer import build_listings_daily_v3_16 as pvp
from importer.pvp_sale_fields_v1 import pvp_sale_attempt, pvp_lot_number


def _desc(search_item, detail_body):
    lot = detail_body.get("lotto") or {}
    if not isinstance(lot, dict):
        lot = {}
    for v in (search_item.get("descLotto"), lot.get("descLotto"), detail_body.get("descLotto")):
        s = str(v or "").strip()
        if s:
            return s
    return ""


def _dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    a = argparse.ArgumentParser()
    a.add_argument("--latest", type=int, default=20, help="Published-date-desc PVP search sample (0 disables)")
    a.add_argument("--ids", default="", help="Comma-separated exact PVP IDs to inspect in addition to latest")
    args = a.parse_args(argv)
    if not 0 <= args.latest <= 100:
        a.error("--latest must be 0..100")
    extra_ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    if not all(re.fullmatch(r"\d{1,19}", x) for x in extra_ids):
        a.error("--ids must contain only numeric PVP IDs")
    if not args.latest and not extra_ids:
        a.error("Provide --latest or --ids")

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    out = ROOT / "runtime/raw/audits"
    reports = ROOT / "runtime/reports"
    out.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)

    wanted = {}
    search_json = None
    errors = []
    with pvp.requests.Session() as session:
        if args.latest:
            # Uses exactly the same PVP search endpoint, request body and headers
            # as production. Does not call its main() and does not append to RAW.
            search_json = pvp.fetch_search_page(session, page=0, size=max(10, args.latest))
            if not isinstance(search_json, dict):
                raise RuntimeError("Unexpected PVP search response")
            content = (search_json.get("body") or {}).get("content") or []
            for item in content[:args.latest]:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("id") or "")
                if re.fullmatch(r"\d{1,19}", pid):
                    wanted[pid] = item
            _dump(out / f"pvp_search_response_{stamp}.json", search_json)
        for pid in extra_ids:
            wanted.setdefault(pid, {})

        target = out / f"pvp_sale_fields_raw_{stamp}.jsonl"
        summary = []
        with target.open("x", encoding="utf-8") as fh:
            for pos, (pid, search_item) in enumerate(wanted.items(), 1):
                try:
                    detail_response = pvp.fetch_detail(session, pid)
                    if not isinstance(detail_response, dict):
                        raise RuntimeError("PVP detail is not a JSON object")
                    detail_body = detail_response.get("body") or {}
                    if not isinstance(detail_body, dict) or not detail_body:
                        raise RuntimeError("Empty PVP detail body")

                    # Preserve the entire original search result (when known)
                    # and the full detail endpoint response (not just body).
                    fh.write(json.dumps({
                        "pvp_id": pid,
                        "search_item": search_item,
                        "detail_response": detail_response,
                    }, ensure_ascii=False) + "\n")
                    fh.flush()

                    lot = detail_body.get("lotto") or {}
                    if not isinstance(lot, dict):
                        lot = {}
                    desc = _desc(search_item, detail_body)
                    row = {
                        "pvp_id": pid,
                        "search_numeroLotto": search_item.get("numeroLotto"),
                        "detail_lotto_numeroLotto": lot.get("numeroLotto"),
                        "detail_lotto_codiLotto_FOR_REFERENCE_ONLY": lot.get("codiLotto"),
                        "detail_numeEsperimento": detail_body.get("numeEsperimento"),
                        "search_numeEsperimento": search_item.get("numeEsperimento"),
                        "selected_lotto": pvp_lot_number(detail_body, search_item, desc),
                        "selected_esperimento": pvp_sale_attempt(detail_body, search_item),
                        "description_excerpt": desc[-230:],
                    }
                    summary.append(row)
                    print(
                        f"[PVP RAW] {pos}/{len(wanted)} id={pid} "
                        f"lotto={row['selected_lotto'] or '-'} "
                        f"esperimento={row['selected_esperimento'] or '-'}",
                        flush=True,
                    )
                except Exception as exc:
                    errors.append({"pvp_id": pid, "error": f"{type(exc).__name__}: {exc}"})
                    print(f"[PVP RAW ERROR] id={pid} {type(exc).__name__}: {exc}", flush=True)

    report = reports / f"pvp_sale_fields_audit_{stamp}.json"
    _dump(report, {
        "mode": "READ_ONLY_PVP_API_SAMPLE",
        "created_at": datetime.now().astimezone().isoformat(),
        "requested": len(wanted),
        "successful": len(summary),
        "errors": errors,
        "raw_jsonl": str(target),
        "search_response_file": str(out / f"pvp_search_response_{stamp}.json") if search_json else None,
        "fields": summary,
    })
    print("[AUDIT] RAW completo salvato in", target)
    print("[AUDIT] Riepilogo in", report)
    print("[AUDIT] Nessun aggiornamento al database né ai RAW di produzione.")
    return 0 if summary else 1


if __name__ == "__main__":
    raise SystemExit(main())
