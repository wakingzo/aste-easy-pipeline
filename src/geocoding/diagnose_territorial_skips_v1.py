#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GET-only diagnosis of the 29 conditional-filter skips from territorial_safe_v1.

No PATCH; no Google; no new geocoding. The local apply CSV and pre-apply
snapshot must match the audited, fixed SHA256 fingerprints.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
for path in (HERE, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import repair_territorial_safe_v1 as repair
from aste_paths import REPORTS_DIR
from project_env import load_project_env

APPLY_NAME = 'territorial_safe_v1_apply_20260919-151837.csv'
APPLY_SHA256 = '1624cb6090e0ad895774cdf228966dc7eefcdeef53309fd07df957972bd95bbb'
BACKUP_NAME = 'territorial_safe_v1_before_apply_20260919-151346.json'
BACKUP_SHA256 = 'c9a5cdd5a5d12c92bb51ed5b95a0031a5737fd874b94474d089a364264315a05'
EXPECTED_SKIPS = 29
PROBE_FIELDS = tuple(f for f in repair.FIELDS if f != 'pvp_id')
TIMEOUT = 40


def load_inputs():
    paths = ((APPLY_NAME, APPLY_SHA256), (BACKUP_NAME, BACKUP_SHA256))
    for name, digest in paths:
        file = REPORTS_DIR / name
        if not file.is_file():
            raise RuntimeError(f'Manca report locale: {name}; nessuna richiesta al DB')
        if hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'Impronta SHA256 diversa per {name}; nessuna richiesta al DB')
    with (REPORTS_DIR / APPLY_NAME).open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    skipped = [r['pvp_id'] for r in rows if r['patch_status'] == 'SKIP_CONDITIONAL_FILTER']
    counts = Counter(r['patch_status'] for r in rows)
    if not (len(rows) == 373 and len(skipped) == EXPECTED_SKIPS and
            len(set(skipped)) == EXPECTED_SKIPS and
            counts['PATCHED_GET_VERIFIED'] == 287 and counts['NOT_APPLIED'] == 57 and
            all(i.isdigit() for i in skipped)):
        raise RuntimeError('Il report APPLY non corrisponde ai 29 skip attesi: stop')
    original = json.loads((REPORTS_DIR / BACKUP_NAME).read_text(encoding='utf-8'))
    if not isinstance(original, dict) or len(original) != 316 or not set(skipped).issubset(original):
        raise RuntimeError('Backup originale incompleto o inatteso: stop')
    return skipped, original


def original_equal(current, original):
    """Only compare the exact original 12 selected fields; null stays distinct."""
    changed = []
    for field in repair.FIELDS:
        now, old = current.get(field), original.get(field)
        if field in ('latitudine', 'longitudine'):
            if not repair.same_num(now, old):
                changed.append(field)
        elif now != old:
            changed.append(field)
    return changed


def diagnostic_partition(fields, matches):
    """Find singleton failures or filters that fail only when combined.

    matches(fields) is a GET-only callback that returns True iff exactly the
    expected pvp_id matches. Does not assume a lone failing field exists.
    """
    if not fields:
        return [], []
    if matches(fields):
        return [], []
    if len(fields) == 1:
        return [fields[0]], []
    k = len(fields) // 2
    left, right = fields[:k], fields[k:]
    left_match, right_match = matches(left), matches(right)
    if left_match and right_match:
        return [], [' + '.join(fields)]
    bad, coupled = [], []
    for part, ok in ((left, left_match), (right, right_match)):
        if not ok:
            b, c = diagnostic_partition(part, matches)
            bad.extend(b)
            coupled.extend(c)
    return bad, coupled


def probe_get(base, key, pid, original, fields, counts):
    """Replicate the EXACT PostgREST eq/is.null syntax from conditional_patch.

    Select only pvp_id. A missing row means a filter did not match; this
    function never writes or calls a geocoding service.
    """
    filters = {field: ('is.null' if original.get(field) is None
                       else 'eq.' + str(original[field])) for field in fields}
    filters['pvp_id'] = 'eq.' + pid
    url = base.rstrip('/') + '/rest/v1/listings?' + urlencode({
        'select': 'pvp_id', **filters, 'limit': '2'
    })
    request = Request(url, headers={
        'apikey': key, 'Authorization': 'Bearer ' + key, 'Accept': 'application/json'
    }, method='GET')
    counts['get_requests'] += 1
    with urlopen(request, timeout=TIMEOUT) as response:
        result = json.loads(response.read().decode('utf-8'))
    if not isinstance(result, list) or len(result) > 1:
        raise RuntimeError(f'Risposta GET non univoca per ID {pid}: stop')
    if result and str(result[0].get('pvp_id')) != pid:
        raise RuntimeError(f'Risposta GET ID inatteso per {pid}: stop')
    return bool(result)


def save_report(results, summary):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')
    name = 'territorial_skips_diagnostic_v1_get_only_' + stamp
    csvpath = REPORTS_DIR / (name + '.csv')
    jsonpath = REPORTS_DIR / (name + '.json')
    with csvpath.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'pvp_id', 'outcome', 'db_changed_fields', 'single_filter_failures',
            'combined_filter_failures', 'full_filter_matches_now'
        ])
        writer.writeheader()
        for item in results:
            writer.writerow({k: '|'.join(v) if isinstance(v, list) else v
                             for k, v in item.items()})
    summary['csv'] = csvpath.name
    summary['json'] = jsonpath.name
    jsonpath.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print('[OUT]', csvpath.name)
    print('[OUT]', jsonpath.name)


def main():
    ids, before = load_inputs()  # all file checks happen before any network activity
    load_project_env(required=('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE'))
    base, key = os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE']
    counters = Counter()
    # Bounded initial GETs, with an exact per-ID presence check.
    current = repair.fetch_rows(base, key, ids)
    counters['get_requests'] += (len(ids) + 34) // 35
    results = []
    print('[GET ONLY] Diagnosi dei 29 skip; nessun PATCH, nessuna chiamata Google.', flush=True)
    for index, pid in enumerate(ids, 1):
        old, now = before[pid], current[pid]
        changed = original_equal(now, old)
        rec = {'pvp_id': pid, 'outcome': '', 'db_changed_fields': changed,
               'single_filter_failures': [], 'combined_filter_failures': [],
               'full_filter_matches_now': False}
        if changed:
            rec['outcome'] = 'DB_STATE_CHANGED_SINCE_BACKUP'
        else:
            def match(fields):
                return probe_get(base, key, pid, old, fields, counters)
            # Full filter includes the pvp_id equality, as the prior PATCH did.
            full = match(PROBE_FIELDS)
            rec['full_filter_matches_now'] = full
            if full:
                rec['outcome'] = 'FULL_FILTER_NOW_MATCHES'
            else:
                # Probe the id-only equality to rule out an ID filter issue.
                if not match(()) :
                    rec['outcome'] = 'PVP_ID_EQ_MISMATCH'
                else:
                    singles, coupled = diagnostic_partition(PROBE_FIELDS, match)
                    rec['single_filter_failures'] = singles
                    rec['combined_filter_failures'] = coupled
                    rec['outcome'] = ('SINGLE_FILTER_MISMATCH' if singles else
                                      'COMBINED_FILTER_MISMATCH' if coupled else
                                      'FULL_ONLY_MISMATCH')
        results.append(rec)
        if index % 5 == 0 or index == len(ids):
            print(f'[GET ONLY] {index}/{len(ids)} controllati', flush=True)
    summary = {
        'version': 'territorial_skips_diagnostic_v1',
        'mode': 'SUPABASE_GET_ONLY_ZERO_PATCH_ZERO_GOOGLE',
        'examined': len(ids),
        'outcome_counts': dict(Counter(r['outcome'] for r in results)),
        'single_filter_failure_counts': dict(Counter(f for r in results
                    for f in r['single_filter_failures'])),
        'get_requests': counters['get_requests'],
        'supabase_patches': 0, 'google_api_calls': 0,
        'notes': ('Non ripetere il vecchio --apply: contiene 287 PATCH gia riusciti. '
                  'Diagnostica soltanto i 29 ID rimasti; nessuna correzione automatica.')
    }
    save_report(results, summary)
    print('[ESITO]', summary['outcome_counts'], 'campi singoli:',
          summary['single_filter_failure_counts'])


if __name__ == '__main__':
    main()
