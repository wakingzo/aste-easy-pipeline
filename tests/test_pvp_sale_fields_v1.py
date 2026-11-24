#!/usr/bin/env python3
"""Regression tests: source-provenanced API + contextual March parser fallback."""
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from importer.pvp_sale_fields_v1 import pvp_lot_number, pvp_sale_attempt
from importer.pvp_sale_attempt_parser_v1 import parse_sale_attempt_from_filenames, resolve_sale_attempt, attempt_number


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

    def test_detail_codlotto_matches_search_number(self):
        # Verified against 20/20 real PVP sample response pairs.
        self.assertEqual(pvp_lot_number({"lotto": {"codLotto": "LOTTO UNICO"}}, {"numeroLotto": "LOTTO UNICO"}), "LOTTO UNICO")
        self.assertEqual(pvp_lot_number({"lotto": {"codLotto": "2"}}, {}), "2")
        self.assertEqual(pvp_lot_number({"lotto": {"codLotto": "3"}}, {}), "3")
        self.assertEqual(pvp_lot_number({"lotto": {"codLotto": "01"}}, {"numeroLotto": "01"}), "01")
        self.assertEqual(pvp_lot_number({"lotto": {"codiLotto": "2639816"}}, {}), "")
        self.assertEqual(pvp_lot_number({"lotto": {"codLotto": "2", "codiLotto": "2639816"}}, {}), "2")

    def test_quimmo_code_must_not_be_lot(self):
        desc = "Terreni agricoli a Decimomannu. Per info Quimmo - Codice annuncio 2639816"
        self.assertEqual(pvp_lot_number({"lotto": {"codLotto": "2639816"}}, {}, desc), "")
        self.assertEqual(pvp_lot_number({"lotto": {"codLotto": "2639816"}}, {"numeroLotto": "2639816"}, desc), "")
        self.assertEqual(pvp_lot_number({"lotto": {"codiLotto": "8"}}, {"numeroLotto": "2639816"}, desc), "")
        self.assertEqual(pvp_lot_number({"lotto": {"codLotto": "2"}}, {"numeroLotto": "2639816"}, desc), "2")
        self.assertEqual(pvp_lot_number({}, {"numeroLotto": "2639816"}, desc), "")

    def test_real_six_digit_number_not_discarded_by_length_alone(self):
        self.assertEqual(pvp_lot_number({}, {"numeroLotto": "123456"}, "Lotto 123456"), "123456")
        self.assertEqual(pvp_lot_number({}, {"numeroLotto": "123456"}, "Codice annuncio 999999"), "123456")

    def test_different_forms_of_ad_code_are_rejected(self):
        for label in ("Codice annuncio 2639816", "codice annuncio: 2639816", "CODICE ANNUNCIO N. 2639816"):
            with self.subTest(label=label):
                self.assertEqual(pvp_lot_number({}, {"numeroLotto": "2639816"}, label), "")

    def test_contextual_parser_march_examples(self):
        examples = [
            ("IV_ AVVISO VENDITA BENI ARCIDOSSO LOTTO 4_1_redacted.pdf", "IV"),
            ("2° esperimento.pdf", "II"),
            ("2 esperimento.pdf", "II"),
            ("VI gara.pdf", "VI"),
            ("6° gara.pdf", "VI"),
            ("SESTA GARA.pdf", "VI"),
            ("seconda vendita.pdf", "II"),
            ("AVVISO 5vendita.pdf", "V"),
            ("avviso n. 4.pdf", "IV"),
            ("AVVISO DI VENDITA LOTTO 17 XENA IMMOBILIARE - 1 TENTATIVO.docx", "I"),
            ("3° TENTATIVO DI VENDITA.pdf", "III"),
            ("10^ AVVISO DI VENDITA.pdf", "X"),
            ("vendita VI.pdf", "VI"),
        ]
        for filename, expected in examples:
            with self.subTest(filename=filename):
                self.assertEqual(parse_sale_attempt_from_filenames([filename])["value"], expected)

    def test_unrelated_numbers_and_machine_filenames_are_ignored(self):
        filenames = [
            "DELEGA PRIVACY .pdf",
            "00057610000093400244311attoprincipale_1 avviso di vendita.docx.pdf",
            "AVVISO DI VENDITA LOTTO 4.pdf",
            "Avviso di vendita 2 (2).pdf",
            "AVVISO VENDITA 19.01.2027.pdf",
            "FOTO LOTTO 9.pdf",
            "Avviso di vendita terreno VIGONOVO.pdf",
            "perizia in data 05-12-2026.pdf",
            "AVVISO VENDITA XENA IMMOBILIARE.pdf",
            "planimetria piano VI.pdf",
        ]
        for filename in filenames:
            with self.subTest(filename=filename):
                self.assertEqual(parse_sale_attempt_from_filenames([filename])["value"], "")

    def test_real_sample_calendar_and_procedure_numbers_never_override_attempt(self):
        # Discovered on Nov 24 2026 in a 300-RAW sample; initially affected 11 rows.
        examples = [
            ("AVVISO VENDITA DEL 19-02-2027 PROC 450-2018.pdf", ""),
            ("Avviso_vendita_secondo_esperimento_14_gennaio_2027_signed_redacted.pdf", "II"),
            ("27-16 avviso 4 asta 10-02-27 mista.pdf", "IV"),
            ("III AVVISO DI VENDITA RGE 211-24-signed.pdf", "III"),
            ("2026.11.20.I.Avviso.vendita_signed.pdf", "I"),
            ("Avviso vendita 1° esp. 09.02.2027 RGEI n. 878_245 lotto 2.pdf", "I"),
            ("I Esperimento L.G. 06-26 - 28.01.2027.pdf", "I"),
            ("IV_ AVVISO VENDITA BENI ARCIDOSSO LOTTO 4_1_redacted.pdf", "IV"),
            ("AVVISO DI VENDITA II Esperimento rgei 43 2025.pdf", "II"),
            ("II AVVISO DI VENDITA - ASTA del 28.01.2027 RGE 40-2025 Lotto 3.pdf", "II"),
        ]
        for filename, expected in examples:
            with self.subTest(filename=filename):
                match = parse_sale_attempt_from_filenames([filename])
                self.assertEqual(match["value"], expected)
                self.assertFalse(match["multiple_values"], filename)

    def test_date_year_with_month_is_not_a_fourteenth_attempt(self):
        result = parse_sale_attempt_from_filenames(
            ["Avviso_vendita_secondo_esperimento_14_gennaio_2027_signed_redacted.pdf"]
        )
        self.assertEqual(result["value"], "II")
        self.assertEqual(len(result["evidence"]), 1)
        self.assertEqual(result["evidence"][0]["marker"], "SECONDO")

    def test_lowercase_x_means_per_not_roman_ten(self):
        cases = [
            "aavviso di vendita RGE 296 2022 x asta 11 nov 2026.pdf",
            "ESTRATTO avviso x pubblicazione su PVP es. 300 2019 x 24.11.2026.pdf",
            "Ordinanza di vendita x asta 26112026.pdf",
            "Avviso vendita x ud 15.12.26_signed.pdf",
            "Avviso di vendita IV incanto x Comune_Trib .pdf",
        ]
        for name in cases:
            with self.subTest(filename=name):
                result = parse_sale_attempt_from_filenames([name])
                expected = "IV" if "IV incanto" in name else ""
                self.assertEqual(result["value"], expected)
        self.assertEqual(
            parse_sale_attempt_from_filenames(["X avviso 18.11.2026.pdf"])["value"], "X"
        )

    def test_multiple_prior_notices_choose_highest_valid_and_expose_conflict(self):
        outcome = parse_sale_attempt_from_filenames([
            "I AVVISO VENDITA.pdf",
            "IV AVVISO VENDITA.pdf",
            "II AVVISO VENDITA.pdf",
            "DELEGA PRIVACY.pdf",
        ])
        self.assertEqual(outcome["value"], "IV")
        self.assertTrue(outcome["multiple_values"])
        self.assertEqual({m["value"] for m in outcome["evidence"]}, {"I", "II", "IV"})

    def test_api_is_authoritative_and_audit_records_disagreement(self):
        r = resolve_sale_attempt({"numeEsperimento": "2", "allegati": [{"nomeFile": "VI GARA.pdf"}]}, {})
        self.assertEqual(r["value"], "2")
        self.assertEqual(r["source"], "api_detail")
        self.assertEqual(r["parsed_value"], "VI")
        self.assertIs(r["api_parser_agree"], False)
        r = resolve_sale_attempt({"numeEsperimento": "VI", "allegati": [{"nomeFile": "6° GARA.pdf"}]}, {})
        self.assertIs(r["api_parser_agree"], True)
        self.assertEqual(r["source"], "api_detail")
        self.assertEqual(r["value"], "VI")

    def test_api_missing_falls_back_only_when_contextual(self):
        r = resolve_sale_attempt({"allegati": [{"nomeFile": "6° gara.pdf"}]}, {})
        self.assertEqual((r["value"], r["source"]), ("VI", "filename"))
        self.assertIsNone(r["api_parser_agree"])
        r = resolve_sale_attempt({"allegati": [{"nomeFile": "DELEGA PRIVACY.pdf"}]}, {})
        self.assertEqual((r["value"], r["source"]), ("", "missing"))
        r = resolve_sale_attempt({}, {}, ["AVVISO 3° TENTATIVO.pdf"])
        self.assertEqual((r["value"], r["source"]), ("III", "filename"))
        self.assertEqual(attempt_number("10"), 10)

    def test_only_contextual_parser_used_in_production_derive(self):
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
                self.assertIn("resolve_sale_attempt(detail_body, search_item", content)


if __name__ == "__main__":
    unittest.main()
