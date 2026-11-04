#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,json,re,unicodedata
from collections import Counter
from pathlib import Path

VERSION="experimental_google_plan_v1"

def clean(v):
    return re.sub(r"\s+"," ",str(v or "").replace("\xa0"," ")).strip()

def norm(v):
    s=unicodedata.normalize("NFKD",clean(v).replace("’","'"))
    s="".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    return re.sub(r"\s+"," ",re.sub(r"[^a-z0-9]+"," ",s)).strip()

def load(path):
    out={}
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():continue
            x=json.loads(line);out[str(x.get("pvp_id") or "")]=x
    return out

def add(qs,label,q):
    q=clean(q).strip(" ,")
    if not q:return
    k=norm(q)
    if not k:return
    if any(norm(x["query"])==k for x in qs):return
    qs.append({"label":label,"query":q})

def suffix(comune,prov,embedded_place=None):
    parts=[]
    if embedded_place:parts.append(clean(embedded_place))
    if comune and norm(comune)!=norm(embedded_place):parts.append(clean(comune))
    if prov:parts.append(clean(prov))
    parts.append("Italia")
    return ", ".join(x for x in parts if x)

def plan_one(res,ann,loc,unified):
    p=(res or {}).get("primary") or {}
    a=(ann or {}).get("anncsu") or {}
    b=a.get("best") or {}
    lm=(loc or {}).get("match") or {}
    f=(unified or {}).get("final") or {}
    status=clean(f.get("status"))
    if status not in {"NEEDS_COORD","UNRESOLVED","REVIEW"}:
        return {"eligible":False,"status":status,"queries":[]}

    comune=clean(p.get("comune"))
    prov=clean(p.get("provincia"))
    street=clean(p.get("street"))
    civic=clean(p.get("civic_for_geocode"))
    ann_street=clean(b.get("odonimo"))
    embedded=clean(p.get("embedded_place"))
    loc_name=clean(lm.get("nome")) if lm.get("status")=="EXACT_UNIQUE" else ""
    raw_queries=p.get("queries") or []
    qs=[]

    # 1) Best official ANNCSU odonym in the PVP municipality.
    # An exact ISTAT locality is trusted context within the structured PVP comune.
    # An embedded place different from the structured comune is instead a competing
    # hypothesis and must NEVER be merged into the same Google query.
    trusted_context=loc_name

    if ann_street:
        target=" ".join(x for x in (ann_street,civic) if x)
        add(qs,"ANNCSU_OFFICIAL", target+", "+suffix(comune,prov,trusted_context))
        if civic:
            add(qs,"ANNCSU_STREET_ONLY", ann_street+", "+suffix(comune,prov,trusted_context))

    # 2) Parsed PVP address, preserving structured municipality.
    if street:
        target=" ".join(x for x in (street,civic) if x)
        add(qs,"PVP_PARSED", target+", "+suffix(comune,prov,trusted_context))
        if civic:
            add(qs,"PVP_STREET_ONLY", street+", "+suffix(comune,prov,trusted_context))

    # 3) Exact locality point/name as a geography-oriented query.
    if loc_name:
        add(qs,"ISTAT_LOCALITY_NAME", loc_name+", "+suffix(comune,prov))

    # 4) Embedded postal place may reveal structured-PVP contradiction.
    if embedded and norm(embedded)!=norm(comune):
        target_street=ann_street or street
        if target_street:
            target=" ".join(x for x in (target_street,civic) if x)
            add(qs,"EMBEDDED_PLACE", target+", "+suffix("",prov,embedded))
        else:
            add(qs,"EMBEDDED_PLACE_ONLY", embedded+", "+(prov+", Italia" if prov else "Italia"))

    # 5) Existing parser candidates only as final fallback.
    for i,q in enumerate(raw_queries[:2],1):
        add(qs,f"PARSER_QUERY_{i}",q)

    # Keep the planner bounded and deterministic.
    qs=qs[:3]
    return {
        "eligible":True,
        "status":status,
        "priority":{"REVIEW":1,"NEEDS_COORD":2,"UNRESOLVED":3}.get(status,9),
        "queries":qs,
        "context":{
            "comune":comune,"provincia":prov,"embedded_place":embedded,
            "locality_name":loc_name,"anncsu_odonimo":ann_street,
            "street":street,"civic":civic,
        },
    }

def process(pid,res,ann,loc,uni):
    x=plan_one(res,ann,loc,uni)
    return {"pvp_id":pid,**x}

def flat(x):
    qs=x.get("queries") or [];ctx=x.get("context") or {}
    return {
        "pvp_id":x.get("pvp_id"),"eligible":x.get("eligible"),"status":x.get("status"),
        "priority":x.get("priority"),"query_count":len(qs),
        "query_1_label":qs[0]["label"] if len(qs)>0 else None,"query_1":qs[0]["query"] if len(qs)>0 else None,
        "query_2_label":qs[1]["label"] if len(qs)>1 else None,"query_2":qs[1]["query"] if len(qs)>1 else None,
        "query_3_label":qs[2]["label"] if len(qs)>2 else None,"query_3":qs[2]["query"] if len(qs)>2 else None,
        "comune":ctx.get("comune"),"provincia":ctx.get("provincia"),
        "embedded_place":ctx.get("embedded_place"),"locality_name":ctx.get("locality_name"),
        "anncsu_odonimo":ctx.get("anncsu_odonimo"),"street":ctx.get("street"),"civic":ctx.get("civic"),
    }

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("resolver_jsonl",type=Path)
    ap.add_argument("anncsu_jsonl",type=Path)
    ap.add_argument("locality_jsonl",type=Path)
    ap.add_argument("unified_jsonl",type=Path)
    ap.add_argument("--out-dir",type=Path,default=Path("runtime/reports"))
    args=ap.parse_args()

    rs,ans,locs,unis=map(load,[args.resolver_jsonl,args.anncsu_jsonl,args.locality_jsonl,args.unified_jsonl])
    rows=[process(pid,rs[pid],ans.get(pid,{}),locs.get(pid,{}),unis.get(pid,{})) for pid in rs]
    eligible=[x for x in rows if x.get("eligible")]
    all_queries=[q["query"] for x in eligible for q in (x.get("queries") or [])]
    unique={norm(q) for q in all_queries if norm(q)}

    args.out_dir.mkdir(parents=True,exist_ok=True)
    jp=args.out_dir/"experimental_google_plan_v1.jsonl"
    cp=args.out_dir/"experimental_google_plan_v1.csv"
    sp=args.out_dir/"experimental_google_plan_v1_stats.json"

    with jp.open("w",encoding="utf-8") as f:
        for x in rows:f.write(json.dumps(x,ensure_ascii=False)+"\n")
    fs=[flat(x) for x in rows]
    with cp.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(fs[0]));w.writeheader();w.writerows(fs)

    stats={
        "version":VERSION,"rows":len(rows),"eligible_rows":len(eligible),
        "eligible_status":dict(Counter(x.get("status") for x in eligible)),
        "query_count":dict(Counter(len(x.get("queries") or []) for x in eligible)),
        "query_labels":dict(Counter(q["label"] for x in eligible for q in (x.get("queries") or []))),
        "total_candidate_queries":len(all_queries),"unique_candidate_queries":len(unique),
        "rows_without_query":sum(not x.get("queries") for x in eligible),
    }
    sp.write_text(json.dumps(stats,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(stats,ensure_ascii=False,indent=2))
    print("[OUT]",jp);print("[OUT]",cp);print("[OUT]",sp)

if __name__=="__main__":main()
