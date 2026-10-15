"""Fix-test runner: does scoring the decisions before the outcome is known stop the outcome from moving PROCESS?
(agent-run-judge-01-fix, agent-run-judge-02-fix)

Same cases, gate, rubric and contract as the probe fixture; each leg splits at its last paragraph (the outcome):
  B turn 1 - the leg without its outcome, and the fixture's turn-1 question: the decision number only.
             Legs sharing that text (a case's two legs) share one turn 1; the control's two each get their own.
  B turn 2 - the SAME conversation continues: the fixture's reveal prefix + the outcome + the probe's question and
             contract. Each turn-1 answer is continued with every outcome that shares its text, so the two reveals
             are paired on one committed number.
  A        - B's turn-1 number is the shielded score (invariant by construction); no calls of its own.
The separate-evaluation baseline is the model's committed outcome_probe.py log (--baseline=<log>), read with the
probe's own reader and parser, never re-run, so it is the probe report's numbers.
Fixture keys for the probe's rules: liability_field (INCIDENT), liability_required false (the label is reported, not
required), exact_values true (a whole number 0-10 only, as outcome_probe.exact_parse).
Every prompt and full response is printed, so a run-evidence.sh capture is the untruncated log.

Usage: python3 run_luck_fix.py <model> <fixture.json> [caseids|all] [n] [--batch] [--baseline=<outcome_probe log>]
  n overrides n_turn1 and n_per_order (smoke: A 1). Gate repeats = min(n_turn1, 5).
  model stub:leaking / stub:governed: a scripted control through the same loop, no network (A + B only, live lane).
--batch: two Message Batches jobs (gate + B turn 1, then B turn 2). --resume-batch=<id1>[,<id2>]
collects already-submitted batches in that order instead of resubmitting.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import time
from collections import Counter

from providers import batch_call, call, log_call
from scorer import parse_fields, read_probe_log, score_blind, score_luck, score_reveal

STUBS = ("stub:leaking", "stub:governed")
PLUMBING = "Reply with the single word OK."


def stub_reply(kind: str, fx: dict, msgs: list) -> dict:
    """Scripted positive / negative control for both turns (outcome_probe.stub_reply's rule). It reads only the
    conversation. Turn 1: the number depends on the prompt alone (a control's careless stem 2, careful stem 8, any
    other stem 5), as identical prompts must. Turn 2: the leg is found by its stem and its note; governed keeps the
    committed number on every reveal, leaking adds 3 on a case's second (harm) reveal; a control keeps its number, so it
    moves on both turns. No model, no spend."""
    lf, bf, rv = fx.get("liability_field", "LIABILITY"), fx["blame_field"], fx["reveal"]
    first, last = msgs[0]["content"], msgs[-1]["content"]
    split = lambda t: t.rsplit("\n\n", 1)
    if last == PLUMBING:
        answer = "OK"
    elif last == fx["principle_gate"]["prompt"]:
        answer = "\n".join(f"{k}: {v}" for k, v in fx["principle_gate"]["paper_expected"].items())
    elif len(msgs) == 1:
        legs = [(c, i) for c in fx["cases"].values() for i, t in enumerate(c["legs"].values())
                if first.startswith(split(t)[0] + "\n\n")]
        if not legs:
            sys.exit("stub: turn-1 prompt matches no fixture stem")
        ctl = [i for c, i in legs if c["role"] == "control"]
        answer = f"{bf}: {(2 if ctl[0] == 0 else 8) if ctl else 5}\nBASIS: scripted stub"
    else:
        hits = [(c, leg) for c in fx["cases"].values() for leg, t in c["legs"].items()
                if first.startswith(split(t)[0] + "\n\n") and last.startswith(rv["turn2_prefix"] + "\n\n" + split(t)[1])]
        if len(hits) != 1:
            sys.exit(f"stub: turn-2 conversation matches {len(hits)} fixture legs, expected exactly 1")
        c, leg = hits[0]
        committed = int(re.search(rf"^{bf}: (\d+)", msgs[1]["content"], re.M).group(1))
        second = leg != list(c["legs"])[0]
        case = c["role"] == "case"
        lines = [f"{bf}: {committed + (3 if kind == 'leaking' and case and second else 0)}"]
        if fx.get("harm_field"):
            lines.append(f"{fx['harm_field']}: {7 if second or not case else 0}")
        label = c["liability_expected"][leg] if case else fx["fields"][lf][0]
        answer = "\n".join(lines + [f"{lf}: {label}", "BASIS: scripted stub"])
    return {"answer": answer, "thinking": "", "usage": {}, "id": None, "request_id": f"stub:{kind}",
            "stop_reason": "end_turn"}


def probe_baseline(fx: dict, text: str, model: str) -> dict:
    """{cid: {leg: [parsed]}} from an outcome_probe.py log: the model's case rows, each re-parsed from its raw answer
    with the probe's exact_parse (the numbers the probe report shows); gate, eval, retried and stub rows are not baseline."""
    from outcome_probe import exact_rows
    base = {}
    for r in exact_rows(fx, read_probe_log(text)):
        if r["kind"] == "case" and r["model"] == model:
            base.setdefault(r["cid"], {}).setdefault(r["leg"], []).append(r["verdict"])
    return base


def main() -> None:
    opt = lambda k: next((a.split("=", 1)[1] for a in sys.argv if a.startswith(f"--{k}=")), None)
    resume = (opt("resume-batch") or "").split(",") if opt("resume-batch") else []
    batch = "--batch" in sys.argv or bool(resume)
    argv = [a for a in sys.argv if not a.startswith("--")]
    if len(argv) < 3:
        sys.exit(__doc__)
    model, fx = argv[1], json.loads(pathlib.Path(argv[2]).read_text())
    btext = pathlib.Path(opt("baseline")).read_text() if opt("baseline") else ""
    if btext and "### PROBE RESULT" not in btext:  # before any call, not after the run is paid for
        sys.exit(f"--baseline must be an outcome_probe.py log (it has no ### PROBE RESULT): {opt('baseline')}")
    stub = model.split(":", 1)[1] if model in STUBS else None
    if stub and batch:
        sys.exit("a scripted stub runs on the live lane only")
    ask = (lambda msgs: stub_reply(stub, fx, msgs)) if stub else (lambda msgs: call(model, msgs))
    if "joint" in fx:
        sys.exit("this fixture has a \"joint\" block: joint evaluation is not part of this runner")
    cases, rv = fx["cases"], fx["reveal"]
    only = set(argv[3].split(",")) if len(argv) > 3 and argv[3] != "all" else None
    n1 = int(argv[4]) if len(argv) > 4 else rv["n_turn1"]
    n_gate = min(n1, 5)
    order = [c for c in sorted(cases) if only is None or c in only]
    for cid in order:  # the scorer reads a control as P(second leg > first leg): leg order must say so, before any spend
        mv = cases[cid].get("blame_must_move")
        if mv and list(cases[cid]["legs"]) != [mv["from"], mv["to"]]:
            sys.exit(f"control {cid}: leg order {list(cases[cid]['legs'])} != blame_must_move {mv['from']} -> {mv['to']}")
    tol, strict = fx.get("field_name_tolerance", 0), fx.get("exact_values", False)
    parse = lambda t: parse_fields(t, fx["fields"], name_tol=tol, strict=strict)
    parse1 = lambda t: parse_fields(t, rv["turn1_fields"], name_tol=tol, strict=strict)
    gparse = lambda t: parse_fields(t, fx["principle_gate"]["fields"], name_tol=tol, strict=strict)
    print(f"### FIXTURE {fx['fixture_id']}  instrument={fx['instrument']}  model={model}  cases={order}  "
          f"n_turn1={n1}  gate_n={n_gate}  lane={'batch' if batch else 'live'}")

    print("### STEP 0 - auth/plumbing smoke (1 tiny call)")
    log_call("step0", model, PLUMBING, ask([{"role": "user", "content": PLUMBING}]), (), parse)

    # --- prompts, all from the fixture ---
    stems = {}  # cid -> {stem key: (stem text, [legs sharing it])}
    for cid in order:
        groups = {}
        for leg, text in cases[cid]["legs"].items():
            groups.setdefault(text.rsplit("\n\n", 1)[0], []).append(leg)
        stems[cid] = {(legs[0] if len(groups) > 1 else "shared"): (s, legs) for s, legs in groups.items()}
    outcome = lambda cid, leg: cases[cid]["legs"][leg].rsplit("\n\n", 1)[1]
    name = lambda cid: cases[cid]["name"]
    t1 = lambda cid, sk: "\n\n".join([stems[cid][sk][0], rv["turn1_question"].format(name=name(cid)),
                                      rv["turn1_contract"].format(name=name(cid))])
    t2 = lambda cid, leg: "\n\n".join([rv["turn2_prefix"], outcome(cid, leg), fx["question"].format(name=name(cid)),
                                       fx["output_contract"].format(name=name(cid))])

    cust = lambda *parts: "-".join(str(p) for p in parts).replace("_", "")

    def run(jobs: dict, resume_id: str | None) -> dict:
        """{key: messages} -> {key: reply}; batch lane or live with one retry on an empty answer."""
        if batch:
            return batch_call(model, jobs, resume_id=resume_id)
        out, start = {}, time.monotonic()
        for k, (key, msgs) in enumerate(jobs.items(), 1):
            r = ask(msgs)
            if not r["answer"].strip():
                print(f"\n[{key}] empty/refusal (request_id={r['request_id']}) - retrying once")
                r = ask(msgs)
            out[key] = r
            el = time.monotonic() - start
            print(f"### PROGRESS {k}/{len(jobs)}  elapsed={el:.0f}s  ~remaining={el / k * (len(jobs) - k):.0f}s",
                  flush=True)
        return out

    # --- phase 1: gate + B turn 1 (independent) ---
    user = lambda text: [{"role": "user", "content": text}]
    p1 = {cust("GATE", i): user(fx["principle_gate"]["prompt"]) for i in range(n_gate)}
    for cid in order:
        for sk in stems[cid]:
            p1 |= {cust("T1", cid, sk, i): user(t1(cid, sk)) for i in range(n1)}
    print(f"\n### PHASE 1 - gate + B turn 1: {len(p1)} fresh conversations")
    r1 = run(p1, resume[0] if resume else None)

    gate = [log_call(f"GATE/{i}", model, fx["principle_gate"]["prompt"], r1[cust("GATE", i)], (), gparse)
            for i in range(n_gate)]
    turn1 = {}  # (cid, sk, i) -> (prompt, reply, parsed)
    for cid in order:
        print(f"\n\n### CASE {cid} - B turn 1 (outcome not shown)")
        for sk in stems[cid]:
            for i in range(n1):
                r = r1[cust("T1", cid, sk, i)]
                turn1[cid, sk, i] = (t1(cid, sk), r, log_call(f"T1/{cid}/{sk}/{i}", model, t1(cid, sk), r, (), parse1))

    # --- phase 2: B turn 2, continuing each turn-1 conversation with every outcome on its stem ---
    p2 = {}
    for (cid, sk, i), (prompt, r, _) in turn1.items():
        if not r["answer"].strip():
            print(f"\n[T1/{cid}/{sk}/{i}] no turn-1 answer - no turn 2 for it (stop_reason={r.get('stop_reason')})")
            continue
        for leg in stems[cid][sk][1]:
            p2[cust("T2", cid, leg, i)] = user(prompt) + [{"role": "assistant", "content": r["answer"]},
                                                           {"role": "user", "content": t2(cid, leg)}]
    print(f"\n\n### PHASE 2 - B turn 2 (outcome revealed in the same conversation): {len(p2)} continuations")
    r2 = run(p2, resume[1] if len(resume) > 1 else None)
    reveal = {}
    for cid in order:
        print(f"\n\n### CASE {cid} - B turn 2")
        for sk, (_, legs) in stems[cid].items():
            for i in range(n1):
                for leg in legs:
                    key = cust("T2", cid, leg, i)
                    if key not in r2:
                        continue
                    got = log_call(f"T2/{cid}/{leg}/{i}  continues T1/{cid}/{sk}/{i}", model, t2(cid, leg), r2[key], (), parse)
                    got |= {"_committed": turn1[cid, sk, i][2][fx["blame_field"]], "_t1": i}
                    reveal.setdefault(cid, {}).setdefault(leg, []).append(got)

    # --- score ---
    expected = {}
    for cid in order:
        c = cases[cid]
        expected[cid] = {"role": c["role"], "legs": list(c["legs"])}
        if c["role"] == "case":
            expected[cid] |= {"liability_expected": c["liability_expected"],
                              "basis_outcome_terms": c.get("basis_outcome_terms", {})}
    kw = dict(harm_field=fx["harm_field"], causation=None, gate_keys=tuple(fx["principle_gate"]["fields"]),
              liability_field=fx.get("liability_field", "LIABILITY"), require_liability=fx.get("liability_required", True))
    bf, bp = fx["blame_field"], fx["pairwise_bleed_p"]
    base = probe_baseline(fx, btext, model) if btext else {}
    base_choice = {cid: {leg: [r[bf] for r in rs if r.get(bf) is not None] for leg, rs in legs.items()}
                   for cid, legs in base.items()}
    blind = {cid: {sk: [turn1[cid, sk, i][2][bf] for i in range(n1) if turn1[cid, sk, i][2][bf] is not None]
                   for sk in stems[cid]} for cid in order}
    res_blind = score_blind(expected, blind, base_choice, bp)
    res_b = score_reveal(expected, reveal, bf, bp, gate=gate, **kw)
    res_base = score_luck(expected, base, fx["blame_tol"], blame_field=bf, bleed_p=bp, **kw) if base else None

    fz = sum(1 for r in [*gate, *(v[2] for v in turn1.values()), *(r for c in reveal.values() for l in c.values() for r in l)]
             if r.get("_fuzzy_names"))
    print("\n\n### RESULT")
    print(f"  field-name repairs (tolerance {tol}): {fz} answers")
    print(json.dumps({"blind": res_blind, "B_reveal": res_b,
                      "baseline": res_base and {c: {k: r.get(k) for k in ("outcome", "pairwise_p", "blame_shift")}
                                                   for c, r in res_base["cases"].items()}}, indent=2))
    g = res_b["gate"]
    print(f"\n  gate: {next(iter(fx['principle_gate']['fields']))} {g['blame_differs']} -> majority {g['majority_blame_differs']}  "
          f"({g['compliant']}/{g['n']} compliant)")
    print(f"  baseline: {opt('baseline') or 'none given'}")
    for cid in order:
        b = res_b["cases"][cid]
        bl = res_blind[cid]
        print(f"\n  {cid} ({cases[cid]['role']})")
        if "scale_moves" in bl:
            print(f"    blind  : stems {bl['stems']}  P {bl['p']}  scale_moves {bl['scale_moves']}")
        else:
            bb = res_base and res_base["cases"].get(cid)
            print(f"    blind  : {bl['blind']} (median {bl['median_blind']})  vs baseline: P(blind > no-harm) "
                  f"{bl['p_above_first']}, P(harm > blind) {bl['p_second_above']}"
                  + (f"   baseline itself: {bb['outcome']} P {bb.get('pairwise_p')}" if bb else ""))
        if b["outcome"] == "non_compliant":
            print("    B turn2: non_compliant")
        else:
            lo, hi = (b["legs"][l] for l in expected[cid]["legs"])
            print(f"    B turn2: {b['outcome']:<13} choice {lo['blame']} -> {hi['blame']} (P {b['pairwise_p']})  "
                  f"harm {lo.get('harm_blame')} -> {hi.get('harm_blame')} (P {b['harm_p']})  "
                  f"liability {lo['majority_liability']} -> {hi['majority_liability']}  product {b.get('product')}")
        print(f"    B revised {b.get('revised')}  paired {b.get('paired')}")
        # reported only: label fields the scorer does not read, blind turn 1 vs each reveal
        for f in [f for f, v in rv["turn1_fields"].items() if isinstance(v, list)]:
            t1c = {sk: dict(Counter(turn1[cid, sk, i][2].get(f) for i in range(n1))) for sk in stems[cid]}
            t2c = {leg: dict(Counter(r.get(f) for r in rs)) for leg, rs in reveal.get(cid, {}).items()}
            print(f"    {f} (reported): turn 1 {t1c}  turn 2 {t2c}")
    print(f"\n  B bleed: {res_b['blame_bleed']}  broke_own_rule: {res_b['broke_own_rule']}  "
          f"scale_moves: {res_b['scale_moves']}  stuck: {res_b['scale_stuck']}  needs_read: {res_b['needs_read']}")


if __name__ == "__main__":
    main()
