# Aste Easy v3.12.2 - standalone RPC check fix

Fixes `pvp_import_atomic_v1.py --check-rpc` when launched directly.

- includes `src/importer/pvp_import_consolidated_preview.py`
- bootstraps `src`, `src/importer`, `src/pipeline`, and `src/geocoding` in `sys.path`
- no DB behavior changes
- no Google behavior changes
- SQL migration is unchanged
