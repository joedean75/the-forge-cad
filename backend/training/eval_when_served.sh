#!/usr/bin/env bash
# Score the served model on the goal suite, unattended.
#
# Runs from the Windows watcher the moment `serve.py` is up, so a fine-tune that
# finishes at two in the morning has a measurement waiting rather than a
# checkpoint nobody has pointed at anything. Independent of any Claude session.
#
# Three things are pinned rather than defaulted, because the point is a number
# comparable to the ones already recorded:
#   * `--resolution 96`, which is what the 0.6B (0.533) and Opus 5 (0.900) runs
#     used — `capped_resolution` clamps per account, so an unpinned suite scores
#     differently depending on who ran it.
#   * `FORGE_COPILOT_PROMPT=finetuned`, the profile the corpus was built with. The
#     served model has never seen the `full` prompt.
#   * a throwaway database, since a benchmark writes documents and trajectories
#     and must not do that to the real one. A fresh path per run also avoids the
#     stale-WAL "disk I/O error" that stopped the last attempt.
set -uo pipefail
cd "$(dirname "$0")/.."

MODEL="${MODEL:-forge-copilot-qwen3.5-2b}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8099/v1}"
STAMP="$(date -u +%Y%m%d-%H%M)"
OUT="data/eval-goals-2b-${STAMP}.json"
LOG="data/eval-goals-2b-${STAMP}.log"
DB="/tmp/forge-eval-${STAMP}.db"

# The watcher starts the server and this together; loading a 2B and merging the
# adapter takes a minute or two, so wait rather than fail.
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 "${BASE_URL}/models" >/dev/null 2>&1; then break; fi
  sleep 15
done
if ! curl -fsS --max-time 5 "${BASE_URL}/models" >/dev/null 2>&1; then
  echo "no endpoint at ${BASE_URL} after 15 minutes; not running the suite" | tee -a "$LOG"
  exit 1
fi
echo "endpoint up: $(curl -fsS --max-time 5 "${BASE_URL}/models")" | tee -a "$LOG"

# Caged: a suite that OOMs the distro takes the terminal with it (see the WSL
# note in MEMORY and CLAUDE.md's rollout section). **But the cage must not be
# able to skip the suite**: launched from the Windows task the distro may have no
# systemd user session, where `systemd-run --user` fails to connect to the bus —
# and the run would end there, silently, with nothing written. Verified working
# through that path; the fallback is for when it is not.
CAGE=(systemd-run --user --scope -p MemoryMax=8G --)
if ! systemd-run --user --scope -- true >/dev/null 2>&1; then
  echo "systemd-run --user unavailable; running uncaged" | tee -a "$LOG"
  CAGE=()
fi
"${CAGE[@]}" env \
  DATABASE_URL="sqlite:///${DB}" \
  FORGE_RATE_LIMIT=0 \
  FORGE_COPILOT_PROMPT=finetuned \
  LLM_BASE_URL="$BASE_URL" \
  LLM_API_KEY="${LLM_API_KEY:-local}" \
  LLM_MODEL="$MODEL" \
  PYTHONPATH=. \
  .venv/bin/python -m evals.agent --suite goals --resolution 96 \
    --baseline "$OUT" >>"$LOG" 2>&1
status=$?

echo "--- movement against the 0.6B baseline ---" >>"$LOG"
"${CAGE[@]}" env \
  DATABASE_URL="sqlite:///${DB}" PYTHONPATH=. .venv/bin/python - <<'PY' >>"$LOG" 2>&1
import json, pathlib
here = pathlib.Path("data")
new = max(here.glob("eval-goals-2b-*.json"), key=lambda p: p.stat().st_mtime)
rows = [("this 2B run", json.loads(new.read_text()))]
for name, label in (("eval-goals-finetuned-20260924.json", "0.6B"),
                    ("eval-goals-opus-20260924.json", "Opus 5")):
    p = here / name
    if p.exists():
        rows.append((label, json.loads(p.read_text())))
keys = ("mean_score", "solved", "geometry_ok", "finished", "mean_steps", "failed_calls")
print(f"{'':14}" + "".join(f"{k:>14}" for k in keys))
for label, d in rows:
    print(f"{label:14}" + "".join(f"{str(d.get(k, '-')):>14}" for k in keys))
print("\nby_error_code:", {l: d.get("by_error_code") for l, d in rows})
PY
echo "wrote $OUT" | tee -a "$LOG"
exit $status
