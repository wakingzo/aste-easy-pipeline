#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Preview of Google ELIGIBILITY for every new auction. Strictly offline."""
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
import geocode_pending_v2 as offline
from google_eligibility_v3_2 import eligibility


def preview_new_rows(listings, *, max_rows):
    if len(listings) > max_rows:
        raise RuntimeError('Incomplete offline batch; raise --max-geocode (no Google calls)')
    ctx, territorial, local = offline.prepare_context(no_anncsu=False)
    rows = []
    for listing in listings:
        row = {k: listing.get(k) for k in offline.SELECT}
        row['latitudine'] = row['longitudine'] = None
        if row.get('geocode_status') != 'PENDING':
            raise RuntimeError('Non-PENDING listing: stop')
        rows.append(row)
    planned = offline.plan_rows(rows, ctx, territorial, local)
    counts = Counter(); source_counts = Counter(); gate_counts = Counter(); groups = Counter(); details=[]
    for row, dec in planned:
        counts[str(dec.get('geocode_status'))+':'+str(dec.get('geocode_precision'))] += 1
        source_counts[str(dec.get('geocode_source'))] += 1
        gate = eligibility(row, dec)
        gate_counts[gate['google_reason']] += 1
        if gate['google_eligible']:
            groups[gate['google_group']] += 1
        details.append({'pvp_id':row['pvp_id'], 'comune':row.get('comune'),
                        'indirizzo':row.get('indirizzo'), 'pvp_geo_source':row.get('pvp_geo_source'),
                        'pvp_has_coords':offline.valid_pair(row.get('pvp_latitudine'),row.get('pvp_longitudine')),
                        'anncsu_result':dec.get('geocode_source'), 'offline_precision':dec.get('geocode_precision'),
                        'google_eligible':'SI' if gate['google_eligible'] else 'NO',
                        'google_reason':gate['google_reason'], 'address_kind':gate['address_kind'],
                        'address_name':gate['address_name'], 'address_civic':gate['address_civic']})
    ensure_runtime_dirs()
    out = REPORTS_DIR / ('google_policy_plan_v3_4_'+datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')+'.csv')
    with out.open('w',encoding='utf-8-sig',newline='') as f:
        writer = csv.DictWriter(f,fieldnames=('pvp_id','comune','indirizzo','pvp_geo_source','pvp_has_coords',
                         'anncsu_result','offline_precision','google_eligible','google_reason','address_kind','address_name','address_civic'),delimiter=';')
        writer.writeheader();writer.writerows(details)
    return {'reviewed_without_db_writes':len(rows),'total_new':len(listings),'selection_truncated':False,
            'decision_counts':dict(counts),'source_counts':dict(source_counts),'google_calls':0,
            'google_policy_plan':{'eligible_count':sum(groups.values()),'eligible_groups':dict(groups),
                                  'reason_counts':dict(gate_counts),'report_csv':str(out),
                                  'policy_version':'GOOGLE_AFTER_ANNCSU_V3_4',
                                  'mode':'GOOGLE_ZERO_SUPABASE_GET_ONLY'}}
