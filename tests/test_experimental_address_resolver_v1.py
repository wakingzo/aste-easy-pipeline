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


def test_n_prefix_not_stripped_from_name():
    p = parse_address("Via Nicola Agresti 32")
    assert p["primary"]["street"].upper() == "VIA NICOLA AGRESTI"
    assert p["primary"]["civic_for_geocode"] == "32"


def test_snc_label_cleanup():
    p = parse_address("regione santa maria di castiglione, n. snc")
    assert p["primary"]["street"].upper() == "REGIONE SANTA MARIA DI CASTIGLIONE"
    assert p["primary"]["snc"] is True


def test_embedded_full_postal_address():
    p = parse_address("Via dell'Industria, 16, 00034 Colleferro RM, Italia")
    assert p["primary"]["street"].upper() == "VIA DELL'INDUSTRIA"
    assert p["primary"]["civic_for_geocode"] == "16"


def test_snc_no_trailing_punctuation():
    p = parse_address("Contrada Spalla, Snc, 96010 Melilli SR, Italia")
    assert not (p["primary"]["street"] or "").endswith(",")
    assert ", ," not in (p["primary"]["street"] or "")


def test_embedded_postal_without_civic():
    p = parse_address("Via Proserpina, 91100 Trapani TP, Italia")
    assert p["primary"]["street"].upper() == "VIA PROSERPINA"
    assert p["primary"]["civic_for_geocode"] is None


def test_cangiani_not_intersection():
    p = parse_address("Vico Cangiani Al Mercato, 23, 80133 Napoli NA, Italia")
    assert p["is_intersection"] is False
    assert p["primary"]["street"].upper() == "VICO CANGIANI AL MERCATO"
    assert p["primary"]["civic_for_geocode"] == "23"


def test_floor_abbreviation_not_civic():
    p = parse_address("VIA NAZIONALE P.2")
    assert p["primary"]["street"].upper() == "VIA NAZIONALE"
    assert p["primary"]["internal"] == "2"
    assert p["primary"]["civic_for_geocode"] is None


def test_slash_letter_civic():
    p = parse_address("via Nazionale, 25/A")
    assert p["primary"]["street"].upper() == "VIA NAZIONALE"
    assert p["primary"]["civic_for_geocode"] == "25/A"


def test_embedded_postal_tail_without_civic_is_removed():
    p = parse_address("Via Proserpina, 91100 Trapani TP, Italia")
    assert p["primary"]["street"].upper() == "VIA PROSERPINA"
    assert p["primary"]["civic_for_geocode"] is None


def test_incrocio_con_drops_con():
    p = parse_address("via dei Taurini incrocio con via Degli Uliveti")
    assert p["is_intersection"] is True
    assert p["secondary"]["street"].upper() == "VIA DEGLI ULIVETI"
