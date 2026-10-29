# Aste Easy — production pipeline v3.18

Cloud-ready packaging of the validated Aste Easy daily PVP pipeline.

Start here: `docs/README_GITHUB_ACTIONS_V1.md`.

Production entrypoint: `src/pipeline/daily_pipeline_v3_18.py`.

Secrets are never stored in the repository. Large ANNCSU data and mutable runtime state are bootstrapped through a private GitHub Release and then persisted with GitHub Actions cache.
