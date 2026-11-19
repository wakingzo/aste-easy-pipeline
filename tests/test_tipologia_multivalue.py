import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "importer"))

import pvp_import_consolidated_preview as importer


def test_collects_all_bene_tipologie_and_keeps_real_residential_distinction():
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
    assert value == "Abitazione civile | Garage/Autorimessa | Terreno"


def test_residential_classes_remain_distinct():
    detail = {
        "beni": [
            {"descTipologiaBene": "APPARTAMENTO"},
            {"descTipologiaBene": "ABITAZIONE DI TIPO CIVILE"},
            {"descTipologiaBene": "ABITAZIONE DI TIPO ECONOMICO"},
            {"descTipologiaBene": "ABITAZIONE DI TIPO POPOLARE"},
            {"descTipologiaBene": "ABITAZIONE DI TIPO ULTRAPOPOLARE"},
            {"descTipologiaBene": "ABITAZIONE DI TIPO SIGNORILE"},
        ]
    }
    assert importer.pick_tipologie(detail, {}, "") == (
        "Abitazione civile | Abitazione economica | Abitazione popolare | "
        "Abitazione signorile | Abitazione ultrapopolare | Appartamento"
    )


def test_duplicate_synonyms_inside_same_family_are_collapsed():
    detail = {
        "beni": [
            {"descTipologiaBene": "ABITAZIONE_TIPO_CIV"},
            {"descTipologiaBene": "ABITAZIONE DI TIPO CIVILE"},
            {"descTipologiaBene": "A/2"},
            {"descTipologiaBene": "GARAGE"},
            {"descTipologiaBene": "AUTORIMESSA"},
            {"descTipologiaBene": "BOX"},
            {"descTipologiaBene": "MAGAZZINO"},
            {"descTipologiaBene": "LOCALE DI DEPOSITO"},
            {"descTipologiaBene": "UFFICIO"},
            {"descTipologiaBene": "STUDIO PRIVATO"},
        ]
    }
    assert importer.pick_tipologie(detail, {}, "") == (
        "Abitazione civile | Garage/Autorimessa | Magazzino/Deposito | Ufficio"
    )


def test_villa_family_does_not_fall_into_generic_abitazione():
    detail = {
        "lotto": {
            "beni": [
                {"descTipologiaBene": "ABITAZIONE IN VILLINI"},
                {"descTipologiaBene": "VILLA"},
                {"descTipologiaBene": "A/7"},
            ]
        }
    }
    assert importer.pick_tipologie(detail, {}, "") == "Villa/Villino"


def test_unknown_pvp_type_is_preserved():
    detail = {"beni": [{"descTipologiaBene": "LABORATORIO_ARTIGIANALE"}]}
    assert importer.pick_tipologie(detail, {}, "") == "Laboratorio Artigianale"


def test_search_item_is_used_when_beni_have_no_type():
    search = {"categoriaBene": ["TERRENO", "APPARTAMENTO"]}
    assert importer.pick_tipologie({}, search, "") == "Appartamento | Terreno"
