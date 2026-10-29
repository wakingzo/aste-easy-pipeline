#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Opt-in, capped Google comparison of *new* listings, preview ONLY.

Does not change Supabase, geocode_pending_v2, old Google geocoder, or its cache
format; uses existing Google geocode core for its shared cache, billing tracker,
address matcher and municipality guards. No requests unless explicitly invoked.
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env
import geocode_pending_v2 as offline
from google_eligibility_v3_2 import eligibility, address_parts, canonical

MAX_REQUESTS = 500
MAX_CASES = 500


def category(row, offline_decision):
    """Eligibility is checked BEFORE selecting a Google case or loading the API core."""
    gate = eligibility(row, offline_decision)
    if not gate['google_eligible']:
        return None
    source = str(offline_decision.get('geocode_source') or '')
    precision = str(offline_decision.get('geocode_precision') or '')
    if source == 'pvp_civico_non_conclusivo':
        return 'PVP_CIVICO_NON_CONCLUSIVO'
    if source.startswith('review_pvp_'):
        return 'REVIEW'
    if precision == 'COMUNE':
        return 'COMUNE_INDICATIVO'
    if precision == 'PVP_IN_COMUNE':
        return 'PVP_NON_CONFERMATO'
    return 'REVIEW' if offline_decision.get('geocode_status') == 'REVIEW' else None


def previously_compared(report_dir):
    """Exclude previous case IDs AND queries so the next test has fresh addresses."""
    ids, queries, files = set(), set(), []
    for path in sorted(report_dir.glob('google_comparison_v3_*.csv')):
        try:
            with path.open(encoding='utf-8-sig', newline='') as f:
                records = list(csv.DictReader(f, delimiter=';'))
        except (OSError, UnicodeError, csv.Error) as exc:
            raise RuntimeError('Cannot inspect earlier Google report: ' + path.name) from exc
        if records and not {'pvp_id', 'query', 'assessment'}.issubset(records[0]):
            raise RuntimeError('Earlier Google CSV columns invalid: ' + path.name)
        files.append(path.name)
        for record in records:
            pid = str(record.get('pvp_id') or '').strip()
            query = normalize_query(record.get('query') or '')
            if pid:
                ids.add(pid)
            if query:
                queries.add(query)
    if not files or not ids or not queries:
        raise RuntimeError('Previous Google CSV with IDs and queries not found: cannot safely select a NEW sample')
    return ids, queries, files


def normalize_query(query):
    return ' '.join(str(query or '').casefold().split())


def has_civic_address(address):
    """Recognize a true house number, never a route number, CAP or foglio."""
    parsed = address_parts({'indirizzo': address, 'comune': '__known_municipality__'})
    return bool(parsed['kind'] and parsed['civic'])


def _norm_civic(value):
    return re.sub(r'[^0-9a-z]+', '', canonical(value))


def candidate_street_numbers(candidate):
    out = set()
    for comp in candidate.get('components') or []:
        types = {str(t or '').strip() for t in (comp.get('types') or [])}
        if 'street_number' not in types:
            continue
        for key in ('long_name', 'short_name'):
            value = _norm_civic(comp.get(key))
            if value:
                out.add(value)
    return out


def policy_civic_relation(row, candidate):
    """exact | variant | mismatch | missing_result | no_expected.

    Uses the v3.4 parser, not the legacy Google-core parser, so the same civic
    that made a case P2/P3 is also checked in the returned Google components.
    """
    expected = _norm_civic(address_parts(row).get('civic'))
    if not expected:
        return 'no_expected'
    got = candidate_street_numbers(candidate)
    if not got:
        return 'missing_result'
    if expected in got:
        return 'exact'
    em = re.match(r'^(\d+)', expected)
    eb = em.group(1) if em else ''
    for value in got:
        gm = re.match(r'^(\d+)', value)
        if eb and gm and gm.group(1) == eb:
            return 'variant'
    return 'mismatch'


def priority_bucket(row, offline_decision):
    """Return deterministic Google priority after the v3.4 eligibility gate.

    P1: real PVP↔ANNCSU discrepancy (specific address already required by gate)
    P2: no PVP coordinates + civic
    P3: PVP present but not address-confirmed + civic
    P4: no PVP coordinates + named street/locality only
    P5: PVP present but not address-confirmed + named street/locality only
    """
    gate = eligibility(row, offline_decision)
    if not gate['google_eligible']:
        return None
    group = gate['google_group']
    has_civic = bool(str(gate.get('address_civic') or '').strip())
    if group == 'REVIEW_DISCORDANZA':
        return 1, 'P1_DISCORDANZA_PVP_ANNCSU', gate
    if group == 'SENZA_COORDINATE_PVP' and has_civic:
        return 2, 'P2_SENZA_PVP_CON_CIVICO', gate
    if group == 'PVP_NON_CONFERMATO' and has_civic:
        return 3, 'P3_PVP_NON_CONFERMATO_CON_CIVICO', gate
    if group == 'SENZA_COORDINATE_PVP':
        return 4, 'P4_SENZA_PVP_TOPONIMO', gate
    if group == 'PVP_NON_CONFERMATO':
        return 5, 'P5_PVP_NON_CONFERMATO_TOPONIMO', gate
    return 6, 'P6_ALTRO_DUBBIO_SPECIFICO', gate


def pick_fresh_cases(planned, g, prior_ids, prior_queries, max_cases, priority_only=None):
    """Select NEW DISTINCT Google queries in evidence-value order, not CSV order."""
    if not 1 <= max_cases <= MAX_CASES:
        raise ValueError(f'google-max-cases must be 1..{MAX_CASES}')

    available = Counter()
    candidates = []
    for row, decision in planned:
        priority = priority_bucket(row, decision)
        if priority is None:
            continue
        prio_rank, prio_label, gate = priority
        available[prio_label] += 1
        if priority_only and prio_label != priority_only:
            continue
        pid = str(row.get('pvp_id') or '').strip()
        if pid in prior_ids:
            continue
        variants = g.constrained_address_variants(row)
        if not variants:
            continue
        query = variants[0]

        # Never pay for a variant that dropped the meaningful street/locality.
        name_canon = canonical(gate['address_name'])
        name_tokens = [t for t in re.findall(r'[a-z0-9]+', name_canon)
                       if t not in {'del','della','delle','degli','dei','di','via','viale','piazza','localita'}]
        query_tokens = set(re.findall(r'[a-z0-9]+', canonical(query)))
        if not query or not name_tokens or not set(name_tokens).issubset(query_tokens):
            continue

        # Civic cases must keep the recognized civic. Route numbers are not civics
        # because the v3.4 eligibility parser has already separated them.
        civic_value = str(gate.get('address_civic') or '').strip()
        if civic_value:
            civic_pattern = re.escape(civic_value).replace('/', r'\s*/\s*')
            if not re.search(r'(?<!\d)' + civic_pattern + r'(?!\d)', query, re.I):
                continue

        canon = normalize_query(query)
        if canon in prior_queries:
            continue
        rank = hashlib.sha256(pid.encode('utf-8')).hexdigest()
        candidates.append((prio_rank, rank, row, decision, gate, query, prio_label, canon))

    # Higher-value evidence first. Hash is only a deterministic tie-breaker.
    candidates.sort(key=lambda c: (c[0], c[1]))
    used_queries = set(prior_queries)
    selections = []
    for prio_rank, rank, row, decision, gate, query, prio_label, canon in candidates:
        if canon in used_queries:
            continue
        selections.append((row, decision, gate['google_group'], query, prio_rank, prio_label))
        used_queries.add(canon)
        if len(selections) >= max_cases:
            break
    return selections, dict(available)

def load_google_core(max_requests):
    """Only in explicitly approved Google mode, and after full local analysis."""
    if not 1 <= max_requests <= MAX_REQUESTS:
        raise ValueError(f'google-max-requests must be 1..{MAX_REQUESTS}')
    load_project_env(required=('GOOGLE_GEOCODING_API_KEY',))
    if not os.environ.get('GOOGLE_GEOCODING_API_KEY', '').strip():
        raise RuntimeError('GOOGLE_GEOCODING_API_KEY missing; zero requests')
    # Each real HTTP attempt, including retries, must consume the run cap.
    os.environ['MAX_RUN_GEOCODING_CALLS'] = str(max_requests)
    os.environ['PLACES_AUTOCOMPLETE_FALLBACK'] = '0'
    os.environ['ADDRESS_VALIDATION_FALLBACK'] = '0'
    os.environ['HTTP_RETRIES'] = '1'
    os.environ['API_USAGE_SAVE_EVERY'] = '1'
    path = offline.HERE / 'google_geocode_core_v9_3_6.py'
    if not path.is_file():
        raise RuntimeError('Missing existing Google core v9.3.6: no requests made')
    spec = importlib.util.spec_from_file_location('aste_google_preview_v3_core', path)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    for name in ('google_forward_constrained', 'constrained_address_variants',
                 'score_forward_candidate', 'address_specificity', 'CacheStore', 'API_USAGE'):
        if not hasattr(g, name):
            raise RuntimeError('Google core incompatible: missing ' + name)
    if not g.CACHE_FILE.is_file() or not g.API_USAGE_FILE.is_file():
        raise RuntimeError('Existing Google cache and usage counter required; abort to avoid duplicate paid requests')
    if g.MAX_RUN_GEOCODING_CALLS != max_requests or g.HTTP_RETRIES != 1:
        raise RuntimeError('Google core ignored per-run cap or retry settings')
    return g


def compare_candidate(row, baseline, results, ctx, g):
    """Google never decides a definitive replacement in this preview."""
    meta = ctx.resolve_admin(offline.pvp_row(row))
    if meta.get('status') != 'RESOLVED':
        return {'assessment': 'SCARTATO_ADMIN_NON_RISOLTO'}
    evaluated = []
    for candidate in results:
        la, lo = candidate.get('lat'), candidate.get('lon')
        if not offline.valid_pair(la, lo):
            continue
        position = ctx.test(offline.pvp_row(row), lat=la, lon=lo, admin_meta=meta).get('position')
        if position not in ('INSIDE', 'BOUNDARY'):
            continue
        if candidate.get('partial_match'):
            continue
        evaluation = g.score_forward_candidate(row, candidate)
        if not evaluation:
            continue
        evaluated.append((float(evaluation.get('score', 0)), candidate, evaluation, position))
    if not evaluated:
        return {'assessment': 'NESSUN_CANDIDATO_COHERENTE'}
    score, result, ev, pos = max(evaluated, key=lambda v: v[0])
    la, lo = float(result['lat']), float(result['lon'])
    lt = str(result.get('location_type') or '').upper()
    civic_relation = policy_civic_relation(row, result)
    b_lat, b_lon = baseline.get('latitudine'), baseline.get('longitudine')
    offline_delta = (round(offline.distance_m(la, lo, b_lat, b_lon))
                     if offline.valid_pair(b_lat, b_lon) else None)
    p_lat, p_lon = row.get('pvp_latitudine'), row.get('pvp_longitudine')
    pvp_delta = (round(offline.distance_m(la, lo, p_lat, p_lon))
                 if offline.valid_pair(p_lat, p_lon) else None)
    if lt not in ('ROOFTOP', 'RANGE_INTERPOLATED') or score < 82 or pos != 'INSIDE':
        assessment = 'INDIZIO_NON_PRECISO_NON_SOSTITUIRE'
    elif pvp_delta is not None:
        assessment = ('GOOGLE_COINCIDE_CON_PVP_ORIGINE_NON_INDIPENDENTE' if pvp_delta <= 1 else
                      'GOOGLE_COMPATIBILE_CON_PVP_DA_VERIFICARE' if pvp_delta <= 150 else
                      'GOOGLE_DIVERGE_DA_PVP_DA_VERIFICARE')
    else:
        # A P2/P4 result without PVP can only be promoted as a civic candidate
        # when the exact civic that passed the v3.4 gate is present in Google's
        # structured street_number component. Variant/missing civics remain hints.
        expected_civic = bool(str(address_parts(row).get('civic') or '').strip())
        if expected_civic and civic_relation == 'exact':
            assessment = 'GOOGLE_CIVICO_ESATTO_DA_VALIDARE'
        elif expected_civic:
            assessment = 'INDIZIO_NON_PRECISO_NON_SOSTITUIRE'
        else:
            assessment = 'POSSIBILE_MIGLIORAMENTO_DA_VERIFICARE'
    return {'assessment': assessment, 'google_lat': la, 'google_lon': lo,
            'google_location_type': lt, 'google_formatted_address': result.get('formatted_address'),
            'google_score_diagnostico': score, 'google_civic_relation': civic_relation,
            'google_distance_from_pvp_m': pvp_delta,
            'google_distance_from_offline_m': offline_delta,
            'google_istat_position': pos, 'google_reason': '|'.join(ev.get('reasons') or [])}


def preview_new_rows(listings, *, max_rows, max_requests, max_cases, priority_only=None):
    if not 1 <= max_requests <= MAX_REQUESTS or not 1 <= max_cases <= MAX_CASES:
        raise ValueError(f'Explicit Google limits 1..{MAX_REQUESTS} are mandatory')
    if len(listings) > max_rows:
        raise RuntimeError('Incomplete offline batch: raise --max-geocode before Google preview')
    ctx, territorial, local = offline.prepare_context(no_anncsu=False)
    rows = []
    for listing in listings:
        row = {k: listing.get(k) for k in offline.SELECT}
        row['latitudine'] = row['longitudine'] = None
        if row.get('geocode_status') != 'PENDING':
            raise RuntimeError('Non-PENDING listing: stop')
        rows.append(row)
    planned = offline.plan_rows(rows, ctx, territorial, local)
    counts = Counter(p['geocode_status'] + ':' + str(p['geocode_precision']) for _, p in planned)
    sources = Counter(str(p.get('geocode_source') or 'NESSUNA') for _, p in planned)
    # Read earlier reports *before* any call, to avoid rechecking the same
    # deterministic v3 sample and using budget on repeated PVP announcements.
    prior_ids, prior_queries, previous_reports = previously_compared(REPORTS_DIR)
    # Do not require a key or load Google when there are no unresolved groups.
    available_eligible = {group: sum(category(row, dec) == group for row, dec in planned)
                          for group in ('PVP_CIVICO_NON_CONCLUSIVO', 'COMUNE_INDICATIVO', 'PVP_NON_CONFERMATO', 'REVIEW')}
    if not any(available_eligible.values()):
        selected, available = [], available_eligible
        g = None
    else:
        # Preflight core and spend cap. Existing cache and usage are mandatory.
        g = load_google_core(max_requests)
        selected, available = pick_fresh_cases(planned, g, prior_ids, prior_queries, max_cases, priority_only=priority_only)
    summary = {'reviewed_without_db_writes': len(rows), 'total_new': len(listings),
               'selection_truncated': False, 'decision_counts': dict(counts),
               'source_counts': dict(sources), 'google_calls': 0,
               'google_preview': {'mode': 'EXPLICIT_GOOGLE_NETWORK_PREVIEW_NO_DB_WRITES',
                                  'eligibility_policy': 'GOOGLE_AFTER_ANNCSU_V3_5_PRIORITY',
                                  'available_groups': available, 'selected_cases': len(selected),
                                  'selected_priority_counts': dict(Counter(x[5] for x in selected)),
                                  'previously_compared_ids': len(prior_ids),
                                  'previously_compared_queries': len(prior_queries),
                                  'previous_reports': len(previous_reports),
                                  'sampling': ('ONLY_' + priority_only if priority_only else 'PRIORITY_P1_DISCORDANZA_P2_NO_PVP_CIVICO_P3_PVP_CIVICO_THEN_TOPONIMI'),
                                  'priority_only': priority_only,
                                  'max_http_attempts': max_requests, 'max_cases': max_cases,
                                  'assessment_counts': {}, 'network_attempts': 0,
                                  'cache_hits': 0}}
    ensure_runtime_dirs()
    stamp = datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')
    csv_path = REPORTS_DIR / ('google_comparison_v3_' + stamp + '.csv')
    fields = ('pvp_id','group','priority_rank','priority_label','comune','sigla_provincia','indirizzo','pvp_latitudine',
              'pvp_longitudine','offline_source','offline_precision','offline_reason','google_eligibility_reason','address_kind',
              'query','assessment','google_lat','google_lon','google_location_type',
              'google_formatted_address','google_score_diagnostico','google_civic_relation',
              'google_distance_from_pvp_m','google_distance_from_offline_m','google_istat_position','google_reason')
    # If no fresh distinct queries remain, do not send any HTTP requests.
    if not selected:
        summary['google_preview']['assessment_counts'] = {}
        summary['google_preview']['report_csv'] = str(csv_path)
        with csv_path.open('w', encoding='utf-8-sig', newline='') as f:
            csv.DictWriter(f, fieldnames=fields, delimiter=';').writeheader()
        return summary
    # Prepare report before any possible Google network error. Never log key or URL.
    cache = g.CacheStore(g.CACHE_FILE)
    details = []
    try:
        for row, baseline, group, query, priority_rank, priority_label in selected:
            record = {'pvp_id': row['pvp_id'], 'group': group,
                      'priority_rank': priority_rank, 'priority_label': priority_label,
                      'comune': row.get('comune'), 'sigla_provincia': row.get('sigla_provincia'),
                      'indirizzo': row.get('indirizzo'),
                      'pvp_latitudine': row.get('pvp_latitudine'),
                      'pvp_longitudine': row.get('pvp_longitudine'),
                      'offline_source': baseline.get('geocode_source'),
                      'offline_precision': baseline.get('geocode_precision'),
                      'offline_reason': baseline.get('geocode_decision_reason'),
                      'google_eligibility_reason': eligibility(row, baseline)['google_reason'],
                      'address_kind': eligibility(row, baseline)['address_kind']}
            record['query'] = query
            try:
                results = g.google_forward_constrained(query, row, cache)
                record.update(compare_candidate(row, baseline, results, ctx, g))
            except g.ApiBudgetExceeded:
                record['assessment'] = 'STOP_TETTO_RICHIESTE'
                details.append(record)
                break
            except Exception as exc:
                # Do not serialize exception text: an HTTP URL can include the API key.
                record['assessment'] = 'GOOGLE_ERROR_' + type(exc).__name__
                if hasattr(exc, 'code'):
                    record['google_reason'] = 'HTTP_' + str(exc.code)
                details.append(record)
                break
            details.append(record)
    finally:
        cache.save()
        g.API_USAGE.save()
        usage = g.API_USAGE.run['geocoding']
        summary['google_calls'] = int(usage['network_attempts'])
        summary['google_preview']['network_attempts'] = summary['google_calls']
        summary['google_preview']['cache_hits'] = int(usage['cache_hits'])
        summary['google_preview']['assessment_counts'] = dict(Counter(d.get('assessment', 'UNKNOWN') for d in details))
        summary['google_preview']['report_csv'] = str(csv_path)
        with csv_path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore', delimiter=';')
            writer.writeheader()
            writer.writerows(details)
    return summary
