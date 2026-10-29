#!/bin/sh
set -eu

export ASTE_EASY_CONTAINER="${ASTE_EASY_CONTAINER:-1}"
export TZ="${TZ:-Europe/Rome}"

# Fail closed before any production command. No network calls and no DB writes.
python /app/src/pipeline/container_preflight_v3_18.py
export ASTE_EASY_PREFLIGHT_DONE=1

exec "$@"
