# Runs, cost, fix test, G-Eval, layout

Moved from the README on 2026-11-02, unchanged. The README keeps the one command that tests your judge and the results table.

## Run a judge (every option)

Keys come from the environment or a `.env` file at the repo root, and are never printed.

```
export ANTHROPIC_API_KEY=...
python3 outcome_probe.py claude-haiku-4-5-20251001                 # fixture agent-run-judge-01 (default)
python3 outcome_probe.py claude-sonnet-5-5 --fixture fixtures/agent-run-judge-02.json
python3 outcome_probe.py ollama:qwen2.5:7b                         # local, after `ollama pull qwen2.5:7b`
```

| option | |
|---|---|
| `--n N` | scores per version, default 10 (the floor; fewer is reported as "smoke run, not a result") |
| `--cases C,D` | subset; control D required |
| `--eval` | also asks whether each prompt reads as a test |
| `--batch` | Message Batches, half price |
| `RUN_PROMPT_CACHE=1` | env var: prompt caching |
| `--reading` | full reading page instead of the public one |

`openai:` / `deepseek:` adapters exist; no run in `evidence/` used them.

## Cost

A full run has 6 + cases x 2 x n + 5 x cases calls with `--eval`, where cases counts the control: 106 for 01
(3 cases + control) and 56 for 02 (1 case + control, 16-19k-token prompts). Prices are from the logs' token counts at n = 10:

| | Haiku 4.5 | Haiku 5.5 | Sonnet 5.5 | Opus 5.5 |
|---|---|---|---|---|
| 01, list price | $0.16 | $0.02 | $0.42 | $1.23 |
| 02, list price | $0.70 | $0.09 | $1.76 | $3.74 |
| 02, prompt caching (as run) | $0.12 | $0.03 | $0.51 | $1.09 |

`--batch` halves the list price; 01 Sonnet and Opus ran that way ($0.21, $0.61). Batch and caching together were never
run. Everything in `evidence/` cost $12.34 (2,011 calls, 39 paid logs, 17 of them smokes), plus $0.14 for the agent
recordings; recount with `python3 scripts/sum_spend.py`.

## Fix test

Scores PROCESS with the outcome hidden, then reveals it in the same conversation. It held on 01 (Haiku 4.5,
Sonnet 5.5, Opus 5.5). On 02 it failed with Haiku 4.5: 12 of 20 scores changed after the reveal, 9 of 10 harm-leg scores down and
3 of 10 no-harm scores up (`agent-run-judge-02-fix-haiku45.log:3551-3552`). With Opus 5.5 it held: 0 of 20 scores changed, and every BASIS
named the skipped test (`agent-run-judge-02-fix-opus55.log:3619`). It is not a general fix: it depends on the judge.

```
python3 run_luck_fix.py claude-haiku-4-5-20251001 fixtures/agent-run-judge-01-fix.json all \
  --baseline=evidence/raw-logs/outcome-probe-agent-run-judge-01-haiku45.log    # stub:governed: no network
python3 fix_report.py <fix log> fixtures/agent-run-judge-01-fix.json --baseline=<probe log>
```

## G-Eval

DeepEval's G-Eval as the judge, with every call logged. `--gate-log` is the same model's probe log.

```
python3 -m venv .venv && .venv/bin/pip install -r requirements-geval.txt
env -u CONFIDENT_API_KEY DEEPEVAL_TELEMETRY_OPT_OUT=1 .venv/bin/python geval_probe.py claude-haiku-4-5-20251001 \
  --gate-log evidence/raw-logs/outcome-probe-agent-run-judge-01-haiku45.log    # stub:governed: no network
```

## Layout

| path | |
|---|---|
| `outcome_probe.py`, `scorer.py`, `providers.py`, `public_report.py` | probe, judge-free scoring, HTTP + logging, public page |
| `run_luck_fix.py`, `fix_report.py`, `geval_probe.py` | fix test, its page, G-Eval |
| `fixtures/` | `agent-run-judge-01` (made-up runs), `-02` (recorded Claude Code run), `-fix`, 01 variants with the outcome inside the log, 02 follow-ups, `moral-luck-06-sep` (the vignette design it comes from) |
| `evidence/raw-logs/` | every run's full log and the public pages |
| `traces/`, `recording/`, `scripts/ajr02-*` | the recorded agent runs behind 02 and the kit that made them |

The recordings use a made-up identity (Sara Brekke, `lorunasp`) in a staged repo. The `/Users/sara/` paths in the
logs are from the machine the runs were made on.

Fixture 02 rebuilds from the shipped trace (`scripts/build_agent_run_judge_02.py`). Recording a new agent run is a
different matter. The kit pushed to a private repo (`lorunasp/storefront`), so set `REPO` in
`scripts/ajr02-setup-storefront.sh` to a repo you own. It also needs `gh`, Claude Code and macOS with Homebrew. A new
recording is a new run, so `scripts/plant_ajr02.py` has to be adapted to it.
