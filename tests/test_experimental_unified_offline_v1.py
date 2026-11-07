from src.geocoding.experimental_unified_offline_v1 import choose

def test_anncsu_civic_wins():
    res={"primary":{"coord_valid":True,"lat_pvp":40.0,"lon_pvp":9.0}}
    ann={"anncsu":{"status":"MATCH_EXACT","best":{"odonimo":"VIA ROMA"},"civic":{"status":"CIVIC_COORD","lat":40.001,"lon":9.001}}}
    loc={"match":{"status":"NO_HINT"}}
    out=choose(res,ann,loc)
    assert out["final"]["source"]=="ANNCSU"
    assert out["final"]["precision"]=="CIVICO"

def test_locality_used_when_no_better_coord():
    res={"primary":{"coord_valid":False}}
    ann={"anncsu":{"status":"NO_SAFE_MATCH"}}
    loc={"match":{"status":"EXACT_UNIQUE","nome":"Capo Comino","lat":40.53,"lon":9.78}}
    out=choose(res,ann,loc)
    assert out["final"]["source"]=="ISTAT_LOCALITA"
    assert out["final"]["precision"]=="LOCALITA"

def test_pvp_locality_conflict_goes_review():
    res={"primary":{"coord_valid":True,"lat_pvp":40.0,"lon_pvp":9.0}}
    ann={"anncsu":{"status":"NO_SAFE_MATCH"}}
    loc={"match":{"status":"EXACT_UNIQUE","nome":"X","lat":41.0,"lon":10.0}}
    out=choose(res,ann,loc)
    assert out["final"]["status"]=="REVIEW"
    assert "PVP_LOCALITY_CONFLICT" in out["warnings"]
