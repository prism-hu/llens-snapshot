#!/usr/bin/env bash
# Common prelude for the sglang launch scripts. Reads SGLANG_API_KEY from the
# repo-root .env and prepares the SGLANG_AUTH args array passed to `sglang serve`.
# If no key is set the array is empty = start without authentication (local/dev use).
# Exposed models, key handling and transport procedure: see "SGLang authentication" in DEPLOYMENT.md.

_llens_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ -z "${SGLANG_API_KEY:-}" && -f "$_llens_root/.env" ]]; then
  SGLANG_API_KEY="$(grep -E '^SGLANG_API_KEY=' "$_llens_root/.env" | tail -n1 | cut -d= -f2- || true)"
fi

SGLANG_AUTH=()
if [[ -n "${SGLANG_API_KEY:-}" ]]; then
  SGLANG_AUTH=(--api-key "$SGLANG_API_KEY")
fi

# `uv run --no-sync` in each script: the root uv.lock / pyproject.toml is the inference
# environment and is outside the air-gapped update path (media deps; scripts/media/save.sh).
# If the code checkout has a newer lock, a plain `uv run` tries to sync the venv to the lock
# at startup and fails offline. Run the venv as installed (replacing the inference
# environment is a planned stop, same as for models; see CLAUDE.md).
