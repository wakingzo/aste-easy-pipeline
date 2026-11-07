from src.geocoding.experimental_locality_match_v1 import explicit_hint, locality_only_hint

def test_explicit_localita():
    assert explicit_hint("Loc. Capo Comino - Podere 18") == "Capo Comino"

def test_frazione():
    assert explicit_hint("Frazione San Pietro, Via Roma 2") == "San Pietro"

def test_contrada():
    assert explicit_hint("Contrada Sa Petra Ruja, snc") == "Sa Petra Ruja"

def test_locality_only_address():
    assert locality_only_hint("Capo Comino") == "Capo Comino"

def test_road_not_locality_only():
    assert locality_only_hint("Via Roma") == ""
