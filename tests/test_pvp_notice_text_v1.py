#!/usr/bin/env python3
"""Only synthetic first-page text and bytes; uploaded real notices stay private."""
import io
import sys
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from importer.pvp_notice_text_v1 import (
    extract_attempt_from_first_page_text,read_first_page_pdf
)
from pypdf import PdfWriter

class TestPDFNoticeText(unittest.TestCase):
    def test_four_real_title_patterns(self):
        samples=[
            ("TRIBUNALE DI CAGLIARI\nR.G.4408/2024\nAVVISO DI VENDITA\nIV ESPERIMENTO\nLOTTO Unico","IV"),
            ("ESEC. IMMOBILIARI\nAVVISO DI VENDITA DI BENI IMMOBILI\nVI  ESPERIMENTO\nLotto 1","VI"),
            ("AVVISO DI VENDITA DI BENI IMMOBILI\nPROCEDURA ESECUTIVA N.168/2024\n2^ ESPERIMENTO","II"),
            ("PROCEDURA ESECUTIVA N.73/2022 RGE\nAVVISO DI VENDITA\n1° ESPERIMENTO","I"),
        ]
        for text,expect in samples:
            with self.subTest(expect=expect):
                x=extract_attempt_from_first_page_text(text)
                self.assertEqual(x["status"],"explicit")
                self.assertEqual(x["roman"],expect)
                self.assertGreaterEqual(x["evidence"][0]["line"],1)
                self.assertLessEqual(x["evidence"][0]["line"],4)

    def test_concordato_without_attempt(self):
        text="TRIBUNALE DI CAGLIARI\nCONCORDATO PREVENTIVO N.14/2020\nLotti 3,4,5\n"
        x=extract_attempt_from_first_page_text(text)
        self.assertEqual(x["status"],"not_found")
        self.assertIsNone(x["roman"])
        self.assertEqual(x["document_type"],"concordato_preventivo")

    def test_generic_avviso_does_not_assume_first(self):
        x=extract_attempt_from_first_page_text(
            "PROCEDURA ESECUTIVA 200/2024\nAVVISO DI VENDITA\n"
            "Lotto 4\nGara ex art. 573 c.p.c.\nOfferta minima euro 120000")
        self.assertEqual(x["status"],"not_found")
        self.assertIsNone(x["roman"])

    def test_first_page_multi_line_headings_and_reverse(self):
        samples=[
            ("TRIBUNALE\n2^\n ESPERIMENTO\n","II"),
            ("VENDITA\nESPERIMENTO N. 4\n","IV"),
            ("TRIBUNALE\nTerzo tentativo di vendita\n","III"),
            ("AVVISO\nXVI ESPERIMENTO\n","XVI"),
            ("AVVISO\n10o ESPERIMENTO\n","X"),
        ]
        for text,expect in samples:
            with self.subTest(text=text):
                self.assertEqual(
                    extract_attempt_from_first_page_text(text)["roman"],expect)

    def test_conflicting_header_numbers_go_to_review(self):
        x=extract_attempt_from_first_page_text("IV ESPERIMENTO\nVI ESPERIMENTO\n")
        self.assertEqual(x["status"],"ambiguous")
        self.assertIsNone(x["roman"])
        self.assertEqual({v["roman"] for v in x["evidence"]},{"IV","VI"})

    def test_reject_nonpdf(self):
        with self.assertRaisesRegex(ValueError,"Not a PDF"):
            read_first_page_pdf(b"<html>not a pdf</html>")

    def test_blank_first_page_is_unreadable_without_ocr(self):
        writer=PdfWriter()
        writer.add_blank_page(width=595,height=842)
        out=io.BytesIO()
        writer.write(out)
        x=read_first_page_pdf(out.getvalue())
        self.assertEqual(x["status"],"unreadable_text")
        self.assertEqual(x["pages_total"],1)

if __name__=="__main__":
    unittest.main()
