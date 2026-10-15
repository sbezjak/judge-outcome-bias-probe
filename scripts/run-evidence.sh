#!/usr/bin/env bash
# Capture a run's FULL stdout+stderr to a log in evidence/raw-logs/. The raw log carries the receipts
# (provider request_id / batch_id, verbatim usage, every prompt and answer) a claim later rests on, so it
# is never cut short by tail or head.
#
# Usage: scripts/run-evidence.sh <name> --eta "<estimate (source)>" --cost "<estimate (source)>" -- <command...>
#   e.g. scripts/run-evidence.sh agent-run-judge-01-mymodel --eta "~20 min (106 calls)" --cost "~\$0.50 (106 calls)" \
#          -- python3 outcome_probe.py <model> --fixture fixtures/agent-run-judge-01.json --log probe-runs/mymodel.log
#
# --eta and --cost are required: the run does not start without an estimate that names a number, and
# both land in the log header. The exit line records the ACTUAL elapsed time beside the predicted ETA,
# so every log checks its own estimate. A local run still states its cost: --cost "0 (local)".
#
# Writes the whole combined stream to evidence/raw-logs/<name>.log (or $RUN_EVIDENCE_LOGDIR/<name>.log)
# via tee, keeps the command's exit code so a mid-run failure still surfaces, and never overwrites a log
# that exists: a log is evidence, so a rerun gets a new name.
set -uo pipefail

usage='usage: scripts/run-evidence.sh <name> --eta "<estimate (source)>" --cost "<estimate (source)>" -- <command...>'
[ "$#" -ge 1 ] || { echo "$usage" >&2; exit 2; }
name="$1"; shift
eta=""; cost=""
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
  case "$1" in
    --eta) eta="${2:-}"; shift 2 ;;
    --cost) cost="${2:-}"; shift 2 ;;
    *) echo "unknown option: $1" >&2; echo "$usage" >&2; exit 2 ;;
  esac
done
[ "$#" -ge 2 ] && [ "$1" = "--" ] || { echo "$usage" >&2; exit 2; }
shift
for pair in "eta:$eta" "cost:$cost"; do
  label="${pair%%:*}"; val="${pair#*:}"
  if ! printf '%s' "$val" | grep -q '[0-9]'; then
    echo "run-evidence: refusing to start - --$label must state an estimate with a number, got '${val}'" >&2
    echo "$usage" >&2
    exit 2
  fi
done

# Sanitize the name to a safe basename (name a log by its fixture and model).
safe_name=$(printf '%s' "$name" | tr -c 'A-Za-z0-9._-' '_')

repo_root=$(cd "$(dirname "$0")/.." && pwd)
logdir="${RUN_EVIDENCE_LOGDIR:-$repo_root/evidence/raw-logs}"  # RUN_EVIDENCE_LOGDIR overrides the target dir
mkdir -p "$logdir"
logfile="$logdir/${safe_name}.log"
if [ -e "$logfile" ]; then
  echo "run-evidence: refusing to start - $logfile exists; a log is evidence and is never overwritten (pick a new name)" >&2
  exit 2
fi

start=$(date +%s)
printf '[run-evidence] %s | %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee "$logfile"
printf '[run-evidence] predicted eta=%s | cost=%s\n' "$eta" "$cost" | tee -a "$logfile"
"$@" 2>&1 | tee -a "$logfile"
status=${PIPESTATUS[0]}
printf '[run-evidence] exit=%s elapsed=%ss (predicted eta=%s) log=%s\n' \
  "$status" "$(( $(date +%s) - start ))" "$eta" "$logfile" | tee -a "$logfile"
exit "$status"
