#!/usr/bin/env python3
"""Regression tests: do not infer sale attempts from attachment filenames."""
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from importer.pvp_sale_fields_v1 import pvp_lot_number, pvp_sale_attempt


class TestPVPSaleFields(unittest.TestCase):
    def test_missing_experiment_must_never_be_first_by_default(self):
        self.assertEqual(pvp_sale_attempt({}, {}), "")
        self.assertEqual(pvp_sale_attempt({"numeEsperimento": None}, {}), "")
        self.assertEqual(pvp_sale_attempt({}, {"numeEsperimento": ""}), "")

    def test_only_real_api_field_can_set_experiment(self):
        self.assertEqual(pvp_sale_attempt({"numeEsperimento": "II"}, {}), "II")
        self.assertEqual(pvp_sale_attempt({}, {"numeEsperimento": "III"}), "III")
        self.assertEqual(pvp_sale_attempt({"numeEsperimento": "V"}, {"numeEsperimento": "X"}), "V")
        self.assertEqual(pvp_sale_attempt({"numeEsperimento": "VI/VII"}, {}), "VI")

    def test_filename_fingerprint_is_irrelevant(self):
        self.assertEqual(pvp_sale_attempt({
            "allegati": [
                {"filename": "DELEGA PRIVACY.pdf"},
                {"filename": "00057610000093400244311attoprincipale_1 avviso di vendita.docx.pdf"},
                {"filename": "AVVISO DI VENDITA XENA IMMOBILIARE - 1 TENTATIVO.docx"},
            ]
        }, {}), "")

    def test_no_codi_lotto_fallback(self):
        self.assertEqual(pvp_lot_number({"lotto": {"codiLotto": "2639816"}}, {}), "")
        self.assertEqual(pvp_lot_number({"lotto": {"numeroLotto": "2", "codiLotto": "2639816"}}, {}), "2")

    def test_quimmo_code_must_not_be_lot(self):
        desc = "Terreni agricoli a Decimomannu. Per info Quimmo - Codice annuncio 2639816"
        self.assertEqual(pvp_lot_number({"lotto": {"codiLotto": "8"}}, {"numeroLotto": "2639816"}, desc), "")
        self.assertEqual(pvp_lot_number({"lotto": {"numeroLotto": "2"}}, {"numeroLotto": "2639816"}, desc), "2")
        self.assertEqual(pvp_lot_number({}, {"numeroLotto": "2639816"}, desc), "")

    def test_real_six_digit_number_not_discarded_by_length_alone(self):
        self.assertEqual(pvp_lot_number({}, {"numeroLotto": "123456"}, "Lotto 123456"), "123456")
        self.assertEqual(pvp_lot_number({}, {"numeroLotto": "123456"}, "Codice annuncio 999999"), "123456")

    def test_different_forms_of_ad_code_are_rejected(self):
        for label in ("Codice annuncio 2639816", "codice annuncio: 2639816", "CODICE ANNUNCIO N. 2639816"):
            with self.subTest(label=label):
                self.assertEqual(pvp_lot_number({}, {"numeroLotto": "2639816"}, label), "")

    def test_no_filename_parser_or_codilotto_in_production_derive(self):
        for relative in (
            "src/importer/pvp_import_consolidated_preview.py",
            "src/importer/pvp_import_from_api_raw_hybrid_FINAL.py",
            "src/importer/build_listings_daily_v3_16.py",
            "src/importer/build_listings_from_pvp_api_RAW_ONLY.py",
        ):
            with self.subTest(file=relative):
                content = (ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("extract_esperimento_from_filenames", content)
                self.assertIn("pvp_lot_number(detail_body, search_item", content)
                self.assertIn("pvp_sale_attempt(detail_body, search_item)", content)


if __name__ == "__main__":
    unittest.main()
