#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,json,math,os,re,sqlite3,unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

VERSION="experimental_locality_match_v1"
LOCALITY_RE=re.compile(r"(?i)\b(?:localit[aà]|loc\.?|frazione(?:\s+di)?|fraz\.?|borgata|contrada|c\s*/?\s*da|c\.\s*da|zona|regione|podere)\s+")
ROAD_RE=re.compile(r"(?i)\b(?:via|viale|corso|piazza|piazzale|strada|vicolo|vico|largo|traversa|sentiero|rotonda|salita|lungomare|s\.?p\.?|s\.?s\.?|s\.?r\.?)\b")
STOP_RE=re.compile(r"(?i)\b(?:podere|poderi|civ(?:ico)?\.?|n\.?|numero|km|snc|interno|int\.?|scala|piano|p\.)\b")

def clean(v):
    return "" if v is None else re.sub(r"\s+"," ",str(v).replace("\xa0"," ")).strip()

def norm(v):
    s=unicodedata.normalize("NFKD",clean(v).replace("’","'"))
    s="".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    return re.sub(r"\s+"," ",re.sub(r"[^a-z0-9]+"," ",s)).strip()

def fnum(v):
    try:x=float(v)
    except (TypeError,ValueError):return None
    return x if math.isfinite(x) else None

def hav(a,b,c,d):
    if None in (a,b,c,d):return None
    r=6371008.8;p1,p2=math.radians(a),math.radians(c)
    dp,dl=math.radians(c-a),math.radians(d-b)
    z=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(min(1.0,math.sqrt(z)))

def explicit_hint(address):
    raw=clean(address);m=LOCALITY_RE.search(raw)
    if not m:return ""
    tail=raw[m.end():];stop=len(tail)
    for sep in (",",";"):
        i=tail.find(sep)
        if i>=0:stop=min(stop,i)
    rm=ROAD_RE.search(tail)
    if rm:stop=min(stop,rm.start())
    sm=STOP_RE.search(tail)
    if sm:stop=min(stop,sm.start())
    h=clean(tail[:stop]).strip(" -/,.;")
    dm=re.search(r"\s+[–—-]\s+",h)
    if dm:h=clean(h[:dm.start()]).strip(" -/,.;")
    return h

def locality_only_hint(street):
    s=clean(street)
    if not s or ROAD_RE.search(s) or re.search(r"\d",s):return ""
    n=norm(s)
    if not n or n in {"snc","s n c"}:return ""
    return s

class DB:
    def __init__(self,path):
        uri="file:"+str(Path(path).resolve()).replace("\\","/")+"?mode=ro"
        self.con=sqlite3.connect(uri,uri=True);self.con.row_factory=sqlite3.Row
        v=self.con.execute("select value from meta where key='version'").fetchone()
        if not v or v[0]!="istat_locality_index_v1":raise RuntimeError("ISTAT locality index non supportato")
        self.cache={}
    def rows(self,code):
        if code not in self.cache:
            self.cache[code]=list(self.con.execute("select loc21_id,tipo_loc,nome,nome_norm,lat,lon from localities where pro_com=?",(code,)))
        return self.cache[code]
    def close(self):self.con.close()

def match_locality(db,code,hint):
    nh=norm(hint)
    if not code or not nh:return {"status":"NO_HINT"}
    rows=db.rows(code)
    if not rows:return {"status":"NO_LOCALITIES_IN_COMUNE"}
    exact=[r for r in rows if clean(r["nome_norm"])==nh]
    if len(exact)==1:
        r=exact[0]
        return {"status":"EXACT_UNIQUE","score":1.0,"margin":1.0,"loc21_id":r["loc21_id"],"tipo_loc":r["tipo_loc"],"nome":r["nome"],"lat":r["lat"],"lon":r["lon"]}
    if len(exact)>1:return {"status":"EXACT_AMBIGUOUS","score":1.0,"margin":0.0,"matches":len(exact),"best_name":exact[0]["nome"]}
    ranked=sorted(((SequenceMatcher(None,nh,clean(r["nome_norm"])).ratio(),r) for r in rows),key=lambda x:x[0],reverse=True)
    bs,b=ranked[0];ss=ranked[1][0] if len(ranked)>1 else 0.0
    return {"status":"FUZZY_DIAGNOSTIC","score":round(bs,4),"margin":round(bs-ss,4),"best_name":b["nome"],"best_tipo_loc":b["tipo_loc"],"best_lat":b["lat"],"best_lon":b["lon"]}

def process(obj,db):
    p=obj.get("primary") or {};raw=clean(p.get("raw_via"));street=clean(p.get("street"));code=clean(p.get("codice_istat"))
    ex=explicit_hint(raw);imp=locality_only_hint(street) if not ex else "";hint=ex or imp
    src="EXPLICIT_MARKER" if ex else ("LOCALITY_ONLY_ADDRESS" if imp else "NONE")
    out={"pvp_id":obj.get("pvp_id"),"comune":p.get("comune"),"codice_istat":code,"raw_via":raw,"street":street,"locality_hint":hint,"hint_source":src}
    if not hint:out["match"]={"status":"NO_HINT"};return out
    m=match_locality(db,code,hint)
    plat,plon=fnum(p.get("lat_pvp")),fnum(p.get("lon_pvp"));lat,lon=fnum(m.get("lat")),fnum(m.get("lon"))
    if m.get("status")=="EXACT_UNIQUE" and None not in (plat,plon,lat,lon):m["distance_pvp_to_locality_m"]=round(hav(plat,plon,lat,lon),1)
    out["match"]=m;return out

def flat(x):
    m=x.get("match") or {}
    return {"pvp_id":x.get("pvp_id"),"comune":x.get("comune"),"codice_istat":x.get("codice_istat"),"raw_via":x.get("raw_via"),"street":x.get("street"),"locality_hint":x.get("locality_hint"),"hint_source":x.get("hint_source"),"locality_status":m.get("status"),"istat_name":m.get("nome") or m.get("best_name"),"istat_tipo_loc":m.get("tipo_loc") or m.get("best_tipo_loc"),"istat_score":m.get("score"),"istat_margin":m.get("margin"),"istat_lat":m.get("lat") or m.get("best_lat"),"istat_lon":m.get("lon") or m.get("best_lon"),"distance_pvp_to_locality_m":m.get("distance_pvp_to_locality_m")}

def main():
    ap=argparse.ArgumentParser();ap.add_argument("resolver_jsonl",type=Path)
    ap.add_argument("--istat",type=Path,default=Path(os.environ.get("ISTAT_LOCALITY_INDEX","/app/data/istat/istat_localita_2021.sqlite3")))
    ap.add_argument("--out-dir",type=Path,default=Path("runtime/reports"));ap.add_argument("--limit",type=int,default=0);args=ap.parse_args()
    db=DB(args.istat);results=[]
    try:
        for line in args.resolver_jsonl.open(encoding="utf-8"):
            if not line.strip():continue
            results.append(process(json.loads(line),db))
            if args.limit and len(results)>=args.limit:break
    finally:db.close()
    args.out_dir.mkdir(parents=True,exist_ok=True)
    jp=args.out_dir/"experimental_locality_match_v1.jsonl";cp=args.out_dir/"experimental_locality_match_v1.csv";sp=args.out_dir/"experimental_locality_match_v1_stats.json"
    with jp.open("w",encoding="utf-8") as f:
        for x in results:f.write(json.dumps(x,ensure_ascii=False)+"\n")
    rows=[flat(x) for x in results]
    with cp.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    stats={"version":VERSION,"rows":len(results),"hint_source":dict(Counter(x.get("hint_source") for x in results)),"locality_status":dict(Counter((x.get("match") or {}).get("status") for x in results))}
    sp.write_text(json.dumps(stats,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(stats,ensure_ascii=False,indent=2));print("[OUT]",jp);print("[OUT]",cp);print("[OUT]",sp)
if __name__=="__main__":main()
