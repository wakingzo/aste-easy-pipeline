#!/usr/bin/env python3
"""New-only optional PDF enrichment: no DB or remote network in these tests."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from importer import pvp_notice_enrichment_v1 as m


def row(pid="9876543", filename="AVVISO DI VENDITA.pdf", attempt=""):
    listing={"pvp_id":pid, "lotto":"1", "indirizzo":"non toccare",
             "esperimento_vendita":attempt,"prezzo_base":"100000"}
    attachment={"pvp_id":pid,"filename":filename,
                "resource_url":f"https://resource-pvp.giustizia.it/allegati/{pid}/avviso.pdf",
                "size_bytes":1000}
    raw={"pvp_id":pid,"search_item":{},"detail_body":{
         "idVendita":pid, "allegati":[{"nomeFile":filename}]}}
    return (listing,[],[attachment],[]),raw


class TestNewOnlyEnrichment(unittest.TestCase):
    def test_selection_rejects_non_notice_and_untrusted_hosts(self):
        self.assertEqual(m.notice_score("PERIZIA.pdf"),-1)
        self.assertEqual(m.notice_score("Avviso affissione.pdf"),-1)
        self.assertGreater(m.notice_score("AVVISO DI VENDITA.pdf"),0)
        self.assertGreater(m.notice_score("BANDO DI VENDITA.pdf"),0)
        self.assertFalse(m.safe_url("http://resource-pvp.giustizia.it/a.pdf"))
        self.assertFalse(m.safe_url("https://other.example/a.pdf"))
        self.assertFalse(m.safe_url("https://resource-pvp.giustizia.it.evil.example/a.pdf"))

    def test_pdf_explicit_corroboration_and_conflicts(self):
        rec,raw=row()
        with patch.object(m,"fetch_pdf",return_value=b"%PDF-1.4 fake"),\
             patch.object(m,"read_first_page_pdf",return_value={
                 "status":"explicit","roman":"VI","number":6,
                 "document_type":"esecuzione_immobiliare",
                 "possible_previous_sale":False,
                 "page":1,"evidence":[{"text":"VI ESPERIMENTO"}]}):
            audit,override=m.assess(rec[0],rec[2],raw,None)
        self.assertEqual(audit["decision"],"explicit_pdf_override")
        self.assertEqual(override,"VI")
        self.assertEqual(audit["pdf_value"],"VI")

        rec,raw=row(filename="IV AVVISO DI VENDITA.pdf",attempt="IV")
        with patch.object(m,"fetch_pdf",return_value=b"%PDF-1.4 fake"),\
             patch.object(m,"read_first_page_pdf",return_value={
                 "status":"explicit","roman":"VI","number":6,
                 "page":1,"evidence":[{"text":"VI ESPERIMENTO"}]}):
            audit,override=m.assess(rec[0],rec[2],raw,None)
        self.assertEqual(audit["decision"],"filename_pdf_disagree_review")
        self.assertIsNone(override)

    def test_api_has_priority(self):
        rec,raw=row(attempt="III")
        raw["detail_body"]["numeEsperimento"]="III"
        with patch.object(m,"fetch_pdf",return_value=b"%PDF-1.4 fake"),\
             patch.object(m,"read_first_page_pdf",return_value={
                 "status":"explicit","roman":"IV","number":4,
                 "page":1,"evidence":[{"text":"IV ESPERIMENTO"}]}):
            audit,override=m.assess(rec[0],rec[2],raw,None)
        self.assertEqual(audit["decision"],"api_priority")
        self.assertTrue(audit["pdf_api_disagree"])
        self.assertIsNone(override)

    def test_reduction_flag_does_not_infer_first_or_second(self):
        rec,raw=row()
        with patch.object(m,"fetch_pdf",return_value=b"%PDF-1.4 fake"),\
             patch.object(m,"read_first_page_pdf",return_value={
                 "status":"not_found","roman":None,"number":None,
                 "possible_previous_sale":True,"evidence":[],"page":None}):
            audit,override=m.assess(rec[0],rec[2],raw,None)
        self.assertEqual(audit["decision"],"possible_prior_sale_review")
        self.assertIsNone(override)

    def test_read_only_plan_and_opt_in_in_memory_apply(self):
        rec,raw=row()
        with tempfile.TemporaryDirectory() as td:
            delta=Path(td)/"delta.jsonl"
            delta.write_text(json.dumps(raw)+"\n",encoding="utf-8")
            plan={"records":[rec],"raw_sha256":"fixed","incoming_unique":1,
                  "subjects":0,"attachments":1,"locations":0}
            parsed={"status":"explicit","roman":"V","number":5,
                    "page":1,"evidence":[{"text":"V ESPERIMENTO"}]}
            with patch.object(m,"fetch_pdf",return_value=b"%PDF-1.4 fake"),\
                 patch.object(m,"read_first_page_pdf",return_value=parsed):
                preview=m.enrich_new_plan(plan,delta_raw=delta,report_dir=td,
                                          mode="report",pause=0)
                applied=m.enrich_new_plan(plan,delta_raw=delta,report_dir=td,
                                          mode="apply",pause=0)
            self.assertEqual(preview["records"][0][0],rec[0])
            self.assertEqual(rec[0]["esperimento_vendita"],"")
            self.assertEqual(applied["records"][0][0]["esperimento_vendita"],"V")
            self.assertEqual(applied["records"][0][0]["lotto"],"1")
            self.assertEqual(applied["records"][0][0]["prezzo_base"],"100000")
            self.assertEqual(preview["notice_counts"].get("changed_new_plan_rows",0),0)
            self.assertEqual(applied["notice_counts"]["changed_new_plan_rows"],1)
            self.assertEqual(plan["raw_sha256"],"fixed")
            self.assertEqual(json.loads(Path(preview["notice_report"]).read_text())["pdf_requests"],1)

    def test_budget_zero_no_download_or_override(self):
        rec,raw=row()
        with tempfile.TemporaryDirectory() as td:
            delta=Path(td)/"raw.jsonl"
            delta.write_text(json.dumps(raw)+"\n")
            with patch.object(m,"fetch_pdf",side_effect=AssertionError("MUST NOT GET")):
                result=m.enrich_new_plan({"records":[rec]},delta_raw=delta,
                    report_dir=td,mode="report",max_pdfs=0,pause=0)
            self.assertEqual(result["notice_counts"]["budget_exhausted"],1)
            self.assertEqual(result["records"][0][0],rec[0])

if __name__=="__main__":
    unittest.main()
