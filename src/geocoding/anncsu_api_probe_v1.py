#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ANNCSU Open Data API availability probe. Read-only, no project writes."""
from __future__ import annotations
import argparse, json, urllib.parse, urllib.request

URL = "https://anncsu.open.agenziaentrate.gov.it/age-inspire/opendata/anncsu/querydata.php"

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--resource", default="odonimi")
    ap.add_argument("--codice-comune", default="G273")
    ap.add_argument("--denominazione", default="ROMA")
    ap.add_argument("--timeout", type=float, default=12.0)
    a=ap.parse_args()
    params={"resource":a.resource,"codicecomune":a.codice_comune,"denominazione":a.denominazione}
    url=URL+"?"+urllib.parse.urlencode(params)
    req=urllib.request.Request(url,headers={"User-Agent":"AsteEasy-ANNCSU-Probe/1.0","Accept":"application/json,text/plain,*/*"})
    try:
        with urllib.request.urlopen(req,timeout=a.timeout) as r:
            body=r.read(1024*1024).decode("utf-8",errors="replace")
            ctype=r.headers.get("Content-Type","")
            status=getattr(r,"status",200)
    except Exception as e:
        print(json.dumps({"result":"UNAVAILABLE","error":type(e).__name__,"detail":str(e)},ensure_ascii=False,indent=2))
        return 2
    low=body.lower()
    if "open data in manutenzione" in low or "servizio temporaneamente non disponibile" in low:
        result={"result":"MAINTENANCE","http_status":status,"content_type":ctype}
    else:
        try:
            data=json.loads(body)
            result={"result":"JSON_OK","http_status":status,"content_type":ctype,"json_type":type(data).__name__,"sample":data}
        except Exception:
            result={"result":"NON_JSON","http_status":status,"content_type":ctype,"sample":body[:1200]}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result["result"]=="JSON_OK" else 1

if __name__=="__main__":
    raise SystemExit(main())
