# Aste Easy Google Geocoding v9.3.6 - primary-civic parser guard + multi-segment/semantic/API budget guards
import csv
import difflib
import json
import os
import sys
import re
import time
import unicodedata
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
SRC_ROOT = BASE_DIR.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from aste_paths import CACHE_DIR, REPORTS_DIR, ensure_runtime_dirs, env_path

ensure_runtime_dirs()
CACHE_FILE = env_path("GOOGLE_GEOCODE_CACHE_FILE", CACHE_DIR / "google_geocode_cache_v8.json", relative_to=CACHE_DIR)
LOG_FILE = env_path("GOOGLE_GEOCODE_LOG_FILE", REPORTS_DIR / "google_geocode_log_v8.jsonl", relative_to=REPORTS_DIR)
AUDIT_JSONL = env_path("GOOGLE_GEOCODE_AUDIT_JSONL", REPORTS_DIR / "google_geocode_audit_v8.jsonl", relative_to=REPORTS_DIR)
AUDIT_CSV = env_path("GOOGLE_GEOCODE_AUDIT_CSV", REPORTS_DIR / "google_geocode_audit_v8.csv", relative_to=REPORTS_DIR)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE = os.environ.get("SUPABASE_SERVICE_ROLE", "").strip()
GOOGLE_GEOCODING_API_KEY = os.environ.get("GOOGLE_GEOCODING_API_KEY", "").strip()

# missing      -> geocodifica solo coordinate mancanti
# approximate  -> ritenta i record Google APPROXIMATE
# both         -> missing + approximate
# audit        -> valida le coordinate esistenti (default: solo coordinate PVP/non validate), NON scrive sul DB
# repair       -> come audit, ma corregge solo mismatch sicuri con candidato forward ad alta confidenza
GEOCODE_MODE = os.environ.get("GEOCODE_MODE", "audit").strip().lower()
GEOCODE_LIMIT = int(os.environ.get("GEOCODE_LIMIT", "0") or "0")
GEOCODE_PVP_ID = os.environ.get("GEOCODE_PVP_ID", "").strip()
GEOCODE_AUDIT_SCOPE = os.environ.get("GEOCODE_AUDIT_SCOPE", "pvp").strip().lower()  # pvp | all
GEOCODE_PAGE_SIZE = max(50, min(1000, int(os.environ.get("GEOCODE_PAGE_SIZE", "1000") or "1000")))
GEOCODE_SLEEP_SECONDS = float(os.environ.get("GEOCODE_SLEEP_SECONDS", "0.05") or "0.05")
GEOCODE_TIMEOUT = int(os.environ.get("GEOCODE_TIMEOUT", "60") or "60")
HTTP_RETRIES = int(os.environ.get("HTTP_RETRIES", "5") or "5")
HTTP_RETRY_SLEEP = float(os.environ.get("HTTP_RETRY_SLEEP", "2") or "2")
FORWARD_MAX_RESULTS = max(1, min(10, int(os.environ.get("FORWARD_MAX_RESULTS", "8") or "8")))
REPAIR_MIN_SCORE = float(os.environ.get("REPAIR_MIN_SCORE", "82") or "82")
ALLOW_LOCALITY_FALLBACK = os.environ.get("ALLOW_LOCALITY_FALLBACK", "1") == "1"
MARK_VERIFIED_PVP = os.environ.get("MARK_VERIFIED_PVP", "0") == "1"
CACHE_SAVE_EVERY = max(1, int(os.environ.get("CACHE_SAVE_EVERY", "25") or "25"))
PLACES_AUTOCOMPLETE_FALLBACK = os.environ.get("PLACES_AUTOCOMPLETE_FALLBACK", "1") == "1"
PLACES_AUTOCOMPLETE_RADIUS_M = max(1000.0, min(50000.0, float(os.environ.get("PLACES_AUTOCOMPLETE_RADIUS_M", "15000") or "15000")))
PLACES_AUTOCOMPLETE_MAX = max(1, min(5, int(os.environ.get("PLACES_AUTOCOMPLETE_MAX", "5") or "5")))
ADDRESS_VALIDATION_FALLBACK = os.environ.get("ADDRESS_VALIDATION_FALLBACK", "1") == "1"

# v9.0: repair a batch persistenti senza riprocessare gli irrisolti.
REPAIR_CURSOR_ENABLED = os.environ.get("REPAIR_CURSOR_ENABLED", "1") == "1"
REPAIR_CURSOR_RESET = os.environ.get("REPAIR_CURSOR_RESET", "0") == "1"
REPAIR_CURSOR_FILE = env_path("REPAIR_CURSOR_FILE", CACHE_DIR / "google_geocode_repair_cursor_v9.json", relative_to=CACHE_DIR)
# Scrive solo metadati di qualità per le coordinate PVP non corrette.
# Non valorizza geocode_source: l'importer continuerà quindi a proteggere
# soltanto coordinate davvero validate/corrette.
WRITE_AUDIT_MAP_STATUS = os.environ.get("WRITE_AUDIT_MAP_STATUS", "1") == "1"

# v9.2: contatori reali delle richieste Google che escono dalla cache.
# v9.3.1: cap MENSILI persistenti + lettura usage robusta (UTF-8 BOM-safe) e fail-closed
# v9.3.2: guard comuni omonimi: stesso comune ma provincia+CAP entrambi incompatibili = mismatch
# v9.3.3: guard semantico toponomastica: evita falsi match quando un token distintivo viene sostituito
#         (es. Mia Martini -> Arcivescovo Antonio Martini; Cugno Lupo -> Cugno Vasco).
# v9.3.4: guard indirizzi multi-segmento: una sola via coincidente non basta se il PVP
#         dichiara anche altre strade/contrade/localita significative non confermate.
# offset esterni/manuali per allineare il contatore locale a Google Cloud.
# Manteniamo lo stesso file v9.2 per non perdere il cumulativo già raccolto.
API_USAGE_FILE = env_path("GOOGLE_API_USAGE_FILE", CACHE_DIR / "google_api_usage_v9_2.json", relative_to=CACHE_DIR)
MAX_RUN_GEOCODING_CALLS = max(0, int(os.environ.get("MAX_RUN_GEOCODING_CALLS", "0") or "0"))
MAX_RUN_AUTOCOMPLETE_CALLS = max(0, int(os.environ.get("MAX_RUN_AUTOCOMPLETE_CALLS", "0") or "0"))
MAX_RUN_ADDRESS_VALIDATION_CALLS = max(0, int(os.environ.get("MAX_RUN_ADDRESS_VALIDATION_CALLS", "0") or "0"))
MAX_MONTH_GEOCODING_CALLS = max(0, int(os.environ.get("MAX_MONTH_GEOCODING_CALLS", "0") or "0"))
MAX_MONTH_AUTOCOMPLETE_CALLS = max(0, int(os.environ.get("MAX_MONTH_AUTOCOMPLETE_CALLS", "0") or "0"))
MAX_MONTH_ADDRESS_VALIDATION_CALLS = max(0, int(os.environ.get("MAX_MONTH_ADDRESS_VALIDATION_CALLS", "0") or "0"))
# Stringa vuota = conserva l'offset già salvato nel file usage per il mese.
MONTH_USAGE_OFFSET_GEOCODING_RAW = os.environ.get("MONTH_USAGE_OFFSET_GEOCODING", "").strip()
MONTH_USAGE_OFFSET_AUTOCOMPLETE_RAW = os.environ.get("MONTH_USAGE_OFFSET_AUTOCOMPLETE", "").strip()
MONTH_USAGE_OFFSET_ADDRESS_VALIDATION_RAW = os.environ.get("MONTH_USAGE_OFFSET_ADDRESS_VALIDATION", "").strip()
API_USAGE_SAVE_EVERY = max(1, int(os.environ.get("API_USAGE_SAVE_EVERY", "10") or "10"))

TRANSIENT_GOOGLE_STATUSES = {
    "REQUEST_DENIED", "OVER_QUERY_LIMIT", "OVER_DAILY_LIMIT", "UNKNOWN_ERROR",
}


class ApiBudgetExceeded(RuntimeError):
    pass


class ApiUsageTracker:
    SKUS = ("geocoding", "places_autocomplete", "address_validation")

    def __init__(self, path: Path):
        self.path = path
        self.run = {sku: {"logical_requests": 0, "network_attempts": 0, "cache_hits": 0, "retries": 0} for sku in self.SKUS}
        self.operations = {sku: {} for sku in self.SKUS}
        self.persisted = self._load()
        self._dirty_attempts = 0
        self._apply_external_offsets_from_env()

    def _month_key(self):
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _load(self):
        if self.path.exists():
            try:
                # utf-8-sig accetta sia UTF-8 normale sia file con BOM (tipico di Windows PowerShell).
                raw = self.path.read_text(encoding="utf-8-sig")
                obj = json.loads(raw)
                if not isinstance(obj, dict):
                    raise ValueError("la radice JSON non e' un oggetto")
                return obj
            except Exception as exc:
                # Fail closed: un contatore illeggibile NON deve mai essere azzerato in silenzio,
                # altrimenti il safety cap mensile perde significato.
                raise SystemExit(
                    f"[API USAGE] ERRORE: impossibile leggere {self.path}: {exc}. "
                    "Il file NON viene sovrascritto e i contatori NON vengono azzerati. "
                    "Ripristina/correggi il JSON prima di rilanciare."
                )
        return {
            "version": 2,
            "note": "Local Google API counter from geocoder v9.2 onward; v9.3.6 adds primary-civic parser guard; v9.3.3 added semantic street-token substitution guard; v9.3.2 added homonymous-municipality province+CAP guard on top of BOM-safe monthly caps and offsets. Google Cloud remains billing source of truth.",
            "months": {},
        }

    def _month(self):
        self.persisted["version"] = max(2, int(self.persisted.get("version", 1) or 1))
        self.persisted["note"] = (
            "Local Google API counter from geocoder v9.2 onward; v9.3.6 adds primary-civic parser guard on top of v9.3.4 multi-segment and v9.3.3 semantic street-token guard; v9.3.2 homonymous-municipality guard, monthly caps, external offsets "
            "and BOM-safe fail-closed loading. Google Cloud remains billing source of truth."
        )
        months = self.persisted.setdefault("months", {})
        month = months.setdefault(self._month_key(), {})
        for sku in self.SKUS:
            d = month.setdefault(sku, {})
            d.setdefault("network_attempts", 0)
            d.setdefault("external_offset", 0)
        return month

    def _offset_env_values(self):
        return {
            "geocoding": MONTH_USAGE_OFFSET_GEOCODING_RAW,
            "places_autocomplete": MONTH_USAGE_OFFSET_AUTOCOMPLETE_RAW,
            "address_validation": MONTH_USAGE_OFFSET_ADDRESS_VALIDATION_RAW,
        }

    def _apply_external_offsets_from_env(self):
        month = self._month()
        changed = False
        for sku, raw in self._offset_env_values().items():
            if raw == "":
                continue
            try:
                value = max(0, int(raw))
            except ValueError:
                raise SystemExit(f"Offset mensile non valido per {sku}: {raw!r}")
            if int(month[sku].get("external_offset", 0) or 0) != value:
                month[sku]["external_offset"] = value
                changed = True
        if changed:
            self.save()

    def _op(self, sku, operation):
        d = self.operations.setdefault(sku, {})
        return d.setdefault(operation or "other", {"logical_requests": 0, "network_attempts": 0, "cache_hits": 0})

    def cache_hit(self, sku, operation):
        self.run[sku]["cache_hits"] += 1
        self._op(sku, operation)["cache_hits"] += 1

    def logical_request(self, sku, operation):
        self.run[sku]["logical_requests"] += 1
        self._op(sku, operation)["logical_requests"] += 1

    def _run_limit_for(self, sku):
        return {
            "geocoding": MAX_RUN_GEOCODING_CALLS,
            "places_autocomplete": MAX_RUN_AUTOCOMPLETE_CALLS,
            "address_validation": MAX_RUN_ADDRESS_VALIDATION_CALLS,
        }[sku]

    def _month_limit_for(self, sku):
        return {
            "geocoding": MAX_MONTH_GEOCODING_CALLS,
            "places_autocomplete": MAX_MONTH_AUTOCOMPLETE_CALLS,
            "address_validation": MAX_MONTH_ADDRESS_VALIDATION_CALLS,
        }[sku]

    def month_usage(self, sku):
        d = self._month()[sku]
        local = int(d.get("network_attempts", 0) or 0)
        offset = int(d.get("external_offset", 0) or 0)
        return local, offset, local + offset

    def before_network_attempt(self, sku, operation, attempt_index):
        run_limit = self._run_limit_for(sku)
        run_current = self.run[sku]["network_attempts"]
        if run_limit and run_current >= run_limit:
            raise ApiBudgetExceeded(
                f"Safety cap PER ESECUZIONE raggiunto per {sku}: {run_current}/{run_limit} chiamate HTTP. "
                "Il record corrente NON viene avanzato nel cursore."
            )

        month_limit = self._month_limit_for(sku)
        month_local, month_offset, month_effective = self.month_usage(sku)
        if month_limit and month_effective >= month_limit:
            raise ApiBudgetExceeded(
                f"Safety cap MENSILE raggiunto per {sku}: uso effettivo locale+offset "
                f"{month_effective}/{month_limit} (locale={month_local}, offset={month_offset}). "
                "Il record corrente NON viene avanzato nel cursore."
            )

        self.run[sku]["network_attempts"] += 1
        if attempt_index > 0:
            self.run[sku]["retries"] += 1
        self._op(sku, operation)["network_attempts"] += 1
        month = self._month()
        month[sku]["network_attempts"] = int(month[sku].get("network_attempts", 0) or 0) + 1
        self._dirty_attempts += 1
        if self._dirty_attempts >= API_USAGE_SAVE_EVERY:
            self.save()

    def save(self):
        self.persisted["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.persisted, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        self._dirty_attempts = 0

    def one_line(self):
        parts = []
        labels = {"geocoding": "Geocoding", "places_autocomplete": "Autocomplete", "address_validation": "AddressValidation"}
        for sku in self.SKUS:
            d = self.run[sku]
            _, _, month_effective = self.month_usage(sku)
            month_limit = self._month_limit_for(sku)
            month_txt = f"{month_effective}/{month_limit}" if month_limit else str(month_effective)
            parts.append(
                f"{labels[sku]} net={d['network_attempts']} cache={d['cache_hits']} month={month_txt}"
            )
        return " | ".join(parts)

    def print_summary(self):
        print("[API USAGE v9.3.6 - questa esecuzione]")
        labels = {"geocoding": "Geocoding", "places_autocomplete": "Autocomplete (New)", "address_validation": "Address Validation"}
        for sku in self.SKUS:
            d = self.run[sku]
            print(
                f"  {labels[sku]}: network_attempts={d['network_attempts']} "
                f"logical_requests={d['logical_requests']} cache_hits={d['cache_hits']} retries={d['retries']}"
            )
            ops = self.operations.get(sku) or {}
            active = []
            for op, od in sorted(ops.items()):
                if od.get("network_attempts") or od.get("cache_hits"):
                    active.append(
                        f"{op}:net={od.get('network_attempts', 0)},cache={od.get('cache_hits', 0)}"
                    )
            if active:
                print("    breakdown: " + " | ".join(active))

        print(f"[API USAGE locale cumulativo {self._month_key()} - v9.2+ con offset esterno v9.3.6]")
        for sku in self.SKUS:
            local, offset, effective = self.month_usage(sku)
            limit = self._month_limit_for(sku)
            if limit:
                remaining = max(0, limit - effective)
                cap_text = f"cap={limit} remaining={remaining}"
            else:
                cap_text = "cap=OFF"
            print(
                f"  {labels[sku]}: locale={local} offset_esterno={offset} "
                f"uso_effettivo={effective} {cap_text}"
            )
        print("  NOTA: l'offset serve a includere consumo precedente alla v9.2 o prodotto da altri client/progetti.")
        print("  Per il consumo fatturabile ufficiale fa fede Google Cloud Billing/Monitoring.")


API_USAGE = ApiUsageTracker(API_USAGE_FILE)

STREET_PREFIX_RE = re.compile(
    r"\b(via|viale|piazza|corso|largo|vicolo|strada|contrada|localit[àa]|frazione|"
    r"s\.?\s*p\.?|strada\s+provinciale|s\.?\s*s\.?|strada\s+statale)\b",
    re.IGNORECASE,
)

GENERIC_TOKENS = {
    "via", "viale", "piazza", "corso", "largo", "vicolo", "strada", "contrada",
    "localita", "frazione", "provinciale", "statale", "della", "dello", "delle", "degli",
    "dei", "del", "di", "da", "in", "al", "alla", "alle", "allo", "sul", "sulla",
    "n", "nr", "numero", "civico", "civ", "snc", "italia", "italy", "loc", "fraz",
    "dott", "dottore", "prof", "professore", "avv", "avvocato", "ing", "ingegnere",
}

MUNICIPALITY_TYPES = {
    "locality", "postal_town", "administrative_area_level_3", "sublocality", "sublocality_level_1",
}
PROVINCE_TYPES = {"administrative_area_level_2"}
PLACE_DETAIL_TYPES = {
    "route", "neighborhood", "sublocality", "sublocality_level_1", "sublocality_level_2",
    "premise", "establishment", "point_of_interest",
}


def clean(v):
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v).replace("\u00A0", " ")).strip()


def normalize_text(s: str) -> str:
    s = clean(s).lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def has_valid_coords(lat, lon):
    try:
        lat = float(lat)
        lon = float(lon)
        return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)
    except Exception:
        return False


def dedup_keep_order(items):
    out = []
    seen = set()
    for item in items:
        item = clean(item)
        if not item:
            continue
        key = normalize_text(item)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def urlopen_retry(req, timeout=60, retries=5, sleep=2, usage_sku=None, usage_operation=None):
    for attempt in range(retries):
        try:
            if usage_sku:
                API_USAGE.before_network_attempt(usage_sku, usage_operation, attempt)
            return urlopen(req, timeout=timeout)
        except ApiBudgetExceeded:
            raise
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                print(f"[RETRY] HTTP {e.code} retry {attempt + 1}/{retries}")
                time.sleep(sleep)
                continue
            raise
        except Exception:
            if attempt < retries - 1:
                print(f"[RETRY] network error retry {attempt + 1}/{retries}")
                time.sleep(sleep)
                continue
            raise


class CacheStore:
    def __init__(self, path: Path):
        self.path = path
        self.data = {}
        self.dirty = 0
        if path.exists():
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    self.data = obj
            except Exception:
                self.data = {}

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        self.data[key] = value
        self.dirty += 1
        if self.dirty >= CACHE_SAVE_EVERY:
            self.save()

    def save(self):
        if not self.dirty:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        self.dirty = 0


def db_headers():
    return {
        "apikey": SUPABASE_SERVICE_ROLE,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def http_json(method: str, url: str, payload=None):
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, headers=db_headers(), method=method)
    with urlopen_retry(req, timeout=GEOCODE_TIMEOUT, retries=HTTP_RETRIES, sleep=HTTP_RETRY_SLEEP) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else None



def reset_repair_cursor_if_requested():
    if not REPAIR_CURSOR_ENABLED or GEOCODE_MODE != "repair" or GEOCODE_PVP_ID:
        return
    if REPAIR_CURSOR_RESET and REPAIR_CURSOR_FILE.exists():
        try:
            REPAIR_CURSOR_FILE.unlink()
            print(f"[INFO] repair cursor azzerato: {REPAIR_CURSOR_FILE}")
        except OSError:
            pass


def load_repair_cursor():
    if not REPAIR_CURSOR_ENABLED or GEOCODE_MODE != "repair" or GEOCODE_PVP_ID:
        return ""
    if not REPAIR_CURSOR_FILE.exists():
        return ""
    try:
        data = json.loads(REPAIR_CURSOR_FILE.read_text(encoding="utf-8"))
        return clean(data.get("last_pvp_id"))
    except Exception:
        return ""


def _pvp_id_int(value):
    """Converti pvp_id numerico salvato come TEXT; None se non strettamente numerico."""
    s = clean(value)
    if not s or not re.fullmatch(r"\d+", s):
        return None
    try:
        return int(s)
    except Exception:
        return None


def save_repair_cursor(pvp_id):
    if not REPAIR_CURSOR_ENABLED or GEOCODE_MODE != "repair" or GEOCODE_PVP_ID:
        return
    pvp_id = clean(pvp_id)
    if not pvp_id:
        return

    # v10.1.2: pvp_id e' TEXT in Supabase. Impedisci regressioni lessicografiche
    # tipo 4624652 -> 591541, che numericamente tornerebbero indietro.
    current = load_repair_cursor()
    cur_n = _pvp_id_int(current)
    new_n = _pvp_id_int(pvp_id)
    if cur_n is not None and new_n is not None and new_n < cur_n:
        print(
            f"[CURSOR GUARD] IGNORATO pvp_id regressivo {pvp_id} < {current} "
            "(confronto numerico)."
        )
        return

    payload = {
        "last_pvp_id": pvp_id,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    tmp = REPAIR_CURSOR_FILE.with_suffix(REPAIR_CURSOR_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REPAIR_CURSOR_FILE)


def fetch_paged(base_url: str):
    out = []
    offset = 0
    while True:
        sep = "&" if "?" in base_url else "?"
        url = f"{base_url}{sep}limit={GEOCODE_PAGE_SIZE}&offset={offset}"
        page = http_json("GET", url) or []
        if not isinstance(page, list):
            raise RuntimeError("Risposta Supabase inattesa durante fetch paginato")
        out.extend(page)
        if len(page) < GEOCODE_PAGE_SIZE:
            break
        offset += len(page)
        if GEOCODE_LIMIT > 0 and len(out) >= GEOCODE_LIMIT:
            break
    if GEOCODE_LIMIT > 0:
        out = out[:GEOCODE_LIMIT]
    return out


REPAIR_PROCESSED_STATUSES = {"PVP_OK_CIVICO", "PVP_OK_STRADA", "PVP_SUSPECT", "PVP_UNCERTAIN"}


def _coords_from_check_marker(query_used):
    """Estrae le coordinate da reverse:/repair_checked:/anncsu_checked:, se presenti."""
    q = clean(query_used)
    m = re.match(r"^(?:reverse|repair_checked|anncsu_checked):\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)$", q, re.I)
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except Exception:
        return None


def repair_status_marker_is_current(row):
    """True se uno stato PVP_* dimostra che il record e' gia' stato controllato.

    v10.1.3:
    - gli stati PVP_* sono un marker persistente di audit/repair;
    - se geocode_query_used contiene le coordinate controllate, devono coincidere
      con quelle correnti. Se l'importer ha cambiato lat/lon, il record torna
      eleggibile automaticamente;
    - marker legacy senza coordinate restano considerati processati per evitare
      di rifare Google su migliaia di record gia' auditati. Il census li conta
      separatamente per un eventuale backfill a costo Google zero.
    """
    lt = clean(row.get("geocode_location_type")).upper()
    if lt not in REPAIR_PROCESSED_STATUSES:
        return False
    stamped = _coords_from_check_marker(row.get("geocode_query_used"))
    if stamped is None:
        return True
    lat = row.get("latitudine"); lon = row.get("longitudine")
    if not has_valid_coords(lat, lon):
        return False
    # ~1 m o meno alle latitudini italiane; basta a rilevare un vero overwrite.
    return abs(float(lat)-stamped[0]) <= 1e-5 and abs(float(lon)-stamped[1]) <= 1e-5


def fetch_audit_targets(base_url: str, min_pvp_numeric=None):
    """Pagina Supabase finché raccoglie GEOCODE_LIMIT record DAVVERO auditabili.

    v10.1.2: se pvp_id e' TEXT, il filtro server pvp_id=gt.* e order=pvp_id
    sono lessicografici. min_pvp_numeric aggiunge quindi un secondo guard
    numerico client-side, evitando che vecchi ID corti (es. 591541) entrino
    dopo ID nuovi a 7 cifre (es. 4624652).
    """
    out = []
    offset = 0
    while True:
        sep = "&" if "?" in base_url else "?"
        url = f"{base_url}{sep}limit={GEOCODE_PAGE_SIZE}&offset={offset}"
        page = http_json("GET", url) or []
        if not isinstance(page, list):
            raise RuntimeError("Risposta Supabase inattesa durante fetch audit paginato")

        for r in page:
            if not has_valid_coords(r.get("latitudine"), r.get("longitudine")):
                continue
            if GEOCODE_AUDIT_SCOPE == "pvp":
                # Coordinate validate/corrette sono gia' protette dall'importer.
                if clean(r.get("geocode_source")):
                    continue
                # v10.1.3: anche gli esiti audit PVP_* sono gia' stati lavorati.
                # Se le coordinate sono cambiate rispetto all'impronta salvata,
                # repair_status_marker_is_current() restituisce False e il record
                # torna eleggibile.
                if GEOCODE_MODE == "repair" and repair_status_marker_is_current(r):
                    continue
            elif GEOCODE_AUDIT_SCOPE != "all":
                raise SystemExit("GEOCODE_AUDIT_SCOPE non valido. Usa: pvp | all")

            if min_pvp_numeric is not None:
                pid_n = _pvp_id_int(r.get("pvp_id"))
                # I pvp_id normali sono numerici. Se un valore non lo fosse,
                # lo lasciamo passare per non nascondere dati inattesi.
                if pid_n is not None and pid_n <= min_pvp_numeric:
                    continue

            out.append(r)
            if GEOCODE_LIMIT > 0 and len(out) >= GEOCODE_LIMIT:
                return out

        if len(page) < GEOCODE_PAGE_SIZE:
            break
        offset += len(page)

    return out


def fetch_targets(mode: str):
    base = f"{SUPABASE_URL.rstrip('/')}/rest/v1/listings"
    select = (
        "pvp_id,indirizzo,comune,sigla_provincia,cap,latitudine,longitudine,maps_link,"
        "geocode_location_type,geocode_formatted_address,geocode_source,geocode_query_used"
    )

    common = f"{base}?select={select}"
    if GEOCODE_PVP_ID:
        common += f"&pvp_id=eq.{quote(GEOCODE_PVP_ID)}"

    if mode == "missing":
        url = common + "&or=(latitudine.is.null,longitudine.is.null,and(latitudine.eq.0,longitudine.eq.0))"
        return fetch_paged(url)
    if mode == "approximate":
        return fetch_paged(common + "&geocode_location_type=eq.APPROXIMATE")
    if mode == "both":
        url = common + "&or=(latitudine.is.null,longitudine.is.null,and(latitudine.eq.0,longitudine.eq.0),geocode_location_type.eq.APPROXIMATE)"
        return fetch_paged(url)
    if mode in {"audit", "repair"}:
        # Audit deterministico. In repair v9.0 il cursore usa keyset pagination
        # su pvp_id, così gli irrisolti di un batch non bloccano i successivi.
        audit_url = common + "&order=pvp_id.asc"
        min_pvp_numeric = None
        if mode == "repair":
            cursor = load_repair_cursor()
            if cursor and not GEOCODE_PVP_ID:
                # Manteniamo il filtro server per efficienza, ma poiche' pvp_id
                # e' TEXT aggiungiamo anche il confronto numerico client-side.
                audit_url += f"&pvp_id=gt.{quote(cursor)}"
                min_pvp_numeric = _pvp_id_int(cursor)
        return fetch_audit_targets(audit_url, min_pvp_numeric=min_pvp_numeric)
    raise SystemExit("GEOCODE_MODE non valido. Usa: missing | approximate | both | audit | repair")


def google_request(url: str, operation="geocode"):
    API_USAGE.logical_request("geocoding", operation)
    req = Request(url, method="GET")
    req.add_header("Accept", "application/json")
    with urlopen_retry(
        req, timeout=GEOCODE_TIMEOUT, retries=HTTP_RETRIES, sleep=HTTP_RETRY_SLEEP,
        usage_sku="geocoding", usage_operation=operation,
    ) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_google_result(raw: dict):
    geometry = raw.get("geometry") or {}
    loc = geometry.get("location") or {}
    lat = loc.get("lat")
    lon = loc.get("lng")
    if not has_valid_coords(lat, lon):
        return None

    comps = []
    for c in raw.get("address_components") or []:
        comps.append({
            "long_name": clean(c.get("long_name")),
            "short_name": clean(c.get("short_name")),
            "types": [clean(t) for t in (c.get("types") or []) if clean(t)],
        })

    return {
        "ok": True,
        "lat": float(lat),
        "lon": float(lon),
        "formatted_address": clean(raw.get("formatted_address")),
        "place_id": clean(raw.get("place_id")),
        "location_type": clean(geometry.get("location_type")).upper(),
        "types": [clean(t) for t in (raw.get("types") or []) if clean(t)],
        "components": comps,
    }


def _google_status_or_raise(data, cache_key, cache: CacheStore):
    status = clean(data.get("status")).upper()
    error_message = clean(data.get("error_message"))
    if status == "OK":
        return
    if status in {"ZERO_RESULTS"}:
        cache.put(cache_key, {"ok": False, "error": status})
        return

    # Non congeliamo errori di chiave/quota/configurazione nella cache.
    # Stampiamo anche error_message: Google lo usa per spiegare il motivo
    # concreto di REQUEST_DENIED (API non abilitata, restrizioni chiave, ecc.).
    detail = f" - {error_message}" if error_message else ""
    if status in TRANSIENT_GOOGLE_STATUSES:
        raise RuntimeError(
            f"Google Geocoding API: {status}{detail}. "
            "Interrompo senza mettere l'errore in cache."
        )
    raise RuntimeError(f"Google Geocoding API: {status or 'UNKNOWN_STATUS'}{detail}")


def _parse_google_results(data: dict):
    results = []
    for raw in (data.get("results") or [])[:FORWARD_MAX_RESULTS]:
        parsed = parse_google_result(raw or {})
        if parsed:
            parsed["partial_match"] = bool((raw or {}).get("partial_match"))
            results.append(parsed)
    return results


def google_forward(query: str, cache: CacheStore):
    query = clean(query)
    if not query:
        return []
    key = "F|" + query
    cached = cache.get(key)
    if isinstance(cached, dict):
        if cached.get("ok"):
            API_USAGE.cache_hit("geocoding", "forward")
            return cached.get("results") or []
        if clean(cached.get("error")).upper() == "ZERO_RESULTS":
            API_USAGE.cache_hit("geocoding", "forward")
            return []

    url = (
        "https://maps.googleapis.com/maps/api/geocode/json"
        f"?address={quote_plus(query)}"
        "&components=country:IT"
        "&region=it&language=it"
        f"&key={quote_plus(GOOGLE_GEOCODING_API_KEY)}"
    )
    data = google_request(url, "forward")
    _google_status_or_raise(data, key, cache)
    if clean(data.get("status")).upper() != "OK":
        return []
    results = _parse_google_results(data)
    cache.put(key, {"ok": True, "results": results})
    return results


def google_forward_constrained(address_part: str, row: dict, cache: CacheStore):
    """Forward geocoding con i dati geografici separati dall'indirizzo.

    Il CAP (quando presente) e il paese vengono passati come component filter;
    il comune viene usato come locality bias. Questo evita che una via omonima
    in un altro comune vinca solo perche' il nome/civico coincide, ed aiuta Google
    con alias toponomastici come "Via Cavour" -> nome ufficiale esteso.
    """
    address_part = clean(address_part).strip(" ,")
    if not address_part:
        return []

    cap = re.sub(r"\D", "", clean(row.get("cap")))
    comune = clean(row.get("comune"))
    components = ["country:IT"]
    if cap:
        components.append(f"postal_code:{cap}")
    if comune:
        components.append(f"locality:{comune}")
    components_str = "|".join(components)

    key = "FC|" + address_part + "|" + components_str
    cached = cache.get(key)
    if isinstance(cached, dict):
        if cached.get("ok"):
            API_USAGE.cache_hit("geocoding", "forward_constrained")
            return cached.get("results") or []
        if clean(cached.get("error")).upper() == "ZERO_RESULTS":
            API_USAGE.cache_hit("geocoding", "forward_constrained")
            return []

    url = (
        "https://maps.googleapis.com/maps/api/geocode/json"
        f"?address={quote_plus(address_part)}"
        f"&components={quote_plus(components_str)}"
        "&region=it&language=it"
        f"&key={quote_plus(GOOGLE_GEOCODING_API_KEY)}"
    )
    data = google_request(url, "forward_constrained")
    _google_status_or_raise(data, key, cache)
    if clean(data.get("status")).upper() != "OK":
        return []
    results = _parse_google_results(data)
    cache.put(key, {"ok": True, "results": results})
    return results


def google_place_id(place_id: str, cache: CacheStore):
    """Converte un place_id di Places in un normale risultato Geocoding."""
    place_id = clean(place_id)
    if not place_id:
        return []
    key = "PID|" + place_id
    cached = cache.get(key)
    if isinstance(cached, dict):
        if cached.get("ok"):
            API_USAGE.cache_hit("geocoding", "place_id")
            return cached.get("results") or []
        if clean(cached.get("error")).upper() == "ZERO_RESULTS":
            API_USAGE.cache_hit("geocoding", "place_id")
            return []

    url = (
        "https://maps.googleapis.com/maps/api/geocode/json"
        f"?place_id={quote_plus(place_id)}"
        "&region=it&language=it"
        f"&key={quote_plus(GOOGLE_GEOCODING_API_KEY)}"
    )
    data = google_request(url, "place_id")
    _google_status_or_raise(data, key, cache)
    if clean(data.get("status")).upper() != "OK":
        return []
    results = _parse_google_results(data)
    cache.put(key, {"ok": True, "results": results})
    return results


def _places_error_message(exc: urllib.error.HTTPError):
    try:
        body = exc.read().decode("utf-8", errors="replace")
        data = json.loads(body) if body else {}
        err = data.get("error") or {}
        status = clean(err.get("status"))
        message = clean(err.get("message"))
        detail = " - ".join(x for x in [status, message] if x)
        return detail or f"HTTP {exc.code}"
    except Exception:
        return f"HTTP {exc.code}"


def places_autocomplete(input_text: str, center_lat, center_lon, cache: CacheStore):
    """Fallback fuzzy per indirizzi che il Geocoding classico non risolve.

    Autocomplete e' adatto a input parziali/alias e viene ristretto a un cerchio
    attorno al comune atteso. Restituiamo solo placeId e testo; le coordinate
    vengono poi ottenute dalla normale Geocoding API tramite place_id.
    """
    input_text = clean(input_text).strip(" ,")
    if not input_text or not has_valid_coords(center_lat, center_lon):
        return []
    center_lat = float(center_lat)
    center_lon = float(center_lon)
    key = f"PA|{input_text}|{center_lat:.5f},{center_lon:.5f}|r={PLACES_AUTOCOMPLETE_RADIUS_M:.0f}"
    cached = cache.get(key)
    if isinstance(cached, dict):
        if cached.get("ok"):
            API_USAGE.cache_hit("places_autocomplete", "autocomplete")
            return cached.get("predictions") or []
        if clean(cached.get("error")).upper() == "ZERO_RESULTS":
            API_USAGE.cache_hit("places_autocomplete", "autocomplete")
            return []

    payload = {
        "input": input_text,
        "languageCode": "it",
        "regionCode": "IT",
        "includedRegionCodes": ["it"],
        "locationRestriction": {
            "circle": {
                "center": {"latitude": center_lat, "longitude": center_lon},
                "radius": PLACES_AUTOCOMPLETE_RADIUS_M,
            }
        },
    }
    req = Request(
        "https://places.googleapis.com/v1/places:autocomplete",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Goog-Api-Key", GOOGLE_GEOCODING_API_KEY)
    req.add_header(
        "X-Goog-FieldMask",
        "suggestions.placePrediction.placeId,suggestions.placePrediction.text.text,suggestions.placePrediction.types",
    )
    try:
        API_USAGE.logical_request("places_autocomplete", "autocomplete")
        with urlopen_retry(
            req, timeout=GEOCODE_TIMEOUT, retries=HTTP_RETRIES, sleep=HTTP_RETRY_SLEEP,
            usage_sku="places_autocomplete", usage_operation="autocomplete",
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = _places_error_message(e)
        raise RuntimeError(
            "Google Places API (New) Autocomplete: richiesta rifiutata - " + detail + ". "
            "Verifica che 'Places API (New)' sia abilitata nello stesso progetto della chiave."
        )

    predictions = []
    for suggestion in data.get("suggestions") or []:
        pred = suggestion.get("placePrediction") or {}
        pid = clean(pred.get("placeId"))
        text = clean(((pred.get("text") or {}).get("text")))
        if pid:
            predictions.append({
                "place_id": pid,
                "text": text,
                "types": [clean(t) for t in (pred.get("types") or []) if clean(t)],
            })
        if len(predictions) >= PLACES_AUTOCOMPLETE_MAX:
            break

    if predictions:
        cache.put(key, {"ok": True, "predictions": predictions})
    else:
        cache.put(key, {"ok": False, "error": "ZERO_RESULTS"})
    return predictions


def municipality_center(row: dict, cache: CacheStore):
    """Trova un centro di riferimento del comune/CAP per restringere Autocomplete."""
    comune = clean(row.get("comune"))
    sigla = clean(row.get("sigla_provincia")).upper()
    cap = re.sub(r"\D", "", clean(row.get("cap")))
    q = " ".join(x for x in [cap, comune, sigla] if x)
    if not q:
        return None
    for result in google_forward(q, cache):
        if municipality_geo_matches(row, result):
            return result
    return None


def autocomplete_address_variants(row: dict):
    core = strip_locality_tail(
        row.get("indirizzo"), row.get("comune"), row.get("sigla_provincia"), row.get("cap")
    )
    if not core:
        return []
    comune = clean(row.get("comune"))
    out = [core]
    if comune:
        out.append(f"{core}, {comune}")
    for seg in expected_segments(row):
        out.append(seg)
        if comune:
            out.append(f"{seg}, {comune}")
        no_prefix = strip_street_prefix(seg)
        if no_prefix and no_prefix != seg:
            out.append(no_prefix)
            if comune:
                out.append(f"{no_prefix}, {comune}")
    return dedup_keep_order(out)



def _address_validation_error_message(exc: urllib.error.HTTPError):
    try:
        body = exc.read().decode("utf-8", errors="replace")
        data = json.loads(body) if body else {}
        err = data.get("error") or {}
        status = clean(err.get("status"))
        message = clean(err.get("message"))
        detail = " - ".join(x for x in [status, message] if x)
        return detail or f"HTTP {exc.code}"
    except Exception:
        return f"HTTP {exc.code}"


def google_address_validation(row: dict, cache: CacheStore):
    """Valida/canonicalizza l'indirizzo completo con Address Validation API.

    A differenza del geocoder classico, questo servizio e' pensato per segnalare
    componenti corretti/sostituiti/inferiti e puo' quindi recuperare una
    denominazione ufficiale diversa da quella abbreviata nel PVP.
    """
    core = strip_locality_tail(
        row.get("indirizzo"), row.get("comune"), row.get("sigla_provincia"), row.get("cap")
    )
    if not core:
        return []

    comune = clean(row.get("comune"))
    sigla = clean(row.get("sigla_provincia")).upper()
    cap = re.sub(r"\D", "", clean(row.get("cap")))
    key = "AV|" + "|".join([core, comune, sigla, cap])
    cached = cache.get(key)
    if isinstance(cached, dict):
        if cached.get("ok"):
            API_USAGE.cache_hit("address_validation", "validate_address")
            return cached.get("results") or []
        if clean(cached.get("error")).upper() == "ZERO_RESULTS":
            API_USAGE.cache_hit("address_validation", "validate_address")
            return []

    postal = {
        "regionCode": "IT",
        "languageCode": "it",
        "addressLines": [core],
    }
    if comune:
        postal["locality"] = comune
    if sigla:
        postal["administrativeArea"] = sigla
    if cap:
        postal["postalCode"] = cap

    payload = {"address": postal}
    req = Request(
        "https://addressvalidation.googleapis.com/v1:validateAddress?key=" + quote_plus(GOOGLE_GEOCODING_API_KEY),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    try:
        API_USAGE.logical_request("address_validation", "validate_address")
        with urlopen_retry(
            req, timeout=GEOCODE_TIMEOUT, retries=HTTP_RETRIES, sleep=HTTP_RETRY_SLEEP,
            usage_sku="address_validation", usage_operation="validate_address",
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = _address_validation_error_message(e)
        raise RuntimeError(
            "Google Address Validation API: richiesta rifiutata - " + detail + ". "
            "Verifica che 'Address Validation API' sia abilitata nello stesso progetto della chiave."
        )

    vr = (data.get("result") or {})
    address = vr.get("address") or {}
    geocode = vr.get("geocode") or {}
    verdict = vr.get("verdict") or {}
    loc = geocode.get("location") or {}
    lat = loc.get("latitude")
    lon = loc.get("longitude")
    if not has_valid_coords(lat, lon):
        cache.put(key, {"ok": False, "error": "ZERO_RESULTS"})
        return []

    comps = []
    seen_types = set()
    for c in address.get("addressComponents") or []:
        ctype = clean(c.get("componentType"))
        cname = clean(((c.get("componentName") or {}).get("text")))
        if not ctype or not cname:
            continue
        comps.append({"long_name": cname, "short_name": cname, "types": [ctype]})
        seen_types.add(ctype)

    pa = address.get("postalAddress") or {}
    pseudo = [
        ("locality", pa.get("locality")),
        ("postal_code", pa.get("postalCode")),
        ("administrative_area_level_2", pa.get("administrativeArea")),
        ("country", pa.get("regionCode")),
    ]
    for ctype, value in pseudo:
        value = clean(value)
        if value and ctype not in seen_types:
            comps.append({"long_name": value, "short_name": value, "types": [ctype]})

    gran = clean(verdict.get("geocodeGranularity")).upper()
    location_type = {
        "SUB_PREMISE": "ROOFTOP",
        "PREMISE": "ROOFTOP",
        "PREMISE_PROXIMITY": "RANGE_INTERPOLATED",
        "ROUTE": "GEOMETRIC_CENTER",
        "OTHER": "APPROXIMATE",
    }.get(gran, "APPROXIMATE")

    formatted = clean(address.get("formattedAddress"))
    if not formatted:
        lines = [clean(x) for x in (pa.get("addressLines") or []) if clean(x)]
        locality_line = " ".join(x for x in [clean(pa.get("postalCode")), clean(pa.get("locality")), clean(pa.get("administrativeArea"))] if x)
        formatted = ", ".join(lines + ([locality_line] if locality_line else []))

    result = {
        "ok": True,
        "lat": float(lat),
        "lon": float(lon),
        "formatted_address": formatted,
        "place_id": clean(geocode.get("placeId")),
        "location_type": location_type,
        "types": ["address_validation"],
        "components": comps,
        "partial_match": not bool(verdict.get("addressComplete")),
        "address_validation": {
            "input_granularity": clean(verdict.get("inputGranularity")),
            "validation_granularity": clean(verdict.get("validationGranularity")),
            "geocode_granularity": gran,
            "address_complete": bool(verdict.get("addressComplete")),
            "possible_next_action": clean(verdict.get("possibleNextAction")),
            "has_replaced_components": bool(verdict.get("hasReplacedComponents")),
            "has_inferred_components": bool(verdict.get("hasInferredComponents")),
            "has_spell_corrected_components": bool(verdict.get("hasSpellCorrectedComponents")),
            "has_unconfirmed_components": bool(verdict.get("hasUnconfirmedComponents")),
        },
    }
    cache.put(key, {"ok": True, "results": [result]})
    return [result]


def google_reverse(lat, lon, cache: CacheStore):
    if not has_valid_coords(lat, lon):
        return []
    lat = float(lat)
    lon = float(lon)
    key = f"R|{lat:.7f},{lon:.7f}"
    cached = cache.get(key)
    if isinstance(cached, dict):
        if cached.get("ok"):
            API_USAGE.cache_hit("geocoding", "reverse")
            return cached.get("results") or []
        if clean(cached.get("error")).upper() == "ZERO_RESULTS":
            API_USAGE.cache_hit("geocoding", "reverse")
            return []

    url = (
        "https://maps.googleapis.com/maps/api/geocode/json"
        f"?latlng={lat:.7f},{lon:.7f}"
        "&region=it&language=it"
        f"&key={quote_plus(GOOGLE_GEOCODING_API_KEY)}"
    )
    data = google_request(url, "reverse")
    _google_status_or_raise(data, key, cache)
    if clean(data.get("status")).upper() != "OK":
        return []
    results = []
    for raw in (data.get("results") or [])[:FORWARD_MAX_RESULTS]:
        parsed = parse_google_result(raw or {})
        if parsed:
            results.append(parsed)
    cache.put(key, {"ok": True, "results": results})
    return results


def component_values(result: dict, wanted_types):
    vals = []
    for c in result.get("components") or []:
        types = set(c.get("types") or [])
        if types.intersection(wanted_types):
            vals.extend([clean(c.get("long_name")), clean(c.get("short_name"))])
    return dedup_keep_order(vals)


def exact_component_match(expected: str, result: dict, wanted_types):
    e = normalize_text(expected)
    if not e:
        return None
    vals = component_values(result, wanted_types)
    return any(normalize_text(v) == e for v in vals)


def municipality_matches(row: dict, result: dict):
    """Match del comune basato sui componenti Google, non sul testo libero.

    v8.7: il fallback sul formatted_address poteva dare falsi positivi quando
    il nome del comune compariva nel nome della strada (es. "Capurso" dentro
    "Strada Comunale Capurso Carbonara" pur essendo il risultato a Triggiano).
    """
    comune = clean(row.get("comune"))
    if not comune:
        return True

    expected = normalize_text(comune)
    primary_types = {"locality", "postal_town", "administrative_area_level_3"}
    sublocality_types = {"sublocality", "sublocality_level_1"}

    primary_vals = component_values(result, primary_types)
    if any(normalize_text(v) == expected for v in primary_vals):
        return True

    # Se Google dichiara esplicitamente un comune/locality diverso, una
    # sublocality omonima non deve ribaltare il verdetto.
    if primary_vals:
        return False

    # Solo quando mancano del tutto i componenti comunali principali accettiamo
    # una sublocality esatta come fallback.
    sub_vals = component_values(result, sublocality_types)
    return any(normalize_text(v) == expected for v in sub_vals)


def province_matches(row: dict, result: dict):
    sigla = clean(row.get("sigla_provincia")).upper()
    if not sigla:
        return None
    vals = component_values(result, PROVINCE_TYPES)
    return any(clean(v).upper() == sigla for v in vals)


def cap_matches(row: dict, result: dict):
    cap = re.sub(r"\D", "", clean(row.get("cap")))
    if not cap:
        return None
    vals = component_values(result, {"postal_code"})
    return any(re.sub(r"\D", "", clean(v)) == cap for v in vals)


# Sigle storiche/riordinate che Google puo' ancora restituire per la Sardegna.
# Non le consideriamo prova di provincia diversa ai fini del guard v9.3.2.
PROVINCE_EQUIVALENT_GROUPS = (
    {"SU", "CI", "VS", "CA"},
    {"SS", "OT"},
    {"NU", "OG"},
)


def province_alias_compatible(row: dict, result: dict):
    expected = clean(row.get("sigla_provincia")).upper()
    if not expected:
        return False
    vals = {clean(v).upper() for v in component_values(result, PROVINCE_TYPES) if clean(v)}
    if expected in vals:
        return True
    for group in PROVINCE_EQUIVALENT_GROUPS:
        if expected in group and vals.intersection(group):
            return True
    return False


def municipality_geo_matches(row: dict, result: dict):
    """Comune coerente con guard anti-omonimia v9.3.2.

    Il nome del comune resta il criterio principale. Pero', se Google restituisce
    lo stesso nome in una provincia diversa E con un CAP diverso, non lo
    consideriamo lo stesso luogo. Basta invece che provincia o CAP siano
    coerenti (o che la sigla sia una nota equivalenza storica) per non irrigidire
    i casi reali con CAP aggiornati o riforme provinciali.
    """
    if not municipality_matches(row, result):
        return False

    pm = province_matches(row, result)
    cm = cap_matches(row, result)
    province_compatible = (pm is True) or province_alias_compatible(row, result)

    # Il guard scatta solo con DOPPIO mismatch esplicito. Se uno dei due dati
    # manca, non inventiamo un conflitto.
    if pm is False and cm is False and not province_compatible:
        return False
    return True


def strip_locality_tail(indirizzo: str, comune: str, sigla: str, cap: str):
    s = clean(indirizzo).strip(" ,")
    s = re.sub(r",?\s*Italia\s*$", "", s, flags=re.IGNORECASE).strip(" ,")
    c = re.escape(clean(comune)) if clean(comune) else ""
    p = re.escape(clean(sigla)) if clean(sigla) else ""
    z = re.escape(clean(cap)) if clean(cap) else ""
    pats = []
    if z and c and p:
        pats.append(rf",?\s*{z}\s+{c}\s+{p}\s*$")
    if z and c:
        pats.append(rf",?\s*{z}\s+{c}\s*$")
    if c and p:
        pats.append(rf",?\s*{c}\s+{p}\s*$")
    for pat in pats:
        s = re.sub(pat, "", s, flags=re.IGNORECASE).strip(" ,")
    return s


SECONDARY_ADDRESS_PAREN_RE = re.compile(
    r"\([^()]*\b(?:catastal(?:e|i)|catasto|censuari[oa]|indirizzo\s+catastale|"
    r"toponomastica\s+catastale|gia\s+via|già\s+via|ex\s+via|alternativ[oa])\b[^()]*\)",
    re.IGNORECASE,
)


def strip_secondary_address_parentheticals(text: str) -> str:
    """Rimuove solo parentesi che dichiarano un indirizzo secondario/alternativo.

    Esempio reale:
      Via Alcide de Gasperi, 25 (catastale via Umberto I, 19)
    Il civico operativo e' 25; il 19 appartiene all'indirizzo catastale
    alternativo e non deve impedire al parser di vedere il 25.

    Le parentesi generiche NON vengono eliminate: '(civico 12)' resta valido.
    """
    s = clean(text)
    prev = None
    # Supporta piu' annotazioni semplici senza toccare parentesi non pertinenti.
    while s != prev:
        prev = s
        s = SECONDARY_ADDRESS_PAREN_RE.sub(" ", s)
    return clean(s)


def token_list(s: str):
    toks = []
    for t in normalize_text(s).split():
        if t in GENERIC_TOKENS:
            continue
        if t.isdigit():
            continue
        if len(t) < 3:
            continue
        toks.append(t)
    return toks


def road_codes(s: str):
    n = normalize_text(s).replace(" ", "")
    return set(re.findall(r"(?:sp|ss|sr|sc|a)\d{1,4}", n))


def fuzzy_token(a: str, b: str):
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _semantic_token_alignment(expected: str, actual: str):
    """Allineamento 1:1 dei token significativi della strada.

    v9.3.3: il vecchio matcher poteva assegnare 0.90 a due nomi diversi
    quando condividevano un solo token lungo/comune:
      - "Via Mia Martini" -> "Via Arcivescovo Antonio Martini"
      - "Contrada Cugno Lupo" -> "Contrada Cugno Vasco"
      - "Via Giovanni Battista Viotti" -> "Via Giovanni Bobba"

    Usiamo quindi un matching fuzzy 1:1 e misuriamo la copertura in entrambe
    le direzioni. Questo impedisce che lo stesso token reale "spieghi" piu'
    token attesi e rende visibili le sostituzioni semantiche.
    """
    e = token_list(expected)
    a = token_list(actual)
    if not e or not a:
        return None

    pairs = []
    for ei, et in enumerate(e):
        for ai, at in enumerate(a):
            sim = fuzzy_token(et, at)
            if sim >= 0.82:
                pairs.append((sim, ei, ai))

    # Greedy sul match piu' forte, ma ogni token puo' essere usato una sola volta.
    pairs.sort(reverse=True)
    used_e = set()
    used_a = set()
    matched = []
    for sim, ei, ai in pairs:
        if ei in used_e or ai in used_a:
            continue
        used_e.add(ei)
        used_a.add(ai)
        matched.append((sim, ei, ai))

    expected_coverage = len(used_e) / len(e)
    actual_coverage = len(used_a) / len(a)
    avg_match = (
        sum(sim for sim, _, _ in matched) / len(matched)
        if matched
        else 0.0
    )

    # L'ultimo token significativo e' spesso l'ancora del toponimo/persona
    # (Garibaldi, Marconi, Vittorio). Serve per ammettere abbreviazioni
    # conservative tipo "Guglielmo Marconi" -> "G. Marconi"/"Marconi".
    final_expected_matched = (len(e) - 1) in used_e

    return {
        "expected_tokens": e,
        "actual_tokens": a,
        "expected_coverage": expected_coverage,
        "actual_coverage": actual_coverage,
        "avg_match": avg_match,
        "final_expected_matched": final_expected_matched,
    }


def phrase_similarity(expected: str, actual: str):
    e_codes = road_codes(expected)
    a_codes = road_codes(actual)

    # Un codice stradale esplicito e concorde (SS127, SP42...) e' un'ancora forte.
    if e_codes and a_codes:
        if e_codes.intersection(a_codes):
            return 1.0
        # Due codici espliciti ma diversi non devono essere recuperati dal fuzzy text.
        return 0.0

    alignment = _semantic_token_alignment(expected, actual)
    if not alignment:
        return None

    e_cov = alignment["expected_coverage"]
    a_cov = alignment["actual_coverage"]
    avg = alignment["avg_match"]
    final_matched = alignment["final_expected_matched"]

    # Tutti i token attesi sono preservati: consentiamo espansioni ufficiali,
    # es. "Cavour" -> "Camillo Benso Conte di Cavour".
    if e_cov == 1.0:
        return min(1.0, 0.85 + 0.15 * avg)

    # Alias abbreviato: tutti i token del risultato sono contenuti nell'atteso
    # e deve restare l'ancora finale. Esempi validi:
    # "Giuseppe Garibaldi" -> "Garibaldi"
    # "Guglielmo Marconi" -> "G. Marconi" (G viene scartata come token corto).
    if a_cov == 1.0 and final_matched:
        return min(0.92, 0.78 + 0.14 * avg)

    # Token mancanti da ENTRAMBE le parti = sostituzione semantica.
    # Hard cap sotto la soglia 0.55 usata dal geocoder per accettare una strada.
    if e_cov < 1.0 and a_cov < 1.0:
        return min(0.54, 0.50 * e_cov + 0.04 * avg)

    # Caso residuale incompleto: resta utile per diagnostica, mai "strong".
    return min(0.69, 0.65 * e_cov + 0.04 * avg)


def expected_segments(row: dict):
    core = strip_locality_tail(
        row.get("indirizzo"), row.get("comune"), row.get("sigla_provincia"), row.get("cap")
    )
    if not core:
        return []
    # Segmenti multipli: "Località X, Via Y" oppure "Contrada X - S.S. 115".
    segs = [s.strip(" ,-") for s in re.split(r"\s*,\s*|\s+-\s+", core) if clean(s)]
    specific = [s for s in segs if STREET_PREFIX_RE.search(s) or road_codes(s)]
    return dedup_keep_order(specific or segs)


def result_route_phrases(result: dict):
    vals = component_values(result, {"route"})
    return dedup_keep_order(vals)


def result_place_phrases(result: dict):
    vals = component_values(result, PLACE_DETAIL_TYPES)
    return dedup_keep_order(vals)


def address_specificity(row: dict):
    segs = expected_segments(row)
    terms = []
    for s in segs:
        terms.extend(token_list(s))
        terms.extend(road_codes(s))
    return len(set(terms)) > 0


def _meaningful_expected_segments(row: dict):
    """Segmenti semantici utili al guard multi-segmento.

    I segmenti puramente numerici (civici) vengono esclusi: il civico e' gia'
    verificato separatamente da expected_house_numbers()/house_number_relation().
    Manteniamo invece localita/frazioni e segmenti stradali, perche' negli
    indirizzi composti sono informazioni territoriali che non devono essere
    ignorate durante un REPAIR automatico.
    """
    out = []
    for seg in expected_segments(row):
        if token_list(seg) or road_codes(seg):
            out.append(seg)
    return dedup_keep_order(out)


def _multi_segment_actual_phrases(result: dict):
    # Per il controllo multi-segmento servono sia la route sia i contesti
    # territoriali (localita/sublocalita/premise). A differenza del vecchio
    # best_address_match non usiamo route OR place: li consideriamo insieme.
    context_types = set(PLACE_DETAIL_TYPES) | {
        "locality", "postal_town", "administrative_area_level_3",
        "sublocality", "sublocality_level_1", "sublocality_level_2",
        "neighborhood", "premise", "establishment", "point_of_interest",
    }
    vals = []
    vals.extend(result_route_phrases(result))
    vals.extend(component_values(result, context_types))
    return dedup_keep_order(vals)


def address_match_profile(row: dict, result: dict):
    """Profilo di copertura dei segmenti dell'indirizzo PVP.

    Il vecchio matcher prendeva il MAX tra tutte le coppie segmento-risultato:
    su un indirizzo come "Contrada X, Localita Y, Via Z" bastava quindi che
    Google restituisse Via Z per ottenere 1.00, ignorando X/Y.

    Qui assegniamo i segmenti attesi alle frasi Google uno-a-uno (greedy sul
    punteggio piu' alto). Per un indirizzo con >1 segmento significativo,
    complete=True soltanto se TUTTI i segmenti trovano una controparte con
    similarita >=0.55. Questo e' volutamente conservativo: un indirizzo
    composto incompleto resta utile per review, ma non puo' autorizzare repair.
    """
    segs = _meaningful_expected_segments(row)
    actuals = _multi_segment_actual_phrases(result)
    if not segs or not actuals:
        return {
            "segment_count": len(segs),
            "matched_count": 0,
            "complete": len(segs) <= 1,
            "best_match": None,
            "min_assigned_match": None,
            "matches": [],
        }

    pairs = []
    for i, e in enumerate(segs):
        for j, a in enumerate(actuals):
            s = phrase_similarity(e, a)
            if s is not None:
                pairs.append((float(s), i, j, e, a))
    pairs.sort(key=lambda x: x[0], reverse=True)

    used_e = set()
    used_a = set()
    assigned = []
    for score, i, j, e, a in pairs:
        if i in used_e or j in used_a:
            continue
        used_e.add(i)
        used_a.add(j)
        assigned.append((score, e, a))

    by_expected = {e: 0.0 for e in segs}
    for score, e, a in assigned:
        by_expected[e] = score

    matched_count = sum(1 for s in by_expected.values() if s >= 0.55)
    complete = len(segs) <= 1 or matched_count == len(segs)
    best_match = max(by_expected.values()) if by_expected else None
    min_assigned = min(by_expected.values()) if by_expected else None

    return {
        "segment_count": len(segs),
        "matched_count": matched_count,
        "complete": complete,
        "best_match": best_match,
        "min_assigned_match": min_assigned,
        "matches": [
            {"expected": e, "actual": a, "score": round(score, 3)}
            for score, e, a in assigned
        ],
    }


def best_address_match(row: dict, result: dict):
    # Manteniamo il comportamento storico (best single match) per compatibilita
    # con audit e diagnostica esistenti. La sicurezza multi-segmento viene
    # applicata separatamente da score_forward_candidate/verify_candidate.
    segs = expected_segments(row)
    if not segs:
        return None
    actuals = result_route_phrases(result) or result_place_phrases(result)
    if not actuals:
        return None
    scores = []
    for e in segs:
        for a in actuals:
            s = phrase_similarity(e, a)
            if s is not None:
                scores.append(s)
    return max(scores) if scores else None


def normalize_house_number(value):
    """Normalizza 7/B, 7B, 7-b -> 7b senza confonderlo col CAP."""
    s = normalize_text(value).replace(" ", "")
    return s or None


def expected_house_numbers(row: dict):
    """Estrae i civici dichiarati dall'indirizzo PVP in modo conservativo.

    v9.1: gestisce anche espressioni multicivico e interni, ad esempio:
      "Via Pastoreria 10 e 22"
      "Via Manno, 18/20 22"
      "Via Cavour 229-231"
      "Via dei Carroz, 14-14/A"
      "Via della Regione, 188/int 47"  -> civico 188, NON interno 47

    Se il PVP dichiara piu' civici, basta che il reverse ne confermi uno per
    considerare la coordinata coerente. Questo evita di spostare un pin gia'
    valido da un civico dichiarato a un altro civico dello stesso lotto.
    """
    core = strip_locality_tail(
        row.get("indirizzo"), row.get("comune"), row.get("sigla_provincia"), row.get("cap")
    )
    if not core:
        return set()

    nums = set()
    # v9.3.6: elimina annotazioni di indirizzo secondario/catastale PRIMA di
    # cercare i civici. Senza questo guard, una parentesi come
    # "25 (catastale via Umberto I, 19)" spezzava il segmento e il parser
    # perdeva il civico operativo 25.
    raw = strip_secondary_address_parentheticals(core)

    # v9.3.6: in forme con marker ripetuto (es. "n. 51 e n. 53") la
    # congiunzione italiana "e" non deve essere interpretata come suffisso
    # alfabetico del primo civico (51E). Normalizziamo SOLO quando dopo la
    # congiunzione compare un nuovo marker civico esplicito; "51E" resta intatto.
    raw = re.sub(
        r"(?i)((?:n\.?|nr\.?|numero|civico|civ)\s*\d{1,4}(?:\s*[/\-]?\s*[A-Za-z](?![A-Za-z]))?)\s+e\s+(?=(?:n\.?|nr\.?|numero|civico|civ)\s*\d)",
        r"\1, ",
        raw,
    )

    def add_num(value):
        n = normalize_house_number(value)
        if n:
            nums.add(n)

    # Un atomo civico e' 14, 14A, 14/A, 14-B. Quando / o - separano due
    # NUMERI (18/20, 42-46), sono invece due civici distinti.
    atom = r"\d{1,4}(?:\s*[/\-]?\s*[A-Za-z](?![A-Za-z]))?"
    seq = rf"{atom}(?:\s*(?:[/\-]|\be\b|\s+)\s*{atom})*"

    def add_civic_sequence(value):
        s = clean(value)
        if not s:
            return
        s = re.sub(r"^(?:n\.?|nr\.?|numero|civico|civ)\s*", "", s, flags=re.I).strip()
        # "10 e 22": la e e' un separatore tra civici, non il suffisso del 10.
        s = re.sub(r"\s+\be\b\s+", "-", s, flags=re.I)

        # Se compare un interno/scala/piano, tutto cio' che segue non e' il
        # civico stradale. Es.: "188/int 47" -> 188.
        s = re.split(r"\s*[/\-]?\s*\b(?:int\.?|interno|scala|piano|edificio|sub\.?|subalterno)\b", s, maxsplit=1, flags=re.I)[0].strip()
        if not s:
            return

        if not re.fullmatch(seq, s, flags=re.I):
            return

        # Estrae i singoli atomi. Su "7/C" produce 7C; su "18/20 22"
        # produce 18, 20, 22; su "14-14/A" produce 14, 14A.
        for m in re.finditer(atom, s, flags=re.I):
            token = m.group(0)
            mm = re.fullmatch(r"(\d{1,4})(?:\s*[/\-]?\s*([A-Za-z]))?", token, flags=re.I)
            if mm:
                add_num((mm.group(1) or "") + (mm.group(2) or ""))

    # 0) Caso civico + interno nello stesso segmento: "188/int 47".
    #    Il numero dopo int/interno/scala/piano NON e' un civico.
    for m in re.finditer(
        r"\b(\d{1,4}(?:\s*[/\-]\s*[A-Za-z])?)\s*[/\-]?\s*(?:int\.?|interno|scala|piano|edificio|sub\.?|subalterno)\s*\d+",
        raw,
        flags=re.I,
    ):
        add_civic_sequence(m.group(1))

    # 1) Numeri esplicitamente marcati: "N. 43", "civico 12", ecc.
    #    La sequenza piu' lunga viene catturata prima del resto del testo.
    for m in re.finditer(
        rf"\b(?:n\.?|nr\.?|numero|civico|civ)\s*({seq})",
        raw,
        flags=re.I,
    ):
        add_civic_sequence(m.group(1))

    # 2) Segmenti isolati dopo il nome della strada, anche multicivico.
    parts = [p.strip() for p in re.split(r"[,;]", raw) if p.strip()]
    for p in parts[1:]:
        add_civic_sequence(p)

    # 3) Sequenza civica in coda senza virgola: "Via Torino 120-122",
    #    "Via Pastoreria 10 e 22". La prendiamo solo in fondo al core.
    m = re.search(rf"({seq})\s*$", raw, flags=re.I)
    if m:
        prefix = raw[:m.start(1)].strip()
        ncore = normalize_text(raw)
        road_only = re.fullmatch(
            r"(?:ss|sp|sr|s p|s s|strada statale|strada provinciale)\s*\d{1,4}",
            ncore,
        )
        # Evita che il numero della strada (SP26, SS 131, Via 24 Maggio)
        # venga preso come civico quando non c'e' altro testo utile prima.
        internal_prefix = re.search(
            r"(?:[/\-]?\s*(?:int\.?|interno|scala|piano|edificio|sub\.?|subalterno))\s*$",
            prefix,
            flags=re.I,
        )
        if prefix and not road_only and not internal_prefix:
            add_civic_sequence(m.group(1))

    # 4) Pattern storico: civico immediatamente seguito da CAP intermedio.
    for m in re.finditer(
        r",\s*(\d{1,4})(?:\s*[/\-]?\s*([A-Za-z]))?\s*,\s*\d{5}\b",
        raw,
        flags=re.I,
    ):
        add_num((m.group(1) or "") + (m.group(2) or ""))

    return nums

def result_house_numbers(result: dict):
    vals = component_values(result, {"street_number"})
    return {n for v in vals if clean(v) for n in [normalize_house_number(v)] if n}


def house_number_base(value):
    m = re.match(r"^(\d+)", clean(value))
    return m.group(1) if m else ""


def house_number_relation(expected_nums, result_nums):
    """exact | variant | mismatch | missing_expected | missing_result | none."""
    expected_nums = {clean(x).lower() for x in (expected_nums or set()) if clean(x)}
    result_nums = {clean(x).lower() for x in (result_nums or set()) if clean(x)}
    if not expected_nums and not result_nums:
        return "none"
    if not expected_nums:
        return "missing_expected"
    if not result_nums:
        return "missing_result"
    if expected_nums.intersection(result_nums):
        return "exact"

    eb = {house_number_base(x) for x in expected_nums if house_number_base(x)}
    rb = {house_number_base(x) for x in result_nums if house_number_base(x)}
    if eb.intersection(rb):
        return "variant"
    return "mismatch"


def score_forward_candidate(row: dict, result: dict, locality_fallback=False):
    if not result or not result.get("ok"):
        return None
    if not municipality_geo_matches(row, result):
        return None

    score = 45.0
    reasons = ["comune_ok"]

    pm = province_matches(row, result)
    if pm is True:
        score += 10
        reasons.append("provincia_ok")
    elif pm is False:
        score -= 12
        reasons.append("provincia_mismatch")

    cm = cap_matches(row, result)
    if cm is True:
        score += 8
        reasons.append("cap_ok")
    elif cm is False:
        score -= 4
        reasons.append("cap_mismatch")

    specific = address_specificity(row)
    addr_match = best_address_match(row, result)
    segment_profile = address_match_profile(row, result)
    multi_segment_incomplete = (
        segment_profile.get("segment_count", 0) > 1
        and not segment_profile.get("complete", False)
    )
    if specific and not locality_fallback:
        if addr_match is None:
            # Una risposta che non contiene strada/localita specifica non puo'
            # diventare un falso "ROOFTOP" solo perche' il comune coincide.
            return None
        if addr_match < 0.55:
            return None
        score += 30 * addr_match
        reasons.append(f"indirizzo_match={addr_match:.2f}")
    elif addr_match is not None:
        score += 18 * addr_match
        reasons.append(f"luogo_match={addr_match:.2f}")

    expected_nums = expected_house_numbers(row)
    result_nums = result_house_numbers(result)
    civic_relation = house_number_relation(expected_nums, result_nums)
    if civic_relation == "exact":
        score += 8
        reasons.append("civico_ok")
    elif civic_relation == "variant":
        # Stesso numero base ma suffisso diverso/aggiunto (es. 7 vs 7B):
        # candidato plausibile, ma non abbastanza forte per una correzione automatica.
        score += 1
        reasons.append("civico_variant")
    elif civic_relation == "mismatch":
        # Se l'input contiene un civico esplicito e Google individua un altro
        # civico, non e' una correzione dell'indirizzo richiesto.
        return None
    elif civic_relation == "missing_result":
        reasons.append("civico_non_verificabile")

    lt = clean(result.get("location_type")).upper()
    score += {
        "ROOFTOP": 10,
        "RANGE_INTERPOLATED": 8,
        "GEOMETRIC_CENTER": 5,
        "APPROXIMATE": 1,
    }.get(lt, 0)

    # v8.5: la similarita' del testo non puo' trasformare un geocode poco preciso
    # in una correzione automatica. Address Validation puo' riformattare/ecoare
    # perfettamente l'input anche quando geocodeGranularity=OTHER.
    av = result.get("address_validation") or {}
    if av:
        vg = clean(av.get("validation_granularity")).upper()
        gg = clean(av.get("geocode_granularity")).upper()
        next_action = clean(av.get("possible_next_action")).upper()
        address_complete = bool(av.get("address_complete"))
        has_unconfirmed = bool(av.get("has_unconfirmed_components"))

        reasons.append(f"av_validation={vg or 'N/A'}")
        reasons.append(f"av_geocode={gg or 'N/A'}")
        if next_action:
            reasons.append(f"av_action={next_action}")

        # OTHER/APPROXIMATE: non sappiamo dov'e' realmente il civico/strada.
        # Mai abbastanza forte per REPAIR automatico.
        if gg == "OTHER" or lt == "APPROXIMATE":
            score = min(score, 69.0)
            reasons.append("precision_cap_approximate")
        # ROUTE / GEOMETRIC_CENTER: utile come suggerimento di strada, ma non
        # abbastanza preciso per sostituire automaticamente un punto esistente.
        elif gg in {"ROUTE", "BLOCK"} or lt == "GEOMETRIC_CENTER":
            score = min(score, 79.0)
            reasons.append("precision_cap_route")

        if vg == "OTHER" or next_action == "FIX":
            score = min(score, 69.0)
            reasons.append("validation_not_safe")
        elif not address_complete:
            score -= 4.0
            reasons.append("address_incomplete")
        if has_unconfirmed:
            score -= 4.0
            reasons.append("unconfirmed_components")

    # Regola generale anche per Geocoding/Places: la precisione geometrica
    # limita sempre la fiducia, non solo per Address Validation.
    if specific and lt == "APPROXIMATE":
        score = min(score, 69.0)
        reasons.append("approximate_not_repairable")
    elif specific and lt == "GEOMETRIC_CENTER":
        score = min(score, 79.0)
        reasons.append("geometric_center_not_repairable")

    if civic_relation in {"variant", "missing_result"}:
        # Con un civico esplicito nell'input, una variante o l'assenza del
        # civico nel risultato non devono portare a REPAIR automatico.
        score = min(score, 79.0)
        reasons.append("civico_precision_cap")

    if locality_fallback:
        score = min(score, 69.0)
        reasons.append("fallback_comune")

    # v9.3.4: un indirizzo PVP con piu' segmenti significativi non puo'
    # diventare HIGH/repairable se Google ne conferma soltanto una parte.
    if multi_segment_incomplete:
        score = min(score, 79.0)
        reasons.append(
            "multi_segment_incomplete="
            f"{segment_profile.get('matched_count', 0)}/"
            f"{segment_profile.get('segment_count', 0)}"
        )

    confidence = "HIGH" if score >= 82 else "MEDIUM" if score >= 72 else "LOW"
    return {
        "score": round(score, 2),
        "confidence": confidence,
        "address_match": None if addr_match is None else round(addr_match, 3),
        "multi_segment_count": segment_profile.get("segment_count", 0),
        "multi_segment_matched": segment_profile.get("matched_count", 0),
        "multi_segment_complete": bool(segment_profile.get("complete", True)),
        "multi_segment_matches": segment_profile.get("matches", []),
        "reasons": reasons,
    }


def remove_house_number(segment: str):
    s = clean(segment)
    s = re.sub(r"\b(?:n\.?|nr\.?|numero|civ(?:ico)?\.?)\s*\d+[A-Za-z]?(?:[/\-]\d+[A-Za-z]?)?\b", "", s, flags=re.I)
    s = re.sub(r"[, ]+\d+[A-Za-z]?(?:[/\-]\d+[A-Za-z]?)?\s*$", "", s)
    return clean(s).strip(" ,")


def strip_street_prefix(segment: str):
    s = clean(segment)
    s = re.sub(
        r"^\s*(?:via|viale|piazza|corso|largo|vicolo|strada|contrada|localit[àa]|frazione)\s+",
        "", s, flags=re.I,
    )
    return clean(s)


def build_query_variants(row: dict):
    indirizzo = clean(row.get("indirizzo")).strip(" ,")
    comune = clean(row.get("comune")).strip(" ,")
    sigla = clean(row.get("sigla_provincia")).upper().strip(" ,")
    cap = clean(row.get("cap")).strip(" ,")
    locality = " ".join(x for x in [cap, comune, sigla] if x)
    core = strip_locality_tail(indirizzo, comune, sigla, cap)

    specific = []
    if indirizzo:
        specific.append(indirizzo)
    if core and locality:
        specific.append(f"{core}, {locality}")
    elif core and comune:
        specific.append(f"{core}, {comune} {sigla}".strip())

    for seg in expected_segments(row):
        if locality:
            specific.append(f"{seg}, {locality}")
            no_num = remove_house_number(seg)
            if no_num and no_num != seg:
                specific.append(f"{no_num}, {locality}")
            no_prefix = strip_street_prefix(seg)
            if no_prefix and no_prefix != seg:
                specific.append(f"{no_prefix}, {locality}")
                no_prefix_num = remove_house_number(no_prefix)
                if no_prefix_num and no_prefix_num != no_prefix:
                    specific.append(f"{no_prefix_num}, {locality}")

    fallback = locality or comune
    return dedup_keep_order(specific), clean(fallback)


def constrained_address_variants(row: dict):
    core = strip_locality_tail(
        row.get("indirizzo"), row.get("comune"), row.get("sigla_provincia"), row.get("cap")
    )
    if not core:
        return []
    out = [core]
    # Una seconda forma senza prefisso puo' aiutare sugli alias di toponomastica,
    # mantenendo comunque civico + CAP/comune nei component filter.
    no_prefix = strip_street_prefix(core)
    if no_prefix and no_prefix != core:
        out.append(no_prefix)
    return dedup_keep_order(out)


def find_best_forward(row: dict, cache: CacheStore):
    variants, fallback = build_query_variants(row)
    candidates = []

    def add_candidate(label, result, locality_fallback=False, extra_penalty=0.0):
        ev = score_forward_candidate(row, result, locality_fallback=locality_fallback)
        if not ev:
            return
        ev = dict(ev)
        penalty = float(extra_penalty or 0.0)
        if result.get("partial_match"):
            penalty += 2.0
            ev["reasons"] = list(ev.get("reasons") or []) + ["google_partial_match"]
        if penalty:
            ev["score"] = round(ev["score"] - penalty, 2)
            ev["confidence"] = "HIGH" if ev["score"] >= 82 else "MEDIUM" if ev["score"] >= 72 else "LOW"
        candidates.append((ev["score"], label, result, ev, locality_fallback))

    def current_best():
        if not candidates:
            return None
        return max(candidates, key=lambda x: x[0])

    # 1) Geocoding classico con indirizzo e vincoli separati.
    cap = re.sub(r"\D", "", clean(row.get("cap")))
    comune = clean(row.get("comune"))
    constraint_label = ", ".join(x for x in [cap, comune] if x)
    for q in constrained_address_variants(row):
        for result in google_forward_constrained(q, row, cache):
            add_candidate(f"{q} [vincoli: {constraint_label or 'Italia'}]", result)
        time.sleep(max(0.0, GEOCODE_SLEEP_SECONDS))

    # 2) Query libere: utili per localita'/contrade e indirizzi non standard.
    for q in variants:
        for result in google_forward(q, cache):
            add_candidate(q, result)
        time.sleep(max(0.0, GEOCODE_SLEEP_SECONDS))

    # Se abbiamo gia' un candidato sicuro, inutile consumare altre API.
    best = current_best()
    if best and best[0] >= REPAIR_MIN_SCORE:
        score, q, result, ev, is_fallback = best
        return {"query": q, "result": result, "evaluation": ev, "locality_fallback": is_fallback}

    # 3) Address Validation API: e' il servizio piu' adatto a correggere e
    # standardizzare indirizzi incompleti/abbreviati. In Italia e' supportato.
    if ADDRESS_VALIDATION_FALLBACK:
        for result in google_address_validation(row, cache):
            av = result.get("address_validation") or {}
            label = "ADDRESS_VALIDATION: " + clean(result.get("formatted_address"))
            # Non penalizziamo le sostituzioni dichiarate: sono proprio il segnale
            # utile per alias come Cavour -> Camillo Benso Conte di Cavour.
            add_candidate(label, result)
        best = current_best()
        if best and best[0] >= REPAIR_MIN_SCORE:
            score, q, result, ev, is_fallback = best
            return {"query": q, "result": result, "evaluation": ev, "locality_fallback": is_fallback}

    # 4) Places Autocomplete (New) come ulteriore fuzzy fallback. Ora lo proviamo
    # anche se esiste un candidato mediocre: prima la presenza di qualunque
    # candidato poteva impedire di cercarne uno migliore.
    if PLACES_AUTOCOMPLETE_FALLBACK:
        center = municipality_center(row, cache)
        if center:
            for aq in autocomplete_address_variants(row):
                for pred in places_autocomplete(aq, center.get("lat"), center.get("lon"), cache):
                    for result in google_place_id(pred.get("place_id"), cache):
                        label_text = pred.get("text") or aq
                        add_candidate(f"AUTOCOMPLETE: {label_text}", result)
                time.sleep(max(0.0, GEOCODE_SLEEP_SECONDS))

    # 5) Solo alla fine permettiamo il centro/localita', marcato esplicitamente
    # come fallback e limitato a score <70: utile per non lasciare coordinate vuote,
    # ma mai abbastanza forte da una correzione automatica.
    if ALLOW_LOCALITY_FALLBACK and fallback:
        for result in google_forward(fallback, cache):
            add_candidate(fallback, result, locality_fallback=True)

    best = current_best()
    if best:
        score, q, result, ev, is_fallback = best
        return {"query": q, "result": result, "evaluation": ev, "locality_fallback": is_fallback}
    return None


def evaluate_existing_coords(row: dict, reverse_results):
    if not reverse_results:
        return {
            "status": "PVP_UNCERTAIN",
            "reason": "reverse_no_results",
            "reverse_result": None,
            "address_match": None,
        }

    same_comune_name = [r for r in reverse_results if municipality_matches(row, r)]
    same_comune = [r for r in same_comune_name if municipality_geo_matches(row, r)]
    if not same_comune:
        rr = same_comune_name[0] if same_comune_name else reverse_results[0]
        return {
            "status": "PVP_SUSPECT",
            "reason": (
                "reverse_same_comune_province_cap_mismatch"
                if same_comune_name else "reverse_comune_mismatch"
            ),
            "reverse_result": rr,
            "address_match": best_address_match(row, rr),
        }

    specific = address_specificity(row)
    if not specific:
        # Il reverse conferma soltanto il comune. Se il PVP non fornisce una
        # strada/localita' specifica non possiamo verificare che il marker
        # identifichi davvero il bene: e' coerente, ma non verificato.
        return {
            "status": "PVP_UNCERTAIN",
            "reason": "source_address_not_specific",
            "reverse_result": same_comune[0],
            "address_match": best_address_match(row, same_comune[0]),
        }

    scored = []
    for r in same_comune:
        s = best_address_match(row, r)
        if s is not None:
            scored.append((s, r))
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        best_s, best_r = scored[0]
        if best_s >= 0.55:
            expected_nums = expected_house_numbers(row)
            result_nums = result_house_numbers(best_r)
            civic_relation = house_number_relation(expected_nums, result_nums)
            reverse_lt = clean(best_r.get("location_type")).upper()

            # v8.6: "stessa via" non basta quando il PVP dichiara un civico.
            # Il campione v8.5 mostrava diversi PVP_OK con 7->216, 14->50, ecc.
            if expected_nums:
                if civic_relation == "mismatch":
                    return {
                        "status": "PVP_SUSPECT",
                        "reason": "reverse_civic_mismatch",
                        "reverse_result": best_r,
                        "address_match": round(best_s, 3),
                    }
                if civic_relation == "variant":
                    return {
                        "status": "PVP_UNCERTAIN",
                        "reason": "reverse_civic_variant",
                        "reverse_result": best_r,
                        "address_match": round(best_s, 3),
                    }
                if civic_relation == "missing_result":
                    return {
                        "status": "PVP_UNCERTAIN",
                        "reason": "reverse_civic_not_verifiable",
                        "reverse_result": best_r,
                        "address_match": round(best_s, 3),
                    }
                if civic_relation == "exact" and reverse_lt in {"APPROXIMATE", "GEOMETRIC_CENTER"}:
                    return {
                        "status": "PVP_UNCERTAIN",
                        "reason": "reverse_civic_low_precision",
                        "reverse_result": best_r,
                        "address_match": round(best_s, 3),
                    }

            if expected_nums:
                return {
                    "status": "PVP_OK_CIVICO",
                    "reason": "comune_street_civic_verified",
                    "reverse_result": best_r,
                    "address_match": round(best_s, 3),
                }

            return {
                "status": "PVP_OK_STRADA",
                "reason": "comune_and_street_verified",
                "reverse_result": best_r,
                "address_match": round(best_s, 3),
            }

        # Le coordinate sono nel comune giusto ma il reverse individua una strada
        # chiaramente diversa: mismatch utile da correggere.
        return {
            "status": "PVP_SUSPECT",
            "reason": "reverse_address_mismatch",
            "reverse_result": best_r,
            "address_match": round(best_s, 3),
        }

    return {
        "status": "PVP_UNCERTAIN",
        "reason": "comune_ok_but_street_not_verifiable",
        "reverse_result": same_comune[0],
        "address_match": None,
    }



def verify_candidate_coordinates(row: dict, candidate_result: dict, cache: CacheStore):
    """Seconda verifica indipendente della coordinata candidata prima del REPAIR.

    Il risultato forward/Address Validation non basta: alcuni servizi possono
    riportare i componenti testuali forniti in input anche quando la coordinata
    geocodificata cade in un altro comune. La v8.8 fa quindi reverse-geocoding
    della coordinata proposta e richiede coerenza strutturata con comune,
    strada e, quando presente, civico del PVP.

    Per una correzione automatica richiediamo inoltre un civico esplicito e
    verificato: senza civico la proposta resta utile nell'audit, ma non viene
    mai scritta automaticamente come punto preciso.
    """
    if not candidate_result or not has_valid_coords(candidate_result.get("lat"), candidate_result.get("lon")):
        return {
            "safe": False,
            "reason": "candidate_invalid_coords",
            "reverse_result": None,
            "address_match": None,
            "house_number_relation": "none",
        }

    reverse_results = google_reverse(candidate_result.get("lat"), candidate_result.get("lon"), cache)
    if not reverse_results:
        return {
            "safe": False,
            "reason": "candidate_reverse_no_results",
            "reverse_result": None,
            "address_match": None,
            "house_number_relation": "none",
        }

    same_comune_name = [r for r in reverse_results if municipality_matches(row, r)]
    same_comune = [r for r in same_comune_name if municipality_geo_matches(row, r)]
    if not same_comune:
        rr = same_comune_name[0] if same_comune_name else reverse_results[0]
        return {
            "safe": False,
            "reason": (
                "candidate_reverse_same_comune_province_cap_mismatch"
                if same_comune_name else "candidate_reverse_comune_mismatch"
            ),
            "reverse_result": rr,
            "address_match": best_address_match(row, rr),
            "house_number_relation": house_number_relation(
                expected_house_numbers(row), result_house_numbers(rr)
            ),
        }

    specific = address_specificity(row)
    scored = []
    for r in same_comune:
        m = best_address_match(row, r)
        if m is not None:
            scored.append((m, r))

    if specific:
        if not scored:
            rr = same_comune[0]
            return {
                "safe": False,
                "reason": "candidate_reverse_street_not_verifiable",
                "reverse_result": rr,
                "address_match": None,
                "house_number_relation": house_number_relation(
                    expected_house_numbers(row), result_house_numbers(rr)
                ),
            }
        scored.sort(key=lambda x: x[0], reverse=True)
        best_match, rr = scored[0]
        if best_match < 0.55:
            return {
                "safe": False,
                "reason": "candidate_reverse_address_mismatch",
                "reverse_result": rr,
                "address_match": round(best_match, 3),
                "house_number_relation": house_number_relation(
                    expected_house_numbers(row), result_house_numbers(rr)
                ),
            }

        reverse_profile = address_match_profile(row, rr)
        if (
            reverse_profile.get("segment_count", 0) > 1
            and not reverse_profile.get("complete", False)
        ):
            return {
                "safe": False,
                "reason": (
                    "candidate_reverse_multi_segment_incomplete="
                    f"{reverse_profile.get('matched_count', 0)}/"
                    f"{reverse_profile.get('segment_count', 0)}"
                ),
                "reverse_result": rr,
                "address_match": round(best_match, 3),
                "house_number_relation": house_number_relation(
                    expected_house_numbers(row), result_house_numbers(rr)
                ),
            }
    else:
        rr = same_comune[0]
        best_match = best_address_match(row, rr)

    expected_nums = expected_house_numbers(row)
    reverse_nums = result_house_numbers(rr)
    civic_relation = house_number_relation(expected_nums, reverse_nums)

    # Per il REPAIR automatico non inventiamo precisione che il PVP non fornisce.
    # Senza civico possiamo proporre/mostrare il candidato, ma non sostituire
    # automaticamente una coordinata esistente con un punto "preciso".
    if not expected_nums:
        return {
            "safe": False,
            "reason": "candidate_source_civic_missing",
            "reverse_result": rr,
            "address_match": None if best_match is None else round(best_match, 3),
            "house_number_relation": civic_relation,
        }

    if civic_relation != "exact":
        return {
            "safe": False,
            "reason": f"candidate_reverse_civic_{civic_relation}",
            "reverse_result": rr,
            "address_match": None if best_match is None else round(best_match, 3),
            "house_number_relation": civic_relation,
        }

    # Anche il reverse del punto candidato deve essere abbastanza preciso.
    reverse_lt = clean(rr.get("location_type")).upper()
    if reverse_lt in {"APPROXIMATE", "GEOMETRIC_CENTER"}:
        return {
            "safe": False,
            "reason": "candidate_reverse_low_precision",
            "reverse_result": rr,
            "address_match": None if best_match is None else round(best_match, 3),
            "house_number_relation": civic_relation,
        }

    return {
        "safe": True,
        "reason": "candidate_reverse_verified",
        "reverse_result": rr,
        "address_match": None if best_match is None else round(best_match, 3),
        "house_number_relation": civic_relation,
    }


def coordinate_maps_link(lat, lon):
    return f"https://www.google.com/maps?q={float(lat):.7f},{float(lon):.7f}"


def patch_listing(pvp_id, lat, lon, maps_link, query_used, location_type, formatted_address, source):
    base = f"{SUPABASE_URL.rstrip('/')}/rest/v1/listings"
    url = f"{base}?pvp_id=eq.{quote(str(pvp_id))}"
    payload = {
        "latitudine": float(lat),
        "longitudine": float(lon),
        "maps_link": maps_link,
        "geocode_query_used": clean(query_used),
        "geocode_location_type": clean(location_type),
        "geocode_formatted_address": clean(formatted_address),
        "geocode_source": clean(source),
    }
    req = Request(url, data=json.dumps(payload).encode("utf-8"), method="PATCH")
    req.add_header("apikey", SUPABASE_SERVICE_ROLE)
    req.add_header("Authorization", f"Bearer {SUPABASE_SERVICE_ROLE}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "return=minimal")
    with urlopen_retry(req, timeout=GEOCODE_TIMEOUT, retries=HTTP_RETRIES, sleep=HTTP_RETRY_SLEEP) as resp:
        resp.read()


def mark_pvp_verified(row, reverse_result):
    if not MARK_VERIFIED_PVP or not reverse_result:
        return False
    lat = row.get("latitudine")
    lon = row.get("longitudine")
    patch_listing(
        row.get("pvp_id"), lat, lon, coordinate_maps_link(lat, lon),
        f"reverse:{float(lat):.7f},{float(lon):.7f}",
        reverse_result.get("location_type", ""),
        reverse_result.get("formatted_address", ""),
        "pvp_verified",
    )
    return True


def patch_pvp_map_status(row, status, reverse_result=None):
    """Aggiorna solo il metadato usato dalla mappa per coordinate PVP non corrette.

    geocode_source resta vuoto: non trasformiamo una coordinata sospetta/incerta
    in una coordinata "protetta" dall'importer.
    """
    if not WRITE_AUDIT_MAP_STATUS:
        return False
    status = clean(status)
    if status not in {"PVP_OK_STRADA", "PVP_UNCERTAIN", "PVP_SUSPECT"}:
        return False

    base = f"{SUPABASE_URL.rstrip('/')}/rest/v1/listings"
    pvp_id = clean(row.get("pvp_id"))
    if not pvp_id:
        return False

    payload = {
        "geocode_location_type": status,
    }
    lat = row.get("latitudine")
    lon = row.get("longitudine")
    if reverse_result:
        payload["geocode_formatted_address"] = clean(reverse_result.get("formatted_address", ""))
        if has_valid_coords(lat, lon):
            payload["geocode_query_used"] = f"reverse:{float(lat):.7f},{float(lon):.7f}"
    elif has_valid_coords(lat, lon):
        # v10.1.3: anche un LOCAL_ACCEPT_STRADA senza Google lascia l'impronta
        # delle coordinate effettivamente controllate.
        payload["geocode_query_used"] = f"repair_checked:{float(lat):.7f},{float(lon):.7f}"

    url = f"{base}?pvp_id=eq.{quote(str(pvp_id))}"
    req = Request(url, data=json.dumps(payload).encode("utf-8"), method="PATCH")
    req.add_header("apikey", SUPABASE_SERVICE_ROLE)
    req.add_header("Authorization", f"Bearer {SUPABASE_SERVICE_ROLE}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "return=minimal")
    with urlopen_retry(req, timeout=GEOCODE_TIMEOUT, retries=HTTP_RETRIES, sleep=HTTP_RETRY_SLEEP) as resp:
        resp.read()
    return True


def write_jsonl_line(f, obj):
    f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    f.flush()


def audit_csv_header():
    return [
        "pvp_id", "status", "reason", "indirizzo", "comune", "sigla_provincia", "cap",
        "old_lat", "old_lon", "reverse_formatted_address", "reverse_location_type", "address_match",
        "expected_house_numbers", "reverse_house_numbers", "house_number_relation",
        "suggested_lat", "suggested_lon", "suggested_formatted_address", "suggested_location_type",
        "suggested_score", "suggested_confidence", "suggested_query",
        "av_input_granularity", "av_validation_granularity", "av_geocode_granularity",
        "av_address_complete", "av_possible_next_action", "av_has_unconfirmed_components",
        "candidate_reverse_safe", "candidate_reverse_reason",
        "candidate_reverse_formatted_address", "candidate_reverse_location_type",
        "candidate_reverse_address_match", "candidate_reverse_house_numbers",
        "candidate_reverse_house_number_relation",
        "action",
    ]


def audit_row_record(row, evaluation, suggestion=None, action="none"):
    rr = evaluation.get("reverse_result") or {}
    sr = (suggestion or {}).get("result") or {}
    se = (suggestion or {}).get("evaluation") or {}
    cr = (suggestion or {}).get("reverse_check") or {}
    crr = cr.get("reverse_result") or {}
    return {
        "pvp_id": clean(row.get("pvp_id")),
        "status": evaluation.get("status", ""),
        "reason": evaluation.get("reason", ""),
        "indirizzo": clean(row.get("indirizzo")),
        "comune": clean(row.get("comune")),
        "sigla_provincia": clean(row.get("sigla_provincia")),
        "cap": clean(row.get("cap")),
        "old_lat": row.get("latitudine"),
        "old_lon": row.get("longitudine"),
        "reverse_formatted_address": rr.get("formatted_address", ""),
        "reverse_location_type": rr.get("location_type", ""),
        "address_match": evaluation.get("address_match"),
        "expected_house_numbers": "|".join(sorted(expected_house_numbers(row))),
        "reverse_house_numbers": "|".join(sorted(result_house_numbers(rr))),
        "house_number_relation": house_number_relation(expected_house_numbers(row), result_house_numbers(rr)),
        "suggested_lat": sr.get("lat"),
        "suggested_lon": sr.get("lon"),
        "suggested_formatted_address": sr.get("formatted_address", ""),
        "suggested_location_type": sr.get("location_type", ""),
        "suggested_score": se.get("score"),
        "suggested_confidence": se.get("confidence", ""),
        "suggested_query": (suggestion or {}).get("query", ""),
        "av_input_granularity": (sr.get("address_validation") or {}).get("input_granularity", ""),
        "av_validation_granularity": (sr.get("address_validation") or {}).get("validation_granularity", ""),
        "av_geocode_granularity": (sr.get("address_validation") or {}).get("geocode_granularity", ""),
        "av_address_complete": (sr.get("address_validation") or {}).get("address_complete", ""),
        "av_possible_next_action": (sr.get("address_validation") or {}).get("possible_next_action", ""),
        "av_has_unconfirmed_components": (sr.get("address_validation") or {}).get("has_unconfirmed_components", ""),
        "candidate_reverse_safe": cr.get("safe", ""),
        "candidate_reverse_reason": cr.get("reason", ""),
        "candidate_reverse_formatted_address": crr.get("formatted_address", ""),
        "candidate_reverse_location_type": crr.get("location_type", ""),
        "candidate_reverse_address_match": cr.get("address_match", ""),
        "candidate_reverse_house_numbers": "|".join(sorted(result_house_numbers(crr))),
        "candidate_reverse_house_number_relation": cr.get("house_number_relation", ""),
        "action": action,
    }


def main():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
        raise SystemExit("Mancano SUPABASE_URL / SUPABASE_SERVICE_ROLE")
    if not GOOGLE_GEOCODING_API_KEY:
        raise SystemExit("Manca GOOGLE_GEOCODING_API_KEY")

    if GEOCODE_MODE == "repair":
        reset_repair_cursor_if_requested()
    repair_cursor_before = load_repair_cursor() if GEOCODE_MODE == "repair" else ""
    rows = fetch_targets(GEOCODE_MODE)
    print(f"[INFO] mode={GEOCODE_MODE} audit_scope={GEOCODE_AUDIT_SCOPE} targets={len(rows)}")
    if GEOCODE_MODE == "repair" and REPAIR_CURSOR_ENABLED and not GEOCODE_PVP_ID:
        print(f"[INFO] repair_cursor_before={repair_cursor_before or '<inizio>'}")
    if GEOCODE_PVP_ID:
        print(f"[INFO] filtro pvp_id={GEOCODE_PVP_ID}")
    if not rows:
        print("[DONE] Nulla da fare.")
        return

    cache = CacheStore(CACHE_FILE)
    updated = 0
    not_found = 0
    audited_ok = 0
    audited_ok_civico = 0
    audited_ok_strada = 0
    audited_suspect = 0
    audited_uncertain = 0
    repaired = 0

    audit_mode = GEOCODE_MODE in {"audit", "repair"}
    csv_file = None
    csv_writer = None
    audit_json_file = None
    budget_stop_message = ""

    try:
        if audit_mode:
            csv_file = AUDIT_CSV.open("w", encoding="utf-8-sig", newline="")
            csv_writer = csv.DictWriter(csv_file, fieldnames=audit_csv_header())
            csv_writer.writeheader()
            audit_json_file = AUDIT_JSONL.open("w", encoding="utf-8")

        with LOG_FILE.open("a", encoding="utf-8") as logf:
            for idx, row in enumerate(rows, start=1):
                pvp_id = clean(row.get("pvp_id"))

                if audit_mode:
                    reverse_results = google_reverse(row.get("latitudine"), row.get("longitudine"), cache)
                    ev = evaluate_existing_coords(row, reverse_results)
                    status = ev["status"]
                    suggestion = None
                    action = "none"

                    if status in {"PVP_OK_CIVICO", "PVP_OK_STRADA"}:
                        audited_ok += 1
                        if status == "PVP_OK_CIVICO":
                            audited_ok_civico += 1
                            if GEOCODE_MODE == "repair" and mark_pvp_verified(row, ev.get("reverse_result")):
                                action = "marked_pvp_verified"
                        else:
                            audited_ok_strada += 1
                            if GEOCODE_MODE == "repair" and patch_pvp_map_status(row, "PVP_OK_STRADA", ev.get("reverse_result")):
                                action = "marked_pvp_ok_strada"
                    elif status == "PVP_SUSPECT":
                        audited_suspect += 1
                        suggestion = find_best_forward(row, cache)
                        if suggestion and suggestion["evaluation"]["score"] >= REPAIR_MIN_SCORE and not suggestion.get("locality_fallback"):
                            # v8.8: prima di autorizzare qualunque REPAIR,
                            # reverse-geocodifichiamo la coordinata proposta.
                            reverse_check = verify_candidate_coordinates(row, suggestion["result"], cache)
                            suggestion["reverse_check"] = reverse_check
                            if reverse_check.get("safe"):
                                if GEOCODE_MODE == "repair":
                                    r = suggestion["result"]
                                    patch_listing(
                                        pvp_id, r["lat"], r["lon"], coordinate_maps_link(r["lat"], r["lon"]),
                                        suggestion["query"], r.get("location_type", ""), r.get("formatted_address", ""),
                                        "google_corrected_pvp",
                                    )
                                    repaired += 1
                                    action = "corrected_from_pvp"
                                else:
                                    action = "suggest_correction_verified"
                            else:
                                action = "candidate_failed_reverse_check"
                        elif suggestion:
                            action = "suggestion_below_repair_threshold"
                        else:
                            action = "no_safe_forward_candidate"

                        # Se non è stata applicata una correzione, memorizza solo
                        # la qualità PVP per la mappa (senza geocode_source).
                        if GEOCODE_MODE == "repair" and action != "corrected_from_pvp":
                            if patch_pvp_map_status(row, "PVP_SUSPECT", ev.get("reverse_result")):
                                action = f"{action}|marked_pvp_suspect"
                    else:
                        audited_uncertain += 1
                        if GEOCODE_MODE == "repair" and patch_pvp_map_status(row, "PVP_UNCERTAIN", ev.get("reverse_result")):
                            action = "marked_pvp_uncertain"

                    rec = audit_row_record(row, ev, suggestion, action)
                    csv_writer.writerow(rec)
                    csv_file.flush()
                    write_jsonl_line(audit_json_file, rec)
                    write_jsonl_line(logf, {"mode": GEOCODE_MODE, **rec})

                    # Avanza solo dopo che l'eventuale PATCH e il log del record
                    # sono terminati con successo. In caso di crash, il record
                    # corrente verrà ripreso al riavvio.
                    if GEOCODE_MODE == "repair":
                        save_repair_cursor(pvp_id)

                    if not status.startswith("PVP_OK_") or idx <= 5:
                        print(
                            f"[AUDIT] {idx}/{len(rows)} pvp_id={pvp_id} {status} "
                            f"reason={ev.get('reason')} action={action}"
                        )
                else:
                    best = find_best_forward(row, cache)
                    if not best:
                        not_found += 1
                        print(f"[MISS] {idx}/{len(rows)} pvp_id={pvp_id} indirizzo={clean(row.get('indirizzo'))}")
                        write_jsonl_line(logf, {
                            "mode": GEOCODE_MODE, "pvp_id": pvp_id, "status": "MISS",
                            "indirizzo": clean(row.get("indirizzo")), "comune": clean(row.get("comune")),
                        })
                        continue

                    r = best["result"]
                    ev = best["evaluation"]
                    source = "google_locality_fallback" if best.get("locality_fallback") else "google"

                    # In approximate aggiorniamo solo se abbiamo trovato qualcosa di piu'
                    # specifico del vecchio APPROXIMATE oppure se il vecchio record e' di fatto mancante.
                    if GEOCODE_MODE == "approximate" and best.get("locality_fallback"):
                        not_found += 1
                        write_jsonl_line(logf, {
                            "mode": GEOCODE_MODE, "pvp_id": pvp_id, "status": "NO_IMPROVEMENT",
                            "query": best["query"], "score": ev["score"],
                        })
                        continue

                    patch_listing(
                        pvp_id, r["lat"], r["lon"], coordinate_maps_link(r["lat"], r["lon"]),
                        best["query"], r.get("location_type", ""), r.get("formatted_address", ""), source,
                    )
                    updated += 1
                    write_jsonl_line(logf, {
                        "mode": GEOCODE_MODE,
                        "pvp_id": pvp_id,
                        "status": "UPDATED",
                        "query_used": best["query"],
                        "formatted_address": r.get("formatted_address", ""),
                        "location_type": r.get("location_type", ""),
                        "latitudine": r["lat"],
                        "longitudine": r["lon"],
                        "score": ev["score"],
                        "confidence": ev["confidence"],
                        "geocode_source": source,
                    })

                if idx % 25 == 0:
                    if audit_mode:
                        print(
                            f"[INFO] {idx}/{len(rows)} ok={audited_ok} "
                            f"(civico={audited_ok_civico}, strada={audited_ok_strada}) "
                            f"suspect={audited_suspect} uncertain={audited_uncertain} repaired={repaired}"
                        )
                    else:
                        print(f"[INFO] {idx}/{len(rows)} updated={updated} not_found={not_found}")
                    print(f"[API] {API_USAGE.one_line()}")

                time.sleep(max(0.0, GEOCODE_SLEEP_SECONDS))

    except ApiBudgetExceeded as e:
        budget_stop_message = str(e)
        print(f"[STOP SAFETY] {budget_stop_message}")

    finally:
        cache.save()
        API_USAGE.save()
        if csv_file:
            csv_file.close()
        if audit_json_file:
            audit_json_file.close()

    print("[DONE PARZIALE - SAFETY CAP]" if budget_stop_message else "[DONE]")
    if audit_mode:
        print("audited_ok:", audited_ok)
        print("audited_ok_civico:", audited_ok_civico)
        print("audited_ok_strada:", audited_ok_strada)
        print("audited_suspect:", audited_suspect)
        print("audited_uncertain:", audited_uncertain)
        print("repaired:", repaired)
        print("audit_csv:", AUDIT_CSV)
        print("audit_jsonl:", AUDIT_JSONL)
        if GEOCODE_MODE == "repair" and REPAIR_CURSOR_ENABLED and not GEOCODE_PVP_ID:
            print("repair_cursor_file:", REPAIR_CURSOR_FILE)
            print("repair_cursor_after:", load_repair_cursor() or "<inizio>")
    else:
        print("updated:", updated)
        print("not_found:", not_found)
    print("cache_file:", CACHE_FILE)
    print("log_file:", LOG_FILE)
    print("api_usage_file:", API_USAGE_FILE)
    API_USAGE.print_summary()
    if budget_stop_message:
        print("[INFO] Il cursore non è avanzato sul record interrotto; rilanciando, quel record verrà ripreso.")


if __name__ == "__main__":
    main()
