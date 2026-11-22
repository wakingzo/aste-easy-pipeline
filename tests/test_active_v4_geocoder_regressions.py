"""Regression guards for the active v4 offline geocoder and historical backfill.

All tests are pure and require no database, ANNCSU index, Google calls, or
Supabase connection.
"""

from src.geocoding.unified_offline_v1 import choose
from src.geocoding.primary_pin_publisher_v2_preview import published_confidence


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


def test_300m_pvp_anncsu_disagreement_requires_review():
    result = _civic_case(pvp_lat=40.003)  # approx 334 m
    assert result["final"]["source"] == "ANNCSU"
    assert result["final"]["status"] == "REVIEW"
    assert result["final"]["confidence"] == "MEDIUM"
    assert "PVP_ANNCSU_CIVIC_CONFLICT" in result["warnings"]


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
