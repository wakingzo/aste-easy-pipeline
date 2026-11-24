#!/usr/bin/env python3
"""Offline regression tests for digital-text PVP daily queue."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SRC=Path(__file__).resolve().parents[1]/"src"
sys.path.insert(0,str(SRC))
from importer import pvp_document_text_daily_v1 as module


class Response:
    def __init__(self,body,status=200):
        self.ok=200<=status<300
        self.status_code=status
        self.content=b"data" if body is not None else b""
        self._body=body
    def json(self):
        return self._body


class TestPVPTextDaily(unittest.TestCase):
    def test_read_only_rpc_does_not_request_minimal_return(self):
        session=MagicMock()
        session.request.return_value=Response(318344339)
        result=module.database_size(session,"https://db.supabase.co","fake")
        self.assertEqual(result,318344339)
        self.assertNotIn("Prefer",session.request.call_args.kwargs["headers"])
        session.request.return_value=Response([])
        rows=module.queue(session,"https://db.supabase.co","fake",150)
        self.assertEqual(rows,[])

    def test_table_upsert_uses_conflict_resolution(self):
        session=MagicMock()
        session.request.return_value=Response(None)
        module.supabase(session,"POST","https://db.supabase.co","fake",
                        "listing_allegati_testo",
                        params={"on_conflict":"allegato_id"},
                        payload={"allegato_id":"test"})
        headers=session.request.call_args.kwargs["headers"]
        self.assertEqual(headers["Prefer"],"resolution=merge-duplicates,return=minimal")

    def test_one_document_saved_and_roundtrip_verified(self):
        saved={}
        def fake_rpc(db,method,base,key,resource,*,params=None,payload=None):
            if method=="POST" and resource=="listing_allegati_testo":
                saved.update(payload)
                return None
            if method=="GET" and resource=="listing_allegati_testo":
                return [dict(saved)]
            raise AssertionError(f"Unexpected endpoint {method} {resource}")
        row={"allegato_id":"01234567-89ab-cdef-0123-456789abcdef",
             "pvp_id":"4664833",
             "resource_url":"https://resource-pvp.giustizia.it/allegati/4664833/a.pdf",
             "size_bytes":100}
        result={"status":"ok","first":"6° AVVISO DI VENDITA",
                "full":"6° AVVISO DI VENDITA\n\nAltra pagina",
                "pages":2,"parsed_experiment":"VI","full_text_bytes":43}
        with patch.object(module,"fetch_pdf",return_value=b"%PDF-1.4 fake"),\
             patch.object(module,"extract_digital_text",return_value=result),\
             patch.object(module,"supabase",side_effect=fake_rpc):
            out=module.process_one(None,None,"https://db.supabase.co","secret",
                                   row,apply=True)
        self.assertTrue(out["verified"])
        self.assertEqual(out["experiment_found"],"VI")
        self.assertEqual(saved["stato_estrazione"],"ok")
        self.assertEqual(saved["testo_completo"],result["full"])
        self.assertEqual(set(saved),module.ROW_KEYS)

    def test_missing_digital_text_becomes_no_text(self):
        row={"allegato_id":"01234567-89ab-cdef-0123-456789abcdef",
             "pvp_id":"4664833",
             "resource_url":"https://resource-pvp.giustizia.it/allegati/4664833/a.pdf"}
        result={"status":"no_text","first":None,"full":None,
                "pages":1,"parsed_experiment":None,"full_text_bytes":0}
        with patch.object(module,"fetch_pdf",return_value=b"%PDF-1.4 fake"),\
             patch.object(module,"extract_digital_text",return_value=result):
            out=module.process_one(None,None,"https://db.supabase.co","secret",
                                   row,apply=False)
        self.assertFalse(out["verified"])
        self.assertEqual(out["status"],"no_text")

    def test_size_guard_and_retry_queue(self):
        with patch.dict(os.environ,{
            "SUPABASE_URL":"https://db.supabase.co",
            "SUPABASE_SERVICE_ROLE":"fake",
            "ALLOW_PVP_TEXT_DAILY_WRITE":"YES_NEW_ONLY"
        }):
            with tempfile.TemporaryDirectory() as td,\
                patch.object(module,"REPORTS",Path(td)),\
                patch.object(module,"database_size",return_value=430_000_001),\
                patch.object(module,"queue",side_effect=AssertionError("no reads")):
                report=module.run(apply=True,max_items=100,max_seconds=60)
                self.assertEqual(report["stop_reason"],"database_size_guard")
                self.assertEqual(report["processed"],0)

    def test_apply_requires_server_opt_in(self):
        with patch.dict(os.environ,{
            "SUPABASE_URL":"https://db.supabase.co",
            "SUPABASE_SERVICE_ROLE":"fake",
        },clear=True):
            with self.assertRaises(PermissionError):
                module.run(apply=True)


if __name__=="__main__":
    unittest.main()
