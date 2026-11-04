from src.geocoding.experimental_address_resolver_v1 import parse_address, valid_coord, first_civic_component


def test_civic_and_internal():
    p = parse_address("VIA CLAUDIO MONTEVERDI, 203 - INT. 8")
    assert p["primary"]["street"].upper() == "VIA CLAUDIO MONTEVERDI"
    assert p["primary"]["civic_for_geocode"] == "203"
    assert p["primary"]["internal"] == "8"


def test_snc():
    p = parse_address("VIA TRIESTE SNC")
    assert p["primary"]["street"].upper() == "VIA TRIESTE"
    assert p["primary"]["snc"] is True
    assert p["primary"]["civic_for_geocode"] is None


def test_intersection():
    p = parse_address("Via Martiri di Otranto ang. Don Minzoni")
    assert p["is_intersection"] is True
    assert p["primary"]["street"].upper() == "VIA MARTIRI DI OTRANTO"
    assert p["secondary"]["street"].upper() == "DON MINZONI"


def test_multi_civic_representative():
    assert first_civic_component("55-56") == "55"
    assert first_civic_component("43/45") == "43"
    assert first_civic_component("171/A") == "171/A"
    assert first_civic_component("289/TER") == "289/TER"


def test_coordinates():
    assert valid_coord(41.9, 12.5)[0] is True
    assert valid_coord(0, 0)[1] == "ZERO_ZERO"
    assert valid_coord(45.0, 9190000)[1] == "OUT_OF_RANGE"
