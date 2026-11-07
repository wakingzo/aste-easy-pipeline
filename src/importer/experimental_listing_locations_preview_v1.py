#!/usr/bin/env python3
from __future__ import annotations

import argparse,csv,json
from collections import Counter
from pathlib import Path

VERSION="experimental_listing_locations_preview_v1"

def clean(v):
    if v is None:return None
    s=" ".join(str(v).replace("\xa0"," ").split()).strip()
    return s or None

def load_jsonl(path):
    out={}
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():continue
            x=json.loads(line)
            out[str(x.get("pvp_id") or "")]=x
    return out

def valid_pair(lat,lon):
    if lat is None and lon is None:return True
    if lat is None or lon is None:return False
    try:
        la,lo=float(lat),float(lon)
    except Exception:
        return False
    return -90<=la<=90 and -180<=lo<=180

def build_rows(resolver,unified):
    rows=[]
    problems=[]
    for pid,obj in resolver.items():
        primary_idx=obj.get("primary_bene_index")
        uni=(unified.get(pid) or {})
        final=(uni.get("final") or {})
        warnings=list(uni.get("warnings") or [])
        for bene in obj.get("beni") or []:
            idx=bene.get("bene_index")
            is_primary=(idx==primary_idx)
            row={
                "pvp_id":pid,
                "bene_index":idx,
                "is_primary":is_primary,

                "address_raw":clean(bene.get("raw_via")),
                "comune_pvp":clean(bene.get("comune")),
                "provincia_pvp":clean(bene.get("provincia")),
                "sigla_provincia_pvp":clean(bene.get("provincia")),
                "cap_pvp":clean(bene.get("cap")),
                "codice_istat":clean(bene.get("codice_istat")),

                "pvp_latitudine":bene.get("lat_pvp") if bene.get("coord_valid") else None,
                "pvp_longitudine":bene.get("lon_pvp") if bene.get("coord_valid") else None,

                "street":clean(bene.get("street")),
                "civic":clean(bene.get("civic_for_geocode")),
                "internal":clean(bene.get("internal")),
                "locality":None,
                "snc":bool(bene.get("snc")),
                "is_intersection":bool(bene.get("is_intersection")),

                "latitudine":None,
                "longitudine":None,
                "source":None,
                "precision":None,
                "confidence":None,
                "status":"PENDING",
                "formatted_address":None,

                "anncsu_status":None,
                "google_status":None,
                "warnings":[],
                "resolution_reason":None,
                "resolver_version":VERSION,
            }

            # Current v1 downstream matching resolves only the representative bene.
            # Preserve that fact explicitly instead of pretending all beni were resolved.
            if is_primary and final:
                row.update({
                    "latitudine":final.get("lat"),
                    "longitudine":final.get("lon"),
                    "source":clean(final.get("source")),
                    "precision":clean(final.get("precision")),
                    "confidence":clean(final.get("confidence")),
                    "status":clean(final.get("status")) or "PENDING",
                    "warnings":warnings,
                    "resolution_reason":clean(final.get("reason")),
                    "anncsu_status":clean(((uni.get("evidence") or {}).get("anncsu_status"))),
                    "resolver_version":"experimental_unified_offline_v1",
                })

            if not valid_pair(row["pvp_latitudine"],row["pvp_longitudine"]):
                problems.append({"pvp_id":pid,"bene_index":idx,"problem":"INVALID_PVP_COORD_PAIR"})
            if not valid_pair(row["latitudine"],row["longitudine"]):
                problems.append({"pvp_id":pid,"bene_index":idx,"problem":"INVALID_FINAL_COORD_PAIR"})
            rows.append(row)

    return rows,problems

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("resolver_jsonl",type=Path)
    ap.add_argument("unified_jsonl",type=Path)
    ap.add_argument("--out-dir",type=Path,default=Path("runtime/reports"))
    args=ap.parse_args()

    resolver=load_jsonl(args.resolver_jsonl)
    unified=load_jsonl(args.unified_jsonl)
    rows,problems=build_rows(resolver,unified)

    keys=[(r["pvp_id"],r["bene_index"]) for r in rows]
    dup_count=len(keys)-len(set(keys))

    by_listing=Counter(r["pvp_id"] for r in rows)
    primaries=Counter(r["pvp_id"] for r in rows if r["is_primary"])
    missing_primary=[pid for pid in resolver if primaries[pid]!=1 and (resolver[pid].get("beni") or [])]

    stats={
        "version":VERSION,
        "listings":len(resolver),
        "location_rows":len(rows),
        "listings_with_locations":len(by_listing),
        "single_location_listings":sum(n==1 for n in by_listing.values()),
        "multi_location_listings":sum(n>1 for n in by_listing.values()),
        "duplicate_listing_bene_keys":dup_count,
        "listings_without_exactly_one_primary":len(missing_primary),
        "primary_rows":sum(r["is_primary"] for r in rows),
        "rows_with_valid_pvp_coords":sum(r["pvp_latitudine"] is not None for r in rows),
        "rows_with_final_coords":sum(r["latitudine"] is not None for r in rows),
        "status":dict(Counter(r["status"] for r in rows)),
        "source":dict(Counter(r["source"] or "NONE" for r in rows)),
        "precision":dict(Counter(r["precision"] or "NONE" for r in rows)),
        "problems":dict(Counter(x["problem"] for x in problems)),
    }

    args.out_dir.mkdir(parents=True,exist_ok=True)
    jp=args.out_dir/"experimental_listing_locations_preview_v1.jsonl"
    cp=args.out_dir/"experimental_listing_locations_preview_v1.csv"
    sp=args.out_dir/"experimental_listing_locations_preview_v1_stats.json"
    pp=args.out_dir/"experimental_listing_locations_preview_v1_problems.json"

    with jp.open("w",encoding="utf-8") as f:
        for r in rows:f.write(json.dumps(r,ensure_ascii=False)+"\n")

    if rows:
        # warnings are serialized for CSV readability only.
        csv_rows=[]
        for r in rows:
            x=dict(r)
            x["warnings"]="|".join(x.get("warnings") or [])
            csv_rows.append(x)
        with cp.open("w",encoding="utf-8-sig",newline="") as f:
            w=csv.DictWriter(f,fieldnames=list(csv_rows[0]))
            w.writeheader();w.writerows(csv_rows)

    sp.write_text(json.dumps(stats,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    pp.write_text(json.dumps(problems,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    print(json.dumps(stats,ensure_ascii=False,indent=2))
    print("[OUT]",jp);print("[OUT]",cp);print("[OUT]",sp);print("[OUT]",pp)

if __name__=="__main__":
    main()
