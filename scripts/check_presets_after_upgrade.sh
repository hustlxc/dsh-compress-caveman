#!/usr/bin/env bash
# Post-upgrade check for the fixes described in this repository.
#
# Run it with the NEW checkout as the working directory, after upgrading
# deepseek-harness:
#
#   cd /path/to/deepseek-harness-<new-version>
#   bash scripts/check_presets_after_upgrade.sh
#
# It answers four questions:
#   1. does the home patch layer still target a row this release has?
#   2. are the two preset edits still on disk?
#   3. does the preset still parse under the loader's own YAML dialect?
#   4. did the shipped `standard` preset change (so the copy needs re-merging)?
#
# Environment overrides:
#   DSH_HOME   DSH home directory        (default: ~/.dsh)
#   PRESET_ID  authored preset id        (default: qwen-caveman)
#   BASE       shipped preset to diff    (default: standard)

set -uo pipefail

DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
PRESET_ID="${PRESET_ID:-qwen-caveman}"
BASE="${BASE:-standard}"

PRESET_DIR="$DSH_HOME/.agent-presets/$PRESET_ID"
PRESET_YML="$PRESET_DIR/agent.cordis.yml"
HOME_PATCH="$DSH_HOME/cordis.patch.yml"
SHIPPED="packages/preset/agent-presets/presets/$BASE/agent.cordis.yml"

fails=0

echo "== 1. home patch layer ($HOME_PATCH)"
if [[ ! -f "$HOME_PATCH" ]]; then
  echo "  skip: no home patch file"
else
  err=$(pnpm dsh --profile web --dump-config 2>&1 >/dev/null \
        | grep 'cordis.patch.yml] patch: entry' || true)
  if [[ -n "$err" ]]; then
    echo "  FAIL: the home patch targets a row this release no longer has:"
    echo "$err" | sed 's/^/    /'
    fails=$((fails + 1))
  else
    echo "  ok: every targeted row exists"
  fi
fi

echo "== 2. preset edits ($PRESET_YML)"
if [[ ! -f "$PRESET_YML" ]]; then
  echo "  FAIL: preset composition not found"
  fails=$((fails + 1))
else
  grep -q 'Respond and think terse like smart caveman' "$PRESET_YML" \
    && echo "  ok: persona rule present" \
    || { echo "  FAIL: persona rule missing"; fails=$((fails + 1)); }
  grep -q 'maxTokens: 16384' "$PRESET_YML" \
    && echo "  ok: compaction maxTokens present" \
    || { echo "  FAIL: compaction maxTokens missing"; fails=$((fails + 1)); }
fi

echo "== 3. preset parses under the loader dialect"
if [[ -f "$PRESET_YML" ]]; then
  pnpm dsh --profile web --patch "$PRESET_YML" --dump-config >/dev/null 2>/tmp/dsh-preset-check.err
  # 'entry "<id>" not found' lines are expected: an agent-plane preset names rows
  # the host tree does not have. Only syntax/schema failures matter here.
  if grep -qiE 'yaml|cannot|invalid|unexpected|failed to parse' /tmp/dsh-preset-check.err; then
    echo "  FAIL:"
    head -5 /tmp/dsh-preset-check.err | sed 's/^/    /'
    fails=$((fails + 1))
  else
    echo "  ok: parses (unmatched-row diagnostics are expected)"
  fi
fi

echo "== 4. shipped '$BASE' preset vs the copy"
if [[ -f "$SHIPPED" && -f "$PRESET_YML" ]]; then
  if diff -q "$SHIPPED" "$PRESET_YML" >/dev/null; then
    echo "  note: identical (no edits applied?)"
  else
    echo "  the copy differs from the shipped preset. Expected diff = the persona block"
    echo "  and the compaction maxTokens block. Anything else means the shipped preset"
    echo "  moved on and the copy should be re-created and re-patched."
    diff "$SHIPPED" "$PRESET_YML" | sed 's/^/    /'
  fi
fi

echo
if [[ "$fails" -eq 0 ]]; then
  echo "RESULT: all checks passed"
else
  echo "RESULT: $fails check(s) failed"
  exit 1
fi
