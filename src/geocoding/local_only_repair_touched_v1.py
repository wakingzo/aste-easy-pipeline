#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy: conservative local-only repair for 2026-09-16 touched batch.

Default: offline preflight (reads a specific, SHA256-pinned planner CSV and local
ISTAT boundaries, writes only local reports). No Google imports, keys, or API calls.
--apply: uses ONLY Supabase GET/PATCH, with strict before-state guard and per-ID
confirmation and atomic conditional PATCH; never geocodes via Google and never edits existing coordinates.

This is a one-time, explicitly scoped remediation tool, NOT the daily pipeline.
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
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

GEOCODING_DIR = Path(__file__).resolve().parent
SRC_DIR = GEOCODING_DIR.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from aste_paths import PROJECT_ROOT, REFERENCE_DIR, REPORTS_DIR

PLAN_NAME = 'geocode_unified_plan_v11_6_4_20260916-224958.csv'
PLAN_SHA256 = '1fd8b57115ff2f638aa72c4369d1c2402b062e8c73750ea05e93206ff0b93fc9'
TARGET_COUNT = 2402
LOCAL_PLANS = {'LOCAL_CIVICO_READY', 'LOCAL_STRADA_READY', 'LOCAL_LOCALITA_READY'}
EXPECTED_COUNTS = {'LOCAL_CIVICO_READY': 49, 'LOCAL_STRADA_READY': 11, 'LOCAL_LOCALITA_READY': 6}

# Manually inspected PVP/ANNCSU semantic conflicts or excessive loss of precision.
# These are NEVER auto-patched by this script, even when a polygon accepts the point.
REVIEW = {
    '4575297': 'Indirizzo solo Collepasso; candidata Via Lecce non verificata',
    '4622509': 'Indirizzo Via snc snc; candidata Contrada Via di Chieti',
    '4626435': 'Indirizzo solo Collepasso; candidata Via Lecce non verificata',
    '4630736': 'Via Torino 181: punto medio della via non conferma il civico',
    '4630773': 'Via Torino 181: punto medio della via non conferma il civico',
    '4625134': 'Via Ghino Valenti: strada molto estesa, punto mediano poco rappresentativo',
    '4625830': 'Strada Comunale della Roveda vs Via Brianza',
    '4625843': 'Strada Comunale della Roveda vs Via Brianza',
    '4625845': 'Strada Comunale senza nome vs Via Brianza',
    '4627665': 'Via Manzoni vs Corso Vercelli',
    '4629548': 'Strada Terza Zona Industriale vs Via del Piano',
    '4631382': 'Via Cesare Battisti vs Via Belluno',
    '4627299': 'Localita Pietrara: localita esplicita non confermata dal guard',
    '4627417': 'Via Mario Alberto vs Via Alberto Mario; inversione potenzialmente significativa',
    '4625162': 'Contrada Caselle: localita esplicita non confermata dal guard',
    '4627298': 'Carnaiola: medoid localita con dispersione p90 465 m, da verificare',
}

TIMEOUT = 60


def clean(v):
    return re.sub(r'\s+', ' ', str(v or '').replace('\u00a0', ' ')).strip()


def cap(v):
    s = re.sub(r'\D', '', clean(v))
    return s.zfill(5) if s else ''


def float_checked(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def candidate_coords(row):
    kind = row['plan']
    if kind == 'LOCAL_CIVICO_READY':
        pair = row.get('anncsu_lat'), row.get('anncsu_lon')
    elif kind == 'LOCAL_STRADA_READY':
        pair = row.get('street_rep_lat'), row.get('street_rep_lon')
    else:
        pair = row.get('runtime_locality_lat'), row.get('runtime_locality_lon')
    lat, lon = [float_checked(x) for x in pair]
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == lon == 0):
        return None
    return lat, lon


def candidate_row(row):
    return {
        'pvp_id': clean(row['pvp_id']),
        'indirizzo': clean(row['candidate_indirizzo']),
        'comune': clean(row['candidate_comune']),
        'sigla_provincia': clean(row['candidate_sigla_provincia']),
        'cap': cap(row['candidate_cap']),
    }


def load_boundary_context():
    # Never invoke the boundary module's optional download fallback: local files required.
    if not (REFERENCE_DIR / 'Limiti01012026.zip').is_file():
        raise RuntimeError('ISTAT boundary ZIP locale assente; abort senza download')
    boundary_path = GEOCODING_DIR / 'istat_boundary_runtime_v11_6.py'
    spec = importlib.util.spec_from_file_location('aste_localonly_boundary', boundary_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.get_context()


def load_plan(path):
    if not path.is_file():
        raise RuntimeError(f'Piano non trovato: {path}')
    if hashlib.sha256(path.read_bytes()).hexdigest() != PLAN_SHA256:
        raise RuntimeError('Piano CSV diverso da quello verificato (SHA256); non procedere')
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    if len(rows) != TARGET_COUNT:
        raise RuntimeError(f'Piano incompleto: {len(rows)} anziche {TARGET_COUNT} record')
    ids = [clean(x.get('pvp_id')) for x in rows]
    if any(not x for x in ids) or len(set(ids)) != len(ids):
        raise RuntimeError('PVP ID mancante o duplicato nel piano')
    locals_ = [x for x in rows if x.get('plan') in LOCAL_PLANS]
    if dict(Counter(x['plan'] for x in locals_)) != EXPECTED_COUNTS:
        raise RuntimeError('Conteggi candidati locali diversi dall audit: stop')
    if not set(REVIEW).issubset(set(ids)):
        raise RuntimeError('Mancano PVP ID della blacklist REVIEW')
    return locals_


def preflight_one(row, boundary):
    pid = clean(row['pvp_id'])
    if pid in REVIEW:
        return 'REVIEW', REVIEW[pid], None
    if clean(row.get('row_basis')) != 'CURRENT_DB_FALLBACK_CHANGED_NO_HYBRID':
        return 'REVIEW', 'row_basis inattesa', None
    if clean(row.get('admin_status')) != 'RESOLVED' or clean(row.get('istat_boundary_position')) != 'NO_COORDS':
        return 'REVIEW', 'stato amministrativo / coordinate iniziali non attesi', None
    if clean(row.get('current_geocode_source')) or clean(row.get('current_geocode_location_type')):
        return 'REVIEW', 'geocoding gia presente: non sovrascrivere', None
    if float_checked(row.get('current_latitudine')) is not None or float_checked(row.get('current_longitudine')) is not None:
        return 'REVIEW', 'coordinate DB gia presenti: non sovrascrivere', None
    if clean(row.get('existing')) != 'YES' or clean(row.get('geographic_input_changed')) != 'YES':
        return 'REVIEW', 'non e una nuova inserzione del manifest atteso', None
    if clean(row.get('municipality_guard')).startswith('GROSS_OUTSIDE'):
        return 'REVIEW', 'gross municipality conflict', None
    guard = clean(row.get('runtime_locality_guard_reason'))
    if guard and guard not in {'explicit_exact', 'context_exact'}:
        return 'REVIEW', 'guard localita esplicita negativo: ' + guard, None
    if row['plan'] == 'LOCAL_LOCALITA_READY' and clean(row.get('direct_locality_guard')) != 'CONFIRMED':
        return 'REVIEW', 'localita non confermata', None
    if row['plan'] == 'LOCAL_CIVICO_READY':
        if clean(row.get('civic_relation')).lower() != 'exact' or not clean(row.get('anncsu_civic')):
            return 'REVIEW', 'civico ANNCSU non esatto', None
        if float_checked(row.get('strict_street_similarity')) is None or float(row['strict_street_similarity']) < 0.65:
            return 'REVIEW', 'odonimo non abbastanza simile', None
    if row['plan'] == 'LOCAL_STRADA_READY':
        if not clean(row.get('anncsu_odonimo')) or float_checked(row.get('strict_street_similarity')) is None or float(row['strict_street_similarity']) < 0.9:
            return 'REVIEW', 'match strada non abbastanza specifico', None
        if float_checked(row.get('street_rep_p90_m')) is None or float(row['street_rep_p90_m']) > 300:
            return 'REVIEW', 'strada troppo estesa per medoid automatico', None
    if row['plan'] == 'LOCAL_LOCALITA_READY':
        if clean(row.get('runtime_locality_match_mode')) != 'EXACT':
            return 'REVIEW', 'localita non EXACT', None
        if not clean(row.get('runtime_anncsu_locality')):
            return 'REVIEW', 'nome localita mancante', None
        if float_checked(row.get('runtime_locality_p90_m')) is None or float(row['runtime_locality_p90_m']) > 300:
            return 'REVIEW', 'dispersione localita >300m', None
    pair = candidate_coords(row)
    if pair is None:
        return 'REVIEW', 'coordinate ANNCSU mancanti o non valide', None
    r = candidate_row(row)
    if not r['comune'] or not r['sigla_provincia']:
        return 'REVIEW', 'Comune o sigla assente', None
    bt = boundary.test(r, lat=pair[0], lon=pair[1])
    if clean(bt.get('canonical_code')) != clean(row.get('canonical_istat_code')):
        return 'REVIEW', 'codice ISTAT non coerente con piano', None
    if clean(bt.get('position')) != 'INSIDE':
        return 'REVIEW', 'candidato non strettamente INSIDE ISTAT: ' + clean(bt.get('position')), None
    return 'READY_FOR_DB_RECHECK', 'candidato locale e territorialmente valido; DB da ricontrollare', pair


def make_payload(row, pair):
    lat, lon = pair
    kind = row['plan']
    odonimo = clean(row.get('anncsu_odonimo'))
    civico = clean(row.get('anncsu_civic'))
    comune = clean(row['candidate_comune'])
    prov = clean(row['candidate_sigla_provincia'])
    c = cap(row['candidate_cap'])
    tail = ' '.join(x for x in (c, comune, prov) if x)
    if kind == 'LOCAL_CIVICO_READY':
        method = clean(row.get('anncsu_metodo')).removesuffix('.0')
        source, ltype = 'anncsu_missing_civico', f'ANNCSU_METHOD_{method}'
        addr = ', '.join(x for x in (odonimo + ', ' + civico, tail, 'Italia') if x)
    elif kind == 'LOCAL_STRADA_READY':
        source, ltype = 'anncsu_missing_strada', 'ANNCSU_STREET_MEDOID'
        addr = ', '.join(x for x in (odonimo, tail, 'Italia') if x)
    else:
        source, ltype = 'anncsu_missing_localita', 'ANNCSU_LOCALITY_MEDOID'
        addr = f"Localita {clean(row['runtime_anncsu_locality'])}, {comune} {prov}, Italia"
    fingerprint = hashlib.sha256('|'.join([
        clean(row['pvp_id']), clean(row['source_indirizzo']),
        comune, prov, c, kind, f'{lat:.7f}', f'{lon:.7f}',
    ]).encode('utf-8')).hexdigest()[:24]
    return {
        'latitudine': lat, 'longitudine': lon,
        'maps_link': f'https://www.google.com/maps?q={lat:.7f},{lon:.7f}',
        'geocode_source': source, 'geocode_location_type': ltype,
        'geocode_formatted_address': addr,
        'geocode_query_used': f'local_only_v1:{kind}:{fingerprint}',
    }


def db_headers(key, write=False):
    h = {'apikey': key, 'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}
    if write:
        h['Content-Type'] = 'application/json'
        h['Prefer'] = 'return=representation'
    return h


def db_get_one(base, key, pid):
    cols = ('pvp_id,indirizzo,comune,sigla_provincia,cap,latitudine,longitudine,'
            'geocode_source,geocode_location_type,geocode_query_used,geocode_formatted_address')
    url = base.rstrip('/') + '/rest/v1/listings?select=' + quote(cols, safe=',') + '&pvp_id=eq.' + quote(pid, safe='')
    req = Request(url, headers=db_headers(key))
    with urlopen(req, timeout=TIMEOUT) as resp:
        rows = json.loads(resp.read().decode('utf-8'))
    if not isinstance(rows, list) or len(rows) != 1 or clean(rows[0].get('pvp_id')) != pid:
        raise RuntimeError(f'GET id={pid}: attesa esattamente una riga')
    return rows[0]


def db_state_ok(row, now):
    if clean(now.get('pvp_id')) != clean(row['pvp_id']):
        return False, 'id cambiato'
    for db, source in (('indirizzo', 'source_indirizzo'), ('comune', 'source_comune'),
                       ('sigla_provincia', 'source_sigla_provincia')):
        if clean(now.get(db)).casefold() != clean(row.get(source)).casefold():
            return False, 'campo PVP cambiato: ' + db
    if cap(now.get('cap')) != cap(row.get('source_cap')):
        return False, 'CAP cambiato'
    for name in ('latitudine', 'longitudine'):
        if float_checked(now.get(name)) is not None:
            return False, 'coordinate ora presenti: ' + name
    for name in ('geocode_source', 'geocode_location_type', 'geocode_query_used', 'geocode_formatted_address'):
        if clean(now.get(name)):
            return False, 'geocode ora presente: ' + name
    return True, 'ok'


def db_filter_value(value):
    """Direct PostgREST eq: quotes would become LITERAL address characters.

    urlencode(conditions) percent-encodes the entire parameter value.  Never
    wrap a direct eq value in double quotes; preserve SQL NULL vs empty string.
    See PostgREST issue #1943 (direct eq vs quoted logical/in filters).
    """
    if value is None:
        return 'is.null'
    return 'eq.' + str(value)


def db_patch_one(base, key, pid, payload, before):
    """Conditional one-row PATCH: do not overwrite a concurrent update after GET."""
    guard_columns = (
        'pvp_id', 'indirizzo', 'comune', 'sigla_provincia', 'cap',
        'latitudine', 'longitudine', 'geocode_source',
        'geocode_location_type', 'geocode_query_used', 'geocode_formatted_address',
    )
    conditions = {name: db_filter_value(before.get(name)) for name in guard_columns}
    # Use the explicit ID, never a caller-supplied ID from the GET response.
    conditions['pvp_id'] = db_filter_value(pid)
    url = base.rstrip('/') + '/rest/v1/listings?' + urlencode(conditions)
    req = Request(url, method='PATCH', headers=db_headers(key, write=True),
                  data=json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    with urlopen(req, timeout=TIMEOUT) as resp:
        rows = json.loads(resp.read().decode('utf-8'))
    if rows == []:
        return None  # conditional filter did not match; safe no-op, cause not assumed
    if not isinstance(rows, list) or len(rows) != 1 or clean(rows[0].get('pvp_id')) != pid:
        raise RuntimeError(f'PATCH id={pid}: risposta inattesa, verifica manualmente lo stato DB')
    # Verify Supabase echoed back what was requested.
    for field, expected in payload.items():
        got = rows[0].get(field)
        if field in ('latitudine', 'longitudine'):
            if float_checked(got) is None or abs(float(got) - float(expected)) > 0.0000001:
                raise RuntimeError(f'PATCH id={pid}: coordinate {field} inattese; verifica DB')
        elif str(got) != str(expected):
            raise RuntimeError(f'PATCH id={pid}: campo {field} inatteso; verifica DB')
    return rows[0]


def write_reports(records, meta, stamp, suffix):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f'local_only_touched_v1_{suffix}_{stamp}.csv'
    summary = REPORTS_DIR / f'local_only_touched_v1_summary_{suffix}_{stamp}.json'
    cols = ['pvp_id', 'plan', 'source_indirizzo', 'candidate_comune', 'anncsu_odonimo',
            'anncsu_civic', 'proposed_lat', 'proposed_lon', 'status', 'reason', 'apply_status']
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows([{k: x.get(k, '') for k in cols} for x in records])
    summary.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    print('[OUT]', path)
    print('[OUT]', summary)


def main():
    ap = argparse.ArgumentParser(description='One-time local-only touched repair, ZERO Google')
    ap.add_argument('--plan', type=Path, default=REPORTS_DIR / PLAN_NAME)
    ap.add_argument('--apply', action='store_true', help='DB PATCH with manual typed confirmation')
    args = ap.parse_args()
    stamp = datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')
    rows = load_plan(args.plan)
    ctx = load_boundary_context()
    out = []
    approved = []
    for row in rows:
        state, reason, pair = preflight_one(row, ctx)
        rec = {
            'pvp_id': clean(row['pvp_id']), 'plan': row['plan'],
            'source_indirizzo': row['source_indirizzo'], 'candidate_comune': row['candidate_comune'],
            'anncsu_odonimo': row.get('anncsu_odonimo', ''),
            'anncsu_civic': row.get('anncsu_civic', ''),
            'proposed_lat': pair[0] if pair else '', 'proposed_lon': pair[1] if pair else '',
            'status': state, 'reason': reason, 'apply_status': 'NOT_APPLIED',
        }
        out.append(rec)
        if state == 'READY_FOR_DB_RECHECK':
            approved.append((row, rec, make_payload(row, pair)))
    print('[OFFLINE PREFLIGHT] candidati=', len(rows),
          'pronti_per_recheck_DB=', len(approved), 'review=', len(rows) - len(approved))
    meta = {
        'version': 'local_only_touched_v1_2', 'plan_name': PLAN_NAME,
        'plan_sha256': PLAN_SHA256, 'targeted_manifest_total': TARGET_COUNT,
        'local_candidates': len(rows), 'ready_for_db_recheck': len(approved),
        'review_count': len(rows) - len(approved),
        'google_api_calls': 0, 'db_patches': 0, 'conditional_db_patch': True,
        'mode': 'APPLY' if args.apply else 'OFFLINE_PREVIEW_ZERO_NETWORK',
    }
    if not args.apply:
        write_reports(out, meta, stamp, 'preview')
        print('[DONE] Nessuna rete, nessun PATCH, zero Google. Carica il CSV e JSON prima di --apply.')
        return
    if len(approved) > 50:
        raise RuntimeError('Safety cap: piu di 50 candidati, stop')
    from project_env import load_project_env
    load_project_env(required=('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE'))
    base, key = os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE']
    # All-or-nothing preflight: if a single row changed, do not start any PATCH.
    changed = []
    for row, rec, payload in approved:
        now = db_get_one(base, key, clean(row['pvp_id']))
        ok, why = db_state_ok(row, now)
        if not ok:
            rec['apply_status'] = 'ABORT_RECHECK:' + why
            changed.append(clean(row['pvp_id']))
    if changed:
        meta['aborted_recheck_ids'] = changed
        write_reports(out, meta, stamp, 'aborted')
        raise RuntimeError('Uno o piu record DB sono cambiati. Zero PATCH; rigenerare piano.')
    phrase = 'LOCAL_ONLY_APPLY_' + str(len(approved))
    print('ATTENZIONE: saranno applicati PATCH Supabase solo ai', len(approved),
          'ID verificati. Nessuna chiamata Google.')
    if input('Per procedere digita esattamente ' + phrase + ': ').strip() != phrase:
        write_reports(out, meta, stamp, 'cancelled')
        print('Annullato: 0 PATCH')
        return
    patches = 0
    try:
        for row, rec, payload in approved:
            pid = clean(row['pvp_id'])
            now = db_get_one(base, key, pid)
            ok, why = db_state_ok(row, now)
            if not ok:
                rec['apply_status'] = 'SKIP_RECHECK:' + why
                continue
            patched = db_patch_one(base, key, pid, payload, now)
            if patched is None:
                rec['apply_status'] = 'SKIP_CONCURRENT_CHANGE'
                print('[SKIP CONCURRENT CHANGE]', pid)
                continue
            rec['apply_status'] = 'PATCHED'
            patches += 1
            print('[PATCHED]', pid, row['plan'])
    finally:
        meta['db_patches'] = patches
        meta['apply_counts'] = dict(Counter(r['apply_status'] for r in out))
        write_reports(out, meta, stamp, 'apply')
    print('[DONE] PATCHED=', patches, 'ZERO Google')


if __name__ == '__main__':
    main()
