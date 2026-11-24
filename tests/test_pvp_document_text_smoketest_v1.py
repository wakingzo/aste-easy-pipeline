#!/usr/bin/env python3
"""Offline smoke-test regressions; never call PVP or Supabase."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
SRC=Path(__file__).resolve().parents[1]/"src"
sys.path.insert(0,str(SRC))
from audit import pvp_document_text_smoketest_v1 as pilot


class Page:
    def __init__(self,text):
        self.text=text
    def extract_text(self):
        return self.text


class TestPVPTextPersistence(unittest.TestCase):
    def test_complete_document_and_first_page(self):
        doc=MagicMock(is_encrypted=False)
        doc.pages=[Page("TRIBUNALE\n6° AVVISO DI VENDITA\n"+
                        "La procedura esecutiva ha disposto la vendita."),
                   Page("Il presente documento contiene termini di vendita."*3)]
        with patch.object(pilot,"PdfReader",return_value=doc):
            r=pilot.extract_digital_text(b"%PDF-1.4 fake")
        self.assertEqual(r["pages"],2)
        self.assertEqual(r["parsed_experiment"],"VI")
        self.assertEqual(r["status"],"ok")
        self.assertIn("6° AVVISO DI VENDITA",r["first"])
        self.assertIn("termini di vendita",r["full"])

    def test_scanned_pdf_has_no_text_without_ocr(self):
        doc=MagicMock(is_encrypted=False)
        doc.pages=[Page(""),Page("")]
        with patch.object(pilot,"PdfReader",return_value=doc):
            r=pilot.extract_digital_text(b"%PDF-1.4 fake")
        self.assertEqual(r["status"],"no_text")
        self.assertIsNone(r["full"])
        self.assertEqual(r["full_text_bytes"],0)

    def test_encrypted_pdf_does_not_attempt_ocr(self):
        doc=MagicMock(is_encrypted=True)
        with patch.object(pilot,"PdfReader",return_value=doc):
            with self.assertRaisesRegex(ValueError,"Encrypted"):
                pilot.extract_digital_text(b"%PDF-1.4 fake")

    def test_pdf_overflow_is_rejected_not_truncated(self):
        doc=MagicMock(is_encrypted=False)
        doc.pages=[Page("A"*(pilot.MAX_TEXT_BYTES+1))]
        with patch.object(pilot,"PdfReader",return_value=doc):
            with self.assertRaisesRegex(ValueError,"exceeds"):
                pilot.extract_digital_text(b"%PDF-1.4 fake")

    def test_write_requires_second_opt_in(self):
        with patch.dict("os.environ",{
                "SUPABASE_URL":"https://example.supabase.co",
                "SUPABASE_SERVICE_ROLE":"not_used"},
                clear=True):
            with self.assertRaises(PermissionError):
                pilot.pilot(apply=True)

if __name__=="__main__":
    unittest.main()
