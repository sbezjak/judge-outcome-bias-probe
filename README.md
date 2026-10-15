# judge-outcome-bias-probe

Does an LLM judge's score of an agent's decisions move when only the outcome moves?

The probe gives a judge model one agent run twice. The two prompts are byte-identical except for the outcome
(the run went fine, or it caused harm). The judge scores the agent's decisions (PROCESS, 0-10). The judge's
own rule says the outcome should not change that score, and the probe asks the judge its rule first, so a leak
is the judge breaking a rule it stated. A control case changes the agent's conduct instead, so the score has
to move there; that proves the instrument can see a move at all.

Scoring is judge-free: every number is read from the first lines of the judge's answer by string matching
(`scorer.py`), never by a second model. Before every run, two scripted stubs go through the same loop: one that
leaks (must be flagged on every case) and one that does not (must be flagged on none). If either stub is
misread, or the control does not move, no verdict is printed.

## Quick start (no API key, no network)

The probe itself needs only `python3` (standard library).

```
git clone https://github.com/sbezjak/judge-outcome-bias-probe.git
cd judge-outcome-bias-probe
python3 test_scorer.py                    # the scorer's checks, no model: "90 checks passed"
python3 outcome_probe.py stub:governed    # a full probe run on the two scripted stubs
```

The stub run prints `instrument gate passed` and writes its log and report page to `probe-runs/`.

Rebuild a shipped report page from its raw log, and check that only the build date changed:

```
python3 outcome_probe.py --report evidence/raw-logs/outcome-probe-agent-run-judge-01-opus55.log
git diff --word-diff                      # only the "report built" date differs
git checkout -- evidence
```

For the recorded-run fixture add `--fixture fixtures/agent-run-judge-02.json`.

## Run it on a real judge

Keys come from the environment, or from a `.env` file at the repo root (ignored by git). They are never printed.

```
export ANTHROPIC_API_KEY=...
python3 outcome_probe.py claude-haiku-4-5-20251001                 # default fixture: agent-run-judge-01
python3 outcome_probe.py claude-sonnet-5-5 --fixture fixtures/agent-run-judge-02.json
```

Options: `--n 10` scores per prompt (default from the fixture; fewer is a smoke run, reported as "not a
result"), `--cases A,D` a subset (the control D is
required), `--batch` uses Anthropic Message Batches (half price, slower), `--eval` also asks the judge, in
separate calls, whether each prompt reads as a test. `--reading` writes the full reading page instead of the
public one.

Free and local, with [Ollama](https://ollama.com) running on its default port:

```
ollama pull qwen2.5:7b
python3 outcome_probe.py ollama:qwen2.5:7b
```

`openai:<model>` and `deepseek:<model>` adapters exist in `providers.py`, but no run in `evidence/` used them.

To keep a run as evidence, capture its full output with the wrapper. It refuses to start without an estimate
of time and cost, and never overwrites an existing log:

```
scripts/run-evidence.sh agent-run-judge-01-mymodel --eta "~20 min (106 calls)" --cost "~\$0.50 (106 calls)" \
  -- python3 outcome_probe.py <model>
```

The measured cost of every run in `evidence/`, per log: `python3 scripts/sum_spend.py --rows agent-run-judge`.

## The fix test

Does scoring the decisions before the outcome is known stop the leak? `run_luck_fix.py` asks for the
PROCESS score first, then reveals the outcome in the same conversation. The separate-evaluation baseline is the
model's existing probe log, read back, never re-run.

```
python3 run_luck_fix.py stub:governed fixtures/agent-run-judge-01-fix.json all       # no network
python3 run_luck_fix.py claude-haiku-4-5-20251001 fixtures/agent-run-judge-01-fix.json all \
  --baseline=evidence/raw-logs/outcome-probe-agent-run-judge-01-haiku45.log
python3 fix_report.py <fix log> fixtures/agent-run-judge-01-fix.json --baseline=<probe log>
```

## The same probe on an off-the-shelf judge (G-Eval)

`geval_probe.py` runs DeepEval's G-Eval metric as installed; only the transport is the probe's, so every call
is logged. It needs `deepeval` in a virtual environment, with telemetry off:

```
python3 -m venv .venv && .venv/bin/pip install -r requirements-geval.txt
env -u CONFIDENT_API_KEY DEEPEVAL_TELEMETRY_OPT_OUT=1 .venv/bin/python geval_probe.py stub:governed \
  --gate-log evidence/raw-logs/outcome-probe-agent-run-judge-01-haiku45.log           # no network
env -u CONFIDENT_API_KEY DEEPEVAL_TELEMETRY_OPT_OUT=1 .venv/bin/python geval_probe.py claude-haiku-4-5-20251001 \
  --gate-log evidence/raw-logs/outcome-probe-agent-run-judge-01-haiku45.log
```

`--gate-log` is the same model's probe log: the rule check is read from it, not asked again.

## What is in the repo

| path | what |
|---|---|
| `outcome_probe.py` | the probe: stubs, rule check, interleaved legs, verdict, report page |
| `scorer.py` | judge-free parsing, pairwise P, the instrument gate, the verdict wording |
| `providers.py` | the HTTP layer (Anthropic native, one OpenAI-compatible adapter for Ollama / OpenAI / DeepSeek) and the log format |
| `public_report.py` | the public report page, built only from the log |
| `run_luck_fix.py`, `fix_report.py` | the fix test and its report |
| `geval_probe.py` | the probe with DeepEval's G-Eval as the judge |
| `test_scorer.py` | every scorer branch on canned answers, no model |
| `fixtures/` | the prompts. `agent-run-judge-01`: a made-up customer-support agent run, three cases and a control. `agent-run-judge-02`: a recorded Claude Code run in a staged repo, one case and a control. `-fix`: the fix test. `-intrace`, `-inrun`, `-inrun-split`: the outcome inside the log instead of in a note after it. `agent-run-judge-02-direction`, `agent-run-judge-02-cues-removed`: follow-up checks on 02. `moral-luck-06-sep`: the earlier vignette design the probe comes from |
| `evidence/raw-logs/` | the full raw log of every run (every prompt, answer, request id and token count) and the public report pages built from them |
| `traces/` | the recorded Claude Code runs behind fixture 02 (stream-json) |
| `recording/storefront-draft/`, `scripts/ajr02-*` | the staged repo and the scripts that recorded the agent |
| `scripts/` | fixture builders, log and trace reading views, the evidence wrapper, the cost counter |

The recordings use a made-up identity (Sara Brekke, `lorunasp`) in a staged repo. Paths under `/Users/sara/`
in the logs are the machine the runs were made on.
