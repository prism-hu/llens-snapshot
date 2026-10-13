#!/usr/bin/env bash
set -euo pipefail

# Usage: sglang-kimi-k2.6.sh [--eagle]
#   default : no speculative decoding (known-good)
#   --eagle : enable EAGLE3 speculative decoding (MLA draft; long context still under verification)
EAGLE=0
for arg in "$@"; do
  case "$arg" in
    --eagle) EAGLE=1 ;;
    *) echo "Usage: $0 [--eagle]" >&2; exit 1 ;;
  esac
done

source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

args=(
  "${SGLANG_AUTH[@]}"
  --model-path ./models/Kimi-K2.6
  --tp 8
  --mem-fraction-static 0.87
  --context-length 262144
  --schedule-conservativeness 1.5
  --max-running-requests 16
  --trust-remote-code
  --reasoning-parser kimi_k2
  --tool-call-parser kimi_k2
  --served-model-name kimi-k2.6
  --enable-metrics
  --host 0.0.0.0
  --port 13300
)

if [[ "$EAGLE" == "1" ]]; then
  # Use the MLA draft (lightseekorg/kimi-k2.6-eagle3-mla). Override with KIMI_EAGLE_DRAFT (for the 3.1 comparison).
  # Background and subtleties (MHA vs MLA draft, backend branching, weight lineage, why the version is pinned): docs/eagle3.md.
  # Quick list of dead ends (do not retry; details in the table in docs/eagle3.md):
  #   MHA draft (Kimi-K2.6-eagle3) / --attention-backend flashmla / fa3
  args+=(
    --speculative-algorithm EAGLE3
    --speculative-draft-model-path "${KIMI_EAGLE_DRAFT:-./models/Kimi-K2.6-eagle3-mla}"
    --speculative-num-steps 3
    --speculative-eagle-topk 1
    --speculative-num-draft-tokens 4
  )
fi

exec uv run --no-sync sglang serve "${args[@]}"
