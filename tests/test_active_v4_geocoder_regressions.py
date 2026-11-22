"""Regression guards for the active v4 offline geocoder and historical backfill.

All tests are pure and require no database, ANNCSU index, Google calls, or
Supabase connection.
"""

from src.geocoding.unified_offline_v1 import choose
from src.geocoding.primary_pin_publisher_v2_preview import published_confidence
from src.geocoding.google_civic_arbitration_v1 import eligible, judge, ConflictArbitrator


def _civic_case(*, pvp_lat=40.0, with_pvp=True):
    res = {
        "primary": {
            "coord_valid": with_pvp,
            "lat_pvp": pvp_lat if with_pvp else None,
            "lon_pvp": 9.0 if with_pvp else None,
        }
    }
    ann = {
        "anncsu": {
            "status": "MATCH_EXACT",
            "best": {"odonimo": "VIA ROMA"},
            "civic": {"status": "CIVIC_COORD", "lat": 40.0, "lon": 9.0},
        }
    }
    return choose(res, ann, {"match": {"status": "NO_HINT"}})


def test_close_pvp_anncsu_agreement_retains_exact_civic():
    result = _civic_case(pvp_lat=40.001)  # approx 111 m
    assert result["final"]["source"] == "ANNCSU"
    assert result["final"]["precision"] == "CIVICO"
    assert result["final"]["status"] == "RESOLVED"
    assert "PVP_ANNCSU_CIVIC_CONFLICT" not in result["warnings"]


def test_300m_not_a_large_pvp_anncsu_conflict():
    result = _civic_case(pvp_lat=40.003)  # approx 334 m
    assert result["final"]["source"] == "ANNCSU"
    assert result["final"]["status"] == "RESOLVED"
    assert result["final"]["confidence"] == "HIGH"
    assert "PVP_ANNCSU_CIVIC_CONFLICT" not in result["warnings"]


def test_far_pvp_anncsu_disagreement_still_requires_review():
    result = _civic_case(pvp_lat=40.014)  # approx 1.56 km
    assert result["final"]["status"] == "REVIEW"
    assert "PVP_ANNCSU_CIVIC_CONFLICT" in result["warnings"]


def test_no_pvp_coordinate_does_not_veto_anncsu_civic():
    result = _civic_case(with_pvp=False)
    assert result["final"]["status"] == "RESOLVED"
    assert result["final"]["precision"] == "CIVICO"


def test_municipal_map_fallback_never_inherits_high_confidence():
    assert published_confidence({"precision": "COMUNE"}, "HIGH") == "NONE"
    assert published_confidence({"precision": "COMUNE"}, "MEDIUM") == "NONE"
    assert published_confidence({"precision": "COMUNE"}, None) == "NONE"


def test_precise_non_fallback_retains_original_confidence():
    assert published_confidence({"precision": "CIVICO"}, "HIGH") == "HIGH"
    assert published_confidence({"precision": "PVP_COORD"}, "MEDIUM") == "MEDIUM"


def _large_conflict():
    return {
        "pvp_id": "TEST", "is_primary": True, "source": "ANNCSU",
        "precision": "CIVICO", "status": "REVIEW", "confidence": "MEDIUM",
        "anncsu_status": "MATCH_EXACT",
        "warnings": ["PVP_ANNCSU_CIVIC_CONFLICT"],
        "street": "Via Roma", "civic": "40", "comune_pvp": "Olbia",
        "sigla_provincia_pvp": "SS", "cap_pvp": "07026",
        "latitudine": 40.0, "longitudine": 9.0,
        "pvp_latitudine": 40.02, "pvp_longitudine": 9.0,
    }


class _FakeGoogle:
    @staticmethod
    def expected_house_numbers(row):
        assert row["indirizzo"] == "Via Roma 40"
        return {"40"}

    @staticmethod
    def municipality_geo_matches(row, result):
        return result.get("comune_ok", True)

    @staticmethod
    def house_number_relation(expected, got):
        return "exact" if expected == got else "mismatch"

    @staticmethod
    def result_house_numbers(result):
        return {"40"} if result.get("civic_ok", True) else {"41"}

    @staticmethod
    def best_address_match(row, result):
        return result.get("address_match", .99)

    @staticmethod
    def score_forward_candidate(row, result):
        return {"score": result.get("score", 99)}


class _FakeBoundary:
    @staticmethod
    def test(row, *, lat, lon):
        if lat > 41:
            return {"position": "OUTSIDE"}
        return {"position": "INSIDE"}


def _google_result(lat, **kwargs):
    return {
        "ok": True, "lat": lat, "lon": 9.0,
        "location_type": "ROOFTOP", "partial_match": False,
        **kwargs,
    }


def test_large_conflict_eligible_single_bene_only():
    assert eligible(_large_conflict()) == (True, "ELIGIBLE")
    assert eligible(_large_conflict(), number_of_beni=2)[0] is False
    assert eligible({**_large_conflict(), "street": ""})[0] is False


def test_large_conflict_disabled_performs_no_google_call():
    loc = _large_conflict()
    arb = ConflictArbitrator(max_http=0)
    got = arb.resolve(loc, 1, {"comune": "Olbia"}, _FakeBoundary())
    assert got is loc
    assert arb.summary()["network_attempts"] == 0
    assert arb.summary()["counts"]["disabled_no_google"] == 1


def test_google_strict_rooftop_near_anncsu_confirms_anncsu():
    loc = _large_conflict()
    got, why = judge(loc, {"comune": "Olbia"}, [_google_result(40.0003)], _FakeGoogle(), _FakeBoundary())
    assert why == "ANNCSU_CONFIRMED"
    assert got["source"] == "ANNCSU"
    assert got["precision"] == "CIVICO"
    assert got["confidence"] == "HIGH"
    assert got["status"] == "RESOLVED"
    assert got["latitudine"] == loc["latitudine"]


def test_google_strict_rooftop_near_pvp_confirms_pvp_without_civic_promotion():
    loc = _large_conflict()
    got, why = judge(loc, {"comune": "Olbia"}, [_google_result(40.0202)], _FakeGoogle(), _FakeBoundary())
    assert why == "PVP_CONFIRMED"
    assert got["source"] == "PVP"
    assert got["precision"] == "PVP_COORD"
    assert got["status"] == "RESOLVED"
    assert got["latitudine"] == loc["pvp_latitudine"]
    assert got["confidence"] == "MEDIUM_HIGH"


def test_google_partial_nonrooftop_or_wrong_civic_never_confirms():
    bad = [
        _google_result(40.0002, partial_match=True),
        _google_result(40.0002, location_type="GEOMETRIC_CENTER"),
        _google_result(40.0002, civic_ok=False),
        _google_result(40.0002, comune_ok=False),
        _google_result(40.0002, address_match=.50),
    ]
    for candidate in bad:
        got, why = judge(_large_conflict(), {"comune": "Olbia"}, [candidate], _FakeGoogle(), _FakeBoundary())
        assert got is None
        assert why == "NO_STRICT_GOOGLE_AGREEMENT"


def test_google_near_neither_candidate_never_confirms():
    got, why = judge(
        _large_conflict(), {"comune": "Olbia"},
        [_google_result(40.0100)], _FakeGoogle(), _FakeBoundary()
    )
    assert got is None
    assert why == "NO_STRICT_GOOGLE_AGREEMENT"


def test_google_ambiguous_two_rooftops_cannot_arbitrate():
    got, why = judge(
        _large_conflict(), {"comune": "Olbia"},
        [_google_result(40.0001), _google_result(40.0201)],
        _FakeGoogle(), _FakeBoundary()
    )
    assert got is None
    assert why == "CONTRADICTORY_GOOGLE_RESULTS"


def test_arbitration_rejects_any_outside_google_point():
    got, why = judge(
        _large_conflict(), {"comune": "Olbia"},
        [_google_result(41.6)], _FakeGoogle(), _FakeBoundary()
    )
    assert got is None
    assert why == "NO_STRICT_GOOGLE_AGREEMENT"
