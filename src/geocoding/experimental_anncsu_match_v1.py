#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,json,math,os,re,sqlite3,statistics,unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

VERSION="experimental_anncsu_match_v1"
ROAD={"VIA","VIALE","VICO","VICOLO","PIAZZA","PIAZZALE","CORSO","LARGO","STRADA","CONTRADA","LOCALITA","FRAZIONE","BORGO","TRAVERSA","SALITA","ROTONDA","LUNGOMARE"}
WEAK={"DI","DEL","DELLA","DELLE","DEI","DEGLI","DA","DAL","DALLA","DALLE","DE","LA","LE","IL","LO","L","D","SAN","SANTA","SANTO"}|ROAD

def clean(v):
    return re.sub(r"\s+"," ",str(v or "").replace("\xa0"," ")).strip()

def norm(v):
    s=unicodedata.normalize("NFKD",clean(v).replace("’","'"))
    s="".join(ch for ch in s if not unicodedata.combining(ch)).upper()
    return re.sub(r"\s+"," ",re.sub(r"[^A-Z0-9]+"," ",s)).strip()

def toks(v):
    return [x for x in norm(v).split() if x not in WEAK and len(x)>1]

def kind(v):
    n=norm(v)
    if not n:return ""
    x=n.split()[0]
    return x if x in ROAD else ""

def civic_norm(v):
    return re.sub(r"[^A-Z0-9]+","",norm(v))

def hav(a,b,c,d):
    r=6371008.8;p1=math.radians(a);p2=math.radians(c)
    dp=math.radians(c-a);dl=math.radians(d-b)
    z=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(min(1,math.sqrt(z)))

def street_score(src,cand):
    sn,cn=norm(src),norm(cand); st,ct=toks(src),toks(cand)
    ss,cs=set(st),set(ct); sk,ck=kind(src),kind(cand)
    if not sn or not cn:return 0,{"mode":"EMPTY"}
    if sn==cn:return 1,{"mode":"EXACT_FULL"}
    if st and st==ct:
        sc=.985-(.08 if sk and ck and sk!=ck else 0)
        return sc,{"mode":"EXACT_SIGNIFICANT"}
    inter=len(ss&cs); union=len(ss|cs) or 1
    jac=inter/union; covs=inter/max(1,len(ss)); seq=SequenceMatcher(None," ".join(st) or sn," ".join(ct) or cn).ratio()
    single_tail_ok = bool(len(st)==1 and ct and st[0]==ct[-1])
    if ss and covs==1:
        sc=.90+.05*seq+.03*jac; mode="SOURCE_TOKENS_CONTAINED"
    else:
        sc=.58*seq+.42*jac; mode="FUZZY"
    if sk and ck: sc += .025 if sk==ck else -.10
    return max(0,min(.999,sc)),{"mode":mode,"seq":round(seq,4),"jaccard":round(jac,4),"source_coverage":round(covs,4),"source_token_count":len(st),"single_token_tail_ok":single_tail_ok,"kind_source":sk,"kind_candidate":ck}

class DB:
    def __init__(self,path):
        uri="file:"+str(Path(path).resolve()).replace("\\","/")+"?mode=ro"
        self.con=sqlite3.connect(uri,uri=True);self.con.row_factory=sqlite3.Row
        v=self.con.execute("select value from meta where key='version'").fetchone()
        if not v or v[0]!="anncsu_sqlite_index_v1":raise RuntimeError("ANNCSU index non supportato")
        self.sc={};self.ac={}
    def streets(self,code):
        if code not in self.sc:
            out=[];seen=set()
            for r in self.con.execute("select progressivo_nazionale,odonimo,localita,dizione_lingua1,dizione_lingua2 from streets where codice_istat=? order by source_row",(code,)):
                p=clean(r["progressivo_nazionale"]);o=clean(r["odonimo"])
                if not p or not o or p in seen:continue
                seen.add(p);vs=[x for x in [o,clean(r["dizione_lingua1"]),clean(r["dizione_lingua2"])] if x]
                out.append({"progressivo":p,"odonimo":o,"localita":clean(r["localita"]),"variants":vs})
            self.sc[code]=out
        return self.sc[code]
    def accesses(self,p):
        if p not in self.ac:self.ac[p]=list(self.con.execute("select civic_norm,lat,lon,metodo from accesses where progressivo_nazionale=?",(p,)))
        return self.ac[p]
    def close(self):self.con.close()

def match_street(db,code,src,topn=5):
    ranked=[]
    for row in db.streets(code):
        best=max(((*street_score(src,v),v) for v in row["variants"]),key=lambda x:x[0])
        ranked.append({"score":round(best[0],6),"evidence":best[1],"matched_variant":best[2],"progressivo":row["progressivo"],"odonimo":row["odonimo"],"localita":row["localita"]})
    ranked.sort(key=lambda x:x["score"],reverse=True);ranked=ranked[:topn]
    if not ranked:return {"status":"NO_STREETS","candidates":[]}
    b=ranked[0];second=ranked[1]["score"] if len(ranked)>1 else 0;margin=b["score"]-second
    ev=b.get("evidence") or {}
    single_containment = ev.get("mode")=="SOURCE_TOKENS_CONTAINED" and ev.get("source_token_count")==1
    single_safe = (not single_containment) or bool(ev.get("single_token_tail_ok"))
    if b["score"]>=.999:status="MATCH_EXACT"
    elif b["score"]>=.93 and margin>=.06 and single_safe:status="MATCH_ALIAS_HIGH"
    elif b["score"]>=.90 and margin>=.10 and single_safe:status="MATCH_ALIAS_HIGH"
    elif b["score"]>=.84:status="AMBIGUOUS"
    else:status="NO_SAFE_MATCH"
    return {"status":status,"best":b,"second_score":round(second,6),"margin":round(margin,6),"candidates":ranked}

def match_civic(db,prog,civic):
    cn=civic_norm(civic)
    if not cn:return {"status":"NO_CIVIC"}
    hits=[]
    for r in db.accesses(prog):
        if clean(r["civic_norm"]).upper()!=cn:continue
        try:lat,lon=float(r["lat"]),float(r["lon"])
        except:continue
        if 35<=lat<=48.5 and 5<=lon<=20.5:hits.append((lat,lon,clean(r["metodo"])))
    if not hits:return {"status":"CIVIC_NOT_FOUND","civic_norm":cn}
    lat=statistics.median(x[0] for x in hits);lon=statistics.median(x[1] for x in hits)
    spread=max((hav(lat,lon,a,b) for a,b,_ in hits),default=0)
    if spread>500:return {"status":"CIVIC_AMBIGUOUS_COORDS","civic_norm":cn,"hits":len(hits),"max_spread_m":round(spread,1)}
    return {"status":"CIVIC_COORD","civic_norm":cn,"lat":lat,"lon":lon,"hits":len(hits),"max_spread_m":round(spread,1),"methods":dict(Counter(x[2] for x in hits))}

def process(x,db):
    p=x.get("primary") or {};code=clean(p.get("codice_istat"));street=clean(p.get("street"))
    out={"pvp_id":x.get("pvp_id"),"comune":p.get("comune"),"codice_istat":code,"raw_via":p.get("raw_via"),"street_pvp":street,"civic_pvp":p.get("civic_for_geocode")}
    if not code or not street:out["anncsu"]={"status":"SKIP_MISSING_CODE_OR_STREET"};return out
    m=match_street(db,code,street)
    if m["status"] in {"MATCH_EXACT","MATCH_ALIAS_HIGH"}:m["civic"]=match_civic(db,m["best"]["progressivo"],p.get("civic_for_geocode"))
    out["anncsu"]=m;return out

def flat(x):
    a=x.get("anncsu") or {};b=a.get("best") or {};c=a.get("civic") or {}
    return {"pvp_id":x.get("pvp_id"),"comune":x.get("comune"),"codice_istat":x.get("codice_istat"),"raw_via":x.get("raw_via"),"street_pvp":x.get("street_pvp"),"civic_pvp":x.get("civic_pvp"),"anncsu_status":a.get("status"),"anncsu_odonimo":b.get("odonimo"),"anncsu_localita":b.get("localita"),"anncsu_score":b.get("score"),"anncsu_margin":a.get("margin"),"anncsu_match_mode":(b.get("evidence") or {}).get("mode"),"anncsu_civic_status":c.get("status"),"anncsu_lat":c.get("lat"),"anncsu_lon":c.get("lon"),"anncsu_civic_hits":c.get("hits"),"anncsu_civic_spread_m":c.get("max_spread_m")}

def main():
    ap=argparse.ArgumentParser();ap.add_argument("resolver_jsonl",type=Path)
    ap.add_argument("--anncsu",type=Path,default=Path(os.environ.get("ANNCSU_SQLITE_INDEX","/app/data/anncsu/anncsu_runtime.sqlite3")))
    ap.add_argument("--out-dir",type=Path,default=Path("runtime/reports"));ap.add_argument("--limit",type=int,default=0);args=ap.parse_args()
    db=DB(args.anncsu);results=[]
    try:
        for line in args.resolver_jsonl.open(encoding="utf-8"):
            if not line.strip():continue
            results.append(process(json.loads(line),db))
            if args.limit and len(results)>=args.limit:break
    finally:db.close()
    args.out_dir.mkdir(parents=True,exist_ok=True)
    jp=args.out_dir/"experimental_anncsu_match_v1.jsonl";cp=args.out_dir/"experimental_anncsu_match_v1.csv";sp=args.out_dir/"experimental_anncsu_match_v1_stats.json"
    with jp.open("w",encoding="utf-8") as f:
        for x in results:f.write(json.dumps(x,ensure_ascii=False)+"\n")
    fs=[flat(x) for x in results]
    with cp.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(fs[0]));w.writeheader();w.writerows(fs)
    stats={"version":VERSION,"rows":len(results),"anncsu_status":dict(Counter((x.get("anncsu") or {}).get("status") for x in results)),"civic_status":dict(Counter(((x.get("anncsu") or {}).get("civic") or {}).get("status","NOT_ATTEMPTED") for x in results))}
    sp.write_text(json.dumps(stats,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(stats,ensure_ascii=False,indent=2));print("[OUT]",jp);print("[OUT]",cp);print("[OUT]",sp)
if __name__=="__main__":main()
