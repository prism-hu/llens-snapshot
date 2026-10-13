#!/usr/bin/env bash
set -euo pipefail

# Provisional: --reasoning-parser / --tool-call-parser reuse the V3-series parsers
# (deepseek-v3 / deepseekv32). V4 uses a new chat template (encoding_dsv4) that is
# likely incompatible with the V3 series; replace these once an SGLang release with
# V4 support is out (details in the DeepSeek V4 Pro section of the README).

source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

exec uv run --no-sync sglang serve \
  "${SGLANG_AUTH[@]}" \
  --model-path ./models/DeepSeek-V4-Pro \
  --tp 8 \
  --mem-fraction-static 0.9 \
  --context-length 131072 \
  --chunked-prefill-size 16384 \
  --schedule-conservativeness 1.5 \
  --max-running-requests 8 \
  --cuda-graph-max-bs 8 \
  --reasoning-parser deepseek-v3 \
  --tool-call-parser deepseekv32 \
  --trust-remote-code \
  --served-model-name deepseek-v4-pro \
  --enable-metrics \
  --host 0.0.0.0 \
  --port 13300
