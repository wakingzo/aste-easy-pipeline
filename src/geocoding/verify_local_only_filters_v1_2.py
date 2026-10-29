#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GET-ONLY Supabase preflight for the 50 local-only touched candidates.

Does not PATCH, call Google, or modify any source/DB records. Verifies that the
exact PostgREST filters used by repair v1.2 match the record previously read.
No credentials or addresses are written to the report.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GEOCODING_DIR = Path(__file__).resolve().parent
SRC_DIR = GEOCODING_DIR.parent
for folder in (GEOCODING_DIR, SRC_DIR):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import local_only_repair_touched_v1 as repair
from project_env import load_project_env

PREVIOUS_APPLY = "local_only_touched_v1_apply_20260917-000056.csv"
GUARD_COLUMNS = (
    'pvp_id', 'indirizzo', 'comune', 'sigla_provincia', 'cap',
    'latitudine', 'longitudine', 'geocode_source',
    'geocode_location_type', 'geocode_query_used', 'geocode_formatted_address',
)


def get_guard_match(base: str, key: str, before: dict, pid: str) -> bool:
    conditions = {col: repair.db_filter_value(before.get(col)) for col in GUARD_COLUMNS}
    conditions['pvp_id'] = repair.db_filter_value(pid)
    params = {'select': 'pvp_id', **conditions}
    url = base.rstrip('/') + '/rest/v1/listings?' + urlencode(params)
    req = Request(url, method='GET', headers=repair.db_headers(key))
    with urlopen(req, timeout=repair.TIMEOUT) as resp:
        records = json.loads(resp.read().decode('utf-8'))
    if not isinstance(records, list) or len(records) > 1:
        raise RuntimeError('GET filter response must be a list with at most one row')
    if records and repair.clean(records[0].get('pvp_id')) != pid:
        raise RuntimeError('GET filter returned a different pvp_id')
    return len(records) == 1


def main() -> None:
    report_path = repair.REPORTS_DIR / PREVIOUS_APPLY
    if not report_path.is_file():
        raise SystemExit(f'Manca il report precedente: {report_path}')
    with report_path.open(encoding='utf-8-sig', newline='') as f:
        previous = list(csv.DictReader(f))
    statuses = Counter(x.get('apply_status') for x in previous)
    ids = [repair.clean(x.get('pvp_id')) for x in previous if x.get('apply_status') == 'SKIP_CONCURRENT_CHANGE']
    if (len(previous) != 66 or statuses != Counter({'SKIP_CONCURRENT_CHANGE': 50, 'NOT_APPLIED': 16})
            or len(ids) != 50 or len(set(ids)) != 50):
        raise SystemExit('Report apply precedente diverso dai 50/16 previsti. ZERO GET/PATCH.')
    plan_rows = repair.load_plan(repair.REPORTS_DIR / repair.PLAN_NAME)
    rows = {repair.clean(x['pvp_id']): x for x in plan_rows}
    if any(pid not in rows or pid in repair.REVIEW for pid in ids):
        raise SystemExit('ID non presenti nel piano SHA256-pinned o in lista REVIEW. ZERO GET/PATCH.')
    load_project_env(required=('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE'))
    base, key = os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE']
    outcomes = []
    print('[GET ONLY] Verifica dei 50 ID con filtri corretti; ZERO PATCH e ZERO Google')
    for idx, pid in enumerate(ids, 1):
        now = repair.db_get_one(base, key, pid)
        state_ok, reason = repair.db_state_ok(rows[pid], now)
        if not state_ok:
            outcome = 'DB_STATE_CHANGED'
        else:
            outcome = 'FILTERS_MATCH' if get_guard_match(base, key, now, pid) else 'FILTERS_MISMATCH'
            reason = 'ok' if outcome == 'FILTERS_MATCH' else 'conditional_filter_did_not_match'
        outcomes.append({'pvp_id': pid, 'status': outcome, 'reason': reason})
        if outcome != 'FILTERS_MATCH':
            print(f'[{idx}/50] {pid}: {outcome}: {reason}')
    counts = dict(Counter(x['status'] for x in outcomes))
    passed = counts == {'FILTERS_MATCH': 50}
    report = {
        'version': 'local_only_filters_v1_2', 'mode': 'GET_ONLY_ZERO_PATCH_ZERO_GOOGLE',
        'plan_sha256': repair.PLAN_SHA256, 'targeted': len(ids),
        'counts': counts, 'passed': passed, 'google_api_calls': 0, 'db_patches': 0,
        'failed_ids': [x for x in outcomes if x['status'] != 'FILTERS_MATCH'],
    }
    repair.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')
    out = repair.REPORTS_DIR / f'local_only_filters_v1_2_get_only_{timestamp}.json'
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print('[RESULT] ' + str(counts) + ' | PASS=' + str(passed))
    print('[OUT]', out)
    if not passed:
        raise SystemExit('Non avviare --apply. Invia il report diagnostico.')
    print('[DONE] 50/50 filtri verificati in GET. Nessun PATCH; invia il report prima di --apply.')


if __name__ == '__main__':
    main()
