import os
import json
import time
import random
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
import csv

ROOT = Path(__file__).resolve().parent
SRC_ROOT = ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from aste_paths import RAW_DIR, REFERENCE_DIR, ensure_runtime_dirs

ensure_runtime_dirs()
# RAW/intermediate data lives outside the code tree so cloud containers may mount it.
OUT_ALLEGATI_JSONL = RAW_DIR / "allegati_links.jsonl"
OUT_SOGGETTI_JSONL = RAW_DIR / "listing_soggetti.jsonl"
OUT_RAW_JSONL = RAW_DIR / "listings_api_raw.jsonl"
OUT_ERRORS_JSONL = RAW_DIR / "api_errors.jsonl"
COMUNI_CSV = REFERENCE_DIR / "comuni_cap.csv"

RICERCA_URL = "https://pvp.giustizia.it/ric-496b258c-986a1b71/ric-ms/ricerca/vendite"
DETAIL_URL_TMPL = "https://pvp.giustizia.it/ve-3f723b85-986a1b71/ve-ms/vendite/{id_annuncio}/restricted"
DETAIL_PAGE_TMPL = "https://pvp.giustizia.it/pvp/it/detail_annuncio.page?idAnnuncio={id_annuncio}"
RESOURCE_HOST = "https://resource-pvp.giustizia.it"

PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "200"))
START_PAGE = int(os.environ.get("START_PAGE", "0"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "0"))
MAX_DETAILS = int(os.environ.get("MAX_DETAILS", "0"))
STOP_ON_PAST = os.environ.get("STOP_ON_PAST", "1") == "1"
MAX_VALID_YEAR = int(os.environ.get("MAX_VALID_YEAR", "2100"))

MIN_DATA_PUBBLICAZIONE = os.environ.get("MIN_DATA_PUBBLICAZIONE", "").strip()
MIN_DATA_PUBBLICAZIONE_SAFETY_DAYS = int(os.environ.get("MIN_DATA_PUBBLICAZIONE_SAFETY_DAYS", "0"))
AUTO_MIN_DATA_PUBBLICAZIONE_FROM_DB = os.environ.get("AUTO_MIN_DATA_PUBBLICAZIONE_FROM_DB", "0") == "1"
STOP_ON_OLD_PUBLICATION = os.environ.get("STOP_ON_OLD_PUBLICATION", "0") == "1"

SLEEP_MIN = float(os.environ.get("SLEEP_MIN", "0.12"))
SLEEP_MAX = float(os.environ.get("SLEEP_MAX", "0.30"))
PAUSE_EVERY_N = int(os.environ.get("PAUSE_EVERY_N", "150"))
PAUSE_SECONDS = float(os.environ.get("PAUSE_SECONDS", "6"))

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://pvp.giustizia.it/pvp/it/lista_annunci.page",
    "Origin": "https://pvp.giustizia.it",
    "User-Agent": "Mozilla/5.0",
}

RICERCA_BODY = {
    "tipoLotto": "IMMOBILI",
    "categoriaBene": [],
    "flagRicerca": 1,
    "nazione": "Italia",
    "localita": "",
    "indirizzo": ""
}

# Ricerca attuale:
# - tutte le vendite IMMOBILI in Italia
# - ordinate per dataOraVendita desc (più future prima)
# - si ferma quando una pagina contiene solo date plausibili nel passato, se STOP_ON_PAST=1

TIPOLOGIA_MAP = {
    "NEGOZIO": "Negozio",
    "APPARTAMENTO": "Appartamento",
    "TERRENO": "Terreno",
    "GARAGE_AUTORIMESSA": "Garage O Autorimessa",
    "ABITAZIONE_TIPO_CIV": "Abitazione Di Tipo Civile",
    "ABITAZIONE_TIPO_POP": "Abitazione Di Tipo Popolare",
    "ABITAZIONE_TIPO_ECO": "Abitazione Di Tipo Economico",
    "ABITAZIONE_TIPO_UPOP": "Abitazione Di Tipo Ultrapopolare",
    "ABITAZIONE_IN_VILLINI": "Abitazione In Villini",
    "ABITAZIONE_IN_VILLE": "Abitazione In Ville",
    "VILLA": "Villa",
    "MAGAZZINI_E_DEPOSITO": "Magazzino E Locale Di Deposito",
    "FABBRICATI_COMMERCIALI": "Fabbricati Costruiti Per Esigenze Commerciali",
    "FABBRICATO_COMMERCIALE": "Fabbricato Commerciale",
    "COMMERCIO_AL_DETTAGLIO": "Attività Commerciale",
    "PREZIOSI": "Preziosi",
}


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_existing_keys_jsonl(path: Path, key_name: str) -> set:
    out = set()
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                v = obj.get(key_name)
                if v is not None:
                    out.add(str(v))
            except Exception:
                continue
    return out


def read_existing_allegati_keys(path: Path) -> set:
    out = set()
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                k = (str(obj.get("pvp_id", "")), str(obj.get("resource_url", "")))
                if k[0] and k[1]:
                    out.add(k)
            except Exception:
                continue
    return out


def read_existing_tuple_keys(path: Path, key_names: List[str]) -> set:
    out = set()
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                k = tuple(str(obj.get(k, "")) for k in key_names)
                if any(k):
                    out.add(k)
            except Exception:
                continue
    return out


def sanitize_text(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        s = " ".join(v.replace("\xa0", " ").split())
        return s if s else None
    return str(v)


def _fmt_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join([str(x) for x in v if x is not None])
    return str(v)


def load_geo_maps():
    province_map: Dict[str, Dict[str, Optional[str]]] = {}
    comune_prov_map: Dict[Tuple[str, str], Dict[str, Optional[str]]] = {}

    if not COMUNI_CSV.exists():
        return province_map, comune_prov_map

    prov_acc = {}

    with open(COMUNI_CSV, "r", encoding="utf-8-sig", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            keys = {str(k).strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}

            comune = keys.get("comune") or keys.get("desccomune") or keys.get("city")
            provincia = keys.get("provincia") or keys.get("descprovincia")
            regione = keys.get("regione") or keys.get("descregione")
            sigla = keys.get("sigla_provincia") or keys.get("siglaprovincia") or keys.get("sigla")
            cap = keys.get("cap")

            if comune and provincia:
                comune_prov_map[(comune.lower(), provincia.lower())] = {
                    "cap": cap or None,
                    "sigla_provincia": sigla or None,
                    "regione": regione or None,
                }

            if provincia:
                p = provincia.lower()
                if p not in prov_acc:
                    prov_acc[p] = {"sigle": set(), "regioni": set()}
                if sigla:
                    prov_acc[p]["sigle"].add(sigla)
                if regione:
                    prov_acc[p]["regioni"].add(regione)

    for prov, acc in prov_acc.items():
        province_map[prov] = {
            "sigla_provincia": next(iter(acc["sigle"])) if len(acc["sigle"]) == 1 else None,
            "regione": next(iter(acc["regioni"])) if len(acc["regioni"]) == 1 else None,
        }

    return province_map, comune_prov_map


PROVINCE_MAP, COMUNE_PROV_MAP = load_geo_maps()


def lookup_geo(comune: Optional[str], provincia: Optional[str], cap: Optional[str]) -> Dict[str, Optional[str]]:
    result = {"regione": None, "sigla_provincia": None, "cap": cap}

    if provincia:
        pinfo = PROVINCE_MAP.get(provincia.lower()) or {}
        result["regione"] = pinfo.get("regione")
        result["sigla_provincia"] = pinfo.get("sigla_provincia")

    if comune and provincia:
        cinfo = COMUNE_PROV_MAP.get((comune.lower(), provincia.lower())) or {}
        if not result["cap"]:
            result["cap"] = cinfo.get("cap") or result["cap"]
        if not result["sigla_provincia"]:
            result["sigla_provincia"] = cinfo.get("sigla_provincia")
        if not result["regione"]:
            result["regione"] = cinfo.get("regione")

    return result


def euro_to_db_text(v: Any) -> Optional[str]:
    if v is None:
        return None
    try:
        num = float(v)
        s = f"{num:,.2f}"
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{s} €"
    except Exception:
        return sanitize_text(v)


def parse_iso_like(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def format_data_asta_db(data_vendita: Optional[str], ora_vendita: Optional[str]) -> Optional[str]:
    dt = parse_iso_like(f"{data_vendita}T{ora_vendita}" if data_vendita and ora_vendita and "T" not in data_vendita else data_vendita)
    if dt:
        return dt.strftime("%d/%m/%Y")
    if data_vendita:
        try:
            dt2 = datetime.strptime(data_vendita, "%Y-%m-%d")
            return dt2.strftime("%d/%m/%Y")
        except Exception:
            return sanitize_text(data_vendita)
    return None


def is_plausible_datetime(dt: Optional[datetime]) -> bool:
    return dt is not None and dt.year <= MAX_VALID_YEAR


def page_should_stop(content: List[Dict[str, Any]]) -> bool:
    plausible = []
    for item in content:
        dt = parse_iso_like(item.get("dataOraVendita"))
        if is_plausible_datetime(dt):
            plausible.append(dt)
    if not plausible:
        return False
    now = datetime.now()
    return all(dt < now for dt in plausible)


def first_plausible_datetime(content: List[Dict[str, Any]]) -> Optional[datetime]:
    for item in content:
        dt = parse_iso_like(item.get("dataOraVendita"))
        if is_plausible_datetime(dt):
            return dt
    return None


def parse_publication_datetime(s: Optional[str]) -> Optional[datetime]:
    dt = parse_iso_like(s)
    if dt:
        return dt
    if not s:
        return None
    raw = str(s).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            continue
    return None


def resolve_min_data_pubblicazione_threshold(session: requests.Session) -> Optional[datetime]:
    raw = MIN_DATA_PUBBLICAZIONE
    threshold = parse_publication_datetime(raw) if raw else None

    if threshold is None and AUTO_MIN_DATA_PUBBLICAZIONE_FROM_DB:
        supabase_url = os.environ.get("SUPABASE_URL", "").strip()
        supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE", "").strip()
        if supabase_url and supabase_key:
            url = f"{supabase_url.rstrip('/')}/rest/v1/listings"
            headers = {
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Accept": "application/json",
            }
            params = {
                "select": "data_pubblicazione_date",
                "data_pubblicazione_date": "not.is.null",
                "order": "data_pubblicazione_date.desc",
                "limit": "1",
            }
            try:
                resp = session.get(url, headers=headers, params=params, timeout=60)
                resp.raise_for_status()
                rows = resp.json() or []
                if rows:
                    threshold = parse_publication_datetime(rows[0].get("data_pubblicazione_date"))
            except Exception as e:
                print(f"[WARN] impossibile leggere la max(data_pubblicazione_date) da Supabase: {e}")

    if threshold and MIN_DATA_PUBBLICAZIONE_SAFETY_DAYS:
        threshold = threshold - timedelta(days=MIN_DATA_PUBBLICAZIONE_SAFETY_DAYS)

    return threshold


def item_is_older_than_publication_threshold(item: Dict[str, Any], threshold: Optional[datetime]) -> bool:
    if threshold is None:
        return False
    pub_dt = parse_publication_datetime(item.get("dataPubblicazione"))
    if pub_dt is None:
        return False
    return pub_dt < threshold


def page_should_stop_on_old_publication(content: List[Dict[str, Any]], threshold: Optional[datetime]) -> bool:
    if threshold is None:
        return False
    publication_dates = []
    for item in content:
        pub_dt = parse_publication_datetime(item.get("dataPubblicazione"))
        if pub_dt is not None:
            publication_dates.append(pub_dt)
    if not publication_dates:
        return False
    return all(pub_dt < threshold for pub_dt in publication_dates)


def build_maps_link(lat, lon, address_text: Optional[str]) -> Optional[str]:
    if lat is not None and lon is not None:
        return f"https://www.google.com/maps?q={lat},{lon}"
    if address_text:
        return f"https://www.google.com/maps/search/?api=1&query={requests.utils.quote(address_text)}"
    return None


def titleize_tipologia(value: Optional[str]) -> Optional[str]:
    v = sanitize_text(value)
    if not v:
        return None
    if v in TIPOLOGIA_MAP:
        return TIPOLOGIA_MAP[v]
    return v.replace("_", " ").title()


def extract_tipologia_from_search(item: Dict[str, Any]) -> Optional[str]:
    cats = item.get("categoriaBene") or []
    if isinstance(cats, list) and cats:
        return titleize_tipologia(cats[0])
    return titleize_tipologia(item.get("categoriaLotto"))


def extract_field_from_beni(detail_body: Dict[str, Any], candidate_keys: List[str], suffix: str = "") -> Optional[str]:
    lotto = detail_body.get("lotto") or {}
    beni = lotto.get("beni") or []
    vals = []
    for b in beni:
        for k in candidate_keys:
            v = sanitize_text((b or {}).get(k))
            if v:
                if suffix and not v.lower().endswith(suffix):
                    v = f"{v} {suffix}"
                vals.append(v)
                break
    vals = list(dict.fromkeys(vals))
    return " + ".join(vals) if vals else None


def join_disponibilita(values: Any) -> Optional[str]:
    if not values:
        return None
    if isinstance(values, list):
        vals = [sanitize_text(x) for x in values]
        vals = [x for x in vals if x]
        vals = list(dict.fromkeys(vals))
        return ", ".join(vals) if vals else None
    return sanitize_text(values)


def build_full_property_address(via, cap, comune, sigla):
    parts = []
    if via:
        parts.append(via)
    tail = " ".join([x for x in [cap, comune] if x]).strip()
    if sigla:
        if tail:
            tail = f"{tail} ({sigla})"
        else:
            tail = f"({sigla})"
    if tail:
        parts.append(tail)
    return ", ".join(parts) if parts else None


def soggetto_full_name(s: Dict[str, Any]) -> Optional[str]:
    parts = [sanitize_text((s or {}).get("nome")), sanitize_text((s or {}).get("cognome"))]
    name = " ".join([p for p in parts if p]).strip()
    return name or sanitize_text((s or {}).get("email"))


def role_tokens(s: Dict[str, Any]) -> str:
    vals = [
        sanitize_text((s or {}).get("ruolo")),
        sanitize_text((s or {}).get("descRuolo")),
        sanitize_text((s or {}).get("codiceRuolo")),
        sanitize_text((s or {}).get("codiceTipoSoggetto")),
        sanitize_text((s or {}).get("tipoSoggetto")),
    ]
    return " | ".join([v.upper() for v in vals if v])


def choose_people(soggetti: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    delegato = None
    custode = None
    named = []

    for s in soggetti or []:
        full_name = soggetto_full_name(s)
        if not full_name:
            continue
        named.append(full_name)
        tokens = role_tokens(s)
        if not delegato and any(tok in tokens for tok in ["DELEG", "PROFDELEG", "DELEGATO", "PROFESSIONISTA"]):
            delegato = full_name
        if not custode and any(tok in tokens for tok in ["CUSTOD", "CUSTODE"]):
            custode = full_name

    uniq_named = list(dict.fromkeys(named))
    if len(uniq_named) == 1:
        if not delegato:
            delegato = uniq_named[0]
        if not custode:
            custode = uniq_named[0]

    return delegato, custode


def normalize_role(role_code: Optional[str], tokens_text: str) -> Tuple[Optional[str], Optional[str], int]:
    rc = (sanitize_text(role_code) or "").upper()
    rt = tokens_text.upper()

    if any(tok in rt for tok in ["DELEG", "PROFDELEG", "DELEGATO", "PROFESSIONISTA"]):
        return "DELEGATO", "Delegato alla vendita", 20
    if any(tok in rt for tok in ["CUSTOD", "CUSTODE"]):
        return "CUSTODE", "Custode", 30
    if any(tok in rt for tok in ["CURATOR", "CURATORE"]):
        return "CURATORE", "Curatore", 40
    if any(tok in rt for tok in ["COMMLIQUID", "COMMISSARIO", "LIQUIDATORE"]):
        return "COMMISSARIO_LIQUIDATORE", "Commissario Liquidatore", 50
    if any(tok in rt for tok in ["SOGGSPV", "SOGGETTO SPECIALIZZATO"]):
        return "SOGGETTO_SPECIALIZZATO", "Soggetto specializzato alla vendita", 60
    if any(tok in rt for tok in ["IVG", "ISTITUTO VENDITE"]):
        return "IVG", "Istituto Vendite Giudiziarie", 70
    if any(tok in rt for tok in ["GIUDICE"]):
        return "GIUDICE", "Giudice", 80

    if rc:
        return rc, rc.replace("_", " ").title(), 999
    if rt:
        label = rt.split(" | ")[0].replace("_", " ").title()
        return label.upper().replace(" ", "_"), label, 999
    return None, None, 999


def build_listing_row(search_item: Dict[str, Any], detail_body: Dict[str, Any]) -> Dict[str, Any]:
    ann_id = str(search_item.get("id") or detail_body.get("idVendita"))
    link_pvp = DETAIL_PAGE_TMPL.format(id_annuncio=ann_id)

    s_addr = search_item.get("indirizzo") or {}
    s_via = sanitize_text(s_addr.get("via"))
    s_num = sanitize_text(s_addr.get("numeroCivico"))
    s_via_full = " ".join([x for x in [s_via, s_num] if x]).strip() or s_via

    comune = sanitize_text(s_addr.get("citta"))
    provincia = sanitize_text(s_addr.get("provincia"))
    cap = sanitize_text(s_addr.get("cap"))

    coords = s_addr.get("coordinate") or {}
    lat = coords.get("latitudine")
    lon = coords.get("longitudine")

    geo = lookup_geo(comune, provincia, cap)
    regione = geo.get("regione")
    sigla_provincia = geo.get("sigla_provincia")
    cap = geo.get("cap")

    indirizzo_txt = build_full_property_address(s_via_full, cap, comune, sigla_provincia)
    address_for_maps = ", ".join([x for x in [s_via_full, cap, comune, provincia, "Italia"] if x])

    procedura = detail_body.get("procedura") or {}
    soggetti = detail_body.get("soggetti") or []
    lotto = detail_body.get("lotto") or {}

    delegato, custode = choose_people(soggetti)

    descrizione = sanitize_text(search_item.get("descLotto")) or sanitize_text(lotto.get("descLotto")) or sanitize_text(detail_body.get("descLotto"))
    lotto_txt = sanitize_text(search_item.get("numeroLotto")) or sanitize_text(detail_body.get("codiLotto")) or sanitize_text(lotto.get("numeroLotto"))

    rialzo_val = search_item.get("rialzoMinimo")
    rialzo_txt = euro_to_db_text(rialzo_val)
    if rialzo_val in (0, 0.0, "0", "0.0"):
        rialzo_txt = ""

    return {
        "pvp_id": ann_id,
        "link_pvp": link_pvp,
        "data_asta": format_data_asta_db(search_item.get("dataVendita"), search_item.get("orarioVendita")),
        "regione": regione,
        "comune": comune,
        "provincia": provincia,
        "sigla_provincia": sigla_provincia,
        "indirizzo": indirizzo_txt,
        "maps_link": build_maps_link(lat, lon, address_for_maps),
        "tipologia": extract_tipologia_from_search(search_item),
        "tribunale": sanitize_text(search_item.get("tribunale")) or sanitize_text(procedura.get("descUfficio")),
        "tipo_procedura": sanitize_text(procedura.get("descTipoRito")) or sanitize_text(procedura.get("descTipoRegistro")),
        "numero_procedura": sanitize_text(procedura.get("numeRg")),
        "anno_procedura": sanitize_text(procedura.get("numeAnnoRg")),
        "disponibilita": join_disponibilita(search_item.get("disponibilita")),
        "lotto": lotto_txt,
        "descrizione": descrizione,
        "prezzo_base": euro_to_db_text(search_item.get("prezzoBaseAsta")),
        "offerta_minima": euro_to_db_text(search_item.get("offertaMinima")),
        "rialzo_minimo": rialzo_txt,
        "delegato_vendita": delegato,
        "custode": custode,
        "vani": extract_field_from_beni(detail_body, ["numeroVani", "vani"]),
        "piano": extract_field_from_beni(detail_body, ["piano", "descPiano", "ubicazionePiano"]),
        "superficie_mq": extract_field_from_beni(detail_body, ["superficie", "superficieCommerciale", "superficieMq"], "mq"),
        "note": None,
        "esperimento_vendita": sanitize_text(detail_body.get("numeEsperimento")),
        "cap": cap,
        "status": "active",
    }


def build_allegati_rows(detail_body: Dict[str, Any]) -> List[Dict[str, Any]]:
    ann_id = str(detail_body.get("idVendita"))
    out = []

    for a in (detail_body.get("allegati") or []):
        link = sanitize_text((a or {}).get("linkAllegato"))
        if not link:
            continue
        if link.startswith("/"):
            link = RESOURCE_HOST + link
        out.append({
            "pvp_id": ann_id,
            "filename": sanitize_text((a or {}).get("nomeFile")),
            "resource_url": link,
            "size_bytes": (a or {}).get("dimensioneAllegato"),
            "mime": None,
            "seen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })

    lotto = detail_body.get("lotto") or {}
    for bene in (lotto.get("beni") or []):
        for a in ((bene or {}).get("allegati") or []):
            link = sanitize_text((a or {}).get("linkAllegato"))
            if not link:
                continue
            if link.startswith("/"):
                link = RESOURCE_HOST + link
            out.append({
                "pvp_id": ann_id,
                "filename": sanitize_text((a or {}).get("nomeFile")),
                "resource_url": link,
                "size_bytes": (a or {}).get("dimensioneAllegato"),
                "mime": None,
                "seen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            })

    seen = set()
    uniq = []
    for r in out:
        k = (r["pvp_id"], r["resource_url"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq


def build_soggetti_rows(detail_body: Dict[str, Any]) -> List[Dict[str, Any]]:
    ann_id = str(detail_body.get("idVendita"))
    soggetti = detail_body.get("soggetti") or []

    out = []
    sort_counter = 100

    for s in soggetti:
        display_name = soggetto_full_name(s)
        email = sanitize_text((s or {}).get("email"))
        telefono = sanitize_text((s or {}).get("telefono")) or sanitize_text((s or {}).get("telefono1"))
        nome = sanitize_text((s or {}).get("nome"))
        cognome = sanitize_text((s or {}).get("cognome"))

        tokens = role_tokens(s)
        role_code, role_label, default_sort = normalize_role(
            sanitize_text((s or {}).get("ruolo")),
            tokens,
        )

        sort_order = default_sort if default_sort != 999 else sort_counter
        sort_counter += 1

        if not display_name and not email:
            continue

        out.append({
            "pvp_id": ann_id,
            "role_code": role_code,
            "role_label": role_label,
            "nome": nome,
            "cognome": cognome,
            "display_name": display_name,
            "email": email,
            "telefono": telefono,
            "sort_order": sort_order,
        })

    seen = set()
    uniq = []
    for r in out:
        k = (
            str(r.get("pvp_id", "")),
            str(r.get("role_code", "") or ""),
            str(r.get("display_name", "") or ""),
            str(r.get("email", "") or ""),
        )
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)

    return uniq


def build_raw_text(search_item: Dict[str, Any], detail_body: Dict[str, Any]) -> str:
    lines: List[str] = []

    ann_id = str(search_item.get("id") or detail_body.get("idVendita") or "")
    if ann_id:
        lines.append(f"PVP ID: {ann_id}")

    lines.append("=== SEARCH ITEM ===")
    for key in [
        "tipoLotto", "categoriaLotto", "numeroLotto", "procedura",
        "prezzoBaseAsta", "offertaMinima", "rialzoMinimo",
        "dataVendita", "orarioVendita", "dataOraVendita",
        "dataPubblicazione", "tribunale", "codiceTribunale"
    ]:
        val = search_item.get(key)
        if val is not None:
            lines.append(f"{key}: {_fmt_value(val)}")

    s_addr = search_item.get("indirizzo") or {}
    if s_addr:
        lines.append("Indirizzo ricerca:")
        for k, label in [
            ("via", "Via"),
            ("numeroCivico", "Numero civico"),
            ("cap", "CAP"),
            ("citta", "Comune"),
            ("provincia", "Provincia"),
        ]:
            if s_addr.get(k) is not None:
                lines.append(f"{label}: {_fmt_value(s_addr.get(k))}")
        coords = s_addr.get("coordinate") or {}
        if coords:
            if coords.get("latitudine") is not None:
                lines.append(f"Latitudine: {coords.get('latitudine')}")
            if coords.get("longitudine") is not None:
                lines.append(f"Longitudine: {coords.get('longitudine')}")

    if search_item.get("categoriaBene"):
        lines.append(f"categoriaBene: {_fmt_value(search_item.get('categoriaBene'))}")
    if search_item.get("disponibilita"):
        lines.append(f"Disponibilità: {_fmt_value(search_item.get('disponibilita'))}")
    if search_item.get("descLotto"):
        lines.append(f"Descrizione lotto: {_fmt_value(search_item.get('descLotto'))}")

    lines.append("")
    lines.append("=== DETAIL BODY ===")

    procedura = detail_body.get("procedura") or {}
    if procedura:
        lines.append("Procedura:")
        for k in ["descUfficio", "descTipoRito", "descTipoRegistro", "numeRg", "numeAnnoRg"]:
            if procedura.get(k) is not None:
                lines.append(f"{k}: {_fmt_value(procedura.get(k))}")

    indirizzo = detail_body.get("indirizzo") or {}
    if indirizzo:
        lines.append("Indirizzo dettaglio:")
        for k in ["via", "numeroCivico", "cap", "descComune", "descProvincia", "descRegione", "codProvincia"]:
            if indirizzo.get(k) is not None:
                lines.append(f"{k}: {_fmt_value(indirizzo.get(k))}")

    lotto = detail_body.get("lotto") or {}
    if lotto:
        lines.append("Lotto:")
        for k in ["numeroLotto", "descLotto"]:
            if lotto.get(k) is not None:
                lines.append(f"{k}: {_fmt_value(lotto.get(k))}")

        beni = lotto.get("beni") or []
        if beni:
            lines.append("Beni inclusi nel lotto")
            for i, bene in enumerate(beni, start=1):
                lines.append(f"-- Bene {i} --")
                for k in [
                    "descTipologiaBene", "codTipologiaBene", "superficie",
                    "superficieCommerciale", "superficieMq", "numeroVani",
                    "vani", "piano", "descPiano", "ubicazionePiano",
                    "indirizzo", "descBene"
                ]:
                    if bene.get(k) is not None:
                        lines.append(f"{k}: {_fmt_value(bene.get(k))}")

                b_addr = bene.get("indirizzo") or {}
                if b_addr:
                    lines.append("indirizzo_bene:")
                    for k in ["via", "numeroCivico", "cap", "descComune", "descProvincia", "descRegione", "codProvincia"]:
                        if b_addr.get(k) is not None:
                            lines.append(f"  {k}: {_fmt_value(b_addr.get(k))}")

    soggetti = detail_body.get("soggetti") or []
    if soggetti:
        lines.append("Referenti")
        for i, s in enumerate(soggetti, start=1):
            lines.append(f"-- Soggetto {i} --")
            for k in ["ruolo", "descRuolo", "codiceRuolo", "codiceTipoSoggetto", "tipoSoggetto", "nome", "cognome", "email", "telefono", "telefono1"]:
                if s.get(k) is not None:
                    lines.append(f"{k}: {_fmt_value(s.get(k))}")

    allegati = detail_body.get("allegati") or []
    if allegati:
        lines.append("Allegati")
        for a in allegati:
            parts = [sanitize_text(a.get("nomeFile")), sanitize_text(a.get("linkAllegato"))]
            parts = [p for p in parts if p]
            if parts:
                lines.append(" - " + " | ".join(parts))

    return "\n".join(lines).strip()


def build_raw_row(search_item: Dict[str, Any], detail_body: Dict[str, Any]) -> Dict[str, Any]:
    ann_id = str(search_item.get("id") or detail_body.get("idVendita"))
    return {
        "pvp_id": ann_id,
        "link_pvp": DETAIL_PAGE_TMPL.format(id_annuncio=ann_id),
        "raw_text": build_raw_text(search_item, detail_body),
        "search_item": search_item,
        "detail_body": detail_body,
    }


def fetch_search_page(session: requests.Session, page: int, size: int) -> Dict[str, Any]:
    params = [
        ("language", "it"),
        ("page", str(page)),
        ("size", str(size)),
        ("sort", "dataOraVendita,desc"),
        ("sort", "citta,asc"),
    ]
    r = session.post(RICERCA_URL, params=params, headers=HEADERS, json=RICERCA_BODY, timeout=120)
    r.raise_for_status()
    return r.json()


def fetch_detail(session: requests.Session, ann_id: str) -> Dict[str, Any]:
    url = DETAIL_URL_TMPL.format(id_annuncio=ann_id)
    r = session.get(url, headers=HEADERS, timeout=120)
    r.raise_for_status()
    return r.json()


def main():
    # Usiamo il RAW come riferimento principale per capire cosa è già stato processato
    existing_pvp_ids = read_existing_keys_jsonl(OUT_RAW_JSONL, "pvp_id")
    existing_allegati = read_existing_allegati_keys(OUT_ALLEGATI_JSONL)
    existing_soggetti = read_existing_tuple_keys(
        OUT_SOGGETTI_JSONL,
        ["pvp_id", "role_code", "display_name", "email"]
    )
    existing_raw = read_existing_keys_jsonl(OUT_RAW_JSONL, "pvp_id")

    session = requests.Session()
    min_data_pubblicazione_threshold = resolve_min_data_pubblicazione_threshold(session)

    page = START_PAGE
    pages_done = 0
    details_done = 0
    search_items = 0
    listings_written = 0
    allegati_written = 0
    soggetti_written = 0
    raw_written = 0

    print("API pipeline FINAL FIXED + SOGGETTI + RAW")
    print("PAGE_SIZE:", PAGE_SIZE)
    print("START_PAGE:", START_PAGE)
    print("STOP_ON_PAST:", int(STOP_ON_PAST))
    print("MAX_VALID_YEAR:", MAX_VALID_YEAR)
    print("STOP_ON_OLD_PUBLICATION:", int(STOP_ON_OLD_PUBLICATION))
    print("AUTO_MIN_DATA_PUBBLICAZIONE_FROM_DB:", int(AUTO_MIN_DATA_PUBBLICAZIONE_FROM_DB))
    print("MIN_DATA_PUBBLICAZIONE:", MIN_DATA_PUBBLICAZIONE or "(vuota)")
    print("MIN_DATA_PUBBLICAZIONE_SAFETY_DAYS:", MIN_DATA_PUBBLICAZIONE_SAFETY_DAYS)
    print("effective MIN_DATA_PUBBLICAZIONE threshold:", min_data_pubblicazione_threshold.isoformat() if min_data_pubblicazione_threshold else "None")

    while True:
        if MAX_PAGES and pages_done >= MAX_PAGES:
            print("Stop: raggiunto MAX_PAGES")
            break

        print(f"[search page {page}] ...")
        try:
            data = fetch_search_page(session, page=page, size=PAGE_SIZE)
        except Exception as e:
            append_jsonl(OUT_ERRORS_JSONL, {
                "stage": "search",
                "page": page,
                "error": f"{type(e).__name__}: {e}",
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
            print("  search error:", e)
            break

        body = data.get("body") or {}
        content = body.get("content") or []

        if not content:
            print("Stop: pagina vuota")
            break

        first_dt_any = content[0].get("dataOraVendita")
        first_dt_plausible = first_plausible_datetime(content)
        first_pub_any = content[0].get("dataPubblicazione")
        first_pub_dt = parse_publication_datetime(first_pub_any)
        print("  first raw dataOraVendita:", first_dt_any)
        print("  first plausible dataOraVendita:", first_dt_plausible.isoformat() if first_dt_plausible else "None")
        print("  first raw dataPubblicazione:", first_pub_any)
        print("  first parsed dataPubblicazione:", first_pub_dt.isoformat() if first_pub_dt else "None")

        page_stop_after_processing = STOP_ON_PAST and page_should_stop(content)
        page_stop_after_publication = STOP_ON_OLD_PUBLICATION and page_should_stop_on_old_publication(content, min_data_pubblicazione_threshold)

        page_new = 0
        page_skipped = 0
        page_skipped_old_publication = 0

        for item in content:
            ann_id = str(item.get("id")) if item.get("id") is not None else None
            if not ann_id:
                continue

            search_items += 1

            if MAX_DETAILS and details_done >= MAX_DETAILS:
                print("Stop: raggiunto MAX_DETAILS")
                break

            if item_is_older_than_publication_threshold(item, min_data_pubblicazione_threshold):
                page_skipped_old_publication += 1
                continue

            if ann_id in existing_pvp_ids:
                page_skipped += 1
                continue

            try:
                detail = fetch_detail(session, ann_id)
                detail_body = detail.get("body") or {}
                if not detail_body:
                    append_jsonl(OUT_ERRORS_JSONL, {
                        "stage": "detail",
                        "pvp_id": ann_id,
                        "error": "body vuoto",
                        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                    })
                    continue

                # listings_api.jsonl non viene più scritto: il parser finale legge il RAW
                existing_pvp_ids.add(ann_id)
                listings_written += 1
                page_new += 1

                if ann_id not in existing_raw:
                    raw_row = build_raw_row(item, detail_body)
                    append_jsonl(OUT_RAW_JSONL, raw_row)
                    existing_raw.add(ann_id)
                    raw_written += 1

                allegati = build_allegati_rows(detail_body)
                for a in allegati:
                    k = (str(a.get("pvp_id", "")), str(a.get("resource_url", "")))
                    if k in existing_allegati:
                        continue
                    append_jsonl(OUT_ALLEGATI_JSONL, a)
                    existing_allegati.add(k)
                    allegati_written += 1

                soggetti = build_soggetti_rows(detail_body)
                for s in soggetti:
                    k = (
                        str(s.get("pvp_id", "")),
                        str(s.get("role_code", "") or ""),
                        str(s.get("display_name", "") or ""),
                        str(s.get("email", "") or ""),
                    )
                    if k in existing_soggetti:
                        continue
                    append_jsonl(OUT_SOGGETTI_JSONL, s)
                    existing_soggetti.add(k)
                    soggetti_written += 1

                details_done += 1

            except Exception as e:
                append_jsonl(OUT_ERRORS_JSONL, {
                    "stage": "detail",
                    "pvp_id": ann_id,
                    "error": f"{type(e).__name__}: {e}",
                    "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                })

            if details_done and (details_done % PAUSE_EVERY_N) == 0:
                print(f"  pausa {PAUSE_SECONDS}s ogni {PAUSE_EVERY_N} dettagli")
                time.sleep(PAUSE_SECONDS)
            else:
                time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

        print(
            f"  page summary -> search:{len(content)} new_details:{page_new} "
            f"skipped_existing:{page_skipped} skipped_old_publication:{page_skipped_old_publication}"
        )

        if MAX_DETAILS and details_done >= MAX_DETAILS:
            break

        if page_stop_after_publication:
            print("Stop: nella pagina tutte le dataPubblicazione valide risultano precedenti alla soglia minima")
            break

        if page_stop_after_processing:
            print("Stop: nella pagina tutte le date plausibili risultano nel passato")
            break

        page += 1
        pages_done += 1

    print("\nDONE")
    print("search_items_seen:", search_items)
    print("details_processed:", listings_written)
    print("raw_written:", raw_written)
    print("allegati_written:", allegati_written)
    print("soggetti_written:", soggetti_written)
    print("listings_api_raw.jsonl:", OUT_RAW_JSONL)
    print("allegati_links.jsonl:", OUT_ALLEGATI_JSONL)
    print("listing_soggetti.jsonl:", OUT_SOGGETTI_JSONL)
    print("api_errors.jsonl:", OUT_ERRORS_JSONL)


if __name__ == "__main__":
    main()