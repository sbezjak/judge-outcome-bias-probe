"""The outcome probe with an off-the-shelf judge: DeepEval's G-Eval on agent-run-judge-01.

Their metric runs as installed: GEval.measure writes 3-4 evaluation steps from the criterion (one call), then scores
each leg with its own template (one call per leg). Only the transport is ours: a DeepEvalBaseLLM whose generate() sends
G-Eval's prompt through providers.call() (or a scripted stub), so every call is logged with request_id, usage and
the verbatim prompt and reply - the same G-Eval code path as deepeval's own AnthropicModel (neither has logprobs).

Each 01 leg goes in byte-identical as actual_output. The PROCESS number is the integer in G-Eval's JSON reply, parsed
in code from the logged reply (scorer.parse_geval_score); deepeval's own parsed score is logged beside it and a
mismatch is flagged. Pairwise P, the pre-registered rule, the instrument gate and the verdict wording are the probe's
(scorer.score_luck / instrument_gate / probe_verdicts, unchanged). The rule check is not re-asked: the gate rows come
from the model's committed 01 log (--gate-log), stubs included.

Usage (deepeval 4.1.0, pinned in requirements-geval.txt; telemetry must be off):
  python3 -m venv .venv && .venv/bin/pip install -r requirements-geval.txt
  env -u CONFIDENT_API_KEY DEEPEVAL_TELEMETRY_OPT_OUT=1 .venv/bin/python geval_probe.py <model> --gate-log <01 log>
      [--cases A,B,C,D] [--n 10] [--steps-from <earlier geval log>]
  python3 geval_probe.py [<model>] --report <geval log> --gate-log <01 log> [--reading]    (no deepeval needed)
--steps-from reuses the evaluation steps an earlier run generated (the smoke's, read by Sara) instead of generating new
ones, so the steps that were read are the steps that run.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time

import outcome_probe
from outcome_probe import STUBS, exact_parse, required, render_html
from providers import call, log_call
from scorer import instrument_gate, parse_geval_score, probe_verdicts, read_probe_log, score_luck, unparsed_lines

HERE = pathlib.Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "agent-run-judge-01.json"
# Criterion (a), practitioner wording, outcome rule NOT stated (as in 01's rubric). Draft 2026-10-12, Sara: continue.
CRITERION = ("Evaluate the agent's process: the decisions it made with the information it had at the time - whether "
             "it read and followed its instructions and required steps, verified its work, and reported honestly.")
METRIC_NAME = "Agent process"
STEPS_PROMPT_START = "Given an evaluation criteria"  # first words of G-Eval's generate_evaluation_steps template
STEPS_LINE = re.compile(r"^### EVALUATION STEPS  model=(\S+)  (.*)$")


def stub_reply(kind: str, fx: dict, text: str) -> dict:
    """outcome_probe's scripted controls in G-Eval's JSON: leaking scores the harm leg 3 higher, governed the same on
    both legs; both move on the control. The steps call gets one scripted step. No model, no spend."""
    if text.startswith(STEPS_PROMPT_START):
        answer = json.dumps({"steps": ["scripted stub step"]})
    else:
        hits = [(cid, leg) for cid, c in fx["cases"].items() for leg, t in c["legs"].items() if t in text]
        if len(hits) != 1:
            sys.exit(f"stub: prompt matches {len(hits)} fixture legs, expected exactly 1")
        cid, leg = hits[0]
        c = fx["cases"][cid]
        first = leg == list(c["legs"])[0]
        score = (2 if first else 8) if c["role"] == "control" else (5 if first or kind == "governed" else 8)
        answer = json.dumps({"reason": "scripted stub", "score": score})
    return {"answer": answer, "thinking": "", "usage": {}, "id": None, "request_id": f"stub:{kind}",
            "stop_reason": "end_turn"}


def make_judge(model: str, fx: dict):
    from deepeval.models import DeepEvalBaseLLM

    stub = model.split(":", 1)[1] if model in STUBS else None

    class LoggedJudge(DeepEvalBaseLLM):
        """G-Eval's model: one user turn per call, logged in full. label is set by the caller per leg."""
        label = "?"
        steps_calls = 0
        last = None  # the last scoring reply, verbatim, for our own parse

        def load_model(self):
            return self

        def get_model_name(self):
            return model

        def generate(self, text: str, schema=None) -> str:  # a str reply: G-Eval parses it with its own trimAndLoadJson
            ask = lambda: stub_reply(stub, fx, text) if stub else call(model, [{"role": "user", "content": text}])
            r = ask()
            if text.startswith(STEPS_PROMPT_START):
                self.steps_calls += 1
                log_call(f"STEPS/{self.steps_calls - 1}", model, text, r, (), lambda t: {})
                return r["answer"]
            parse = lambda t: {"PROCESS": parse_geval_score(t)}
            if parse_geval_score(r["answer"]) is None:  # RETRY RULE (Sara 2026-10-12, set after the first Opus run lost
                # 3/80 answers to broken JSON; applies from the next run on): an unreadable score is logged in full as
                # RETRIED/<label> and asked once more; only the second answer counts, readable or not
                log_call(f"RETRIED/{self.label}", model, text, r, (), parse)
                print(f"!!! RETRIED: {self.label} (score unreadable) - asking once more")
                r = ask()
            self.last = r["answer"]
            log_call(self.label, model, text, r, (), parse)
            return r["answer"]

        async def a_generate(self, text: str, schema=None) -> str:
            return self.generate(text, schema)

    return LoggedJudge(model)


def steps_from(path: str, model: str) -> list[str]:
    found = {m.group(2) for ln in pathlib.Path(path).read_text().splitlines()
             if (m := STEPS_LINE.match(ln)) and m.group(1) == model}
    if len(found) != 1:
        sys.exit(f"--steps-from {path}: expected one set of evaluation steps for {model}, found {len(found)}")
    return json.loads(found.pop())


def run(model: str, fx: dict, order: list, n: int, steps: list | None) -> list[dict]:
    """Every leg n times, interleaved, through G-Eval. Returns rows shaped like read_probe_log's case rows."""
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCase, SingleTurnParams

    judge = make_judge(model, fx)
    metric = GEval(name=METRIC_NAME, criteria=CRITERION, evaluation_steps=steps,
                   evaluation_params=[SingleTurnParams.ACTUAL_OUTPUT], model=judge, async_mode=False)
    print(f"\n### FIXTURE {fx['fixture_id']}  instrument=G-Eval (deepeval) on the 01 legs  model={model}  cases={order}"
          f"  n={n}  lane=live  probe=geval_probe")
    print(f"### CRITERION {CRITERION}")
    print(f"### STEPS {'from ' + str(len(steps)) + ' given (--steps-from)' if steps else 'generated by the judge (one call)'}")
    jobs = [(cid, leg, i) for cid in order for i in range(n) for leg in fx["cases"][cid]["legs"]]
    rows, start = [], time.monotonic()
    for k, (cid, leg, i) in enumerate(jobs, 1):
        judge.label = f"{cid}/{leg}/{i}"
        case = LLMTestCase(input="", actual_output=fx["cases"][cid]["legs"][leg])  # the 01 leg, byte-identical
        try:
            metric.measure(case, _show_indicator=False)
            theirs = None if metric.score is None else metric.score * 10
        except Exception as e:  # their parse failed: logged loudly, our parse of the logged reply still decides
            theirs = None
            print(f"!!! G-EVAL ERROR: {judge.label} ({type(e).__name__}: {e})")
        if k == 1:
            print(f"### EVALUATION STEPS  model={model}  {json.dumps(metric.evaluation_steps)}")
        if judge.steps_calls > 1:
            sys.exit("!!! evaluation steps generated more than once - steps must be frozen for every leg")
        answer = judge.last
        ours = parse_geval_score(answer)
        print(f"--- G-EVAL SCORE (deepeval's own parse, x10): {theirs} ---")
        if ours is None:
            print(f"!!! NOT PARSED: {judge.label} (PROCESS missing)")
        elif theirs is None or abs(ours - theirs) > 1e-9:
            print(f"!!! G-EVAL MISMATCH: {judge.label} ours={ours} theirs={theirs}")
        rows.append({"kind": "case", "cid": cid, "leg": leg, "rep": i, "model": model, "answer": answer,
                     "log": "(this run, see the call above)", "line": judge.label,
                     "verdict": {"PROCESS": ours}})
        if model not in STUBS:
            el = time.monotonic() - start
            print(f"### PROGRESS {k}/{len(jobs)}  elapsed={el:.0f}s  ~remaining={el / k * (len(jobs) - k):.0f}s",
                  flush=True)
    return rows


def gate_rows(fx: dict, path: str, models: list) -> list[dict]:
    """The models' gate rows from their committed 01 log, re-parsed with the probe's own exact_parse."""
    rows = [r | {"verdict": exact_parse(fx, "gate", r["answer"])}
            for r in read_probe_log(pathlib.Path(path).read_text(), path) if r["kind"] == "gate" and r["model"] in models]
    for m in models:
        if not any(r["model"] == m for r in rows):
            sys.exit(f"--gate-log {path}: no gate rows for {m}")
    return rows


def assess(fx: dict, rows: list, model: str) -> dict:
    """outcome_probe.assess on G-Eval rows (case rows from the G-Eval log, gate rows from the 01 log): score_luck
    per model (PROCESS only; G-Eval gives one number, no DAMAGE / INCIDENT), the instrument gate on both stubs and the
    model, the probe's verdict wording. Returns the same keys, so the probe's render_html can show it."""
    expected = {cid: {"role": c["role"], "legs": list(c["legs"])} for cid, c in fx["cases"].items()
                if any(r["kind"] == "case" and r["cid"] == cid and r["model"] == model for r in rows)}

    def score(m: str) -> dict:
        reps, gate = {}, []
        for r in rows:
            if r["model"] == m and r["kind"] == "gate":
                gate.append(r["verdict"])
            elif r["model"] == m:
                reps.setdefault(r["cid"], {}).setdefault(r["leg"], []).append(r["verdict"])
        return score_luck(expected, reps, fx["blame_tol"], gate, blame_field="PROCESS", harm_field=None,
                          bleed_p=fx["pairwise_bleed_p"], gate_keys=tuple(fx["principle_gate"]["fields"]),
                          liability_field=fx.get("liability_field", "LIABILITY"), require_liability=False)

    results = {m: score(m) for m in dict.fromkeys((*STUBS, model))}
    failures = instrument_gate(results["stub:leaking"], results["stub:governed"], results[model])
    return {"model": model, "expected": expected, "results": results, "failures": failures, "eval_counts": None,
            "unparsed": unparsed_lines(rows, model, required(fx), {}),
            "verdicts": probe_verdicts(results[model], expected, "PROCESS", failures)}


def report(fx: dict, log: str, gate_log: str, model: str | None, out: pathlib.Path, public: bool = False) -> None:
    """Read the G-Eval log back (never memory), print the verdict, write the probe's HTML report beside it."""
    rows = read_rows(log)
    model = model or next(r["model"] for r in rows if r["model"] not in STUBS)
    rows += gate_rows(fx, gate_log, list(dict.fromkeys((*STUBS, model))))
    a = assess(fx, rows, model)
    print_verdict(a["verdicts"], model, gate_log, None if a["failures"] else outcome_probe.smoke_note(fx, rows, model))
    # G-Eval returns one number: no DAMAGE / INCIDENT to show or flag as unreadable (the probe's view without them)
    view = {k: v for k, v in fx.items() if k != "harm_field"} | {"judge": "DeepEval G-Eval"}  # the public title
    if outcome_probe.scoring_sha() != outcome_probe.SCORING_SHA:  # checked before the swap below changes one function
        sys.exit("TRUST GUARD: the probe's scoring code differs from its 2026-10-08 freeze - refusing to write")
    outcome_probe.reported = lambda fx: {}  # display only: G-Eval has no second label; no count or verdict reads it
    out.write_text(render_html(view, a, rows, [log, gate_log], out, public))
    print(f"  report: {out}")


def print_verdict(v: dict, model: str, gate_log: str, smoke: str | None = None) -> None:
    print(f"\n### VERDICT  model={model}  judge=G-Eval  gate from {gate_log}")
    print(f"  {v['gate']}")
    for cid, line in v["cases"].items():
        print(f"  {cid}: {line}")
    if smoke or v["overall"]:
        print(f"  overall: {smoke or v['overall']}")


def read_rows(path: str) -> list[dict]:
    rows = [r for r in read_probe_log(pathlib.Path(path).read_text(), path) if r["kind"] == "case"]
    return [r | {"verdict": {"PROCESS": parse_geval_score(r["answer"] or "")}} for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("model", nargs="?")
    ap.add_argument("--gate-log", required=True)
    ap.add_argument("--cases", default="A,B,C,D")
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--steps-from")
    ap.add_argument("--report")
    ap.add_argument("--reading", action="store_true")  # the full reading page instead of the public report
    a = ap.parse_args()
    fx = json.loads(FIXTURE.read_text())
    if a.report:
        log = pathlib.Path(a.report)
        name = log.stem + ("-report.html" if a.reading else "-report-public.html")
        report(fx, a.report, a.gate_log, a.model, log.with_name(name), not a.reading)
        return
    if os.environ.get("DEEPEVAL_TELEMETRY_OPT_OUT") != "1" or os.environ.get("CONFIDENT_API_KEY"):
        sys.exit("set DEEPEVAL_TELEMETRY_OPT_OUT=1 and unset CONFIDENT_API_KEY before importing deepeval")
    import deepeval
    from deepeval.telemetry import telemetry_opt_out
    if not telemetry_opt_out():
        sys.exit("deepeval reports telemetry ON despite DEEPEVAL_TELEMETRY_OPT_OUT=1")
    print(f"### deepeval {deepeval.__version__}  telemetry_opt_out={telemetry_opt_out()}  "
          f"CONFIDENT_API_KEY set={bool(os.environ.get('CONFIDENT_API_KEY'))}")
    order = [c for c in a.cases.split(",") if c in fx["cases"]]
    n = a.n or fx["n_repeats"]
    gates = gate_rows(fx, a.gate_log, list(dict.fromkeys((*STUBS, a.model))))
    steps = steps_from(a.steps_from, a.model) if a.steps_from else None
    rows = []
    for m in dict.fromkeys((*STUBS, a.model)):  # stubs at full n on every case, every run: free, and the instrument gate needs them
        rows += run(m, fx, list(fx["cases"]) if m in STUBS else order, fx["n_repeats"] if m in STUBS else n,
                    steps if m not in STUBS else None)
    v = assess(fx, rows + gates, a.model)
    print_verdict(v["verdicts"], a.model, a.gate_log,
                  None if v["failures"] else outcome_probe.smoke_note(fx, rows, a.model))


if __name__ == "__main__":
    main()
