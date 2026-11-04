#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,json,math
from collections import Counter
from pathlib import Path

VERSION="experimental_unified_offline_v1"

def fnum(v):
    try:x=float(v)
    except (TypeError,ValueError):return None
    return x if math.isfinite(x) else None

def hav(a,b,c,d):
    if None in (a,b,c,d):return None
    r=6371008.8
    p1,p2=math.radians(a),math.radians(c)
    dp,dl=math.radians(c-a),math.radians(d-b)
    z=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(min(1.0,math.sqrt(z)))

def load_jsonl(path):
    out={}
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():continue
            x=json.loads(line)
            out[str(x.get("pvp_id") or "")]=x
    return out

def choose(res,ann,loc):
    p=(res or {}).get("primary") or {}
    a=(ann or {}).get("anncsu") or {}
    ab=a.get("best") or {}
    ac=a.get("civic") or {}
    lm=(loc or {}).get("match") or {}

    pvp_lat,pvp_lon=fnum(p.get("lat_pvp")),fnum(p.get("lon_pvp"))
    pvp_valid=bool(p.get("coord_valid")) and None not in (pvp_lat,pvp_lon)
    ann_lat,ann_lon=fnum(ac.get("lat")),fnum(ac.get("lon"))
    ann_civic=ac.get("status")=="CIVIC_COORD" and None not in (ann_lat,ann_lon)
    ann_street=a.get("status") in {"MATCH_EXACT","MATCH_ALIAS_HIGH"}
    loc_lat,loc_lon=fnum(lm.get("lat")),fnum(lm.get("lon"))
    loc_exact=lm.get("status")=="EXACT_UNIQUE" and None not in (loc_lat,loc_lon)

    d_pvp_ann=hav(pvp_lat,pvp_lon,ann_lat,ann_lon) if pvp_valid and ann_civic else None
    d_pvp_loc=hav(pvp_lat,pvp_lon,loc_lat,loc_lon) if pvp_valid and loc_exact else None
    d_ann_loc=hav(ann_lat,ann_lon,loc_lat,loc_lon) if ann_civic and loc_exact else None

    warnings=[]
    if d_pvp_ann is not None and d_pvp_ann>1000:
        warnings.append("PVP_ANNCSU_CIVIC_CONFLICT")
    if d_pvp_loc is not None and d_pvp_loc>6000:
        warnings.append("PVP_LOCALITY_CONFLICT")
    if d_ann_loc is not None and d_ann_loc>6000:
        warnings.append("ANNCSU_CIVIC_LOCALITY_CONFLICT")

    final={
        "lat":None,"lon":None,"source":"NONE","precision":"NESSUNA",
        "confidence":"NONE","status":"UNRESOLVED","reason":"Nessun candidato offline con coordinate."
    }

    if ann_civic:
        final.update({
            "lat":ann_lat,"lon":ann_lon,"source":"ANNCSU",
            "precision":"CIVICO","confidence":"HIGH","status":"RESOLVED",
            "reason":"Civico trovato su strada ANNCSU riconosciuta nello stesso comune PVP."
        })
        if "ANNCSU_CIVIC_LOCALITY_CONFLICT" in warnings:
            final["status"]="REVIEW"
            final["confidence"]="MEDIUM"
            final["reason"]+=" Conflitto forte con località ISTAT esatta."
    elif pvp_valid:
        final.update({
            "lat":pvp_lat,"lon":pvp_lon,"source":"PVP",
            "precision":"PVP_COORD","confidence":"MEDIUM","status":"RESOLVED",
            "reason":"Coordinate fornite dal bene PVP; ancora da validare territorialmente."
        })
        if loc_exact and d_pvp_loc is not None and d_pvp_loc<=1500:
            final["confidence"]="MEDIUM_HIGH"
            final["reason"]+=" Compatibili con località ISTAT esatta."
        elif "PVP_LOCALITY_CONFLICT" in warnings:
            final["status"]="REVIEW"
            final["confidence"]="LOW"
            final["reason"]+=" Conflitto forte con località ISTAT esatta."
    elif loc_exact:
        final.update({
            "lat":loc_lat,"lon":loc_lon,"source":"ISTAT_LOCALITA",
            "precision":"LOCALITA","confidence":"MEDIUM","status":"RESOLVED",
            "reason":"Punto rappresentativo ISTAT della località/frazione esatta nello stesso comune; non è il civico."
        })
    elif ann_street:
        final.update({
            "source":"ANNCSU","precision":"STRADA","confidence":"HIGH",
            "status":"NEEDS_COORD","reason":"Strada ANNCSU riconosciuta, ma nessuna coordinata civico disponibile offline."
        })

    return {
        "final":final,
        "warnings":warnings,
        "distances_m":{
            "pvp_anncsu_civic":round(d_pvp_ann,1) if d_pvp_ann is not None else None,
            "pvp_locality":round(d_pvp_loc,1) if d_pvp_loc is not None else None,
            "anncsu_civic_locality":round(d_ann_loc,1) if d_ann_loc is not None else None,
        },
        "evidence":{
            "pvp_coord_valid":pvp_valid,
            "anncsu_status":a.get("status"),
            "anncsu_odonimo":ab.get("odonimo"),
            "anncsu_civic_status":ac.get("status"),
            "locality_status":lm.get("status"),
            "locality_name":lm.get("nome"),
        }
    }

def process(res,ann,loc):
    x=choose(res,ann,loc)
    p=(res or {}).get("primary") or {}
    return {
        "pvp_id":(res or {}).get("pvp_id"),
        "comune":p.get("comune"),
        "codice_istat":p.get("codice_istat"),
        "raw_via":p.get("raw_via"),
        "street":p.get("street"),
        "civic":p.get("civic_for_geocode"),
        "multi_address":(res or {}).get("multi_address"),
        "multi_comune":(res or {}).get("multi_comune"),
        **x,
    }

def flat(x):
    f=x["final"];e=x["evidence"];d=x["distances_m"]
    return {
        "pvp_id":x.get("pvp_id"),"comune":x.get("comune"),"codice_istat":x.get("codice_istat"),
        "raw_via":x.get("raw_via"),"street":x.get("street"),"civic":x.get("civic"),
        "multi_address":x.get("multi_address"),"multi_comune":x.get("multi_comune"),
        "final_status":f.get("status"),"final_source":f.get("source"),"final_precision":f.get("precision"),
        "final_confidence":f.get("confidence"),"final_lat":f.get("lat"),"final_lon":f.get("lon"),
        "final_reason":f.get("reason"),"warnings":"|".join(x.get("warnings") or []),
        "anncsu_status":e.get("anncsu_status"),"anncsu_odonimo":e.get("anncsu_odonimo"),
        "anncsu_civic_status":e.get("anncsu_civic_status"),"locality_status":e.get("locality_status"),
        "locality_name":e.get("locality_name"),
        "dist_pvp_anncsu_civic_m":d.get("pvp_anncsu_civic"),
        "dist_pvp_locality_m":d.get("pvp_locality"),
        "dist_anncsu_civic_locality_m":d.get("anncsu_civic_locality"),
    }

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("resolver_jsonl",type=Path)
    ap.add_argument("anncsu_jsonl",type=Path)
    ap.add_argument("locality_jsonl",type=Path)
    ap.add_argument("--out-dir",type=Path,default=Path("runtime/reports"))
    args=ap.parse_args()

    rs=load_jsonl(args.resolver_jsonl);ans=load_jsonl(args.anncsu_jsonl);locs=load_jsonl(args.locality_jsonl)
    ids=list(rs)
    rows=[process(rs[i],ans.get(i,{}),locs.get(i,{})) for i in ids]

    args.out_dir.mkdir(parents=True,exist_ok=True)
    jp=args.out_dir/"experimental_unified_offline_v1.jsonl"
    cp=args.out_dir/"experimental_unified_offline_v1.csv"
    sp=args.out_dir/"experimental_unified_offline_v1_stats.json"

    with jp.open("w",encoding="utf-8") as f:
        for x in rows:f.write(json.dumps(x,ensure_ascii=False)+"\n")
    fs=[flat(x) for x in rows]
    with cp.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(fs[0]));w.writeheader();w.writerows(fs)

    stats={
        "version":VERSION,"rows":len(rows),
        "final_status":dict(Counter(x["final"]["status"] for x in rows)),
        "final_source":dict(Counter(x["final"]["source"] for x in rows)),
        "final_precision":dict(Counter(x["final"]["precision"] for x in rows)),
        "final_confidence":dict(Counter(x["final"]["confidence"] for x in rows)),
        "warnings":dict(Counter(w for x in rows for w in x.get("warnings",[]))),
    }
    sp.write_text(json.dumps(stats,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(stats,ensure_ascii=False,indent=2))
    print("[OUT]",jp);print("[OUT]",cp);print("[OUT]",sp)

if __name__=="__main__":main()
