#!/usr/bin/env python3
from __future__ import annotations

import argparse,csv,hashlib,importlib.util,json,os,re,sys
from collections import Counter
from pathlib import Path

HERE=Path(__file__).resolve().parent
SRC=HERE.parent
ROOT=SRC.parent
if str(SRC) not in sys.path: sys.path.insert(0,str(SRC))

from project_env import load_project_env

VERSION="experimental_google_live_v1"

def clean(v):
    return re.sub(r"\s+"," ",str(v or "").replace("\xa0"," ")).strip()

def load_jsonl(path):
    rows=[]
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if line.strip(): rows.append(json.loads(line))
    return rows

def load_core(run_cap,monthly_cap):
    load_project_env(required=("GOOGLE_GEOCODING_API_KEY",))
    if not os.environ.get("GOOGLE_GEOCODING_API_KEY","").strip():
        raise RuntimeError("GOOGLE_GEOCODING_API_KEY mancante")
    os.environ["MAX_RUN_GEOCODING_CALLS"]=str(run_cap)
    os.environ["MAX_MONTH_GEOCODING_CALLS"]=str(monthly_cap)
    os.environ["HTTP_RETRIES"]="1"
    os.environ["API_USAGE_SAVE_EVERY"]="1"
    os.environ["PLACES_AUTOCOMPLETE_FALLBACK"]="0"
    os.environ["ADDRESS_VALIDATION_FALLBACK"]="0"
    path=HERE/"google_geocode_core_v9_3_6.py"
    spec=importlib.util.spec_from_file_location("experimental_google_core_v936",path)
    g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
    if not g.CACHE_FILE.is_file():
        raise RuntimeError("Cache Google esistente obbligatoria: "+str(g.CACHE_FILE))
    if not g.API_USAGE_FILE.is_file():
        raise RuntimeError("Contatore Google esistente obbligatorio: "+str(g.API_USAGE_FILE))
    g.API_USAGE=g.ApiUsageTracker(g.API_USAGE_FILE)
    return g

def street_core(ctx):
    street=clean(ctx.get("anncsu_odonimo") or ctx.get("street"))
    civic=clean(ctx.get("civic"))
    return " ".join(x for x in (street,civic) if x)

def hypothesis_row(item,q):
    ctx=item.get("context") or {}
    label=clean(q.get("label"))
    embedded=clean(ctx.get("embedded_place"))
    comune=embedded if label.startswith("EMBEDDED_PLACE") and embedded else clean(ctx.get("comune"))
    prov=clean(ctx.get("provincia"))
    if prov in {"-","--","N/D","ND"}: prov=""
    indirizzo=street_core(ctx)
    if label=="ISTAT_LOCALITY_NAME":
        indirizzo=clean(ctx.get("locality_name"))
    return {
        "pvp_id":item.get("pvp_id"),
        "indirizzo":indirizzo or clean(q.get("query")),
        "comune":comune,
        "sigla_provincia":prov,
        "provincia":prov,
        "cap":"",
    }

def rank_candidate(g,row,result,label,query):
    scored=g.score_forward_candidate(row,result,locality_fallback=(label=="ISTAT_LOCALITY_NAME"))
    if not scored:return None
    score=float(scored.get("score") or 0)
    if result.get("partial_match"): score-=8
    lt=clean(result.get("location_type")).upper()
    # A street-only query should not be presented as civic precision.
    requested_civic=bool(re.search(r"\b\d+[A-Za-z]?(?:[/\-]\w+)?\b", clean(row.get("indirizzo"))))
    precision="CIVICO" if requested_civic and lt in {"ROOFTOP","RANGE_INTERPOLATED"} else "STRADA"
    if label=="ISTAT_LOCALITY_NAME": precision="LOCALITA"
    return {
        "score":round(score,2),
        "confidence":"HIGH" if score>=82 else ("MEDIUM" if score>=72 else "LOW"),
        "precision":precision,
        "lat":result.get("lat"),"lon":result.get("lon"),
        "formatted_address":result.get("formatted_address"),
        "location_type":lt,
        "partial_match":bool(result.get("partial_match")),
        "place_id":result.get("place_id"),
        "query_label":label,"query":query,
        "hypothesis_comune":row.get("comune"),
        "score_details":scored,
    }

def hav(a,b,c,d):
    import math
    if None in (a,b,c,d):return None
    r=6371008.8
    p1,p2=math.radians(float(a)),math.radians(float(c))
    dp,dl=math.radians(float(c)-float(a)),math.radians(float(d)-float(b))
    z=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(min(1.0,math.sqrt(z)))

def arbitrate_review(attempts):
    cands=[]
    for a in attempts:
        b=a.get("best")
        if b and float(b.get("score") or 0)>=82:
            cands.append(b)
    if not cands:
        return "NO_GOOGLE_MATCH",None,{}
    cands.sort(key=lambda x:float(x.get("score") or 0),reverse=True)
    top=cands[0]
    if len(cands)==1:
        return "ARBITRATION_WINNER",top,{"reason":"single_high_candidate"}

    second=cands[1]
    d=hav(top.get("lat"),top.get("lon"),second.get("lat"),second.get("lon"))
    gap=float(top.get("score") or 0)-float(second.get("score") or 0)

    # Different textual hypotheses that land on effectively the same place
    # are consensus, not a conflict (e.g. comune vs frazione naming).
    if d is not None and d<=250:
        return "ARBITRATION_CONSENSUS",top,{"reason":"high_candidates_same_place","top_second_distance_m":round(d,1),"score_gap":round(gap,2)}

    # A materially better HIGH candidate can win. Otherwise preserve REVIEW.
    if gap>=8:
        return "ARBITRATION_WINNER",top,{"reason":"score_gap","top_second_distance_m":round(d,1) if d is not None else None,"score_gap":round(gap,2)}

    return "ARBITRATION_CONFLICT",None,{"reason":"competing_high_candidates","top_second_distance_m":round(d,1) if d is not None else None,"score_gap":round(gap,2),"top":top,"second":second}

def selected_rows(plan,limit_rows):
    eligible=[x for x in plan if x.get("eligible") and x.get("queries")]
    pr={"REVIEW":0,"NEEDS_COORD":1,"UNRESOLVED":2}
    eligible.sort(key=lambda x:(pr.get(clean(x.get("status")),9),hashlib.sha256(str(x.get("pvp_id")).encode()).hexdigest()))
    return eligible[:limit_rows] if limit_rows else eligible

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("plan_jsonl",type=Path)
    ap.add_argument("--run-cap",type=int,default=30,help="Massimo tentativi HTTP Google Geocoding in questa esecuzione")
    ap.add_argument("--monthly-cap",type=int,default=9000)
    ap.add_argument("--max-rows",type=int,default=30)
    ap.add_argument("--out-dir",type=Path,default=Path("runtime/reports"))
    args=ap.parse_args()
    if not 1<=args.run_cap<=200: raise SystemExit("--run-cap deve essere 1..200")
    if args.monthly_cap<args.run_cap: raise SystemExit("--monthly-cap deve essere >= --run-cap")

    plan=load_jsonl(args.plan_jsonl)
    todo=selected_rows(plan,args.max_rows)
    g=load_core(args.run_cap,args.monthly_cap)
    cache=g.CacheStore(g.CACHE_FILE)

    out=[]
    stopped_budget=False
    for item in todo:
        attempts=[];best=None
        for q in item.get("queries") or []:
            label=clean(q.get("label"));query=clean(q.get("query"))
            row=hypothesis_row(item,q)
            try:
                results=g.google_forward(query,cache)
            except g.ApiBudgetExceeded:
                stopped_budget=True
                attempts.append({"label":label,"query":query,"outcome":"BUDGET_STOP"})
                break
            except Exception as e:
                attempts.append({"label":label,"query":query,"outcome":"ERROR","error":f"{type(e).__name__}: {e}"})
                stopped_budget=True
                break

            ranked=[]
            for result in results:
                rc=rank_candidate(g,row,result,label,query)
                if rc: ranked.append(rc)
            ranked.sort(key=lambda x:x["score"],reverse=True)
            cand=ranked[0] if ranked else None
            attempts.append({
                "label":label,"query":query,
                "outcome":"CANDIDATE" if cand else ("ZERO_RESULTS" if not results else "REJECTED"),
                "google_results":len(results),
                "best":cand,
            })
            if cand and (best is None or cand["score"]>best["score"]): best=cand
            # For ordinary unresolved rows, HIGH is enough and we stop early.
            # REVIEW rows are different: they exist because offline sources conflict,
            # so every planned hypothesis must be evaluated before arbitration.
            if clean(item.get("status"))!="REVIEW" and cand and cand["score"]>=82:
                break
        input_status=clean(item.get("status"))
        arbitration={}
        if input_status=="REVIEW":
            decision,arb_best,arbitration=arbitrate_review(attempts)
            if arb_best is not None:
                best=arb_best
            elif decision=="ARBITRATION_CONFLICT":
                best=None
        else:
            decision="ACCEPT_HIGH" if best and best["score"]>=82 else ("CANDIDATE_REVIEW" if best else "NO_GOOGLE_MATCH")
        out.append({
            "pvp_id":item.get("pvp_id"),"input_status":input_status,
            "best":best,"attempts":attempts,
            "decision":decision,"arbitration":arbitration,
        })
        if stopped_budget: break

    cache.save();g.API_USAGE.save()

    args.out_dir.mkdir(parents=True,exist_ok=True)
    jp=args.out_dir/"experimental_google_live_v1.jsonl"
    cp=args.out_dir/"experimental_google_live_v1.csv"
    sp=args.out_dir/"experimental_google_live_v1_stats.json"

    with jp.open("w",encoding="utf-8") as f:
        for x in out:f.write(json.dumps(x,ensure_ascii=False)+"\n")

    flat=[]
    for x in out:
        b=x.get("best") or {}
        flat.append({
            "pvp_id":x.get("pvp_id"),"input_status":x.get("input_status"),"decision":x.get("decision"),
            "best_score":b.get("score"),"best_confidence":b.get("confidence"),"best_precision":b.get("precision"),
            "best_lat":b.get("lat"),"best_lon":b.get("lon"),"best_location_type":b.get("location_type"),
            "best_formatted_address":b.get("formatted_address"),"best_query_label":b.get("query_label"),
            "best_query":b.get("query"),"hypothesis_comune":b.get("hypothesis_comune"),
            "attempt_count":len(x.get("attempts") or []),
            "arbitration_reason":(x.get("arbitration") or {}).get("reason"),
            "arbitration_distance_m":(x.get("arbitration") or {}).get("top_second_distance_m"),
            "arbitration_score_gap":(x.get("arbitration") or {}).get("score_gap"),
        })
    if flat:
        with cp.open("w",encoding="utf-8-sig",newline="") as f:
            w=csv.DictWriter(f,fieldnames=list(flat[0]));w.writeheader();w.writerows(flat)

    usage=g.API_USAGE.run["geocoding"]
    stats={
        "version":VERSION,"planned_rows":len(todo),"processed_rows":len(out),
        "run_cap":args.run_cap,"monthly_cap":args.monthly_cap,
        "network_attempts":usage.get("network_attempts",0),"cache_hits":usage.get("cache_hits",0),
        "logical_requests":usage.get("logical_requests",0),"budget_stopped":stopped_budget,
        "input_status":dict(Counter(x.get("input_status") for x in out)),
        "decision":dict(Counter(x.get("decision") for x in out)),
        "best_precision":dict(Counter((x.get("best") or {}).get("precision","NONE") for x in out)),
    }
    local,offset,effective=g.API_USAGE.month_usage("geocoding")
    stats["month_usage"]={"local":local,"external_offset":offset,"effective":effective,"remaining_under_cap":max(0,args.monthly_cap-effective)}
    sp.write_text(json.dumps(stats,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(stats,ensure_ascii=False,indent=2))
    print("[OUT]",jp);print("[OUT]",cp);print("[OUT]",sp)

if __name__=="__main__":main()
