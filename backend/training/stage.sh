#!/usr/bin/env bash
# Copy the trainer, the server and the dataset to the Windows side, where the
# CUDA interpreter and the weights live. Writes nothing into the book projects:
# their README says reuse, never modify.
set -euo pipefail
# Staging destination: pass it, or set FORGE_COPILOT_DIR. No path is baked in,
# so a checkout of this repo carries nobody's home directory.
DEST="${1:-${FORGE_COPILOT_DIR:?pass a destination or set FORGE_COPILOT_DIR}}"
DATASET="${2:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$DEST/dataset" "$DEST/runs"
cp "$HERE/copilot_sft.py" "$HERE/serve.py" "$HERE/start_when_free.ps1" "$DEST/"
if [ -n "$DATASET" ]; then
  cp "$DATASET"/train.jsonl "$DATASET"/val.jsonl "$DATASET"/CARD.md "$DATASET"/stats.json "$DEST/dataset/"
fi
echo "staged to $DEST"
