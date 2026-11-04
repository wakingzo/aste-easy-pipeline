#!/usr/bin/env python3
"""Experimental offline resolver for PVP RAW addresses.

Rules:
- sale-place address is ignored;
- bene addresses are authoritative;
- no Supabase writes, no Google calls, no ANNCSU calls;
- output is diagnostic only.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SNC_RE = re.compile(r"\b(?:S\.?\s*N\.?\s*C\.?|SENZA\s+NUMERO\s+CIVICO)\b", re.I)
INTERNAL_RE = re.compile(r"(?:[,;\s-]+)(?:INT(?:ERNO)?\.?|SCALA|PIANO)\s*[:.]?\s*([A-Z0-9/-]+)\b", re.I)
HOUSE_RE = re.compile(
    r"(?P<num>\d{1,5}(?:\s*[/\-]\s*\d{1,5})?(?:\s*(?:BIS|TER|QUATER|[A-Z]))?)"
    r"(?=\s*(?:[,;.\-]|$|\bINT\b|\bINTERNO\b|\bSCALA\b|\bPIANO\b))",
    re.I,
)
ANGLE_SPLIT_RE = re.compile(r"\s*(?:,?\s*(?:ANG(?:OLO)?\.?|ANGOLO|INCROCIO|ALL['’]ANGOLO\s+CON)\s*)", re.I)
SPACE_RE = re.compile(r"\s+")
MULTI_CIVIC_RE = re.compile(r"^\s*(\d+)\s*(?:/|-)\s*(\d+)(.*)$", re.I)

WEAK_TOKENS = {
    "DI","DEL","DELLA","DELLE","DEI","DEGLI","DA","DAL","DALLA","DE",
    "LA","LE","IL","LO","L","D","VIA","VIALE","VICOLO","PIAZZA",
    "PIAZZALE","CORSO","LARGO","STRADA","CONTRADA","LOCALITA","LOCALITÀ",
    "REGIONE","FRAZIONE","BORGO","TRAVERSA",
}


def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = SPACE_RE.sub(" ", str(value).replace("\xa0", " ").strip())
    return s or None


def norm_for_compare(value: Optional[str]) -> str:
    if not value:
        return ""
    s = "".join(
        ch for ch in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(ch)
    ).upper()
    return SPACE_RE.sub(" ", re.sub(r"[^A-Z0-9]+", " ", s)).strip()


def meaningful_tokens(value: Optional[str]) -> List[str]:
    return [t for t in norm_for_compare(value).split() if t not in WEAK_TOKENS and len(t) > 1]


def valid_coord(lat: Any, lon: Any) -> Tuple[bool, str]:
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return False, "MISSING"
    if la == 0 and lo == 0:
        return False, "ZERO_ZERO"
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        return False, "OUT_OF_RANGE"
    if not (35.0 <= la <= 48.5 and 5.0 <= lo <= 20.5):
        return False, "OUTSIDE_ITALY_BBOX"
    return True, "VALID_SYNTAX"


def first_civic_component(civic: Optional[str]) -> Optional[str]:
    if not civic:
        return None
    x = civic.strip().upper()
    m = MULTI_CIVIC_RE.match(x)
    return m.group(1) if m else x


def strip_internal(s: str) -> Tuple[str, Optional[str]]:
    m = INTERNAL_RE.search(s)
    if not m:
        return s, None
    internal = clean_text(m.group(1))
    s = (s[:m.start()] + " " + s[m.end():]).strip(" ,;-")
    return clean_text(s) or "", internal


def parse_single_street(raw: str) -> Dict[str, Any]:
    original = clean_text(raw) or ""
    work, internal = strip_internal(original)
    snc = bool(SNC_RE.search(work))
    work = SNC_RE.sub(" ", work).strip(" ,;-")
    work = re.sub(r"\bN(?:UM(?:ERO)?)?\.?\s*", "", work, flags=re.I)

    street, civic = work, None
    matches = list(HOUSE_RE.finditer(work))
    if matches:
        m = matches[-1]
        tail = work[m.end():].strip(" ,;.-")
        if len(tail) <= 12:
            civic = clean_text(m.group("num"))
            street = (work[:m.start()] + " " + work[m.end():]).strip(" ,;-")

    return {
        "raw": original,
        "street": clean_text(street),
        "civic_raw": civic.upper() if civic else None,
        "civic_for_geocode": first_civic_component(civic),
        "internal": internal,
        "snc": snc,
    }


def parse_address(via_raw: Any) -> Dict[str, Any]:
    raw = clean_text(via_raw) or ""
    pieces = [clean_text(x) for x in ANGLE_SPLIT_RE.split(raw) if clean_text(x)]
    is_intersection = len(pieces) >= 2
    return {
        "raw": raw,
        "is_intersection": is_intersection,
        "primary": parse_single_street(pieces[0] if pieces else raw),
        "secondary": parse_single_street(pieces[1]) if is_intersection else None,
    }


def query_variants(parsed: Dict[str, Any], comune: Optional[str], provincia: Optional[str]) -> List[str]:
    suffix = ", ".join(x for x in [clean_text(comune), clean_text(provincia), "Italia"] if x)
    out: List[str] = []

    def add(obj: Optional[Dict[str, Any]]) -> None:
        if not obj or not obj.get("street"):
            return
        street = obj["street"]
        civic = obj.get("civic_for_geocode")
        full = " ".join(x for x in [street, civic] if x)
        out.append(f"{full}, {suffix}" if suffix else full)
        if civic:
            out.append(f"{street}, {suffix}" if suffix else street)

    add(parsed.get("primary"))
    if parsed.get("is_intersection"):
        add(parsed.get("secondary"))

    seen, final = set(), []
    for q in out:
        k = norm_for_compare(q)
        if k and k not in seen:
            seen.add(k)
            final.append(q)
    return final


def canonical_address_key(addr: Dict[str, Any]) -> str:
    p = parse_address(addr.get("via"))["primary"]
    return "|".join([
        norm_for_compare(p.get("street")),
        norm_for_compare(p.get("civic_for_geocode")),
        norm_for_compare(addr.get("descComune")),
        norm_for_compare(addr.get("codProvincia") or addr.get("descProvincia")),
    ])


def iter_beni(row: Dict[str, Any]) -> Iterable[Tuple[int, Dict[str, Any], Dict[str, Any]]]:
    detail = row.get("detail_body") or {}
    beni = detail.get("beni") or (detail.get("lotto") or {}).get("beni") or []
    for idx, bene in enumerate(beni):
        if not isinstance(bene, dict):
            continue
        addr = bene.get("indirizzo") or {}
        if isinstance(addr, dict):
            yield idx, bene, addr


def choose_primary_bene(records: List[Dict[str, Any]]) -> Optional[int]:
    if not records:
        return None

    def score(r: Dict[str, Any]) -> Tuple[int, int, int, int]:
        return (
            int(bool(r["street"])),
            int(bool(r["comune"])),
            int(bool(r["civic_for_geocode"])),
            int(bool(r["coord_valid"])),
        )

    return max(range(len(records)), key=lambda i: (score(records[i]), -records[i]["bene_index"]))


def process_row(row: Dict[str, Any]) -> Dict[str, Any]:
    records = []
    for idx, bene, addr in iter_beni(row):
        parsed = parse_address(addr.get("via"))
        p = parsed["primary"]
        coords = addr.get("coordinate") or {}
        lat, lon = coords.get("latitudine"), coords.get("longitudine")
        ok, coord_status = valid_coord(lat, lon)
        comune = clean_text(addr.get("descComune"))
        provincia = clean_text(addr.get("codProvincia") or addr.get("descProvincia"))

        records.append({
            "bene_index": idx,
            "tipologia": clean_text(bene.get("descTipologiaBene") or bene.get("codTipologiaBene")),
            "raw_via": clean_text(addr.get("via")),
            "street": p.get("street"),
            "civic_raw": p.get("civic_raw"),
            "civic_for_geocode": p.get("civic_for_geocode"),
            "internal": p.get("internal"),
            "snc": bool(p.get("snc")),
            "is_intersection": bool(parsed.get("is_intersection")),
            "intersection_secondary": (parsed.get("secondary") or {}).get("street"),
            "cap": clean_text(addr.get("cap")),
            "comune": comune,
            "provincia": provincia,
            "regione": clean_text(addr.get("descRegione")),
            "lat_pvp": lat,
            "lon_pvp": lon,
            "coord_valid": ok,
            "coord_status": coord_status,
            "queries": query_variants(parsed, comune, provincia),
            "address_key": canonical_address_key(addr),
        })

    addresses = list(dict.fromkeys(r["address_key"] for r in records if r["address_key"]))
    comuni = list(dict.fromkeys(norm_for_compare(r["comune"]) for r in records if r["comune"]))
    pos = choose_primary_bene(records)
    primary = records[pos] if pos is not None else None

    return {
        "pvp_id": str(row.get("pvp_id") or ""),
        "link_pvp": row.get("link_pvp"),
        "beni_count": len(records),
        "unique_address_count": len(addresses),
        "multi_address": len(addresses) > 1,
        "unique_comune_count": len(comuni),
        "multi_comune": len(comuni) > 1,
        "primary_bene_index": primary["bene_index"] if primary else None,
        "primary": primary,
        "beni": records,
    }


def flatten(obj: Dict[str, Any]) -> Dict[str, Any]:
    p = obj.get("primary") or {}
    qs = p.get("queries") or []
    return {
        "pvp_id": obj.get("pvp_id"),
        "beni_count": obj.get("beni_count"),
        "unique_address_count": obj.get("unique_address_count"),
        "multi_address": obj.get("multi_address"),
        "unique_comune_count": obj.get("unique_comune_count"),
        "multi_comune": obj.get("multi_comune"),
        "primary_bene_index": obj.get("primary_bene_index"),
        "raw_via": p.get("raw_via"),
        "street": p.get("street"),
        "civic_raw": p.get("civic_raw"),
        "civic_for_geocode": p.get("civic_for_geocode"),
        "internal": p.get("internal"),
        "snc": p.get("snc"),
        "is_intersection": p.get("is_intersection"),
        "intersection_secondary": p.get("intersection_secondary"),
        "comune": p.get("comune"),
        "provincia": p.get("provincia"),
        "lat_pvp": p.get("lat_pvp"),
        "lon_pvp": p.get("lon_pvp"),
        "coord_valid": p.get("coord_valid"),
        "coord_status": p.get("coord_status"),
        "query_1": qs[0] if qs else None,
        "query_2": qs[1] if len(qs) > 1 else None,
        "link_pvp": obj.get("link_pvp"),
    }


def write_outputs(processed: List[Dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "experimental_address_resolver_v1.jsonl"
    csv_path = out_dir / "experimental_address_resolver_v1.csv"
    stats_path = out_dir / "experimental_address_resolver_v1_stats.json"

    with jsonl_path.open("w", encoding="utf-8") as f:
        for obj in processed:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    flat = [flatten(x) for x in processed]
    if flat:
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(flat[0].keys()))
            w.writeheader()
            w.writerows(flat)

    stats = {
        "version": "experimental_address_resolver_v1",
        "rows": len(processed),
        "beni": sum(x["beni_count"] for x in processed),
        "single_bene": sum(x["beni_count"] == 1 for x in processed),
        "multi_bene": sum(x["beni_count"] > 1 for x in processed),
        "multi_address": sum(bool(x["multi_address"]) for x in processed),
        "multi_comune": sum(bool(x["multi_comune"]) for x in processed),
        "primary_coord_valid": sum(bool((x.get("primary") or {}).get("coord_valid")) for x in processed),
        "primary_snc": sum(bool((x.get("primary") or {}).get("snc")) for x in processed),
        "primary_intersection": sum(bool((x.get("primary") or {}).get("is_intersection")) for x in processed),
        "coord_status": dict(Counter((x.get("primary") or {}).get("coord_status", "NO_PRIMARY") for x in processed)),
    }
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"[OUT] {jsonl_path}")
    print(f"[OUT] {csv_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("raw", type=Path, help="Path to listings_api_raw.jsonl")
    ap.add_argument("--out-dir", type=Path, default=Path("runtime/reports"))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    processed = []
    with args.raw.open("r", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception as e:
                raise SystemExit(f"Invalid JSON at line {line_no}: {e}")
            processed.append(process_row(row))
            if args.limit and len(processed) >= args.limit:
                break

    write_outputs(processed, args.out_dir)


if __name__ == "__main__":
    main()
