#!/usr/bin/env bash
# agent-run-judge-02: leak check of a recorded trace, run by Sara in her own terminal BEFORE anyone (Claude included)
# reads the trace. Prints counts only, never the matching text, so a leaked key is not echoed anywhere.
# Usage: ANTHROPIC_API_KEY exported (the same key the run used); scripts/ajr02-check-trace.sh traces/ajr02-<name>.jsonl
set -uo pipefail

F="${1:?usage: ajr02-check-trace.sh <trace.jsonl>}"
[ -s "$F" ] || { echo "FAIL: $F missing or empty" >&2; exit 2; }
[ -n "${ANTHROPIC_API_KEY:-}" ] || { echo "FAIL: export ANTHROPIC_API_KEY first (needed to search for it)" >&2; exit 2; }

fail=0
count() {  # label, then grep args; counts matching lines
  n=$(grep -c "${@:2}" "$F"); n=${n:-0}
  printf '%-34s %s\n' "$1" "$n"
}
key=$(grep -cF "$ANTHROPIC_API_KEY" "$F")
key_tail=$(grep -cF "${ANTHROPIC_API_KEY: -12}" "$F")
printf '%-34s %s\n' "your API key (exact)" "$key" "your API key (last 12 chars)" "$key_tail"
[ "$key" -eq 0 ] && [ "$key_tail" -eq 0 ] || fail=1
count "any sk-ant- string" -F "sk-ant-"
count "bezjak / sbezjak (any case)" -i "bezjak"
count "gmail" -i "gmail"
# the repo path ~/code/storefront is allowed (Sara 2026-10-10: "sara" may show); any other path under the home is not
home=$(grep -oE '/Users/sara[^ "\\]*' "$F" | grep -vc '^/Users/sara/code/storefront' || true)
printf '%-34s %s\n' "/Users/sara outside the repo path" "$home"
[ "$home" -eq 0 ] || fail=1
for p in "sk-ant-" "bezjak" "gmail"; do grep -qiF "$p" "$F" && fail=1; done
echo "lines in trace: $(wc -l < "$F")"
if [ $fail -eq 0 ]; then echo "CLEAN: safe to share the trace"; else
  echo "NOT CLEAN: do not share or paste this trace; tell Claude only which rows are non-zero"; exit 1; fi
