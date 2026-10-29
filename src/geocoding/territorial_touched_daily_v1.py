#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy: general territorial guard for INSERTED PVP IDs, offline geodata.

Default GET-only. --apply is a separate, explicitly authorized operation.
NO Google API, NO web lookup, NO per-auction overrides. Supabase GET/PATCH only.
Fast default: ISTAT municipality fallback; --with-anncsu opts into the expensive
local STRAD/INDIR pass for road suggestions. Municipality points are INDICATIVE.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from aste_paths import ANNCSU_DIR, REFERENCE_DIR, REPORTS_DIR
from project_env import load_project_env

VERSION = 'territorial_touched_daily_v1'
FIELDS = ('pvp_id', 'indirizzo', 'comune', 'sigla_provincia', 'cap',
          'latitudine', 'longitudine', 'maps_link', 'geocode_source',
          'geocode_location_type', 'geocode_query_used', 'geocode_formatted_address')
NUMERIC = frozenset(('latitudine', 'longitudine'))
HIGH_SOURCE = frozenset(('pvp_verified', 'google_corrected_pvp'))
HIGH_TYPE = frozenset(('ROOFTOP', 'PVP_OK_CIVICO', 'PVP_OK_STRADA'))
BOUND_EPS = 0.000005  # 0.5 m latitude, only around the *original* value
SNAPSHOT_EPS = 0.0000001
TIMEOUT = 45
OUTPUT_FIELDS = ('pvp_id', 'decision', 'comune', 'indirizzo', 'precision',
                 'old_lat', 'old_lon', 'new_lat', 'new_lon', 'preflight', 'patch_status')


def text(value):
    return re.sub(r'\s+', ' ', str(value if value is not None else '').replace('\u00a0', ' ')).strip()


def num(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def close(a, b, eps=SNAPSHOT_EPS):
    aa, bb = num(a), num(b)
    if aa is None or bb is None:
        return aa is None and bb is None
    return abs(aa - bb) <= eps


def load_territorial():
    path = HERE / 'geocode_territorial_offline_v1.py'
    if not path.is_file():
        raise RuntimeError(f'Manca il modulo territoriale gia installato: {path}')
    spec = importlib.util.spec_from_file_location('territorial_touched_source_v1', path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Impossibile caricare il modulo territoriale: {path}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_manifest(path, max_ids):
    if not path.is_file():
        raise RuntimeError(f'Manifest mancante: {path}')
    ids = []
    with path.open(encoding='utf-8-sig') as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise RuntimeError(f'Manifest: riga {lineno} non oggetto')
            pid = text(item.get('pvp_id'))
            if not re.fullmatch(r'[0-9]{1,20}', pid):
                raise RuntimeError(f'Manifest: pvp_id non valido alla riga {lineno}')
            if item.get('fresh_inserted') != 'YES' or item.get('handoff_source') != 'pvp_import_incremental_v12_3':
                raise RuntimeError(f'Manifest: non e un handoff di INSERT alla riga {lineno}')
            ids.append(pid)
            if len(ids) > max_ids:
                raise RuntimeError(f'Manifest: oltre {max_ids} ID; cambiare --max-ids esplicitamente')
    if len(set(ids)) != len(ids):
        raise RuntimeError('Manifest: pvp_id duplicati, stop')
    return ids


def fetch_rows(base, key, ids):
    out = {}
    for off in range(0, len(ids), 35):
        group = ids[off:off+35]
        qs = [('select', ','.join(FIELDS)), ('pvp_id', 'in.(' + ','.join(group) + ')'), ('limit', '100')]
        url = base.rstrip('/') + '/rest/v1/listings?' + urlencode(qs)
        req = Request(url, method='GET', headers=headers(key))
        with urlopen(req, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode('utf-8'))
        if not isinstance(data, list) or len(data) != len(group):
            raise RuntimeError('Lettura DB incompleta/duplicata nel lotto touched: stop')
        for row in data:
            pid = text(row.get('pvp_id'))
            if pid not in group or pid in out:
                raise RuntimeError('Lettura DB: pvp_id inatteso o duplicato')
            out[pid] = row
    if set(out) != set(ids):
        raise RuntimeError('Lettura DB: manifest e pvp_id DB discordanti')
    return out


def headers(key, patched=False):
    h = {'apikey': key, 'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}
    if patched:
        h.update({'Content-Type': 'application/json', 'Prefer': 'return=representation'})
    return h


def guards(pid, before):
    """Atomic old-state guard; numeric ranges avoid PostgREST float-equality traps."""
    if text(before.get('pvp_id')) != pid:
        raise RuntimeError('Guard: ID originale diverso')
    pairs = [('pvp_id', 'eq.' + pid)]
    for k in FIELDS:
        if k == 'pvp_id':
            continue
        value = before.get(k)
        if k in NUMERIC and value is not None:
            v = num(value)
            if v is None or not (-90 <= v <= 90 if k == 'latitudine' else -180 <= v <= 180):
                raise RuntimeError('Guard: coordinata originale non valida')
            pairs.extend(((k, 'gte.' + format(v-BOUND_EPS, '.10f')),
                          (k, 'lte.' + format(v+BOUND_EPS, '.10f'))))
        else:
            pairs.append((k, 'is.null' if value is None else 'eq.' + str(value)))
    return pairs


def guarded_url(base, pid, before, *, select=False):
    pairs = guards(pid, before)
    if select:
        pairs.extend((('select', 'pvp_id'), ('limit', '2')))
    return base.rstrip('/') + '/rest/v1/listings?' + urlencode(pairs)


def guarded_get(base, key, pid, before):
    req = Request(guarded_url(base, pid, before, select=True), method='GET', headers=headers(key))
    with urlopen(req, timeout=TIMEOUT) as response:
        data = json.loads(response.read().decode('utf-8'))
    if not isinstance(data, list) or len(data) > 1 or (data and text(data[0].get('pvp_id')) != pid):
        raise RuntimeError(f'Guard GET non univoco per {pid}')
    return bool(data)


def guarded_patch(base, key, pid, before, payload):
    req = Request(guarded_url(base, pid, before), method='PATCH',
                  headers=headers(key, patched=True),
                  data=json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    with urlopen(req, timeout=TIMEOUT) as response:
        data = json.loads(response.read().decode('utf-8'))
    if data == []:
        return False
    if not isinstance(data, list) or len(data) != 1 or text(data[0].get('pvp_id')) != pid:
        raise RuntimeError(f'PATCH non univoco per {pid}: verificare DB prima di ripetere')
    return True


def unchanged(now, initial):
    return all(close(now.get(k), initial.get(k)) if k in NUMERIC
               else now.get(k) == initial.get(k) for k in FIELDS)


def verified(now, payload):
    return all(close(now.get(k), v) if k in NUMERIC else now.get(k) == v
               for k, v in payload.items())


def allowed(proposal, now, ctx, territorial, cache, *, include_missing=False):
    """Re-evaluate municipality and precision; NEVER auto-apply a civic/locality."""
    decision = proposal['decision']
    if decision not in ('PROPOSE_ISTAT_COMUNE_INDICATIVE', 'PROPOSE_ANNCSU_STRADA'):
        return 'HOLD_NOT_ALLOWED_DECISION'
    if text(now.get('geocode_source')).lower() in HIGH_SOURCE or text(now.get('geocode_location_type')).upper() in HIGH_TYPE:
        return 'HOLD_PRECISE_SOURCE'
    if proposal['pvp_id'] != text(now.get('pvp_id')) or any(
        text(proposal.get(k)) != text(now.get(k)) for k in ('indirizzo', 'comune', 'sigla_provincia')):
        return 'HOLD_SOURCE_CHANGED'
    if not close(proposal.get('original_lat'), now.get('latitudine')) or not close(
        proposal.get('original_lon'), now.get('longitudine')):
        return 'HOLD_COORDINATES_CHANGED'
    if text(proposal['old_geocode_source']) != text(now.get('geocode_source')) or text(
        proposal['old_geocode_location_type']) != text(now.get('geocode_location_type')):
        return 'HOLD_GEOCODE_CHANGED'
    meta = ctx.resolve_admin(now)
    if meta.get('status') != 'RESOLVED' or text(meta.get('canonical_code')) != text(proposal['istat_code']):
        return 'HOLD_ADMIN_CHANGED'
    old = ctx.test(now, admin_meta=meta).get('position')
    if old != 'OUTSIDE' and not (include_missing and old == 'NO_COORDS'):
        return 'HOLD_NOT_OUTSIDE'
    if old == 'OUTSIDE' and territorial.alternative_comune_for_pin(
        now, ctx, num(now.get('latitudine')), num(now.get('longitudine'))):
        return 'HOLD_ALTERNATIVE_COMUNE'
    lat, lon = num(proposal.get('proposed_lat')), num(proposal.get('proposed_lon'))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return 'HOLD_BAD_TARGET'
    if ctx.test(now, lat=lat, lon=lon, admin_meta=meta).get('position') != 'INSIDE':
        return 'HOLD_TARGET_OUTSIDE'
    if decision == 'PROPOSE_ANNCSU_STRADA':
        strict = num(proposal.get('anncsu_strict_street_similarity'))
        score = num(proposal.get('anncsu_street_score'))
        if (strict is None or strict < .90 or score is None or score < .96 or
            not text(proposal.get('anncsu_odonimo')) or
            text(proposal.get('reason')) not in ('simple_no_civic', 'explicit_snc')):
            return 'HOLD_WEAK_ROAD'
        return 'READY_STRADA_INDICATIVE'
    code = text(meta.get('canonical_code'))
    if code not in cache:
        cache[code] = ctx.representative_point(now, admin_meta=meta)
    rep = cache[code]
    if not rep.get('ok') or not close(rep.get('lat'), lat) or not close(rep.get('lon'), lon):
        return 'HOLD_CENTROID_CHANGED'
    return 'READY_COMUNE_INDICATIVE'


def payload_for(proposal):
    lat, lon = num(proposal['proposed_lat']), num(proposal['proposed_lon'])
    if lat is None or lon is None:
        raise RuntimeError('Destinazione mancante')
    comune, sigla = text(proposal['comune']), text(proposal['sigla_provincia'])
    if proposal['decision'] == 'PROPOSE_ISTAT_COMUNE_INDICATIVE':
        source, kind = 'istat_corrected_boundary_comune', 'ISTAT_COMUNE_FALLBACK'
        address = f'Comune di {comune} ({sigla}), Italia - posizione indicativa'
        query = f'OFFLINE_TERRITORIAL:ISTAT_COMUNE:{proposal["istat_code"]}'
    elif proposal['decision'] == 'PROPOSE_ANNCSU_STRADA':
        source, kind = 'anncsu_corrected_boundary_strada', 'ANNCSU_STREET_MEDOID'
        address = f'{text(proposal["anncsu_odonimo"])}, {comune} ({sigla}), Italia - posizione indicativa'
        query = f'OFFLINE_TERRITORIAL:ANNCSU_STRADA:{proposal["istat_code"]}'
    else:
        raise RuntimeError('Decisione non autorizzata per payload')
    return {'latitudine': lat, 'longitudine': lon,
            'maps_link': f'https://www.google.com/maps?q={lat:.7f},{lon:.7f}',
            'geocode_source': source, 'geocode_location_type': kind,
            'geocode_formatted_address': address, 'geocode_query_used': query}


def write_reports(rows, summary, tag):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')
    name = 'territorial_touched_daily_v1_' + tag + '_' + stamp
    csv_path = REPORTS_DIR / (name + '.csv')
    json_path = REPORTS_DIR / (name + '.json')
    with csv_path.open('w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS)
        w.writeheader()
        w.writerows(rows)
    summary.update(csv=csv_path.name, json=json_path.name)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print('[OUT]', csv_path, flush=True)
    print('[OUT]', json_path, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Territorial guard touched, offline data; GET-only by default')
    ap.add_argument('--manifest', type=Path, required=True, help='handoff JSONL of newly inserted IDs')
    ap.add_argument('--apply', action='store_true', help='conditional PATCH after checks')
    ap.add_argument('--pipeline-authorized', action='store_true', help='only after daily_pipeline --apply')
    ap.add_argument('--expected-ready', type=int, help='manual apply: expected READY count from GET-only')
    ap.add_argument('--include-missing', action='store_true', help='OPT IN: indicative positions for missing coordinates')
    ap.add_argument('--with-anncsu', action='store_true', help='OPT IN: heavy local STRAD/INDIR scan')
    ap.add_argument('--max-ids', type=int, default=5000, help='safety cap on manifest size (default 5000)')
    args = ap.parse_args(argv)
    if args.max_ids < 1:
        ap.error('--max-ids must be positive')
    if args.pipeline_authorized and (not args.apply or os.environ.get('ASTE_PIPELINE_TERRITORIAL_AUTHORIZED') != '1'):
        ap.error('--pipeline-authorized requires parent daily pipeline authorization')
    if args.apply and not args.pipeline_authorized and args.expected_ready is None:
        ap.error('--apply outside pipeline requires --expected-ready N from a reviewed GET-only run')
    if args.expected_ready is not None and args.expected_ready < 0:
        ap.error('--expected-ready cannot be negative')
    ids = read_manifest(args.manifest, args.max_ids)
    summary = {'version': VERSION, 'mode': 'APPLY' if args.apply else 'GET_ONLY_ZERO_GOOGLE_ZERO_PATCH',
               'manifest': args.manifest.name,
               'manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
               'manifest_ids': len(ids), 'anncsu_scan_enabled': args.with_anncsu,
               'include_missing': args.include_missing, 'google_calls': 0,
               'db_patches': 0, 'numeric_guard_epsilon_degrees': BOUND_EPS,
               'note': 'Comune/strada = posizione indicativa, non immobile. Nessuna ricerca online.'}
    if not ids:
        summary.update(boundary_counts={}, decision_counts={}, preflight_counts={}, ready=0, apply_counts={})
        write_reports([], summary, 'empty')
        return 0
    if not (REFERENCE_DIR / 'Elenco-comuni-italiani.xlsx').is_file():
        raise RuntimeError('ISTAT workbook non disponibile localmente; stop, nessun download')
    load_project_env(required=('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE'))
    base, key = os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE']
    # Boundary module only loads local ZIP. Preflight avoids its download-if-missing path.
    territorial = load_territorial()
    boundary = territorial.mod(HERE / 'istat_boundary_runtime_v11_6.py', 'territorial_touched_boundary_v1')
    if not boundary.ISTAT_ZIP.is_file():
        raise RuntimeError('ISTAT boundary ZIP assente; nessun download')
    ctx = boundary.get_context()
    original = fetch_rows(base, key, ids)
    boundaries = Counter()
    target = []
    for pid in ids:
        row = original[pid]
        position = ctx.test(row).get('position', 'UNKNOWN')
        boundaries[position] += 1
        if position == 'OUTSIDE' or (args.include_missing and position == 'NO_COORDS'):
            target.append(row)
    print(f'[TERRITORIAL] manifest={len(ids)} OUTSIDE={boundaries["OUTSIDE"]} '
          f'NO_COORDS={boundaries["NO_COORDS"]} target={len(target)} Google=ZERO', flush=True)
    evidence = {}
    local = None
    if target and args.with_anncsu:
        for filename in ('STRAD_ITA_20260803.csv', 'INDIR_ITA_20260803.csv'):
            if not (ANNCSU_DIR / filename).is_file():
                raise RuntimeError(f'ANNCSU locale mancante: {filename}')
        # Existing ANNCSU adapter imports a Google parser for string utilities only.
        # Prevent its optional MONTH_USAGE_OFFSET synchronization; no API is called.
        for name in ('MONTH_USAGE_OFFSET_GEOCODING', 'MONTH_USAGE_OFFSET_AUTOCOMPLETE',
                     'MONTH_USAGE_OFFSET_ADDRESS_VALIDATION'):
            os.environ.pop(name, None)
        planner = territorial.mod(HERE / 'geocode_unified_v11_6_4_plan.py', 'territorial_touched_planner_v1')
        local = planner.local
        evs, _ = local.build_local_evidence(target)
        evidence = {text(ev.get('pvp_id')): ev for ev in evs}
    rep_cache = {}
    proposals = {}
    report = []
    counts = Counter()
    preflights = Counter()
    for row in target:
        pid = text(row['pvp_id'])
        p = territorial.evaluate(row, ctx, local, evidence.get(pid), rep_cache,
                                 include_missing=args.include_missing)
        proposals[pid] = p
        state = allowed(p, row, ctx, territorial, rep_cache, include_missing=args.include_missing)
        if state.startswith('READY_') and not guarded_get(base, key, pid, row):
            state = 'HOLD_FILTER_NOT_MATCHED'
        counts[p['decision']] += 1
        preflights[state] += 1
        report.append({'pvp_id': pid, 'decision': p['decision'], 'comune': p['comune'],
                       'indirizzo': p['indirizzo'], 'precision': p['precision'],
                       'old_lat': p['original_lat'], 'old_lon': p['original_lon'],
                       'new_lat': p['proposed_lat'], 'new_lon': p['proposed_lon'],
                       'preflight': state, 'patch_status': 'NOT_APPLIED'})
    ready = sum(r['preflight'].startswith('READY_') for r in report)
    summary.update(boundary_counts=dict(boundaries), target_rows=len(target),
                   decision_counts=dict(counts), preflight_counts=dict(preflights), ready=ready)
    print(f'[TERRITORIAL] proposte={len(report)} ready={ready} classi={dict(preflights)}', flush=True)
    if not args.apply:
        write_reports(report, summary, 'get_only')
        return 0
    if args.expected_ready is not None and ready != args.expected_ready:
        write_reports(report, summary, 'blocked')
        raise RuntimeError(f'Ready {ready} diverso da --expected-ready {args.expected_ready}: 0 PATCH')
    if not args.pipeline_authorized:
        phrase = f'TERRITORIAL_TOUCHED_APPLY_{ready}'
        if input(f'Per autorizzare {ready} PATCH indicativi digita {phrase}: ').strip() != phrase:
            write_reports(report, summary, 'cancelled')
            return 1
    if not ready:
        summary['apply_counts'] = dict(Counter(r['patch_status'] for r in report))
        write_reports(report, summary, 'apply')
        return 0
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = REPORTS_DIR / ('territorial_touched_daily_v1_before_apply_' +
                datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f') + '.json')
    backup_path.write_text(json.dumps({r['pvp_id']: original[r['pvp_id']] for r in report
                                       if r['preflight'].startswith('READY_')},
                                      ensure_ascii=False, indent=2), encoding='utf-8')
    summary['before_backup'] = backup_path.name
    print('[BACKUP]', backup_path, flush=True)
    patched = 0
    try:
        for i, record in enumerate(report, 1):
            if not record['preflight'].startswith('READY_'):
                continue
            pid = record['pvp_id']
            fresh = fetch_rows(base, key, [pid])[pid]
            state = allowed(proposals[pid], fresh, ctx, territorial, rep_cache,
                            include_missing=args.include_missing)
            if not unchanged(fresh, original[pid]) or state != record['preflight']:
                record['patch_status'] = 'SKIP_RECHECK_' + state
            elif not guarded_get(base, key, pid, fresh):
                record['patch_status'] = 'SKIP_FILTER_GET'
            else:
                pay = payload_for(proposals[pid])
                if not guarded_patch(base, key, pid, fresh, pay):
                    record['patch_status'] = 'SKIP_CONDITIONAL_PATCH'
                else:
                    patched += 1
                    record['patch_status'] = 'PATCHED_RESPONSE'
                    latest = fetch_rows(base, key, [pid])[pid]
                    record['patch_status'] = ('PATCHED_GET_VERIFIED' if verified(latest, pay)
                                              else 'PATCHED_GET_DIFFERENT_REVIEW_DB')
                    if record['patch_status'] != 'PATCHED_GET_VERIFIED':
                        raise RuntimeError(f'PATCH {pid} non verificato: stop, non ritentare automaticamente')
            if i % 25 == 0 or i == len(report):
                print(f'[TERRITORIAL] esaminati={i}/{len(report)} PATCH_verificati={patched}', flush=True)
    finally:
        summary['db_patches'] = patched
        summary['apply_counts'] = dict(Counter(r['patch_status'] for r in report))
        write_reports(report, summary, 'apply')
    skipped = sum(1 for r in report if r['preflight'].startswith('READY_') and
                  r['patch_status'] != 'PATCHED_GET_VERIFIED')
    if skipped:
        raise RuntimeError(f'{skipped} PATCH saltati/non verificati: non rilanciare importer, usare manifest originale')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
