#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a compact SQLite index from ISTAT 2021 national point localities.

Reads the official LocalitaPuntuali_21.zip (EPSG:32632) and stores locality names
plus WGS84 representative points. Standard library only.

Default is local/offline if the ZIP already exists. --download fetches the
official ISTAT ZIP if missing.
"""
from __future__ import annotations
import argparse, csv, io, json, math, os, re, sqlite3, struct, time, unicodedata, urllib.request, zipfile
from datetime import datetime
from pathlib import Path

VERSION='istat_locality_index_v1'
OFFICIAL_URL='https://www.istat.it/storage/cartografia/basi_territoriali/2021/LocalitaPuntuali_21.zip'

HERE=Path(__file__).resolve()
SRC=HERE.parents[1]
ROOT=SRC.parent
DATA_DIR=ROOT/'data'/'istat'
DEFAULT_ZIP=DATA_DIR/'LocalitaPuntuali_21.zip'
DEFAULT_DB=DATA_DIR/'istat_localita_2021.sqlite3'
DEFAULT_REPORT=DATA_DIR/'istat_localita_2021.build.json'

def clean(v):
    return '' if v is None else re.sub(r'\s+',' ',str(v).replace('\u00a0',' ')).strip()

def norm(v):
    s=clean(v).replace('’',"'").replace('`',"'")
    s=unicodedata.normalize('NFKD',s)
    s=''.join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    s=re.sub(r'[^a-z0-9]+',' ',s)
    return re.sub(r'\s+',' ',s).strip()

def _decode(b):
    b=b.rstrip(b'\x00 ')
    if not b: return ''
    for enc in ('utf-8','cp1252','latin1'):
        try: return b.decode(enc).strip()
        except UnicodeDecodeError: pass
    return b.decode('latin1','replace').strip()

def read_dbf(data: bytes):
    if len(data)<32: raise RuntimeError('DBF troppo corto')
    nrec=struct.unpack_from('<I',data,4)[0]
    hlen=struct.unpack_from('<H',data,8)[0]
    rlen=struct.unpack_from('<H',data,10)[0]
    fields=[]; off=32
    while off+32 <= hlen and data[off] != 0x0D:
        d=data[off:off+32]
        name=_decode(d[:11]).upper(); typ=chr(d[11]); ln=d[16]; dec=d[17]
        if name: fields.append((name,typ,ln,dec))
        off += 32
    rows=[]; pos=hlen
    for _ in range(nrec):
        rec=data[pos:pos+rlen]; pos += rlen
        if len(rec)<rlen: break
        if rec[:1] == b'*':
            rows.append(None); continue
        x={}; p=1
        for name,typ,ln,dec in fields:
            raw=rec[p:p+ln]; p += ln
            x[name]=_decode(raw)
        rows.append(x)
    return fields, rows

def read_point_shp(data: bytes):
    if len(data)<100: raise RuntimeError('SHP troppo corto')
    main_type=struct.unpack_from('<i',data,32)[0]
    if main_type not in (1,11,21):
        raise RuntimeError(f'SHP non puntuale: shape type {main_type}')
    pts=[]; pos=100
    while pos+8 <= len(data):
        _rec_no, words=struct.unpack_from('>2i',data,pos); pos += 8
        n=words*2; content=data[pos:pos+n]; pos += n
        if len(content)<4: break
        st=struct.unpack_from('<i',content,0)[0]
        if st == 0: pts.append(None); continue
        if st not in (1,11,21) or len(content)<20:
            pts.append(None); continue
        x,y=struct.unpack_from('<2d',content,4)
        pts.append((x,y))
    return main_type, pts

def utm32_to_wgs84(easting, northing):
    # Inverse Transverse Mercator for WGS84 / UTM zone 32N (EPSG:32632).
    a=6378137.0; ecc2=0.0066943799901413165; k0=0.9996
    e1=(1-math.sqrt(1-ecc2))/(1+math.sqrt(1-ecc2))
    x=float(easting)-500000.0; y=float(northing)
    m=y/k0
    mu=m/(a*(1-ecc2/4-3*ecc2**2/64-5*ecc2**3/256))
    j1=3*e1/2-27*e1**3/32
    j2=21*e1**2/16-55*e1**4/32
    j3=151*e1**3/96
    j4=1097*e1**4/512
    fp=mu+j1*math.sin(2*mu)+j2*math.sin(4*mu)+j3*math.sin(6*mu)+j4*math.sin(8*mu)
    eprime2=ecc2/(1-ecc2)
    c1=eprime2*math.cos(fp)**2
    t1=math.tan(fp)**2
    n1=a/math.sqrt(1-ecc2*math.sin(fp)**2)
    r1=a*(1-ecc2)/(1-ecc2*math.sin(fp)**2)**1.5
    d=x/(n1*k0)
    lat=fp-(n1*math.tan(fp)/r1)*(d**2/2-(5+3*t1+10*c1-4*c1**2-9*eprime2)*d**4/24+(61+90*t1+298*c1+45*t1**2-252*eprime2-3*c1**2)*d**6/720)
    lon0=math.radians(9.0)
    lon=lon0+(d-(1+2*t1+c1)*d**3/6+(5-2*c1+28*t1-3*c1**2+8*eprime2+24*t1**2)*d**5/120)/math.cos(fp)
    return math.degrees(lat), math.degrees(lon)

def _pick_member(names, ext):
    c=[n for n in names if n.lower().endswith(ext)]
    if not c: return None
    # Prefer same national locality-point stem.
    c.sort(key=lambda n:(0 if 'localita' in n.casefold() else 1,len(n)))
    return c[0]

def _csv_name_lookup(zf, names):
    # Optional fallback if NOME is not embedded in the point DBF.
    look={}
    for name in names:
        if not name.lower().endswith('.csv'): continue
        raw=zf.read(name)
        text=None
        for enc in ('utf-8-sig','cp1252','latin1'):
            try: text=raw.decode(enc); break
            except UnicodeDecodeError: pass
        if text is None: continue
        sample=text[:65536]
        delim=';' if sample.count(';') >= sample.count(',') else ','
        rd=csv.DictReader(io.StringIO(text),delimiter=delim)
        f={str(x).upper():x for x in (rd.fieldnames or [])}
        if 'NOME' not in f or 'PRO_COM' not in f: continue
        locid=f.get('LOC21_ID'); codloc=f.get('COD_LOC')
        if not locid and not codloc: continue
        for r in rd:
            pro=re.sub(r'\D','',clean(r.get(f['PRO_COM']))).zfill(6)[-6:]
            lid=clean(r.get(locid)) if locid else ''
            cl=clean(r.get(codloc)) if codloc else ''
            nm=clean(r.get(f['NOME']))
            if pro and nm:
                if lid: look[('ID',lid)]=nm
                if cl: look[('PC',pro+'|'+cl)]=nm
    return look

def build(zip_path:Path,out:Path,report:Path):
    t0=time.perf_counter(); out.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names=zf.namelist(); dbf_name=_pick_member(names,'.dbf'); shp_name=_pick_member(names,'.shp')
        if not dbf_name or not shp_name: raise RuntimeError('ZIP ISTAT: .dbf/.shp non trovati')
        fields,dbf_rows=read_dbf(zf.read(dbf_name)); _stype,pts=read_point_shp(zf.read(shp_name))
        fname={x[0] for x in fields}; name_lookup={}
        if 'NOME' not in fname: name_lookup=_csv_name_lookup(zf,names)
    if len(dbf_rows)!=len(pts):
        raise RuntimeError(f'DBF/SHP record count mismatch: {len(dbf_rows)} vs {len(pts)}')
    pro_field='PRO_COM' if 'PRO_COM' in fname else ('COD_ISTAT' if 'COD_ISTAT' in fname else None)
    id_field='LOC21_ID' if 'LOC21_ID' in fname else None
    cod_field='COD_LOC' if 'COD_LOC' in fname else None
    tipo_field='TIPO_LOC' if 'TIPO_LOC' in fname else None
    name_field='NOME' if 'NOME' in fname else None
    if not pro_field or (not name_field and not name_lookup):
        report.write_text(json.dumps({'version':VERSION,'result':'FAIL_SCHEMA','zip':str(zip_path),'dbf_member':dbf_name,'shp_member':shp_name,'fields':[x[0] for x in fields],'members':names},ensure_ascii=False,indent=2),encoding='utf-8')
        raise RuntimeError('Schema ISTAT inatteso: manca PRO_COM/COD_ISTAT o denominazione NOME')
    tmp=out.with_suffix('.tmp.sqlite3')
    if tmp.exists(): tmp.unlink()
    con=sqlite3.connect(tmp)
    con.executescript('''
      PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY;
      CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE localities(
        row_no INTEGER PRIMARY KEY,
        pro_com TEXT NOT NULL,
        loc21_id TEXT NOT NULL,
        cod_loc TEXT NOT NULL,
        tipo_loc TEXT NOT NULL,
        nome TEXT NOT NULL,
        nome_norm TEXT NOT NULL,
        lat REAL NOT NULL,
        lon REAL NOT NULL
      );
    ''')
    ins=[]; skipped=0
    for i,(r,p) in enumerate(zip(dbf_rows,pts),1):
        if r is None or p is None: skipped+=1; continue
        pro=re.sub(r'\D','',clean(r.get(pro_field))).zfill(6)[-6:]
        lid=clean(r.get(id_field)) if id_field else ''
        cod=clean(r.get(cod_field)) if cod_field else ''
        nm=clean(r.get(name_field)) if name_field else ''
        if not nm:
            nm=name_lookup.get(('ID',lid),'') if lid else ''
        if not nm and cod:
            nm=name_lookup.get(('PC',pro+'|'+cod),'')
        if not pro or not nm: skipped+=1; continue
        lat,lon=utm32_to_wgs84(*p)
        if not (35<=lat<=48 and 5<=lon<=20): skipped+=1; continue
        ins.append((i,pro,lid,cod,clean(r.get(tipo_field)) if tipo_field else '',nm,norm(nm),lat,lon))
        if len(ins)>=5000:
            con.executemany('INSERT INTO localities VALUES (?,?,?,?,?,?,?,?,?)',ins); ins.clear()
    if ins: con.executemany('INSERT INTO localities VALUES (?,?,?,?,?,?,?,?,?)',ins)
    con.executescript('CREATE INDEX ix_localities_pro_name ON localities(pro_com,nome_norm); CREATE INDEX ix_localities_pro ON localities(pro_com);')
    count=con.execute('SELECT count(*) FROM localities').fetchone()[0]
    meta={
      'version':VERSION,'built_at':datetime.now().astimezone().isoformat(),'source_url':OFFICIAL_URL,
      'zip_name':zip_path.name,'dbf_member':dbf_name,'shp_member':shp_name,'rows':str(count),
    }
    con.executemany('INSERT INTO meta(key,value) VALUES (?,?)',meta.items()); con.commit(); con.close()
    if out.exists(): out.unlink()
    tmp.replace(out)
    rep={'version':VERSION,'result':'PASS','created_at':datetime.now().astimezone().isoformat(),'source_url':OFFICIAL_URL,'zip':str(zip_path),'db':str(out),'dbf_member':dbf_name,'shp_member':shp_name,'fields':[x[0] for x in fields],'rows_dbf':len(dbf_rows),'rows_shp':len(pts),'rows_indexed':count,'rows_skipped':skipped,'size_bytes':out.stat().st_size,'seconds':round(time.perf_counter()-t0,3)}
    report.write_text(json.dumps(rep,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return rep

def download(path:Path):
    path.parent.mkdir(parents=True,exist_ok=True)
    req=urllib.request.Request(OFFICIAL_URL,headers={'User-Agent':'Mozilla/5.0 AsteEasy/1.0'})
    with urllib.request.urlopen(req,timeout=120) as r, path.open('wb') as f:
        while True:
            b=r.read(1024*1024)
            if not b: break
            f.write(b)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--zip',type=Path,default=DEFAULT_ZIP)
    ap.add_argument('--out',type=Path,default=DEFAULT_DB)
    ap.add_argument('--report',type=Path,default=DEFAULT_REPORT)
    ap.add_argument('--download',action='store_true')
    args=ap.parse_args()
    if not args.zip.exists():
        if not args.download: raise SystemExit(f'ERRORE: manca {args.zip}; usa --download')
        print('[ISTAT LOC] download:',OFFICIAL_URL,flush=True); download(args.zip)
    print('[ISTAT LOC] ZIP:',args.zip,flush=True); print('[ISTAT LOC] OUT:',args.out,flush=True)
    try: rep=build(args.zip,args.out,args.report)
    except Exception as e:
        print('[ISTAT LOC] FAIL:',type(e).__name__,e,flush=True); print('[ISTAT LOC] report:',args.report,flush=True); return 2
    print(f"[ISTAT LOC] PASS rows={rep['rows_indexed']:,} size={rep['size_bytes']/1024/1024:.1f} MiB total={rep['seconds']:.1f}s",flush=True)
    print('[ISTAT LOC] report:',args.report,flush=True); return 0
if __name__=='__main__': raise SystemExit(main())
