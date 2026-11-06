#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from openpyxl import load_workbook

HERE=Path(__file__).resolve().parent
SRC=HERE.parent
for p in (SRC,HERE):
    if str(p) not in __import__("sys").path:
        __import__("sys").path.insert(0,str(p))

from aste_paths import REPORTS_DIR, ANNCSU_DIR, DATA_DIR, REFERENCE_DIR, ensure_runtime_dirs
from project_env import load_project_env

VERSION="historical_missing_pin_recovery_preview_v1"
PAGE=1000

def clean(v):
    return "" if v is None else re.sub(r"\s+"," ",str(v).replace("\u00a0"," ")).strip()

def norm(v):
    s=clean(v).replace("’","'").replace("`","'")
    s=unicodedata.normalize("NFKD",s)
    s="".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    return re.sub(r"\s+"," ",re.sub(r"[^a-z0-9]+"," ",s)).strip()

def _f(v):
    try: x=float(v)
    except (TypeError,ValueError): return None
    return x if math.isfinite(x) else None

def valid_pair(lat,lon):
    la,lo=_f(lat),_f(lon)
    return la is not None and lo is not None and -90<=la<=90 and -180<=lo<=180 and not (la==0 and lo==0)

def maps_link(lat,lon):
    return f"https://www.google.com/maps?q={float(lat):.8f},{float(lon):.8f}"

def _get(base,key,path,params):
    url=base.rstrip("/")+"/rest/v1/"+path+"?"+urlencode(params,safe="(),.*")
    req=Request(url,method="GET",headers={"apikey":key,"Authorization":"Bearer "+key,"Accept":"application/json"})
    with urlopen(req,timeout=120) as r:
        return json.loads(r.read().decode("utf-8") or "[]")

def _all(base,key,path,params):
    out=[]; offset=0
    while True:
        p=dict(params); p["limit"]=str(PAGE); p["offset"]=str(offset)
        rows=_get(base,key,path,p); out.extend(rows)
        if len(rows)<PAGE: break
        offset += len(rows)
    return out

def open_sqlite(path):
    path=Path(path).resolve()
    if not path.is_file():
        raise RuntimeError("SQLite missing: "+str(path))
    con=sqlite3.connect("file:"+str(path).replace("\\","/")+"?mode=ro",uri=True)
    con.row_factory=sqlite3.Row
    return con

def find_header_map(ws):
    # ISTAT workbook headers can change wording slightly; discover by normalized labels.
    for row_no,row in enumerate(ws.iter_rows(min_row=1,max_row=20,values_only=True),1):
        vals=[norm(x) for x in row]
        def idx_contains(*tokens):
            for i,v in enumerate(vals):
                if all(t in v for t in tokens):
                    return i
            return None
        name_i = idx_contains("denominazione","italiano")
        if name_i is None: name_i = idx_contains("denominazione","comune")
        code_i = idx_contains("codice","comune","alfanumerico")
        if code_i is None: code_i = idx_contains("codice","comune")
        prov_i = idx_contains("denominazione","provincia")
        sigla_i = idx_contains("sigla","automobilistica")
        reg_i = idx_contains("denominazione","regione")
        if name_i is not None and code_i is not None:
            return row_no,{"name":name_i,"code":code_i,"province":prov_i,"sigla":sigla_i,"region":reg_i}
    raise RuntimeError("Header ISTAT comuni non riconosciuto")

def load_comuni_index(path):
    wb=load_workbook(path,read_only=True,data_only=True)
    ws=wb[wb.sheetnames[0]]
    header_row,hm=find_header_map(ws)
    by_name=defaultdict(list)
    for row in ws.iter_rows(min_row=header_row+1,values_only=True):
        name=clean(row[hm["name"]] if hm["name"] is not None and hm["name"]<len(row) else "")
        code=clean(row[hm["code"]] if hm["code"] is not None and hm["code"]<len(row) else "")
        province=clean(row[hm["province"]] if hm["province"] is not None and hm["province"]<len(row) else "")
        sigla=clean(row[hm["sigla"]] if hm["sigla"] is not None and hm["sigla"]<len(row) else "")
        region=clean(row[hm["region"]] if hm["region"] is not None and hm["region"]<len(row) else "")
        digits="".join(ch for ch in code if ch.isdigit())
        if not name or not digits: continue
        code=digits.zfill(6)[-6:]
        by_name[norm(name)].append({"name":name,"code":code,"province":province,"sigla":sigla,"region":region})
    wb.close()
    return by_name

def resolve_code(row,index):
    comune=clean(row.get("comune"))
    provincia=clean(row.get("provincia"))
    regione=clean(row.get("regione"))
    cands=list(index.get(norm(comune),[]))
    if not cands:
        return None,"NO_EXACT_COMUNE",[]
    if len(cands)==1:
        return cands[0],"EXACT_COMUNE_UNIQUE",cands

    np,nr=norm(provincia),norm(regione)
    p=[x for x in cands if np and norm(x.get("province"))==np]
    if len(p)==1: return p[0],"EXACT_COMUNE_PROVINCIA",cands
    r=[x for x in cands if nr and norm(x.get("region"))==nr]
    if len(r)==1: return r[0],"EXACT_COMUNE_REGIONE",cands
    return None,"AMBIGUOUS_COMUNE",cands

def municipality_pin(code,comune,ann_con,istat_con,cache):
    if code in cache: return cache[code]
    row=ann_con.execute(
        "select min_lat,max_lat,min_lon,max_lon from municipality_bbox where codice_istat=?",(code,)
    ).fetchone()
    if row:
        rep={"lat":(float(row["min_lat"])+float(row["max_lat"]))/2.0,
             "lon":(float(row["min_lon"])+float(row["max_lon"]))/2.0,
             "source":"anncsu_comune_indicativo"}
        if valid_pair(rep["lat"],rep["lon"]):
            cache[code]=rep; return rep

    rows=list(istat_con.execute(
        "select nome,lat,lon from localities where pro_com=? and nome_norm=?",(code,norm(comune))
    ))
    if len(rows)==1 and valid_pair(rows[0]["lat"],rows[0]["lon"]):
        rep={"lat":float(rows[0]["lat"]),"lon":float(rows[0]["lon"]),"source":"istat_comune_indicativo"}
        cache[code]=rep; return rep
    cache[code]=None
    return None

def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument("--sample",type=int,default=80)
    args=ap.parse_args(argv)

    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    listings=_all(base,key,"listings",{
        "select":"listing_id,pvp_id,data_asta_date,regione,provincia,comune,indirizzo,latitudine,longitudine,maps_link,geocode_status",
        "latitudine":"is.null","longitudine":"is.null","geocode_status":"is.null","order":"pvp_id.asc"
    })

    ids=[str(x["pvp_id"]) for x in listings]
    locs=[]
    for i in range(0,len(ids),50):
        batch=ids[i:i+50]
        locs.extend(_get(base,key,"listing_locations",{
            "select":"listing_id,pvp_id,is_primary,comune_pvp,codice_istat,latitudine,longitudine,status,source,precision,resolution_reason,resolver_version",
            "pvp_id":"in.("+",".join(batch)+")","is_primary":"eq.true"
        }))
    by_pid={str(x["pvp_id"]):x for x in locs}

    ref=REFERENCE_DIR/"Elenco-comuni-italiani.xlsx"
    comuni_index=load_comuni_index(ref)
    ann_con=open_sqlite(Path(os.environ.get("ANNCSU_SQLITE_INDEX","") or ANNCSU_DIR/"anncsu_runtime.sqlite3"))
    istat_con=open_sqlite(Path(os.environ.get("ISTAT_LOCALITY_INDEX","") or DATA_DIR/"istat"/"istat_localita_2021.sqlite3"))

    counts=Counter(); code_resolution=Counter(); pin_sources=Counter(); plans=[]; cache={}
    for r in listings:
        pid=str(r["pvp_id"]); loc=by_pid.get(pid)
        if loc and str(loc.get("status") or "").upper()=="RESOLVED" and valid_pair(loc.get("latitudine"),loc.get("longitudine")):
            pin={"lat":float(loc["latitudine"]),"lon":float(loc["longitudine"]),"source":str(loc.get("source") or "listing_locations")}
            action="PUBLISH_EXISTING_PRIMARY"
            code_reason="EXISTING_PRIMARY"
        else:
            code=clean((loc or {}).get("codice_istat"))
            code_reason="EXISTING_ISTAT_CODE"
            if not code:
                match,code_reason,cands=resolve_code(r,comuni_index)
                if not match:
                    counts["NO_SAFE_PIN"]+=1; code_resolution[code_reason]+=1
                    plans.append({"pvp_id":pid,"action":"NO_SAFE_PIN","comune":r.get("comune"),"provincia":r.get("provincia"),
                                  "regione":r.get("regione"),"code_resolution":code_reason,
                                  "candidate_count":len(cands)})
                    continue
                code=match["code"]
            code_resolution[code_reason]+=1
            pin=municipality_pin(code,clean(r.get("comune")),ann_con,istat_con,cache)
            if not pin:
                counts["NO_SAFE_PIN"]+=1
                plans.append({"pvp_id":pid,"action":"NO_SAFE_PIN","comune":r.get("comune"),"provincia":r.get("provincia"),
                              "regione":r.get("regione"),"codice_istat":code,"code_resolution":code_reason,
                              "no_pin_reason":"NO_COMUNE_POINT"})
                continue
            action="PUBLISH_COMUNE_FALLBACK"

        counts[action]+=1; pin_sources[pin["source"]]+=1
        plans.append({"pvp_id":pid,"action":action,"comune":r.get("comune"),"provincia":r.get("provincia"),
                      "regione":r.get("regione"),"latitudine":pin["lat"],"longitudine":pin["lon"],
                      "maps_link":maps_link(pin["lat"],pin["lon"]),"source":pin["source"],
                      "precision":"COMUNE" if action=="PUBLISH_COMUNE_FALLBACK" else (loc or {}).get("precision"),
                      "code_resolution":code_reason})

    ann_con.close(); istat_con.close()

    summary={
        "version":VERSION,"mode":"PREVIEW_ZERO_DB_WRITES",
        "historical_missing_listings":len(listings),
        "primary_locations_found":len(locs),
        "publishable":sum(v for k,v in counts.items() if k.startswith("PUBLISH_")),
        "actions":dict(counts),
        "code_resolution":dict(code_resolution),
        "pin_sources":dict(pin_sources),
        "google_calls":0,"db_writes":0,
        "no_safe_pin_cases":[x for x in plans if x["action"]=="NO_SAFE_PIN"],
        "sample":plans[:max(0,args.sample)]
    }
    out=REPORTS_DIR/"historical_missing_pin_recovery_preview_v1.json"
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print("[OUT]",out)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
