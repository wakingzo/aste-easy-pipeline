#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Conservative, general recovery of ONLY the 29 numeric-equality PATCH skips.

GET-only by default. Uses previous plan, previous apply, backup and diagnostic,
all SHA256-locked. Does not geocode or import new lots, call Google, or touch the
287 already patched records. The geographic fallback decision is inherited from
repair_territorial_safe_v1; this script changes ONLY numeric PostgREST guards.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
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

INPUTS = {
    'territorial_safe_v1_apply_20260919-151837.csv':
        '1624cb6090e0ad895774cdf228966dc7eefcdeef53309fd07df957972bd95bbb',
    'territorial_safe_v1_before_apply_20260919-151346.json':
        'c9a5cdd5a5d12c92bb51ed5b95a0031a5737fd874b94474d089a364264315a05',
    'territorial_skips_diagnostic_v1_get_only_20260919-152853.csv':
        '53a4248b1cde7cb7940da487b0a1a695dd324fd90d119128452af18ef858aa97',
}
EXPECTED_SKIPS = 29
EPS = 0.000005  # 5e-6 degrees: a sub-metre tolerance, not a new geocoding position
NUM_FIELDS = frozenset({'latitudine', 'longitudine'})
TIMEOUT = 50


def load_inputs():
    # Validate all local file fingerprints before connecting to Supabase.
    for name, digest in INPUTS.items():
        path = REPORTS_DIR / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'File assente o SHA256 diverso: {name}; stop prima del DB')
    with (REPORTS_DIR / next(n for n in INPUTS if n.startswith('territorial_safe_v1_apply'))).open(
        encoding='utf-8-sig', newline=''
    ) as stream:
        previous = list(csv.DictReader(stream))
    with (REPORTS_DIR / next(n for n in INPUTS if n.startswith('territorial_skips_diagnostic'))).open(
        encoding='utf-8-sig', newline=''
    ) as stream:
        diagnosed = list(csv.DictReader(stream))
    old = json.loads((REPORTS_DIR / next(n for n in INPUTS if n.startswith('territorial_safe_v1_before'))).read_text(encoding='utf-8'))
    plans = repair.plan_rows()  # Also verifies the original 373-row SHA256.
    counts = Counter(r['patch_status'] for r in previous)
    skipped = [r['pvp_id'] for r in previous if r['patch_status'] == 'SKIP_CONDITIONAL_FILTER']
    dids = [r['pvp_id'] for r in diagnosed]
    if (len(previous) != 373 or counts != Counter({'PATCHED_GET_VERIFIED': 287,
                                                  'SKIP_CONDITIONAL_FILTER': 29,
                                                  'NOT_APPLIED': 57}) or
        len(skipped) != EXPECTED_SKIPS or len(set(skipped)) != EXPECTED_SKIPS or
        len(diagnosed) != EXPECTED_SKIPS or len(set(dids)) != EXPECTED_SKIPS or
        set(skipped) != set(dids) or len(old) != 316 or
        not set(skipped).issubset(old) or not set(skipped).issubset(plans)):
        raise RuntimeError('Report o insieme di ID incoerente: stop')
    for r in diagnosed:
        failures = set(r['single_filter_failures'].split('|'))
        if (r['outcome'] != 'SINGLE_FILTER_MISMATCH' or r['db_changed_fields'] or
            r['combined_filter_failures'] or r['full_filter_matches_now'] != 'False' or
            not failures or not failures.issubset(NUM_FIELDS)):
            raise RuntimeError(f'Diagnosi non compatibile con precisione numerica: {r["pvp_id"]}')
    for pid in skipped:
        p = plans[pid]
        if p['decision'] != 'PROPOSE_ISTAT_COMUNE_INDICATIVE' or repair.preapproved_from_plan(p) != 'CANDIDATE':
            raise RuntimeError(f'Piano non autorizzato per {pid}: stop')
    return skipped, old, plans


def same_original(now, before):
    """Guard against ANY changed field; coordinates compared with the old 1e-7 tolerance."""
    return all(repair.same_num(now.get(k), before.get(k)) if k in NUM_FIELDS
               else now.get(k) == before.get(k) for k in repair.FIELDS)


def guarded_pairs(pid, before):
    """Exact null/text comparisons, narrow numeric range on both old coordinates."""
    if repair.txt(before.get('pvp_id')) != pid:
        raise RuntimeError('Guard ID diversa dal record')
    out = [('pvp_id', 'eq.' + pid)]
    for key in repair.FIELDS:
        if key == 'pvp_id':
            continue
        value = before.get(key)
        if key in NUM_FIELDS:
            number = repair.val(value)
            if number is None or not math.isfinite(number):
                raise RuntimeError(f'Coordinate originali assenti/non valide ({pid}/{key})')
            out.extend(((key, 'gte.' + format(number - EPS, '.10f')),
                        (key, 'lte.' + format(number + EPS, '.10f'))))
        else:
            out.append((key, 'is.null' if value is None else 'eq.' + str(value)))
    return out


def guarded_url(base, pid, before, select=False):
    pairs = guarded_pairs(pid, before)
    if select:
        pairs.extend((('select', 'pvp_id'), ('limit', '2')))
    return base.rstrip('/') + '/rest/v1/listings?' + urlencode(pairs)


def headers(key, patched=False):
    h = {'apikey': key, 'Authorization': 'Bearer ' + key,
         'Accept': 'application/json'}
    if patched:
        h.update({'Content-Type': 'application/json', 'Prefer': 'return=representation'})
    return h


def guard_get(base, key, pid, before):
    req = Request(guarded_url(base, pid, before, select=True),
                  headers=headers(key), method='GET')
    with urlopen(req, timeout=TIMEOUT) as response:
        result = json.loads(response.read().decode('utf-8'))
    if not isinstance(result, list) or len(result) > 1 or (result and repair.txt(result[0].get('pvp_id')) != pid):
        raise RuntimeError(f'GET guard inatteso o ambiguo per {pid}')
    return bool(result)


def guarded_patch(base, key, pid, before, payload):
    # Never called in preview; same guards used for the successful GET above.
    req = Request(guarded_url(base, pid, before), headers=headers(key, patched=True),
                  method='PATCH', data=json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    with urlopen(req, timeout=TIMEOUT) as response:
        result = json.loads(response.read().decode('utf-8'))
    if result == []:
        return False
    if not isinstance(result, list) or len(result) != 1 or repair.txt(result[0].get('pvp_id')) != pid:
        raise RuntimeError(f'PATCH ambiguo: {pid}; controllare il DB')
    return True


def write_reports(rows, summary, mode):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')
    prefix = 'territorial_skips_v2_' + mode + '_' + stamp
    csv_path = REPORTS_DIR / (prefix + '.csv')
    json_path = REPORTS_DIR / (prefix + '.json')
    fields = ['pvp_id', 'comune', 'original_lat', 'original_lon', 'new_lat', 'new_lon',
              'preflight', 'patch_status']
    with csv_path.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    summary.update(csv=csv_path.name, json=json_path.name)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print('[OUT]', csv_path.name, '\n[OUT]', json_path.name, flush=True)


def main():
    ap = argparse.ArgumentParser(description='Solo i 29 skip numerici, anteprima GET-only di default; Google ZERO')
    ap.add_argument('--apply', action='store_true', help='PATCH soltanto dopo anteprima approvata')
    ap.add_argument('--expected-ready', type=int, help='Conteggio esatto READY del report GET-only')
    args = ap.parse_args()
    if args.apply and args.expected_ready != EXPECTED_SKIPS:
        ap.error('--apply richiede --expected-ready 29 dopo verifica report GET-only')
    ids, old, plans = load_inputs()
    load_project_env(required=('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE'))
    base, key = os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE']
    now = repair.fetch_rows(base, key, ids)
    ctx = repair.boundary_context()
    source = repair.module(HERE / 'geocode_territorial_offline_v1.py', 'territorial_skips_source_v2')
    cache = {}
    report = []
    counters = Counter()
    print('[GET ONLY] 29 skip: controllo backup, ISTAT e filtri con intervallo numerico; Google ZERO.', flush=True)
    for i, pid in enumerate(ids, 1):
        p, record = plans[pid], now[pid]
        status = repair.proposal_check(p, record, ctx, source.alternative_comune_for_pin, cache)
        if not same_original(record, old[pid]):
            status = 'HOLD_CHANGED_SINCE_BACKUP'
        if status == 'READY_COMUNE_INDICATIVE':
            status = 'READY_FILTER_BOUNDED' if guard_get(base, key, pid, record) else 'HOLD_FILTER_BOUNDED_NOT_MATCHED'
        counters[status] += 1
        report.append({'pvp_id': pid, 'comune': p['comune'],
                       'original_lat': p['original_lat'], 'original_lon': p['original_lon'],
                       'new_lat': p['proposed_lat'], 'new_lon': p['proposed_lon'],
                       'preflight': status, 'patch_status': 'NOT_APPLIED'})
        if i % 5 == 0 or i == len(ids):
            print(f'[GET ONLY] {i}/{len(ids)} verificati', flush=True)
    summary = {'version': 'territorial_skips_v2', 'mode': 'APPLY' if args.apply else 'SUPABASE_GET_ONLY_ZERO_GOOGLE_ZERO_PATCH',
               'examined': len(ids), 'preflight_counts': dict(counters),
               'ready': counters['READY_FILTER_BOUNDED'], 'google_calls': 0, 'db_patches': 0,
               'numeric_guard_epsilon_degrees': EPS,
               'notes': 'Solo gli originali 29 SKIP; 287 PATCH precedenti esclusi. Comune = punto indicativo.'}
    if not args.apply:
        write_reports(report, summary, 'get_only')
        return
    if summary['ready'] != EXPECTED_SKIPS:
        write_reports(report, summary, 'blocked')
        raise RuntimeError('Non tutti i 29 superano i GET: zero PATCH')
    phrase = 'TERRITORIAL_SKIPS_APPLY_29'
    if input(f'Per autorizzare i SOLI 29 PATCH digita esattamente {phrase}: ').strip() != phrase:
        write_reports(report, summary, 'cancelled')
        return
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    backup = REPORTS_DIR / ('territorial_skips_v2_before_apply_' +
                            datetime.now().astimezone().strftime('%Y%m%d-%H%M%S') + '.json')
    backup.write_text(json.dumps({pid: now[pid] for pid in ids}, ensure_ascii=False, indent=2), encoding='utf-8')
    summary['before_backup'] = backup.name
    print('[BACKUP]', backup.name, flush=True)
    try:
        for i, row in enumerate(report, 1):
            pid = row['pvp_id']
            fresh = repair.fetch_rows(base, key, [pid])[pid]
            state = repair.proposal_check(plans[pid], fresh, ctx, source.alternative_comune_for_pin, cache)
            if not same_original(fresh, now[pid]) or not same_original(fresh, old[pid]) or state != 'READY_COMUNE_INDICATIVE':
                row['patch_status'] = 'SKIP_CONCURRENT_RECHECK'
            elif not guard_get(base, key, pid, fresh):
                row['patch_status'] = 'SKIP_CONDITIONAL_GET'
            else:
                payload = repair.patch_payload(plans[pid])
                if not guarded_patch(base, key, pid, fresh, payload):
                    row['patch_status'] = 'SKIP_CONDITIONAL_PATCH'
                else:
                    summary['db_patches'] += 1
                    row['patch_status'] = 'PATCHED_RESPONSE'
                    latest = repair.fetch_rows(base, key, [pid])[pid]
                    row['patch_status'] = ('PATCHED_GET_VERIFIED' if repair.verified(latest, payload)
                                           else 'PATCHED_GET_DIFFERENT_REVIEW_DB')
                    if row['patch_status'] != 'PATCHED_GET_VERIFIED':
                        raise RuntimeError(f'{pid}: PATCH eseguito ma verifica fallita. Non rilanciare.')
            if i % 5 == 0 or i == len(report):
                print(f'[APPLY] {i}/{len(report)}; PATCH verificati: {summary["db_patches"]}', flush=True)
    finally:
        summary['apply_counts'] = dict(Counter(r['patch_status'] for r in report))
        write_reports(report, summary, 'apply')


if __name__ == '__main__':
    main()
