import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "src" / "geocoding" / "historical_geocache_runtime_v1.py"
spec = importlib.util.spec_from_file_location("hc", MOD)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


def _row(pid="2"):
    return {
        "pvp_id": pid,
        "indirizzo": "Via Roma, n. 10",
        "comune": "Carpi",
        "provincia": "Modena",
        "sigla_provincia": "MO",
        "cap": "41012",
    }


def _init(path):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    INSERT INTO meta VALUES('version','historical_geocache_v1');
    CREATE TABLE geocache(
      fingerprint TEXT PRIMARY KEY,canonical_key TEXT NOT NULL,comune_norm TEXT NOT NULL,
      provincia_norm TEXT NOT NULL,cap_norm TEXT NOT NULL,indirizzo_norm TEXT NOT NULL,
      lat REAL NOT NULL,lon REAL NOT NULL,precision TEXT NOT NULL,source TEXT NOT NULL,
      location_type TEXT NOT NULL,geocode_version TEXT NOT NULL,quality_score INTEGER NOT NULL,
      sample_pvp_id TEXT NOT NULL,sample_indirizzo TEXT NOT NULL,observations INTEGER NOT NULL,
      near_observations INTEGER NOT NULL,conflict_observations INTEGER NOT NULL,
      max_distance_m REAL NOT NULL,usable INTEGER NOT NULL
    );
    """)
    con.commit(); con.close()


def test_normalization_equivalent():
    a = _row()
    b = dict(a, indirizzo="via roma 10")
    assert hc.fingerprint(a) == hc.fingerprint(b)


def test_insert_lookup_and_current_precise_wins(tmp_path):
    db = tmp_path / "h.sqlite3"
    _init(db)
    payload = {
        "latitudine": 44.78, "longitudine": 10.88,
        "geocode_precision": "CIVICO",
        "geocode_source": "google_civico_rooftop_v38",
        "geocode_location_type": "ROOFTOP",
        "geocode_version": "x",
    }
    assert hc.update_verified(db, [(_row("1"), payload)])["inserted"] == 1
    con, meta = hc.open_optional(db)
    hit = hc.lookup(con, _row("2"))
    con.close()
    assert hit and hit["precision"] == "CIVICO"
    assert hc.should_reuse(hit, {"geocode_precision": "COMUNE"}) == (True, "REUSE")
    assert hc.should_reuse(hit, {"geocode_precision": "STRADA"}) == (False, "BYPASS_CURRENT_PRECISE")


def test_conflict_becomes_unusable(tmp_path):
    db = tmp_path / "h.sqlite3"
    _init(db)
    p1 = {"latitudine":44.78,"longitudine":10.88,"geocode_precision":"CIVICO",
          "geocode_source":"x","geocode_location_type":"ROOFTOP","geocode_version":"x"}
    p2 = {"latitudine":45.00,"longitudine":11.10,"geocode_precision":"CIVICO",
          "geocode_source":"x","geocode_location_type":"ROOFTOP","geocode_version":"x"}
    hc.update_verified(db, [(_row("1"), p1)])
    result = hc.update_verified(db, [(_row("2"), p2)])
    assert result["conflicts"] == 1
    con, meta = hc.open_optional(db)
    assert hc.lookup(con, _row("3")) is None
    con.close()
