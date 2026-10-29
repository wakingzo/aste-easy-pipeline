#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only semantic benchmark: legacy planner vs v3.16 fast planner.
Zero DB writes, zero Google. Uses one authoritative read of listings.pvp_id.
"""
from __future__ import annotations
import json, os, sys, time
from datetime import datetime
from pathlib import Path

HERE=Path(__file__).resolve().parent
SRC=HERE.parent
for p in (SRC,HERE,SRC/'pipeline'):
    if str(p) not in sys.path: sys.path.insert(0,str(p))

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env
import pvp_import_atomic_v1 as fast
import pvp_import_consolidated_preview as previous


def sec(t): return round(time.perf_counter()-t,3)

def main():
    ensure_runtime_dirs()
    load_project_env(required=('SUPABASE_URL','SUPABASE_SERVICE_ROLE'))
    for p in (previous.IN_RAW,previous.IN_ALLEGATI,previous.IN_SOGGETTI,previous.ISTAT_XLSX):
        if not p.is_file(): raise RuntimeError('Input missing: '+str(p))
    t0=time.perf_counter()
    raw_by_id,sha,dups=fast._load_raw_unique_with_sha(previous.IN_RAW)
    attachments=previous.load_jsonl(previous.IN_ALLEGATI)
    subjects=previous.load_jsonl(previous.IN_SOGGETTI)
    io_sec=sec(t0)

    base=os.environ['SUPABASE_URL'].rstrip('/'); key=os.environ['SUPABASE_SERVICE_ROLE']
    t=time.perf_counter(); db_ids,pages=fast._fetch_all_existing_listing_ids(base,key); db_sec=sec(t)
    existing=set(raw_by_id)&db_ids
    t=time.perf_counter(); istat=previous.load_istat_admin(previous.ISTAT_XLSX); istat_sec=sec(t)
    if not istat.get('ok'): raise RuntimeError('ISTAT unavailable: '+str(istat.get('reason')))
    today=datetime.now().astimezone().date()

    t=time.perf_counter()
    legacy=previous.plan_new_only(list(raw_by_id.values()),attachments,subjects,existing,istat,today)
    legacy_sec=sec(t)
    t=time.perf_counter()
    modern=fast._build_plan_fast_core(raw_by_id,existing,istat,today,attachments,subjects_path=None)
    fast_sec=sec(t)

    legacy_ids=[str(x['pvp_id']) for x in legacy['eligible']]
    fast_ids=[str(x['pvp_id']) for x in modern['eligible']]
    checks={
        'eligible_rows_exact': modern['eligible']==legacy['eligible'],
        'eligible_id_order_exact': fast_ids==legacy_ids,
        'decision_counts_exact': dict(modern['decisions'])==dict(legacy['decisions']),
        'source_counts_exact': dict(modern['sources'])==dict(legacy['sources']),
        'attachments_exact': modern['attachments']==legacy['attachments'],
    }
    result='PASS' if all(checks.values()) else 'FAIL'
    obj={
        'version':'plan_benchmark_v3_16_1','mode':'READ_ONLY_ZERO_WRITES_ZERO_GOOGLE','result':result,
        'created_at':datetime.now().astimezone().isoformat(),'raw_sha256':sha,
        'incoming_unique':len(raw_by_id),'existing_in_raw':len(existing),'db_listing_ids_total':len(db_ids),
        'new_candidates':len(raw_by_id)-len(existing),'eligible_new':len(fast_ids),
        'raw_duplicate_rows_same_payload':dups,'db_id_pages':pages,'checks':checks,
        'legacy_decisions':dict(legacy['decisions']),'fast_decisions':dict(modern['decisions']),
        'timings_seconds':{'io':io_sec,'db_ids':db_sec,'istat':istat_sec,'legacy_cpu':legacy_sec,'fast_cpu':fast_sec,'total':sec(t0)},
    }
    REPORTS_DIR.mkdir(parents=True,exist_ok=True)
    out=REPORTS_DIR/('plan_benchmark_v3_16_'+datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')+'.json')
    out.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('[PLAN BENCH] RESULT='+result,flush=True)
    print(f"[PLAN BENCH] incoming={len(raw_by_id)} existing={len(existing)} new_candidates={obj['new_candidates']} eligible={len(fast_ids)}",flush=True)
    print(f"[PLAN BENCH] db pages={pages}; legacy_cpu={legacy_sec:.3f}s; fast_cpu={fast_sec:.3f}s; total={obj['timings_seconds']['total']:.3f}s",flush=True)
    print('[PLAN BENCH] checks='+json.dumps(checks,ensure_ascii=False),flush=True)
    print('[PLAN BENCH] report:',out,flush=True)
    return 0 if result=='PASS' else 2

if __name__=='__main__': raise SystemExit(main())
