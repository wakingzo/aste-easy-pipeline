#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy - ANNCSU runtime adapter for consolidated geocoder v2.

Reuses the exact v2.4/v10 local logic validated during the legacy repair, but
exposes it as a callable runtime service for a small set of touched listings.
No Google calls and no Supabase writes.
"""
from __future__ import annotations

import importlib.util
import math
import os
import re
import sys
import statistics
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SRC_ROOT = BASE_DIR.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from aste_paths import REFERENCE_DIR, ANNCSU_DIR

AUDIT_FILE = BASE_DIR / "audit_db_apifree_anncsu_v2_4.py"
ANN_FILE = BASE_DIR / "anncsu_local_engine_v10.py"
GOOGLE_CORE_FILE = BASE_DIR / "google_geocode_core_v9_3_6.py"
SQLITE_RUNTIME_FILE = BASE_DIR / "anncsu_sqlite_runtime_v1.py"
SQLITE_INDEX_FILE = Path(os.environ.get("ANNCSU_SQLITE_INDEX", "") or (ANNCSU_DIR / "anncsu_runtime.sqlite3"))

ALIASES = {
    "molvena": "colceresa",
    "vermezzo": "vermezzo con zelo",
    "corigliano calabro": "corigliano rossano",
    "popoli": "popoli terme",
    "lusiana": "lusiana conco",
    "rossano": "corigliano rossano",
    "gambugliano": "sovizzo",
    "piadena": "piadena drizzona",
}


def _load(path: Path, name: str):
    if not path.exists():
        raise SystemExit(f"ERRORE: manca {path.name} nella cartella del progetto")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def clean(v):
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v).replace("\u00a0", " ")).strip()


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _percentile(vals, p):
    vals = sorted(float(x) for x in vals)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * p
    lo = math.floor(k); hi = math.ceil(k)
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _street_representative(ann, arr):
    vals = []
    seen = set()
    for civic, lat, lon, method in arr or []:
        lat = _f(lat); lon = _f(lon)
        if lat is None or lon is None:
            continue
        key = (round(lat, 7), round(lon, 7))
        if key in seen:
            continue
        seen.add(key)
        vals.append({"lat": lat, "lon": lon, "civic": clean(civic), "method": clean(method)})
    if not vals:
        return None
    med_lat = statistics.median(x["lat"] for x in vals)
    med_lon = statistics.median(x["lon"] for x in vals)
    rep = min(vals, key=lambda x: ann.haversine_m(med_lat, med_lon, x["lat"], x["lon"]))
    ds = [ann.haversine_m(rep["lat"], rep["lon"], x["lat"], x["lon"]) for x in vals]
    methods = Counter(x["method"] or "<EMPTY>" for x in vals)
    return {
        "lat": rep["lat"], "lon": rep["lon"],
        "representative_access_civic": rep["civic"],
        "representative_access_method": rep["method"],
        "unique_coord_count": len(vals),
        "p50_from_rep_m": round(_percentile(ds, .50), 2),
        "p90_from_rep_m": round(_percentile(ds, .90), 2),
        "max_from_rep_m": round(max(ds), 2),
        "method_counts": "|".join(f"{k}:{v}" for k, v in methods.most_common()),
    }


def _pick_rep_for_selected_street(ann, street_cands, ev, accesses):
    selected = ann.norm_name(clean(ev.get("anncsu_odonimo")))
    if not selected:
        return None, "NO_SELECTED_ODONIMO", ""
    exact = []
    for sc, prog, odo, loc in street_cands or []:
        if ann.norm_name(odo) == selected:
            exact.append((sc, prog, odo, loc))
    # Dedup by progressivo; if multiple different progressivi remain, do not guess.
    by_prog = {}
    for x in exact:
        by_prog[clean(x[1])] = x
    exact = list(by_prog.values())
    if len(exact) != 1:
        return None, f"SELECTED_ODONIMO_PROGRESSIVI_{len(exact)}", ""
    prog = clean(exact[0][1])
    return _street_representative(ann, accesses.get(prog, [])), "UNIQUE", prog



def _nearest_high_accuracy_access(ann, arr, lat, lon):
    """Closest distinct ANNCSU access with surveyed method 1/3; never road centroid.

    Method codes other than 1/3 must not confirm a PVP point's fine location.
    All valid coordinates for the selected *unique progressivo* are considered.
    """
    lat = _f(lat); lon = _f(lon)
    if lat is None or lon is None or not (35 <= lat <= 47.9 and 6 <= lon <= 19):
        return {"nearest_m": "", "high_accuracy_accesses": 0}
    seen = set(); nearest = None
    for civic, alat, alon, method in arr or []:
        m = clean(method).removesuffix(".0")
        alat, alon = _f(alat), _f(alon)
        if m not in {"1", "3"} or alat is None or alon is None:
            continue
        if not (35 <= alat <= 47.9 and 6 <= alon <= 19):
            continue
        key = (round(alat, 7), round(alon, 7))
        if key in seen:
            continue
        seen.add(key)
        dist = ann.haversine_m(lat, lon, alat, alon)
        nearest = dist if nearest is None else min(nearest, dist)
    return {"nearest_m": round(nearest, 2) if nearest is not None else "",
            "high_accuracy_accesses": len(seen)}

def _prepare_rows(rows, audit, google_core):
    out = []
    for r in rows:
        x = dict(r)
        x["old_lat"] = audit.safe_float(r.get("latitudine"))
        x["old_lon"] = audit.safe_float(r.get("longitudine"))
        x["expected_house_numbers"] = "|".join(sorted(google_core.expected_house_numbers(r)))
        lt = clean(r.get("geocode_location_type")).upper()
        src = clean(r.get("geocode_source")).lower()
        x["status"] = lt if lt.startswith("PVP_") else ""
        x["reason"] = ""
        x["action"] = "corrected_from_pvp" if src == "google_corrected_pvp" else ""
        x["suggested_lat"] = None
        x["suggested_lon"] = None
        out.append(x)
    return out



LOCALITY_PREFIX_RE = re.compile(
    r"(?i)\b(?:localit[aà]|loc\.?|frazione(?:\s+di)?|fraz\.?|borgata|contrada|c\s*/?\s*da|c\.\s*da)\s+"
)
ROAD_START_RE = re.compile(
    r"(?i)\b(?:via|viale|corso|piazza|piazzale|strada|vicolo|vico|largo|traversa|sentiero|rotonda|salita|lungomare|s\.?p\.?|s\.?s\.?|s\.?r\.?)\b"
)
ROAD_WORDS = {"via","viale","corso","piazza","piazzale","strada","vicolo","vico","largo","traversa","sentiero","rotonda","salita","lungomare","sp","ss","sr","autostrada"}


def _loc_norm(ann, v):
    return ann.norm_name(clean(v))


def _strip_admin_for_locality(ann, text, row):
    s=_loc_norm(ann,text)
    for key in ("comune","sigla_provincia","cap"):
        x=_loc_norm(ann,row.get(key))
        if x:
            s=re.sub(r"(?<![a-z0-9])"+re.escape(x)+r"(?![a-z0-9])"," ",s)
    s=re.sub(r"\b\d{5}\b"," ",s)
    return re.sub(r"\s+"," ",s).strip()


def _plausible_locality_hint(ann, value):
    n=_loc_norm(ann,value)
    if not n or len(n)<3 or len(n.split())>7: return False
    toks=set(n.split())
    if toks & ROAD_WORDS: return False
    if all(t.isdigit() for t in toks): return False
    return True


def _runtime_locality_hints(ann, row):
    raw=clean(row.get("indirizzo"))
    if not raw: return []
    out=[]; seen=set()
    # Explicit locality/frazione marker: stop before road phrase or comma.
    for m in LOCALITY_PREFIX_RE.finditer(raw.replace("’", "'")):
        tail=raw[m.end():]; stop=len(tail)
        for sep in (",",";"):
            i=tail.find(sep)
            if i>=0: stop=min(stop,i)
        rm=ROAD_START_RE.search(tail)
        if rm: stop=min(stop,rm.start())
        h=clean(tail[:stop]).strip(" -/,.;")
        h=_strip_admin_for_locality(ann,h,row)
        if _plausible_locality_hint(ann,h):
            n=_loc_norm(ann,h)
            if n not in seen: seen.add(n); out.append((h,n,"EXPLICIT_PREFIX"))
    # Bare/context toponym, but never a segment that looks like a road/civic.
    for part in re.split(r"[,;]",raw):
        p=_strip_admin_for_locality(ann,part,row)
        p=LOCALITY_PREFIX_RE.sub("",p).strip()
        if re.search(r"\d",p) or not _plausible_locality_hint(ann,p): continue
        n=_loc_norm(ann,p)
        if n and n not in seen: seen.add(n); out.append((p,n,"CONTEXT_SEGMENT"))
    core=_strip_admin_for_locality(ann,raw,row)
    core=LOCALITY_PREFIX_RE.sub("",core).strip()
    if not re.search(r"\d",core) and _plausible_locality_hint(ann,core):
        n=_loc_norm(ann,core)
        if n and n not in seen: out.append((core,n,"BARE_TOPONYM"))
    return out



def _resolve_locality_candidates_from_index(index, live, resolved, ann):
    """Apply the validated locality-hint policy to a prebuilt code->locality index."""
    by_pid={}
    for row in live:
        pid=clean(row.get("pvp_id")); code=resolved.get(pid)
        locs=index.get(code,{})
        hints=_runtime_locality_hints(ann,row)
        explicit=[]; other=[]
        for h in hints:
            (explicit if h[2] == "EXPLICIT_PREFIX" else other).append(h)

        explicit_names={h[1] for h in explicit}
        diag={
            "source_hints_count": len(hints),
            "explicit_hints_count": len(explicit_names),
            "explicit_hints": "|".join(h[0] for h in explicit),
        }

        if len(explicit_names) > 1:
            by_pid[pid]={
                "match_mode":"AMBIGUOUS_SOURCE_HINTS","progressivi":[],
                "guard_reason":"multiple_explicit_locality_hints", **diag,
            }
            continue

        if explicit:
            matches=[]
            for raw,normed,reason in explicit:
                if normed in locs:
                    matches.append((reason,raw,normed,locs[normed]))
            names={m[2] for m in matches}
            if len(names)==1 and matches:
                reason,raw,normed,arr=matches[0]
                canonical=Counter(x[2] for x in arr).most_common(1)[0][0]
                by_pid[pid]={
                    "match_mode":"EXACT","match_score":1.0,"hint_reason":reason,
                    "source_hint":raw,"locality_norm":normed,"anncsu_locality":canonical,
                    "progressivi":sorted({x[0] for x in arr}),
                    "streets_count":len({x[1] for x in arr if x[1]}),
                    "guard_reason":"explicit_exact", **diag,
                }
            elif len(names)>1:
                by_pid[pid]={
                    "match_mode":"AMBIGUOUS","progressivi":[],
                    "guard_reason":"explicit_matches_multiple_localities", **diag,
                }
            else:
                by_pid[pid]={
                    "match_mode":"EXPLICIT_UNMATCHED","progressivi":[],
                    "guard_reason":"explicit_locality_not_in_anncsu", **diag,
                }
            continue

        matches=[]
        for raw,normed,reason in other:
            if normed in locs:
                matches.append((reason,raw,normed,locs[normed]))
        names={m[2] for m in matches}
        if len(names)==1 and matches:
            reason,raw,normed,arr=matches[0]
            canonical=Counter(x[2] for x in arr).most_common(1)[0][0]
            by_pid[pid]={
                "match_mode":"EXACT","match_score":1.0,"hint_reason":reason,
                "source_hint":raw,"locality_norm":normed,"anncsu_locality":canonical,
                "progressivi":sorted({x[0] for x in arr}),
                "streets_count":len({x[1] for x in arr if x[1]}),
                "guard_reason":"context_exact", **diag,
            }
        elif len(names)>1:
            by_pid[pid]={
                "match_mode":"AMBIGUOUS","progressivi":[],
                "guard_reason":"multiple_context_localities", **diag,
            }
    return by_pid


def _locality_index_from_sqlite_rows(rows_by_code, ann):
    index={code:{} for code in rows_by_code}
    for code, rows in rows_by_code.items():
        for row in rows:
            loc=clean(row.get("LOCALITA'")); prog=clean(row.get("PROGRESSIVO_NAZIONALE"))
            if not loc or not prog:
                continue
            nl=_loc_norm(ann,loc)
            if not nl:
                continue
            index.setdefault(code,{}).setdefault(nl,[]).append(
                (prog,clean(row.get("ODONIMO")),loc)
            )
    return index


def _scan_strad_localities(strad, live, resolved, ann):
    """Second lightweight STRAD pass; INDIR remains a single pass.

    v11.2 safety rule:
    - explicit locality/frazione hints have precedence over generic comma-context;
    - if the source contains >1 distinct explicit locality hints, fail closed;
    - if an explicit hint exists but does not match ANNCSU LOCALITA' exactly,
      do not silently replace it with a secondary context segment.

    This prevents cases such as "Frazione Mocaiana, Casamorcia, ..." from
    being auto-resolved to CASAMORCIA just because the explicit Mocaiana label
    is absent from ANNCSU LOCALITA'.
    """
    target_codes=set(resolved.values())
    index={code:{} for code in target_codes}
    fh,reader,delim=ann.sniff_csv(strad)
    try:
        for row in reader:
            code=re.sub(r"\D","",clean(row.get("CODICE_ISTAT")))
            code=code.zfill(6)[-6:] if code else ""
            if code not in target_codes: continue
            loc=clean(row.get("LOCALITA'")); prog=clean(row.get("PROGRESSIVO_NAZIONALE"))
            if not loc or not prog: continue
            nl=_loc_norm(ann,loc)
            if not nl: continue
            index.setdefault(code,{}).setdefault(nl,[]).append((prog,clean(row.get("ODONIMO")),loc))
    finally:
        fh.close()

    return _resolve_locality_candidates_from_index(index, live, resolved, ann)

def _locality_geometry(ann, progressivi, accesses):
    vals=[]; seen=set(); methods=Counter()
    for prog in progressivi or []:
        for civic,lat,lon,method in accesses.get(prog,[]):
            lat=_f(lat); lon=_f(lon)
            if lat is None or lon is None or not (35.0<=lat<=47.9 and 6.0<=lon<=19.0): continue
            key=(round(lat,7),round(lon,7))
            if key in seen: continue
            seen.add(key); vals.append((lat,lon,clean(prog),clean(civic),clean(method))); methods[clean(method) or "<EMPTY>"]+=1
    if not vals: return None
    med_lat=statistics.median(x[0] for x in vals); med_lon=statistics.median(x[1] for x in vals)
    rep=min(vals,key=lambda x: ann.haversine_m(med_lat,med_lon,x[0],x[1]))
    ds=[ann.haversine_m(rep[0],rep[1],x[0],x[1]) for x in vals]
    return {
        "lat":rep[0],"lon":rep[1],"progressivo":rep[2],"civic":rep[3],"method":rep[4],
        "accesses_with_valid_coords":len(vals),"p50_from_rep_m":round(_percentile(ds,.5),2),
        "p90_from_rep_m":round(_percentile(ds,.9),2),"max_from_rep_m":round(max(ds),2),
        "method_counts":"|".join(f"{k}:{v}" for k,v in methods.most_common()),
    }


def _source_specificity(row, google_core):
    nums=google_core.expected_house_numbers(row)
    raw=clean(row.get("indirizzo"))
    # Any explicit road phrase means locality can only be a fallback.
    has_road=bool(ROAD_START_RE.search(raw))
    return "HAS_SPECIFIC" if nums or has_road else "LOCALITY_ONLY"


def locality_for_missing(row, ev, *, allow_fallback=True):
    if clean(ev.get("runtime_locality_match_mode"))!="EXACT": return False,"locality_not_exact"
    lat=_f(ev.get("runtime_locality_lat")); lon=_f(ev.get("runtime_locality_lon"))
    n=int(float(ev.get("runtime_locality_accesses") or 0)); p90=_f(ev.get("runtime_locality_p90_m"))
    if lat is None or lon is None or p90 is None: return False,"locality_no_geometry"
    compact=(n>=5 and p90<=250) or (n>=10 and p90<=500)
    if not compact: return False,"locality_not_compact"
    spec=clean(ev.get("runtime_locality_source_specificity"))
    if spec=="HAS_SPECIFIC" and not allow_fallback: return False,"locality_only_fallback"
    return True,("locality_final" if spec=="LOCALITY_ONLY" else "locality_fallback")

def locality_for_indicative_map(row, ev):
    """Broader LOCALITA-only map fallback, still fully offline and conservative.

    This is intentionally weaker than locality_for_missing(): it is only for
    source addresses that name a locality/frazione/contrada and contain no
    road/civic. The official ANNCSU locality must match exactly in the resolved
    municipality and have at least one real access coordinate. A representative
    point near the median access cloud is acceptable for map display at LOCALITA
    precision; it is never CIVICO and never used to contradict a PVP coordinate.
    """
    if clean(ev.get("runtime_locality_match_mode")) != "EXACT":
        return False, "locality_not_exact"
    if clean(ev.get("runtime_locality_source_specificity")) != "LOCALITY_ONLY":
        return False, "source_not_locality_only"
    guard = clean(ev.get("runtime_locality_guard_reason")).lower()
    if guard not in {"explicit_exact", "context_exact"}:
        return False, "locality_guard_not_exact"
    lat = _f(ev.get("runtime_locality_lat")); lon = _f(ev.get("runtime_locality_lon"))
    try:
        n = int(float(ev.get("runtime_locality_accesses") or 0))
    except Exception:
        n = 0
    p90 = _f(ev.get("runtime_locality_p90_m"))
    if lat is None or lon is None or n < 1 or p90 is None:
        return False, "locality_no_geometry"
    # A fraction/locality can legitimately span kilometres. Reject only very
    # diffuse clouds: the point remains explicitly LOCALITA/indicative.
    if p90 > 5000:
        return False, "locality_too_diffuse_gt5km"
    return True, "locality_indicative_exact_anncsu"


def build_local_evidence(rows, *, top_streets=6):
    """Evaluate a small row set against ISTAT + ANNCSU using validated v2.4 logic."""
    if not rows:
        return [], {"rows": 0}

    audit = _load(AUDIT_FILE, "aste_audit_v24_runtime")
    ann = _load(ANN_FILE, "aste_anncsu_v10_runtime")
    google_core = _load(GOOGLE_CORE_FILE, "aste_google_core_v935_runtime")
    ann.MUNICIPALITY_ALIASES.update(ALIASES)

    use_sqlite = SQLITE_INDEX_FILE.is_file()
    sqlite_mod = None
    sqlite_meta = None
    sqlite_con = None
    strad = indir = None
    if use_sqlite:
        sqlite_mod = _load(SQLITE_RUNTIME_FILE, "aste_anncsu_sqlite_runtime_v1")
        sqlite_con = sqlite_mod.open_index(SQLITE_INDEX_FILE)
        sqlite_meta = sqlite_mod.verify_index(sqlite_con)
        print(f"[ANNCSU] SQLite index: {SQLITE_INDEX_FILE.name} ({SQLITE_INDEX_FILE.stat().st_size/1024/1024:.1f} MiB)", flush=True)
    else:
        strad = audit.pick_input(ann, audit.ANNCSU_STRAD_FILE, ["STRAD_ITA_*.csv"], "STRAD ANNCSU")
        indir = audit.pick_input(ann, audit.ANNCSU_INDIR_FILE, ["INDIR_ITA_*.csv"], "INDIR ANNCSU")
        print("[ANNCSU] SQLite index absent: legacy full-CSV fallback", flush=True)
    istat = Path(audit.ISTAT_COMUNI_FILE)
    if not istat.is_absolute():
        istat = REFERENCE_DIR / istat
    if not istat.exists():
        raise SystemExit(f"ERRORE: elenco ISTAT non trovato: {istat}")

    live = _prepare_rows(rows, audit, google_core)
    comuni = audit.load_istat_comuni_plus(istat, ann)
    audit.assert_istat_sardinia_2026(comuni, ann)
    try:
        if use_sqlite:
            crosswalk = sqlite_mod.load_crosswalk(sqlite_con)
        else:
            crosswalk = audit.build_anncsu_catasto_crosswalk(strad, comuni, ann)
        resolved, muni_meta, unresolved = audit.resolve_municipalities_v2_2(live, comuni, crosswalk, ann)

        if use_sqlite:
            streets, rows_by_code = sqlite_mod.scan_streets(
                sqlite_con, live, resolved, ann, topn=max(3, int(top_streets)))
            locality_index = _locality_index_from_sqlite_rows(rows_by_code, ann)
            locality_candidates = _resolve_locality_candidates_from_index(
                locality_index, live, resolved, ann)
            wanted = {cand[1] for arr in streets.values() for cand in arr}
            wanted.update(prog for m in locality_candidates.values() for prog in (m.get("progressivi") or []))
            accesses = sqlite_mod.load_accesses(sqlite_con, wanted, ann)
            municipality_bbox = sqlite_mod.load_bboxes(sqlite_con, set(resolved.values()))
        else:
            streets = ann.scan_strad(strad, live, resolved, topn=max(3, int(top_streets)))
            locality_candidates = _scan_strad_localities(strad, live, resolved, ann)
            wanted = {cand[1] for arr in streets.values() for cand in arr}
            wanted.update(prog for m in locality_candidates.values() for prog in (m.get("progressivi") or []))
            # IMPORTANT: single INDIR pass for both street/civic and locality evidence.
            accesses, municipality_bbox = audit.scan_indir_plus(indir, wanted, set(resolved.values()), ann)
    except Exception:
        if sqlite_con is not None:
            sqlite_con.close()
        raise

    originals = {clean(r.get("pvp_id")): r for r in rows}
    out = []
    for src in live:
        pid = clean(src.get("pvp_id"))
        orig = originals[pid]
        ev = ann.evaluate_record_v5(src, resolved.get(pid), muni_meta.get(pid, {}), streets.get(pid, []), accesses)
        near = audit.nearest_plausible_street_distance(ann, src, streets.get(pid, []), accesses)
        reverse_ev = audit.stored_reverse_evidence(orig)
        bbox = municipality_bbox.get(resolved.get(pid))
        bbox_distance = audit.distance_outside_bbox_m(ann, orig.get("latitudine"), orig.get("longitudine"), bbox)
        strict = audit.strict_street_similarity(src, ev.get("anncsu_odonimo", ""))
        label, priority = audit.classify_v2_2(ev, near, reverse_ev, bbox_distance, strict, orig)
        tier, tier_reason = audit.correction_tier_v2_2(ev, label, strict, bbox_distance, orig)
        rep, rep_status, rep_prog = _pick_rep_for_selected_street(ann, streets.get(pid, []), ev, accesses)
        meta = muni_meta.get(pid, {})
        item = dict(ev)
        loc = locality_candidates.get(pid, {})
        loc_geom = _locality_geometry(ann, loc.get("progressivi", []), accesses) if loc.get("match_mode") == "EXACT" else None
        # These metrics come from the SAME selected official street/locality,
        # NOT a nearby unrelated road or the municipality-wide bounding box.
        street_near = _nearest_high_accuracy_access(
            ann, accesses.get(rep_prog, []) if rep_status == "UNIQUE" else [],
            orig.get("latitudine"), orig.get("longitudine"))
        locality_near = _nearest_high_accuracy_access(
            ann, (access for prog in loc.get("progressivi", []) for access in accesses.get(prog, []))
            if loc.get("match_mode") == "EXACT" else [],
            orig.get("latitudine"), orig.get("longitudine"))
        item.update({
            "pvp_id": pid,
            "runtime_locality_match_mode": clean(loc.get("match_mode")),
            "runtime_locality_match_score": loc.get("match_score", ""),
            "runtime_locality_hint_reason": clean(loc.get("hint_reason")),
            "runtime_locality_source_hint": clean(loc.get("source_hint")),
            "runtime_locality_guard_reason": clean(loc.get("guard_reason")),
            "runtime_locality_source_hints_count": loc.get("source_hints_count", 0),
            "runtime_locality_explicit_hints_count": loc.get("explicit_hints_count", 0),
            "runtime_locality_explicit_hints": clean(loc.get("explicit_hints")),
            "runtime_anncsu_locality": clean(loc.get("anncsu_locality")),
            "runtime_locality_progressivi_count": len(loc.get("progressivi") or []),
            "runtime_locality_streets_count": loc.get("streets_count", ""),
            "runtime_locality_source_specificity": _source_specificity(orig, google_core),
            "runtime_locality_lat": (loc_geom or {}).get("lat", ""),
            "runtime_locality_lon": (loc_geom or {}).get("lon", ""),
            "runtime_locality_progressivo": (loc_geom or {}).get("progressivo", ""),
            "runtime_locality_accesses": (loc_geom or {}).get("accesses_with_valid_coords", 0),
            "runtime_locality_p50_m": (loc_geom or {}).get("p50_from_rep_m", ""),
            "runtime_locality_p90_m": (loc_geom or {}).get("p90_from_rep_m", ""),
            "runtime_locality_max_m": (loc_geom or {}).get("max_from_rep_m", ""),
            "audit_result": label,
            "priority": priority,
            "candidate_tier": tier,
            "candidate_reason": tier_reason,
            "strict_street_similarity": strict,
            "nearest_plausible_official_street_m": near,
            "distance_outside_municipality_bbox_m": bbox_distance,
            "anncsu_code_used": meta.get("anncsu_code_used", ""),
            "anncsu_code_source": meta.get("anncsu_code_source", ""),
            "province_relation_raw": meta.get("province_relation_raw", ""),
            "province_transition_compatible": meta.get("province_transition_compatible", ""),
            "street_representative_status": rep_status,
            "street_progressivo": rep_prog,
            "street_representative": rep or {},
            "selected_street_nearest_high_accuracy_access_m": street_near["nearest_m"],
            "selected_street_high_accuracy_accesses": street_near["high_accuracy_accesses"],
            "matched_locality_nearest_high_accuracy_access_m": locality_near["nearest_m"],
            "matched_locality_high_accuracy_accesses": locality_near["high_accuracy_accesses"],
        })
        out.append(item)

    if sqlite_con is not None:
        sqlite_con.close()
    return out, {
        "rows": len(rows),
        "municipality_unresolved": len(unresolved),
        "provider": "SQLITE_INDEX" if use_sqlite else "CSV_SCAN",
        "sqlite_index": SQLITE_INDEX_FILE.name if use_sqlite else "",
        "sqlite_built_at": (sqlite_meta or {}).get("built_at", "") if use_sqlite else "",
        "strad": (sqlite_meta or {}).get("strad_member", "") if use_sqlite else strad.name,
        "indir": (sqlite_meta or {}).get("indir_member", "") if use_sqlite else indir.name,
        "istat": istat.name,
        "engine": ANN_FILE.name,
        "audit_logic": AUDIT_FILE.name,
        "google_parser": GOOGLE_CORE_FILE.name,
    }


def direct_civic_for_missing(ev):
    method = clean(ev.get("anncsu_metodo"))
    if method.endswith(".0"):
        method = method[:-2]
    sc = _f(ev.get("street_score")) or 0.0
    try:
        ac = int(float(ev.get("matching_access_candidates") or 0))
    except Exception:
        ac = 0
    guard = clean(ev.get("direct_locality_guard")).upper()
    return (
        clean(ev.get("province_relation")).upper() == "MATCH"
        and clean(ev.get("ambiguity")).upper() == "NO"
        and clean(ev.get("civic_relation")).lower() == "exact"
        and sc >= .90 and ac == 1 and method in {"1", "3"}
        and _f(ev.get("anncsu_lat")) is not None and _f(ev.get("anncsu_lon")) is not None
        and guard in {"NO_HINT", "CONFIRMED"}
    )


def explicit_snc(text):
    return bool(re.search(r"(?i)(?:\bs\.?\s*n\.?\s*c\.?\b|\bsenza\s+numero\s+civico\b)", clean(text)))


def street_for_missing(row, ev):
    """Validated conservative ANNCSU road fallback; never claims a civic."""
    expected = clean(ev.get("expected_house_numbers"))
    if expected:
        return False, "source_has_civic"
    sc = _f(ev.get("street_score"))
    strict = _f(ev.get("strict_street_similarity"))
    rep = ev.get("street_representative") or {}
    guard = clean(ev.get("direct_locality_guard")).upper()
    try:
        nstreet = int(float(ev.get("matching_street_candidates") or 0))
    except Exception:
        nstreet = 0
    ncoords = int(rep.get("unique_coord_count") or 0)
    p90 = _f(rep.get("p90_from_rep_m"))
    if sc is None or sc < .96: return False, "street_score_lt_0_96"
    if strict is None or strict < .90: return False, "strict_similarity_lt_0_90"
    if nstreet != 1: return False, "street_not_unique"
    if clean(ev.get("street_representative_status")) != "UNIQUE": return False, "progressivo_not_unique"
    if guard not in {"NO_HINT", "CONFIRMED"}: return False, "locality_guard"
    if ncoords < 3 or p90 is None: return False, "insufficient_access_geometry"

    if explicit_snc(row.get("indirizzo")):
        return (p90 <= 5000), ("explicit_snc" if p90 <= 5000 else "street_spread_gt5km")

    # Non-SNC road fallback is deliberately stricter and excludes rural/compound
    # forms that historically produced ambiguous matches.
    raw = clean(row.get("indirizzo")).casefold()
    forbidden = (
        "contrada", "c/da", "c.da", "località", "localita", "frazione",
        "borgata", "vicinale", "zona industriale", "scalo ferroviario",
        "strada statale", "strada provinciale", " ss ", " sp ", " km ",
    )
    if any(x in f" {raw} " for x in forbidden):
        return False, "complex_or_rural_no_civic"
    if p90 > 2000:
        return False, "street_spread_gt2km"
    return True, "simple_no_civic"
