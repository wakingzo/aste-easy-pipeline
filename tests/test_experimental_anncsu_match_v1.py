from src.geocoding.experimental_anncsu_match_v1 import street_score

def test_cavour_alias_scores_high():
    s, ev = street_score("Via Cavour", "Via Camillo Benso Conte di Cavour")
    assert s >= 0.93
    assert ev["mode"] == "SOURCE_TOKENS_CONTAINED"

def test_road_kind_mismatch_is_penalized():
    a, _ = street_score("Via Cavour", "Via Camillo Benso Conte di Cavour")
    b, _ = street_score("Via Cavour", "Piazza Camillo Benso Conte di Cavour")
    assert a > b

def test_preposition_difference_is_tolerated():
    s, _ = street_score("Via della Repubblica", "Via Repubblica")
    assert s >= 0.95


def test_single_token_alias_requires_tail_match():
    good, ev_good = street_score("Via Cavour", "Via Camillo Benso Conte di Cavour")
    bad, ev_bad = street_score("Via S. Francesco", "Via Francesco Crispi")
    assert good > 0.93 and ev_good["single_token_tail_ok"] is True
    assert bad > 0.93 and ev_bad["single_token_tail_ok"] is False
