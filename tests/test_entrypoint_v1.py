import os
import sys
import unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
extra=os.environ.get('ASTE_TEST_EXISTING_SRC')
if extra:sys.path.insert(0,extra)
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT/'src'/'importer'))
sys.path.insert(0,str(ROOT/'src'/'geocoding'))
# Reference directory is normally populated on the user's computer.
import tempfile

class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.old_reference = os.environ.get('ASTE_REFERENCE_DIR')
        os.environ['ASTE_REFERENCE_DIR'] = cls.tmp.name
        (Path(cls.tmp.name)/'comuni_cap.csv').write_text(
            'comune;provincia;sigla_provincia;regione;cap\n'
            'Tempio Pausania;Sassari;SS;Sardegna;07029\n', encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        if cls.old_reference is None: os.environ.pop('ASTE_REFERENCE_DIR',None)
        else: os.environ['ASTE_REFERENCE_DIR']=cls.old_reference
        cls.tmp.cleanup()

    def test_default_does_not_build_or_write(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('entrypoint',ROOT/'src'/'pipeline'/'daily_pipeline_consolidated_v1.py')
        pipeline=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pipeline)
        with mock.patch.object(pipeline.geocoder,'prepare_context',return_value=(object(),object(),None)), \
             mock.patch.object(pipeline.importer,'run',return_value={'eligible_new':616}) as imp, \
             mock.patch.object(pipeline.geocoder,'preview_new_rows',return_value={}) as geo, \
             mock.patch.object(pipeline.subprocess,'run',side_effect=AssertionError('build invoked')):
            self.assertEqual(pipeline.main([]),0)
        imp.assert_called_once()
        self.assertFalse(imp.call_args.kwargs['apply'])
        self.assertTrue(callable(imp.call_args.kwargs['preview_geo']))

    def test_build_without_page_caps_rejected_before_network(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('entrypoint2',ROOT/'src'/'pipeline'/'daily_pipeline_consolidated_v1.py')
        pipeline=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pipeline)
        with mock.patch.object(pipeline.subprocess,'run',side_effect=AssertionError('build invoked')):
            with self.assertRaises(SystemExit) as cm:
                pipeline.main(['--build'])
        self.assertEqual(cm.exception.code,2)

if __name__=='__main__':unittest.main(verbosity=2)
