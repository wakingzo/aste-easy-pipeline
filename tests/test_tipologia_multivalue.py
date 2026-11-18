from src.importer import pvp_import_consolidated_preview as importer


def test_collects_all_bene_tipologie_and_normalizes_synonyms():
    detail = {
        "lotto": {
            "beni": [
                {"descTipologiaBene": "ABITAZIONE_TIPO_CIV"},
                {"categoriaBene": "TERRENO"},
                {"descTipologiaBene": "GARAGE_AUTORIMESSA"},
            ]
        }
    }
    value = importer.pick_tipologie(detail, {}, "")
    assert value == "Abitazione | Garage/Autorimessa | Terreno"


def test_duplicate_synonyms_are_collapsed():
    detail = {
        "beni": [
            {"descTipologiaBene": "APPARTAMENTO"},
            {"descTipologiaBene": "ABITAZIONE DI TIPO CIVILE"},
            {"descTipologiaBene": "APPARTAMENTO"},
        ]
    }
    assert importer.pick_tipologie(detail, {}, "") == "Abitazione"


def test_villa_family_does_not_fall_into_generic_abitazione():
    detail = {
        "lotto": {
            "beni": [
                {"descTipologiaBene": "ABITAZIONE IN VILLINI"},
                {"descTipologiaBene": "VILLA"},
            ]
        }
    }
    assert importer.pick_tipologie(detail, {}, "") == "Villa/Villino"


def test_unknown_pvp_type_is_preserved():
    detail = {"beni": [{"descTipologiaBene": "LABORATORIO_ARTIGIANALE"}]}
    assert importer.pick_tipologie(detail, {}, "") == "Laboratorio Artigianale"


def test_search_item_is_used_when_beni_have_no_type():
    search = {"categoriaBene": ["TERRENO", "APPARTAMENTO"]}
    assert importer.pick_tipologie({}, search, "") == "Abitazione | Terreno"
