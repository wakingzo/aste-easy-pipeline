from pathlib import Path
import importlib.util
import os
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_google_state_override():
    m = _load(ROOT / 'src' / 'geocoding' / 'google_production_v3_18.py', 'gp318_test')
    class G:
        CACHE_FILE=Path('/legacy/cache.json')
        API_USAGE_FILE=Path('/legacy/usage.json')
        LOG_FILE=Path('/legacy/log.jsonl')
        AUDIT_JSONL=Path('/legacy/audit.jsonl')
        AUDIT_CSV=Path('/legacy/audit.csv')
    with tempfile.TemporaryDirectory() as td:
        old=dict(os.environ)
        try:
            os.environ['GOOGLE_STATE_DIR']=td
            os.environ.pop('GOOGLE_GEOCODE_CACHE_FILE',None)
            os.environ.pop('GOOGLE_API_USAGE_FILE',None)
            g=m._apply_persistent_state_overrides(G())
            assert g.CACHE_FILE == Path(td).resolve()/'google_geocode_cache_v8.json'
            assert g.API_USAGE_FILE == Path(td).resolve()/'google_api_usage_v9_2.json'
        finally:
            os.environ.clear(); os.environ.update(old)


def test_docker_files_present():
    for rel in ('docker/Dockerfile','docker/entrypoint.sh','docker-compose.yml','requirements-container.txt','.env.container.example'):
        assert (ROOT/rel).is_file()
