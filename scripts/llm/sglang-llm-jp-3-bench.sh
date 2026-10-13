#!/usr/bin/env bash
# llm-jp-3-8x13b family (for benchmark comparison, single-user assumed).
# Usage:   ./scripts/sglang-llm-jp-3-bench.sh {instruct3|sip-jmed}
# Env:     CUDA_VISIBLE_DEVICES (default "0")  PORT (default 13300)
#
# Default is TP=1 (one GPU). Fits in 94GB/141GB and decodes fastest with no NCCL cost.
# Add GPUs only when running in parallel (e.g. two models at once split across GPU 0 and GPU 1):
#   CUDA_VISIBLE_DEVICES=0 PORT=13300 ./scripts/sglang-llm-jp-3-bench.sh instruct3 &
#   CUDA_VISIBLE_DEVICES=1 PORT=8001 ./scripts/sglang-llm-jp-3-bench.sh sip-jmed  &
#   wait
set -euo pipefail

case "${1:-}" in
  instruct3)
    MODEL_PATH=./models/llm-jp-3-8x13b-instruct3
    SERVED_NAME=llm-jp-3-8x13b-instruct3
    ;;
  sip-jmed)
    MODEL_PATH=./models/SIP-jmed-llm-3-8x13b-OP-32k-R0.1
    SERVED_NAME=sip-jmed-llm-3-8x13b
    ;;
  *)
    echo "usage: $0 {instruct3|sip-jmed}" >&2
    exit 1
    ;;
esac

GPUS="${CUDA_VISIBLE_DEVICES:-0}"
TP=$(echo "$GPUS" | tr ',' '\n' | grep -c .)
PORT="${PORT:-13300}"

source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

exec env CUDA_VISIBLE_DEVICES="$GPUS" uv run --no-sync sglang serve \
  "${SGLANG_AUTH[@]}" \
  --model-path "$MODEL_PATH" \
  --tp "$TP" \
  --mem-fraction-static 0.85 \
  --context-length 32768 \
  --served-model-name "$SERVED_NAME" \
  --enable-metrics \
  --trust-remote-code \
  --host 0.0.0.0 \
  --port "$PORT"
