import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
SRC_ROOT = BASE_DIR.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from aste_paths import RAW_DIR, REFERENCE_DIR, ensure_runtime_dirs

ensure_runtime_dirs()
IN_RAW = RAW_DIR / "listings_api_raw.jsonl"
IN_ALLEGATI = RAW_DIR / "allegati_links.jsonl"
IN_SOGGETTI = RAW_DIR / "listing_soggetti.jsonl"
COMUNI_CSV = REFERENCE_DIR / "comuni_cap.csv"

OUT_JSONL = RAW_DIR / "hybrid_api_rows_full.jsonl"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE = os.environ.get("SUPABASE_SERVICE_ROLE", "").strip()
SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE)

BATCH_SIZE_LISTINGS = int(os.environ.get("BATCH_SIZE_LISTINGS", "200"))
BATCH_SIZE_ALLEGATI = int(os.environ.get("BATCH_SIZE_ALLEGATI", "500"))
BATCH_SIZE_DELETE = int(os.environ.get("BATCH_SIZE_DELETE", "200"))

DISP_MAP = {
    "occup": "Occupato",
    "occupato": "Occupato",
    "liber": "Libero",
    "libero": "Libero",
    "occst": "Occupato senza titolo",
    "occupato_senza_titolo": "Occupato senza titolo",
    "non_specificato": "",
    "non_specificata": "",
    "": "",
}

LISTINGS_FIELDS = [
    "pvp_id", "link_pvp", "data_pubblicazione", "data_pubblicazione_date", "data_asta",
    "regione", "comune", "provincia", "sigla_provincia", "cap", "indirizzo", "maps_link",
    "latitudine", "longitudine", "tipologia", "tribunale", "tipo_procedura", "numero_procedura",
    "anno_procedura", "categoria_procedura", "categoria_procedura_reason", "lotto", "descrizione",
    "categoria_immobile", "categoria_immobile_reason", "prezzo_base", "offerta_minima",
    "rialzo_minimo", "disponibilita", "vani", "piano", "superficie_mq", "esperimento_vendita",
    "note", "status"
]


def load_jsonl(path: Path):
    out = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def clean(v) -> str:
    if v is None:
        return ""
    s = str(v).replace("\u00A0", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def first_non_empty(*vals):
    for v in vals:
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            for item in v:
                s = clean(item)
                if s and s.upper() not in {"NULL", "NONE", "NON_SPECIFICATO", "NON_SPECIFICATA", "-"}:
                    return s
            continue
        s = clean(v)
        if s and s.upper() not in {"NULL", "NONE", "NON_SPECIFICATO", "NON_SPECIFICATA", "-"}:
            return s
    return ""


def titleish(s: str) -> str:
    s = clean(s).replace("_", " ").replace("-", " ")
    return " ".join(w.capitalize() if w.isalpha() else w for w in s.split())


def euro_it(v) -> str:
    if v is None or v == "":
        return ""
    try:
        x = float(v)
    except Exception:
        return clean(v)
    s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return s + " €"


def date_it(v: str) -> str:
    s = clean(v).replace("Z", "")
    if not s:
        return ""
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y", "%d/%m/%Y %H:%M"):
        try:
            dt = datetime.strptime(s[:19] if "T" in s and len(s) >= 19 else s, fmt)
            return dt.strftime("%d/%m/%Y")
        except Exception:
            pass
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else ""


def iso_date_from_it(v: str) -> str:
    s = clean(v)
    if not s:
        return ""
    try:
        return datetime.strptime(s, "%d/%m/%Y").strftime("%Y-%m-%d")
    except Exception:
        return ""


def normalize_disponibilita(v) -> str:
    if v is None:
        return ""
    vals = v if isinstance(v, list) else [v]
    out = []
    for x in vals:
        s = clean(x)
        if not s:
            continue
        mapped = DISP_MAP.get(s.lower().replace(" ", "_"), titleish(s))
        if mapped and mapped not in out:
            out.append(mapped)
    return " | ".join(out)


def prettify_api_category(v) -> str:
    s = clean(v)
    return titleish(s) if s else ""


def build_maps_link(lat, lon, indirizzo="", comune="", provincia=""):
    lat_s = clean(lat)
    lon_s = clean(lon)
    if lat_s and lon_s:
        return f"https://www.google.com/maps?q={lat_s},{lon_s}"
    q = " ".join(x for x in [clean(indirizzo), clean(comune), clean(provincia)] if clean(x))
    return f"https://www.google.com/maps?q={quote(q)}" if q else ""


def clean_esperimento(v):
    s = clean(v).replace(" ?", "")
    if "/" in s and re.fullmatch(r"[IVXLC]+/[IVXLC]+", s):
        return s.split("/")[0]
    return s


def _roman(n: int) -> str:
    return {1:"I",2:"II",3:"III",4:"IV",5:"V",6:"VI",7:"VII",8:"VIII",9:"IX",10:"X"}.get(n, "")


def extract_esperimento_from_filenames(files):
    if not files:
        return ""
    for f in files:
        up = f.upper()
        for rn in [" X ", " IX ", " VIII ", " VII ", " VI ", " V ", " IV ", " III ", " II ", " I "]:
            if rn.strip() in up:
                return rn.strip()
        m = re.search(r"\b(10|[1-9])\s*[°ºª\^]?", f)
        if m:
            return _roman(int(m.group(1)))
    return ""


def load_comuni_lookup(csv_path: Path):
    comuni_map = {}
    cap_map = {}
    try:
        f = open(csv_path, "r", encoding="utf-8-sig", errors="replace", newline="")
    except Exception:
        f = open(csv_path, "r", encoding="latin-1", errors="replace", newline="")
    with f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            comune = clean(row.get("comune") or row.get("Comune") or row.get("ï»¿comune") or "")
            if not comune:
                continue
            info = {
                "regione": clean(row.get("regione") or row.get("Regione") or ""),
                "provincia": clean(row.get("provincia") or row.get("Provincia") or ""),
                "sigla_provincia": clean(row.get("sigla_provincia") or row.get("sigla") or row.get("Sigla") or ""),
                "comune": comune,
                "cap": re.sub(r"\D", "", clean(row.get("cap") or row.get("CAP") or "")).zfill(5),
            }
            comuni_map.setdefault(comune.lower(), []).append(info)
            if info["cap"]:
                cap_map.setdefault(info["cap"], []).append(info)
    return comuni_map, cap_map


COMUNI_MAP, CAP_MAP = load_comuni_lookup(COMUNI_CSV)


def match_comune_info(indirizzo: str):
    s = clean(indirizzo).lower()
    if not s:
        return {}
    m = re.search(r"\b(\d{5})\b", s)
    if m and m.group(1) in CAP_MAP:
        return CAP_MAP[m.group(1)][0]
    for comune, infos in COMUNI_MAP.items():
        if comune and comune in s:
            return infos[0]
    return {}




def canonical_text_key(s: str) -> str:
    s = clean(s).lower()
    s = s.replace("’", "'")
    s = s.replace("`", "'")
    s = s.replace("ü", "u").replace("ö", "o").replace("ä", "a")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


REGIONE_ALIASES = {
    "trentino-alto adige": "Trentino-Alto Adige/Südtirol",
    "trentino alto adige": "Trentino-Alto Adige/Südtirol",
    "trentino-alto-adige": "Trentino-Alto Adige/Südtirol",
    "trentino-alto adige/sudtirol": "Trentino-Alto Adige/Südtirol",
    "trentino alto adige/sudtirol": "Trentino-Alto Adige/Südtirol",
    "trentino-alto adige/sud tirol": "Trentino-Alto Adige/Südtirol",
    "trentino alto adige/sud tirol": "Trentino-Alto Adige/Südtirol",
    "trentino-alto adige/südtirol": "Trentino-Alto Adige/Südtirol",
    "trentino alto adige/südtirol": "Trentino-Alto Adige/Südtirol",
}

PROVINCIA_ALIASES = {
    "reggio di calabria": "Reggio Calabria",
}


def canonicalize_geo_labels(regione="", provincia="", sigla=""):
    regione_clean = clean(regione)
    provincia_clean = clean(provincia)
    sigla_clean = clean(sigla).upper()

    reg_key = canonical_text_key(regione_clean)
    prov_key = canonical_text_key(provincia_clean)

    regione_final = REGIONE_ALIASES.get(reg_key, regione_clean)
    provincia_final = PROVINCIA_ALIASES.get(prov_key, provincia_clean)

    if provincia_final == "Reggio Calabria" and not sigla_clean:
        sigla_clean = "RC"

    return {
        "regione": regione_final,
        "provincia": provincia_final,
        "sigla_provincia": sigla_clean,
    }


def normalize_geo_with_csv(comune="", provincia="", sigla="", regione="", cap=""):
    comune = clean(comune)
    provincia = clean(provincia)
    sigla = clean(sigla)
    regione = clean(regione)
    cap = re.sub(r"\D", "", clean(cap))
    if cap:
        cap = cap.zfill(5)
    if not comune:
        return {
            "comune": comune,
            "provincia": provincia,
            "sigla_provincia": sigla,
            "regione": regione,
            "cap": cap,
        }
    candidates = COMUNI_MAP.get(comune.lower(), [])
    chosen = None
    for info in candidates:
        if provincia and clean(info.get("provincia")).lower() == provincia.lower():
            chosen = info
            break
        if sigla and clean(info.get("sigla_provincia")).lower() == sigla.lower():
            chosen = info
            break
        if regione and clean(info.get("regione")).lower() == regione.lower():
            chosen = info
            break
        if cap and clean(info.get("cap")) == cap:
            chosen = info
            break
    if not chosen and len(candidates) == 1:
        chosen = candidates[0]
    if not chosen:
        return {
            "comune": comune,
            "provincia": provincia,
            "sigla_provincia": sigla,
            "regione": regione,
            "cap": cap,
        }
    return {
        "comune": comune or clean(chosen.get("comune")),
        "provincia": clean(chosen.get("provincia")) or provincia,
        "sigla_provincia": clean(chosen.get("sigla_provincia")) or sigla,
        "regione": clean(chosen.get("regione")) or regione,
        "cap": clean(chosen.get("cap")) or cap,
    }


def clean_via_structured(via, comune="", provincia="", sigla="", cap="") -> str:
    s = clean(via)
    if not s:
        return ""
    s = s.replace("lLoc.", "Loc.").replace("lLocalità", "Località")
    s = re.sub(r"\blloc\.", "loc.", s, flags=re.I)
    s = re.sub(r"\bllocalit[àa]\b", "località", s, flags=re.I)
    s = re.sub(r",?\s*Italia\s*$", "", s, flags=re.I)
    s = re.sub(r"\bProvincia\s+(?:di|della|del)\b.*$", "", s, flags=re.I)
    s = re.sub(r"\b(Nord-Est Sardegna|Sud Sardegna)\b.*$", "", s, flags=re.I)
    comune_esc = re.escape(clean(comune))
    if comune:
        s = re.sub(
            rf"^\s*{comune_esc}\s+(?=(?:l?loc\.?|l?località|contrada|c\.da|via|viale|piazza|corso|vicolo|strada)\b)",
            "",
            s,
            flags=re.I,
        )
    prov_esc = re.escape(clean(provincia))
    sigla_esc = re.escape(clean(sigla))
    cap_esc = re.escape(clean(cap))
    patterns = []
    if cap and comune and sigla:
        patterns.append(rf",?\s*{cap_esc}\s+{comune_esc}\s+{sigla_esc}\s*$")
    if cap and comune and provincia:
        patterns.append(rf",?\s*{cap_esc}\s+{comune_esc}\s+{prov_esc}\s*$")
    if comune and sigla:
        patterns.append(rf",?\s*{comune_esc}\s+{sigla_esc}\s*$")
    if comune and provincia:
        patterns.append(rf",?\s*{comune_esc}\s+{prov_esc}\s*$")
    if cap and comune:
        patterns.append(rf",?\s*{cap_esc}\s+{comune_esc}\s*$")
    changed = True
    while changed:
        changed = False
        for pat in patterns:
            ns = re.sub(pat, "", s, flags=re.I).strip(" ,")
            if ns != s:
                s = ns
                changed = True
    s = re.sub(r"\s*,\s*,+", ", ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,")
    parts = [p.strip(" ,") for p in re.split(r"\s*,\s*", s) if p.strip(" ,")]
    dedup = []
    seen_norm = set()
    for p in parts:
        norm = re.sub(r"\W+", "", p).lower()
        if not norm or norm in seen_norm:
            continue
        seen_norm.add(norm)
        dedup.append(p)
    return ", ".join(dedup)


def compose_indirizzo(via="", cap="", comune="", sigla="") -> str:
    line1 = clean(via)
    line2 = " ".join(x for x in [clean(cap), clean(comune), clean(sigla)] if clean(x)).strip()
    return ", ".join(x for x in [line1, line2] if x)


def build_address_from_bene(bene: dict) -> str:
    addr = bene.get("indirizzo") or {}
    comune = clean(addr.get("descComune") or addr.get("citta") or addr.get("comune") or "")
    provincia = clean(addr.get("descProvincia") or addr.get("provincia") or "")
    sigla = clean(addr.get("siglaProvincia") or addr.get("sigla_provincia") or "")
    cap = re.sub(r"\D", "", clean(addr.get("cap") or "")).zfill(5) if clean(addr.get("cap") or "") else ""
    via_raw = clean(addr.get("via") or addr.get("descVia") or "")
    nc = clean(addr.get("numeroCivico") or "")
    via = clean_via_structured(via_raw, comune, provincia, sigla, cap)
    if nc and nc not in via:
        via = " ".join(x for x in [via, nc] if x).strip()
    return compose_indirizzo(via, cap, comune, sigla or provincia)


def extract_geo_api(detail_body: dict, search_item: dict, bene_list: list):
    out = {"comune":"", "provincia":"", "sigla_provincia":"", "regione":"", "cap":"", "lat":"", "lon":""}
    addr_sources = []
    for b in bene_list:
        addr_sources.append(b.get("indirizzo") or {})
    addr_sources.append(search_item.get("indirizzo") or {})
    addr_sources.append(detail_body.get("indirizzo") or {})
    for addr in addr_sources:
        if not isinstance(addr, dict):
            continue
        coords = addr.get("coordinate") or {}
        out["comune"] = out["comune"] or first_non_empty(addr.get("descComune"), addr.get("citta"), addr.get("comune"))
        out["provincia"] = out["provincia"] or first_non_empty(addr.get("descProvincia"), addr.get("provincia"))
        out["sigla_provincia"] = out["sigla_provincia"] or first_non_empty(addr.get("siglaProvincia"), addr.get("sigla_provincia"))
        out["regione"] = out["regione"] or first_non_empty(addr.get("descRegione"), addr.get("regione"), addr.get("codRegione"))
        cap = re.sub(r"\D", "", clean(addr.get("cap") or "")).zfill(5) if clean(addr.get("cap") or "") else ""
        out["cap"] = out["cap"] or cap
        out["lat"] = out["lat"] or first_non_empty(addr.get("latitudine"), addr.get("lat"), coords.get("latitudine"), coords.get("lat"))
        out["lon"] = out["lon"] or first_non_empty(addr.get("longitudine"), addr.get("lon"), addr.get("lng"), coords.get("longitudine"), coords.get("lon"), coords.get("lng"))
    return out


def pick_categoria_immobile(detail_body: dict, search_item: dict, fallback=""):
    lotto = detail_body.get("lotto") or {}
    for cand in [
        detail_body.get("categoriaLotto"), detail_body.get("descTipoCategLotto"), detail_body.get("codTipoCategLotto"),
        lotto.get("categoriaLotto"), lotto.get("descTipoCategLotto"), lotto.get("codTipoCategLotto"),
        search_item.get("ncategoriaLotto"), search_item.get("categoriaLotto"),
    ]:
        s = prettify_api_category(cand)
        if s:
            return s, "api_categoria_lotto"
    return fallback, "fallback"


def pick_tipologia(detail_body: dict, search_item: dict, fallback=""):
    lotto = detail_body.get("lotto") or {}
    beni = (detail_body.get("beni") or []) + (lotto.get("beni") or [])
    for b in beni:
        for k in ("categoriaBene", "descCategoriaBene", "tipoBene", "descTipologiaBene", "tipologia", "codTipologiaBene"):
            s = prettify_api_category(b.get(k))
            if s:
                return s
    for k in ("categoriaBene", "descCategoriaBene", "tipoBene", "descTipologiaBene", "tipologia"):
        s = prettify_api_category(search_item.get(k))
        if s:
            return s
    return fallback


def pick_tipo_procedura(detail_body: dict, search_item: dict, fallback=""):
    procedura = detail_body.get("procedura") or {}
    return first_non_empty(procedura.get("tipoRito"), procedura.get("descTipoRito"), detail_body.get("tipoRito"), detail_body.get("descTipoRito"), search_item.get("tipoRito"), search_item.get("descTipoRito"), fallback)


def pick_categoria_procedura(detail_body: dict, search_item: dict, fallback=""):
    procedura = detail_body.get("procedura") or {}
    s = first_non_empty(procedura.get("tipoRegistro"), procedura.get("descTipoRegistro"), detail_body.get("tipoRegistro"), detail_body.get("descTipoRegistro"), search_item.get("tipoRegistro"), search_item.get("descTipoRegistro"), fallback)
    return s, ("api_tipo_registro" if s and s != fallback else "fallback")


def pick_data_pubblicazione(detail_body: dict, search_item: dict):
    return date_it(first_non_empty(detail_body.get("dataPubblicazione"), detail_body.get("dtPubblicazione"), search_item.get("dataPubblicazione"), search_item.get("dtPubblicazione")))


def best_descrizione(search_item: dict, detail_body: dict) -> str:
    for v in (search_item.get("descLotto"), (detail_body.get("lotto") or {}).get("descLotto"), detail_body.get("descLotto")):
        s = clean(v)
        if s:
            return s
    return ""


def structured_field_from_beni(detail_body: dict, keys, suffix=""):
    vals = []
    beni = (detail_body.get("beni") or []) + ((detail_body.get("lotto") or {}).get("beni") or [])
    for b in beni:
        for k in keys:
            s = clean(b.get(k))
            if s and s not in vals:
                vals.append(s)
    if not vals:
        return ""
    joined = "+".join(vals)
    return f"{joined} {suffix}".strip()


def extract_superficie_text(text: str, descr: str) -> str:
    blob = " ".join([text or "", descr or ""])
    vals = []
    for s in re.findall(r"\b(\d{1,6}(?:[.,]\d{1,2})?)\s*(?:mq|m2|m²)\b", blob, flags=re.I):
        n = s.replace(".", ",")
        if n not in vals:
            vals.append(n)
    return f"{'+'.join(vals)} mq" if vals else ""


def extract_vani_text(text: str, descr: str) -> str:
    blob = " ".join([text or "", descr or ""])
    m = re.search(r"\b(\d{1,2})(?:[.,](\d))?\s*vani\b", blob, flags=re.I)
    return m.group(1) + (("," + m.group(2)) if m and m.group(2) else "") if m else ""


def extract_piano_text(text: str, descr: str) -> str:
    blob = " ".join([text or "", descr or ""]).lower()
    if re.search(r"\bpiano\s+terra\b|\bpt\b|\bp\.t\.\b", blob):
        return "Piano terra"
    if "rialzato" in blob and "piano" in blob:
        return "Piano rialzato"
    if "seminterr" in blob:
        return "Seminterrato"
    m = re.search(r"\b(\d{1,2})\s*(?:°|º)?\s*piano\b", blob)
    return m.group(1) if m else ""


def note_from_raw(raw_text: str, descrizione: str) -> str:
    text = " ".join([raw_text or "", descrizione or ""])
    kw = re.compile(r"(abus|difform|irregolar|sanator|condon|titolo abilitativo|assenza di titolo|privo di titolo)", re.I)
    chunks = re.split(r"(?<=[\.!?;])\s+|\n+", text)
    out = []
    for ch in chunks:
        s = clean(ch)
        if s and kw.search(s) and s not in out:
            out.append(s)
    return " | ".join(out)


def derive_listing_row(obj, allegati_by_id):
    pvp_id = clean(obj.get("pvp_id"))
    link_pvp = clean(obj.get("link_pvp"))
    raw_text = obj.get("raw_text") or ""
    search_item = obj.get("search_item") or {}
    detail_body = obj.get("detail_body") or {}
    descrizione = best_descrizione(search_item, detail_body)
    bene_list = (detail_body.get("beni") or []) + ((detail_body.get("lotto") or {}).get("beni") or [])
    geo_api = extract_geo_api(detail_body, search_item, bene_list)
    indirizzo = ""
    for bene in bene_list:
        indirizzo = build_address_from_bene(bene)
        if indirizzo:
            break
    if not indirizzo:
        s_addr = search_item.get("indirizzo") or {}
        comune0 = clean(s_addr.get("descComune") or s_addr.get("citta") or s_addr.get("comune"))
        provincia0 = clean(s_addr.get("descProvincia") or s_addr.get("provincia"))
        sigla0 = clean(s_addr.get("siglaProvincia") or s_addr.get("sigla_provincia"))
        cap0 = re.sub(r"\D", "", clean(s_addr.get("cap") or "")).zfill(5) if clean(s_addr.get("cap") or "") else ""
        via0 = clean_via_structured(s_addr.get("via") or s_addr.get("descVia"), comune0, provincia0, sigla0, cap0)
        nc0 = clean(s_addr.get("numeroCivico"))
        if nc0 and nc0 not in via0:
            via0 = " ".join(x for x in [via0, nc0] if x).strip()
        indirizzo = compose_indirizzo(via0, cap0, comune0, sigla0 or provincia0)
    info = match_comune_info(indirizzo)
    comune = first_non_empty(geo_api.get("comune"), info.get("comune"))
    provincia = first_non_empty(geo_api.get("provincia"), info.get("provincia"))
    sigla = first_non_empty(geo_api.get("sigla_provincia"), info.get("sigla_provincia"))
    regione = first_non_empty(geo_api.get("regione"), info.get("regione"))
    cap = first_non_empty(geo_api.get("cap"), info.get("cap"))
    geo_norm = normalize_geo_with_csv(comune, provincia, sigla, regione, cap)
    comune = geo_norm.get("comune", "")
    provincia = geo_norm.get("provincia", "")
    sigla = geo_norm.get("sigla_provincia", "")
    regione = geo_norm.get("regione", "")
    cap = geo_norm.get("cap", "")

    geo_canon = canonicalize_geo_labels(regione, provincia, sigla)
    provincia = geo_canon.get("provincia", "")
    sigla = geo_canon.get("sigla_provincia", "")
    regione = geo_canon.get("regione", "")
    if bene_list:
        addr = (bene_list[0].get("indirizzo") or {})
        via_final = clean_via_structured(addr.get("via") or addr.get("descVia"), comune, provincia, sigla, cap)
        nc_final = clean(addr.get("numeroCivico"))
        if nc_final and nc_final not in via_final:
            via_final = " ".join(x for x in [via_final, nc_final] if x).strip()
        indirizzo = compose_indirizzo(via_final, cap, comune, sigla or provincia)
    else:
        s_addr = search_item.get("indirizzo") or {}
        via_final = clean_via_structured(s_addr.get("via") or s_addr.get("descVia"), comune, provincia, sigla, cap)
        nc_final = clean(s_addr.get("numeroCivico"))
        if nc_final and nc_final not in via_final:
            via_final = " ".join(x for x in [via_final, nc_final] if x).strip()
        if via_final or comune or cap:
            indirizzo = compose_indirizzo(via_final, cap, comune, sigla or provincia)
    tipologia = pick_tipologia(detail_body, search_item, "")
    categoria_immobile, categoria_immobile_reason = pick_categoria_immobile(detail_body, search_item, "")
    categoria_immobile = categoria_immobile or "altro"
    procedura = detail_body.get("procedura") or {}
    tipo_procedura = pick_tipo_procedura(detail_body, search_item, "")
    categoria_procedura, categoria_procedura_reason = pick_categoria_procedura(detail_body, search_item, "")
    numero_procedura = re.sub(r"\D+", "", clean(first_non_empty(procedura.get("numeRg"), detail_body.get("numeRg"), search_item.get("numeRg"))))
    anno_procedura = re.sub(r"\D+", "", clean(first_non_empty(procedura.get("numeAnnoRg"), detail_body.get("numeAnnoRg"), search_item.get("numeAnnoRg"))))[:4]
    prezzo_base = euro_it(first_non_empty(search_item.get("prezzoBaseAsta"), detail_body.get("impoBaseAsta")))
    offerta_minima = euro_it(first_non_empty(search_item.get("offertaMinima"), detail_body.get("impoOffertaMinima")))
    rialzo_minimo = euro_it(first_non_empty(search_item.get("rialzoMinimo"), detail_body.get("impoOffertaAumento")))
    data_asta = date_it(first_non_empty(search_item.get("dataVendita"), detail_body.get("dataVendita")))
    data_pubblicazione = pick_data_pubblicazione(detail_body, search_item)
    disponibilita = normalize_disponibilita(first_non_empty(search_item.get("disponibilita"), detail_body.get("disponibilita")))
    if not disponibilita:
        for bene in bene_list:
            disponibilita = normalize_disponibilita(first_non_empty(bene.get("disponibilita"), bene.get("disponibilitaDesc")))
            if disponibilita:
                break
    tribunale = first_non_empty(search_item.get("tribunale"), procedura.get("descUfficio"), detail_body.get("tribunale"))
    lotto = first_non_empty(search_item.get("numeroLotto"), (detail_body.get("lotto") or {}).get("numeroLotto"), (detail_body.get("lotto") or {}).get("codiLotto"))
    superficie_mq = structured_field_from_beni(detail_body, ["superficie", "superficieCommerciale", "superficieMq"], "mq") or extract_superficie_text(raw_text, descrizione)
    vani = structured_field_from_beni(detail_body, ["numeroVani", "vani"]) or extract_vani_text(raw_text, descrizione)
    piano = structured_field_from_beni(detail_body, ["piano", "descPiano", "ubicazionePiano"]) or extract_piano_text(raw_text, descrizione)
    filenames = [clean(a.get("filename")) for a in allegati_by_id.get(pvp_id, []) if clean(a.get("filename"))]
    esperimento = clean_esperimento(first_non_empty(detail_body.get("numeEsperimento"), search_item.get("numeEsperimento")) or extract_esperimento_from_filenames(filenames))
    lat = clean(geo_api.get("lat"))
    lon = clean(geo_api.get("lon"))
    maps_link = build_maps_link(lat, lon, indirizzo, comune, provincia)
    return {
        "pvp_id": pvp_id,
        "link_pvp": link_pvp,
        "data_pubblicazione": data_pubblicazione,
        "data_pubblicazione_date": iso_date_from_it(data_pubblicazione),
        "data_asta": data_asta,
        "regione": regione,
        "comune": comune,
        "provincia": provincia,
        "sigla_provincia": sigla,
        "cap": cap,
        "indirizzo": indirizzo,
        "maps_link": maps_link,
        "latitudine": lat,
        "longitudine": lon,
        "tipologia": tipologia,
        "tribunale": tribunale,
        "tipo_procedura": tipo_procedura,
        "numero_procedura": numero_procedura,
        "anno_procedura": anno_procedura,
        "categoria_procedura": categoria_procedura,
        "categoria_procedura_reason": categoria_procedura_reason,
        "lotto": lotto,
        "descrizione": descrizione,
        "categoria_immobile": categoria_immobile,
        "categoria_immobile_reason": categoria_immobile_reason,
        "prezzo_base": prezzo_base,
        "offerta_minima": offerta_minima,
        "rialzo_minimo": rialzo_minimo,
        "disponibilita": disponibilita,
        "vani": vani,
        "piano": piano,
        "superficie_mq": superficie_mq,
        "esperimento_vendita": esperimento,
        "note": note_from_raw(raw_text, descrizione),
        "status": "active",
    }


def _to_int(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        ds = re.sub(r"\D", "", s)
        return int(ds) if ds else None
    return None


def _http_json(method: str, url: str, payload=None, prefer=None):
    data = None
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if prefer:
        headers["Prefer"] = prefer
    req = Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    with urlopen(req, timeout=120) as resp:
        return resp.read().decode("utf-8", errors="replace")


def sanitize_listing_rows(rows):
    BIGINT_KEYS = {"pvp_id", "numero_procedura", "anno_procedura"}
    def _looks_int_key(k: str) -> bool:
        k2 = (k or "").lower()
        return k2 in BIGINT_KEYS or k2.endswith("_id") or "numero" in k2 or "anno" in k2
    out = []
    for r in rows:
        rr = {k: r.get(k) for k in LISTINGS_FIELDS if k in r}
        for k in list(rr.keys()):
            v = rr.get(k)
            if isinstance(v, str) and v.strip() == "":
                rr.pop(k, None)
        for k in list(rr.keys()):
            v = rr.get(k)
            if k in {"pvp_id", "numero_procedura", "anno_procedura"} or (isinstance(v, str) and _looks_int_key(k)):
                iv = _to_int(v)
                if iv is None:
                    rr.pop(k, None)
                else:
                    rr[k] = iv
        if "pvp_id" not in rr:
            continue
        out.append(rr)
    return out


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]


def upsert_listings(rows):
    if not SUPABASE_ENABLED or not rows:
        return
    rows = sanitize_listing_rows(rows)
    if not rows:
        return
    dedup = {}
    for r in rows:
        dedup[str(r["pvp_id"])] = r
    rows = list(dedup.values())
    from collections import defaultdict as dd
    groups = dd(list)
    for r in rows:
        groups[tuple(sorted(r.keys()))].append(r)
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/listings?on_conflict=pvp_id"
    for _, batch_rows in groups.items():
        for batch in chunked(batch_rows, BATCH_SIZE_LISTINGS):
            _http_json("POST", url, batch, prefer="resolution=merge-duplicates,return=minimal")


def upsert_allegati(rows):
    if not SUPABASE_ENABLED or not rows:
        return
    clean_rows = []
    for r in rows:
        rr = {
            "pvp_id": clean(r.get("pvp_id")),
            "filename": clean(r.get("filename")),
            "resource_url": clean(r.get("resource_url")),
            "mime": clean(r.get("mime")),
            "size_bytes": _to_int(r.get("size_bytes")),
            "seen_at": clean(r.get("seen_at")),
        }
        if not rr["pvp_id"] or not rr["filename"] or not rr["resource_url"]:
            continue
        for k in list(rr.keys()):
            if rr[k] in ("", None):
                rr.pop(k, None)
        clean_rows.append(rr)
    if not clean_rows:
        return
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/listing_allegati?on_conflict=pvp_id,resource_url"
    from collections import defaultdict as dd
    groups = dd(list)
    for r in clean_rows:
        groups[tuple(sorted(r.keys()))].append(r)
    for _, batch_rows in groups.items():
        for batch in chunked(batch_rows, BATCH_SIZE_ALLEGATI):
            _http_json("POST", url, batch, prefer="resolution=merge-duplicates,return=minimal")


def delete_soggetti_for_pvp_ids(pvp_ids):
    if not SUPABASE_ENABLED or not pvp_ids:
        return
    base = f"{SUPABASE_URL.rstrip('/')}/rest/v1/listing_soggetti"
    for chunk in chunked(sorted(set(str(x) for x in pvp_ids if str(x).strip())), BATCH_SIZE_DELETE):
        filt = ",".join(chunk)
        url = f"{base}?pvp_id=in.({filt})"
        _http_json("DELETE", url, None, prefer="return=minimal")


def insert_soggetti(rows):
    if not SUPABASE_ENABLED or not rows:
        return
    clean_rows = []
    for r in rows:
        rr = {
            "pvp_id": clean(r.get("pvp_id")),
            "role_code": clean(r.get("role_code")),
            "role_label": clean(r.get("role_label")),
            "nome": clean(r.get("nome")),
            "cognome": clean(r.get("cognome")),
            "display_name": clean(r.get("display_name")),
            "email": clean(r.get("email")),
            "telefono": clean(r.get("telefono")),
            "sort_order": _to_int(r.get("sort_order")),
        }
        if not rr["pvp_id"]:
            continue
        for k in list(rr.keys()):
            if rr[k] in ("", None):
                rr.pop(k, None)
        clean_rows.append(rr)
    from collections import defaultdict as dd
    groups = dd(list)
    for r in clean_rows:
        groups[tuple(sorted(r.keys()))].append(r)
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/listing_soggetti"
    for _, batch_rows in groups.items():
        for batch in chunked(batch_rows, BATCH_SIZE_ALLEGATI):
            _http_json("POST", url, batch, prefer="return=minimal")


def normalize_soggetti_rows(raw_soggetti_rows):
    out = []
    for r in raw_soggetti_rows:
        pvp_id = clean(r.get("pvp_id"))
        if not pvp_id:
            continue
        out.append({
            "pvp_id": pvp_id,
            "role_code": clean(r.get("role_code") or r.get("ruolo_code") or r.get("codiRuolo")),
            "role_label": clean(r.get("role_label") or r.get("ruolo_label") or r.get("descRuolo")),
            "nome": clean(r.get("nome")),
            "cognome": clean(r.get("cognome")),
            "display_name": clean(r.get("display_name") or r.get("nominativo") or " ".join(x for x in [clean(r.get("nome")), clean(r.get("cognome"))] if x)),
            "email": clean(r.get("email")),
            "telefono": clean(r.get("telefono")),
            "sort_order": r.get("sort_order") if r.get("sort_order") is not None else r.get("ordine"),
        })
    seen = set()
    dedup = []
    for r in out:
        key = (r["pvp_id"], r.get("role_code", ""), r.get("display_name", ""), r.get("email", ""))
        if key in seen:
            continue
        seen.add(key)
        dedup.append(r)
    return dedup


def main():
    raw_rows = load_jsonl(IN_RAW)
    allegati_rows = load_jsonl(IN_ALLEGATI)
    soggetti_rows = load_jsonl(IN_SOGGETTI)
    allegati_by_id = defaultdict(list)
    for a in allegati_rows:
        allegati_by_id[str(a.get("pvp_id") or "")].append(a)
    listing_rows = [derive_listing_row(obj, allegati_by_id) for obj in raw_rows]
    with OUT_JSONL.open("w", encoding="utf-8") as f:
        for row in listing_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"[OK] rows listings: {len(listing_rows)}")
    print(f"[OK] jsonl debug: {OUT_JSONL}")
    print(f"[INFO] Supabase enabled: {SUPABASE_ENABLED}")
    if not SUPABASE_ENABLED:
        print("[INFO] Nessun upsert eseguito: mancano SUPABASE_URL / SUPABASE_SERVICE_ROLE")
        return
    upsert_listings(listing_rows)
    print("[OK] Upsert listings completato")
    if allegati_rows:
        upsert_allegati(allegati_rows)
        print(f"[OK] Upsert allegati completato: {len(allegati_rows)}")
    if soggetti_rows:
        soggetti_norm = normalize_soggetti_rows(soggetti_rows)
        pvp_ids = [r["pvp_id"] for r in soggetti_norm]
        delete_soggetti_for_pvp_ids(pvp_ids)
        insert_soggetti(soggetti_norm)
        print(f"[OK] Refresh soggetti completato: {len(soggetti_norm)}")
    print("[DONE]")


if __name__ == "__main__":
    main()
