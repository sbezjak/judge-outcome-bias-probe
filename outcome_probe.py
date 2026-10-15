"""Outcome-bias probe: does an LLM judge's rating of an agent's decisions move when only the outcome does?

One command against one model gives the full raw log and one report.html beside it. The fixture (default
agent-run-judge-01) pairs prompts that are byte-identical except the outcome; the rating of the decisions (PROCESS
here, BLAME-FOR-CHOICE in the moral-luck-06-sep vignettes) must hold across them. A control case varies the conduct
instead, so the rating must move there.

Every run, before the model: two scripted stubs (no network) go through the same loop, log and reader -
stub:leaking must be flagged on every case, stub:governed on none. Then the model: a cold gate asking its own rule,
then every case leg n times, each a fresh conversation, legs interleaved. The report is built by reading the log back,
so every row in it carries its log line, and nothing is in the report that is not in the log. Any instrument check
failing (a stub misread, or the control not moving on the model) withholds every verdict.

A leak is the strong result. No leak is consistent with governance, not proof (and may be test performance:
--eval asks the model, in separate calls, whether each prompt reads as a test).

Usage:
  python3 outcome_probe.py <model> [--fixture F] [--cases E,F,D] [--n 10] [--eval] [--batch] [--log PATH] [--reading]
  python3 outcome_probe.py --report <log>[@A,B] [<log>[@A,B] ...] [--fixture F] [--html PATH] [--reading]
Models: claude-* (Anthropic), ollama:<name>, openai:<name>, deepseek:<name>, stub:leaking, stub:governed.
--batch = Anthropic Message Batches (half price). --report rebuilds the report from existing logs, no model calls
(@A,B takes only those cases from that log, for a result split across logs). The page written is the public
report (<log>-report-public.html, as in evidence/); --reading writes the full reading page instead (<log>-report.html).
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import html
import json
import os
import pathlib
import re
import statistics
import sys
import time
from collections import Counter

from providers import PROMPT_CACHE, batch_call, call, log_call
from scorer import (_SEPARATOR, _two_sided, instrument_gate, pairwise_p, parse_fields, parse_verdict, probe_verdicts,
                    read_probe_log, robustness_line, score_luck, unparsed_lines)

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_FIXTURE = HERE / "fixtures" / "agent-run-judge-01.json"
EVAL_LABELS = ("EVALUATION", "REAL-USE")
EVAL_REPS = 5
STUBS = ("stub:leaking", "stub:governed")
PLUMBING = "Reply with the single word OK."


# SCORING FROZEN (Sara, 2026-10-08: "freeze the scoring ... so we dont change it before haiku and too much after"). The
# fingerprint covers every function that turns a raw answer into a verdict, the parse constants, and the two frozen
# fixtures. test_scoring_is_frozen fails on ANY change to them; a change needs a new dated line here, agreed with Sara
# first, and every report rebuilt and the change said in the write-up.
SCORING_FROZEN = "2026-10-08"
SCORING_SHA = "9b167d934efda4f0b42d1156bba4ec37b7090fce43ea1d07fb4cd8c85aefa948"


def scoring_sha() -> str:
    import inspect

    import scorer
    funcs = [scorer.parse_fields, scorer._score10, scorer._field_name, scorer._edits, scorer.parse_verdict,
             scorer.pairwise_p, scorer._two_sided, scorer._majority, scorer.score_luck, scorer.read_probe_log,
             scorer._probe_label, scorer._parsed, scorer.unparsed_lines, scorer.robustness_line,
             scorer.instrument_gate, scorer.probe_verdicts, required, reported, exact_parse, exact_rows, stub_reply,
             assess]
    parts = [inspect.getsource(f) for f in funcs] + [f"NAME_REPAIR={NAME_REPAIR} EVAL_REPS={EVAL_REPS}"]
    parts += [(HERE / "fixtures" / f).read_text() for f in ("moral-luck-06-sep.json", "agent-run-judge-01.json")]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def prompt(fx: dict, cid: str, leg: str) -> str:
    """The case leg, the rubric question and the output contract, joined by blank lines: the exact text the judge reads."""
    c = fx["cases"][cid]
    return "\n\n".join([c["legs"][leg], fx["question"].format(name=c["name"]),
                        fx["output_contract"].format(name=c["name"])])


def eval_check(fx: dict) -> dict:
    if "eval_check" not in fx:
        sys.exit(f"{fx['fixture_id']} has no eval_check: --eval needs the test-awareness wording in the fixture")
    return fx["eval_check"]


def eval_items(fx: dict, order: list) -> list:
    """The harm leg of each case and the control's second leg (06-sep: careless), unless the fixture lists them."""
    items = fx.get("eval_check", {}).get("items") or [[cid, list(c["legs"])[1]] for cid, c in fx["cases"].items()]
    return [(cid, leg) for cid, leg in items if cid in order]


def smoke_note(fx: dict, rows: list, model: str) -> str | None:
    """Fewer runs per version than the fixture pre-registers (a --n smoke): with 1 run, one lower score crosses
    pairwise_bleed_p, so the verdict lines are a plumbing check, not a result. Display only - outside scoring_sha."""
    n = max((r["rep"] for r in rows if r["model"] == model and r["kind"] == "case"), default=-1) + 1
    want = fx["n_repeats"]
    return (f"smoke run, not a result: {n} run{'' if n == 1 else 's'} per version, below the {want} the fixture "
            "pre-registers" if n < want else None)


def required(fx: dict) -> dict:
    """The fields a verdict needs, per call kind: an answer missing one is unparsed (marked, listed, never counted).
    A case needs only the number the verdict reads (Sara 2026-10-08); its liability label is reported (see reported)."""
    return {"gate": list(fx["principle_gate"]["fields"]), "case": [fx["blame_field"]], "eval": ["READS-AS"]}


def reported(fx: dict) -> dict:
    """Fields parsed and shown but not required: an unreadable one is marked and listed, never fatal to its case."""
    return {"case": [fx.get("liability_field", "LIABILITY")]}


NAME_REPAIR = 2  # letters a field NAME may be off and still be read (unique nearest only); a value is never repaired


def exact_parse(fx: dict, kind: str, answer: str | None) -> dict:
    """The probe's one parser (Sara 2026-10-08): the value exact - a whole number 0-10 only (a range or a decimal is
    unparsed), labels as the contract lists them; a misspelled field NAME (BLAKE-FOR-CHOICE) is read when it is the
    unique nearest field within NAME_REPAIR letters, and every repair is listed (_fuzzy_names)."""
    if kind == "eval":
        return {"READS-AS": parse_verdict(answer or "", EVAL_LABELS)}
    fields = fx["principle_gate"]["fields"] if kind == "gate" else fx["fields"]
    return parse_fields(answer or "", fields, name_tol=NAME_REPAIR, strict=True)


def exact_rows(fx: dict, rows: list) -> list:
    """Re-parse every gate / case / eval row from its raw answer in the log with exact_parse, so a report on any log
    (a pre-probe run_luck log too, parsed leniently at run time) applies the same rule as a probe run."""
    return [r | {"verdict": exact_parse(fx, r["kind"], r["answer"])} if r["kind"] in ("gate", "case", "eval") else r
            for r in rows]


def stub_reply(kind: str, fx: dict, text: str) -> dict:
    """Scripted positive / negative control. It reads only the prompt: the gate gets the paper's answer (the rule),
    a case leg is found by its text. leaking: the second (harm) leg's number is 3 higher; governed: the same number on
    both legs. Both move on the control. No model, no spend."""
    lf = fx.get("liability_field", "LIABILITY")
    if text == PLUMBING:
        answer = "OK"
    elif text == fx["principle_gate"]["prompt"]:
        answer = "\n".join(f"{k}: {v}" for k, v in fx["principle_gate"]["paper_expected"].items())
    else:
        hits = [(cid, leg) for cid, c in fx["cases"].items() for leg, t in c["legs"].items() if t in text]
        if len(hits) != 1:
            sys.exit(f"stub: prompt matches {len(hits)} fixture legs, expected exactly 1")
        cid, leg = hits[0]
        c = fx["cases"][cid]
        first = leg == list(c["legs"])[0]
        if c["role"] == "control":
            score, label = (2 if first else 8), fx["fields"][lf][0]
        else:
            score, label = (5 if first or kind == "governed" else 8), c["liability_expected"][leg]
        lines = [f"{fx['blame_field']}: {score}"]
        if fx.get("harm_field"):
            lines.append(f"{fx['harm_field']}: {0 if first and c['role'] == 'case' else 7}")
        answer = "\n".join(lines + [f"{lf}: {label}", "BASIS: scripted stub"])
    return {"answer": answer, "thinking": "", "usage": {}, "id": None, "request_id": f"stub:{kind}",
            "stop_reason": "end_turn"}


def run(model: str, fx: dict, order: list, n: int, eval_on: bool = False, batch: bool = False) -> None:
    """Gate, every case leg n times (interleaved), then the eval items; every call logged in full by log_call."""
    stub = model.split(":", 1)[1] if model in STUBS else None
    ask = (lambda msgs: stub_reply(stub, fx, msgs[-1]["content"])) if stub else (lambda msgs: call(model, msgs))
    gfx = fx["principle_gate"]
    if eval_on:
        eval_check(fx)  # before any call, not after the case calls are paid for
    n_gate = min(n, 5)
    print(f"\n### FIXTURE {fx['fixture_id']}  instrument={fx['instrument']}  model={model}  cases={order}  n={n}  "
          f"gate_n={n_gate}  eval={'on' if eval_on else 'off'}  lane={'batch' if batch else 'live'}  probe=outcome_probe")
    print(f"### SCORING frozen {SCORING_FROZEN}  sha {scoring_sha()[:16]}")
    if stub:
        print("### scripted stub: no network; prompts not printed (each is matched to its fixture leg by text)")
    else:
        print(f"### PROMPT CACHE {'on (top-level cache_control ephemeral; message content unchanged)' if PROMPT_CACHE else 'off'}")
        print("### STEP 0 - auth/plumbing smoke (1 tiny call)")
        log_call("step0", model, PLUMBING, ask([{"role": "user", "content": PLUMBING}]), (),
                 lambda t: exact_parse(fx, "case", t))
    if fx.get("system"):  # the judge's own system prompt, sent unchanged; printed once here, not per call
        print("### SYSTEM PROMPT (sent before every gate and case call, not the eval check) ---")
        print(fx["system"])
        print("### END SYSTEM PROMPT")

    jobs = [("GATE", "gate", i) for i in range(n_gate)]
    jobs += [(cid, leg, i) for cid in order for i in range(n) for leg in fx["cases"][cid]["legs"]]
    if eval_on:
        jobs += [("EVAL/" + cid, leg, i) for cid, leg in eval_items(fx, order) for i in range(EVAL_REPS)]

    def text(cid: str, leg: str) -> str:
        if cid == "GATE":
            return gfx["prompt"]
        if cid.startswith("EVAL/"):
            return eval_check(fx)["prompt"].format(prompt=prompt(fx, cid[5:], leg))
        return prompt(fx, cid, leg)

    def msgs(cid: str, p: str) -> list:
        """The user turn, after the fixture's system prompt when it has one. The eval check asks about the prompt from
        outside, so it gets no system turn."""
        system = [] if cid.startswith("EVAL/") or not fx.get("system") else [{"role": "system", "content": fx["system"]}]
        return system + [{"role": "user", "content": p}]

    cust = lambda cid, leg, i: f"{cid}-{leg}-{i}".replace("_", "").replace("/", "-")
    if batch:
        replies = batch_call(model, {cust(*j): msgs(j[0], text(j[0], j[1])) for j in jobs})
    start, section = time.monotonic(), None
    for k, (cid, leg, i) in enumerate(jobs, 1):
        head = "GATE" if cid == "GATE" else "EVAL" if cid.startswith("EVAL/") else "CASE " + cid
        if head != section:
            section = head
            print(f"\n\n### {head} - fresh conversation per call")
        p = text(cid, leg)
        label = f"GATE/{i}" if cid == "GATE" else f"{cid}/{leg}/{i}"
        kind = "gate" if cid == "GATE" else "eval" if cid.startswith("EVAL/") else "case"
        parse = lambda t: exact_parse(fx, kind, t)
        if batch:
            r = replies[cust(cid, leg, i)]
        else:
            r = ask(msgs(cid, p))
            if not r["answer"].strip():  # empty (a dropped reply, or a refusal): logged in full, listed, retried once
                log_call(f"RETRIED/{label}", model, None, r, (), parse)
                r = ask(msgs(cid, p))
        got = log_call(label, model, None if stub else p, r, (), parse)
        missing = [f for f in required(fx)[kind] if got.get(f) is None]
        if missing:  # one grep finds every unparsed answer; its full text is right above
            print(f"!!! NOT PARSED: {label} ({', '.join(missing)} missing)")
        unread = [f for f in reported(fx).get(kind, []) if got.get(f) is None]
        if unread:
            print(f"!!! LABEL UNREADABLE: {label} ({', '.join(unread)})")
        if got.get("_fuzzy_names"):
            print(f"!!! FIELD NAME REPAIRED: {label} ("
                  + ", ".join(f"{seen} -> {name}" for seen, name in got["_fuzzy_names"]) + ")")
        if not batch and not stub:
            el = time.monotonic() - start
            print(f"### PROGRESS {k}/{len(jobs)}  elapsed={el:.0f}s  ~remaining={el / k * (len(jobs) - k):.0f}s",
                  flush=True)


def assess(fx: dict, rows: list, model: str) -> dict:
    """Score the model's rows and both stubs' rows with score_luck, run the instrument gate, word the verdicts.
    Every verdict is re-parsed from the raw answer in the log (exact_rows)."""
    rows = exact_rows(fx, rows)
    lf = fx.get("liability_field", "LIABILITY")
    present = [cid for cid in fx["cases"] if any(r["kind"] == "case" and r["cid"] == cid and r["model"] == model
                                                 for r in rows)]
    expected = {}
    for cid in present:
        c = fx["cases"][cid]
        expected[cid] = {"role": c["role"], "legs": list(c["legs"])}
        if c["role"] == "case":
            expected[cid] |= {"liability_expected": c["liability_expected"],
                              "basis_outcome_terms": c.get("basis_outcome_terms", {})}

    def score(m: str, keep=lambda r: True) -> dict:
        reps, gate = {}, []
        for r in filter(keep, rows):
            if r["model"] == m and r["kind"] == "gate":
                gate.append(r["verdict"])
            elif r["model"] == m and r["kind"] == "case":
                reps.setdefault(r["cid"], {}).setdefault(r["leg"], []).append(r["verdict"])
        return score_luck(expected, reps, fx["blame_tol"], gate, blame_field=fx["blame_field"],
                          harm_field=fx.get("harm_field"), bleed_p=fx["pairwise_bleed_p"],
                          gate_keys=tuple(fx["principle_gate"]["fields"]), liability_field=lf, require_liability=False)

    results = {m: score(m) for m in dict.fromkeys((*STUBS, model))}
    failures = instrument_gate(results["stub:leaking"], results["stub:governed"], results[model])
    ev = [r for r in rows if r["kind"] == "eval" and r["model"] == model]
    counts = None
    if ev:
        counts = {}
        for cid in dict.fromkeys(r["cid"] for r in ev):
            got = Counter(r["verdict"].get("READS-AS") for r in ev if r["cid"] == cid)
            counts[cid] = {"EVALUATION": got["EVALUATION"], "REAL-USE": got["REAL-USE"],
                           "unparsed": got[None], "n": sum(got.values())}
    return {"model": model, "expected": expected, "results": results, "failures": failures, "eval_counts": counts,
            "unparsed": unparsed_lines(rows, model, required(fx), reported(fx)) + [robustness_line(
                results[model], score(model, lambda r: not r["verdict"].get("_fuzzy_names")),
                sum(1 for r in rows if r["model"] == model and r["kind"] == "case" and r["verdict"].get("_fuzzy_names")))],
            "verdicts": probe_verdicts(results[model], expected, fx["blame_field"], failures, counts)}


def expectation_flags(fx: dict, rows: list, model: str) -> list[dict]:
    """Odd answers worth reading (Sara 2026-10-09): reported fields that disagree with what the fixture expects. A
    reading aid, never a verdict, outside the scoring freeze. Three checks: a case leg's outcome label is not the
    fixture's liability_expected; the harm field above 0 on a case's first leg (nothing went wrong there); on a control
    (the same outcome on both legs) the harm field moving with the conduct, by the P rule. Not caught: a harm field
    too low where real damage happened (the fixtures state no expected size). One dict per finding: what, cid, where,
    count, keys (log:line of every answer it covers)."""
    lf, hf, bp = fx.get("liability_field", "LIABILITY"), fx.get("harm_field"), fx["pairwise_bleed_p"]
    key = lambda r: f"{os.path.basename(r['log'])}:{r['line']}"
    out = []
    for cid, c in fx["cases"].items():
        by_leg = {leg: [r for r in rows if r["kind"] == "case" and r["model"] == model and r["cid"] == cid
                        and r["leg"] == leg] for leg in c["legs"]}
        if c["role"] == "case":
            for leg, rs in by_leg.items():
                want = c["liability_expected"][leg]
                off = [r for r in rs if r["verdict"].get(lf) not in (None, want)]
                if off:
                    out.append({"what": f"says {lf} {off[0]['verdict'][lf]}, expected {want}", "cid": cid,
                                "where": f"{cid} {leg}", "count": f"{len(off)} of {len(rs)}", "keys": list(map(key, off))})
            first, rs = next(iter(by_leg.items()))
            off = [r for r in rs if hf and (r["verdict"].get(hf) or 0) > 0]
            if off:
                out.append({"what": f"scores {hf} above 0 although nothing went wrong", "cid": cid,
                            "where": f"{cid} {first}", "count": f"{len(off)} of {len(rs)}", "keys": list(map(key, off))})
        elif hf:
            (l1, r1), (l2, r2) = by_leg.items()
            v1, v2 = ([r["verdict"][hf] for r in rs if r["verdict"].get(hf) is not None] for rs in (r1, r2))
            p = pairwise_p(v1, v2)
            if p is not None and _two_sided(p, bp):
                out.append({"what": f"{hf} changes with the conduct although the outcome is the same", "cid": cid,
                            "where": cid, "count": f"{l1} {statistics.median(v1):g} vs {l2} {statistics.median(v2):g}"
                            f" (P {p:.3f})", "keys": list(map(key, r1 + r2))})
    return out


def other_fields_line(fx: dict, cid: str, rs: list, legs: list, odd: list) -> str:
    """The reported fields beside the verdict's number, per leg, for one case (Sara 2026-10-09: read DAMAGE / INCIDENT
    where they apply, not only in the odd-answers table): a score as medians and P, a label as counts; then the case's
    odd answers (expectation_flags). Shared by the probe and the fix report. Reading aid, outside the scoring freeze."""
    out = []
    for f, kind in fx["fields"].items():
        if f in (fx["blame_field"], "BASIS"):
            continue
        vals = [[r["verdict"].get(f) for r in rs if r["leg"] == leg and r["verdict"].get(f) is not None] for leg in legs]
        if not any(vals):
            continue
        if kind == "score":
            p = pairwise_p(*vals)
            md = lambda v: f"{statistics.median(v):g}" if v else "?"
            out.append(f"{f}: {legs[0]} {md(vals[0])} -> {legs[1]} {md(vals[1])} (medians"
                       + (f", P {p:.3f})" if p is not None else ")"))
        else:
            cnt = lambda v: " · ".join(f"{k} {n}" for k, n in Counter(v).most_common()) or "-"
            out.append(f"{f}: {legs[0]} {cnt(vals[0])} -> {legs[1]} {cnt(vals[1])}")
    out += [f"odd: {x['what']} ({x['where']}, {x['count']})" for x in odd if x["cid"] == cid]
    return "other fields - " + " | ".join(out) if out else ""


def count_calls(text: str) -> int:
    """Calls in a log, counted independently of read_probe_log: every call header right under the ===== separator
    except step0, plus every `[label] empty/refusal ... retrying once` notice (run_luck_fix.py's retry). The trust
    guard compares the two."""
    lines = text.splitlines()
    heads = sum(1 for prev, line in zip([""] + lines, lines)
                if prev == _SEPARATOR and line.startswith("[") and "]  model=" in line and not line.startswith("[step0]"))
    return heads + sum(1 for line in lines if re.match(r"^\[\S+\] empty/refusal \(request_id=\S+\) - retrying once$", line))


def check_fidelity(paths: list, rows: list) -> None:
    """TRUST GUARD: every call in every log must be exactly one row read, or no report is built."""
    for path in paths:
        got, want = sum(1 for r in rows if r["log"] == str(path)), count_calls(pathlib.Path(path).read_text())
        if got != want:
            sys.exit(f"TRUST GUARD: {path}: {got} rows read != {want} calls in the log - refusing to build the report")


def _label(r: dict) -> str:
    of = r.get("of", r["kind"])
    return f"GATE/{r['rep']}" if of == "gate" else ("EVAL/" if of == "eval" else "") + f"{r['cid']}/{r['leg']}/{r['rep']}"


def _value(fx: dict, r: dict) -> str:
    """One row's reading in a few words, for the report summary lines."""
    v = r["verdict"]
    if r["kind"] == "retried":
        return "empty answer, retried"
    keys = {"gate": list(fx["principle_gate"]["fields"])[:1], "eval": ["READS-AS"], "case": [fx["blame_field"]]}[r["kind"]]
    got = " ".join(f"{k} {v[k]:g}" if isinstance(v.get(k), float) else f"{k} {v.get(k)}" for k in keys)
    missing = [f for f in required(fx)[r["kind"]] if v.get(f) is None]
    return (f"NOT PARSED ({', '.join(missing)} missing)" if missing else got) + (
        " · field name repaired" if v.get("_fuzzy_names") else "")


def receipt_check(rows: list) -> int:
    """TRUST GUARD, separate from read_probe_log: every row's answer must equal, byte for byte, the RESPONSE block
    under its own header line in its log. Returns how many answers matched; refuses (exits) on any mismatch."""
    cache, n = {}, 0
    for r in rows:
        if r.get("answer") is None:
            continue
        lines = cache.setdefault(r["log"], pathlib.Path(r["log"]).read_text().splitlines())
        i = r["line"]  # the header's 1-based line = the next line's 0-based index
        while i < len(lines) and lines[i] != "--- RESPONSE (visible text) ---":
            i += 1
        j = i + 1
        while j < len(lines) and not lines[j].startswith("--- PARSED VERDICT: "):
            j += 1
        if "\n".join(lines[i + 1:j]) != r["answer"]:
            sys.exit(f"TRUST GUARD: the answer shown for {_label(r)} is not the one at {r['log']}:{r['line']}")
        n += 1
    return n


NAV_JS = """document.querySelectorAll('details.call[data-pair]').forEach(function(d){d.addEventListener('toggle',function(){
document.querySelectorAll('details.call[data-pair="'+d.dataset.pair+'"]').forEach(function(o){if(o!==d&&o.open!==d.open)o.open=d.open})})});
function openTarget(){var id=location.hash.slice(1),el=id&&document.getElementById(id);if(el){var d=el.querySelector('details.call');if(d)d.open=true}}
window.addEventListener('hashchange',openTarget);openTarget();"""
SCRIPT = """(function(){
var page=document.body, key='outcome-probe:'+page.dataset.key, st={};
try{st=JSON.parse(localStorage.getItem(key)||'{}')}catch(e){}
var boxes=[].slice.call(document.querySelectorAll('input.rd'));
function note(id){return document.querySelector('textarea[data-id="'+id+'"]')}
function count(){document.getElementById('readcount').textContent='read '+boxes.filter(function(b){return b.checked}).length+' of '+boxes.length}
function keep(b){st[b.dataset.id]={x:b.checked,n:note(b.dataset.id).value};try{localStorage.setItem(key,JSON.stringify(st))}catch(e){};count()}
boxes.forEach(function(b){var s=st[b.dataset.id];if(s){b.checked=!!s.x;if(s.n!=null)note(b.dataset.id).value=s.n}
b.onchange=function(){keep(b)};note(b.dataset.id).oninput=function(){keep(b)}});
document.getElementById('savebtn').onclick=function(){
var out=['# Reading record - '+page.dataset.title,'','report '+page.dataset.key+' · saved '+new Date().toISOString(),'',
document.getElementById('readcount').textContent,''];
boxes.forEach(function(b){out.push('- ['+(b.checked?'x':' ')+'] '+b.dataset.id+' · note: '+note(b.dataset.id).value.replace(/\\n/g,' '))});
var a=document.createElement('a');a.href=URL.createObjectURL(new Blob([out.join('\\n')+'\\n'],{type:'text/markdown'}));
a.download=page.dataset.file+'-reading-record.md';a.click()};
""" + NAV_JS + """
count()})();"""
PUBLIC_SCRIPT = "(function(){" + NAV_JS + "})();"  # the public report (the default page): pairs open together, links open calls


OUTCOME_MARKERS = ("lucky", "luck", "this time", "fortunate", "happened to", "no damage", "no harm", "no data",
                   "nothing was", "no actual", "without incident", "no issue", "no problem", "went fine", "resulting",
                   "resulted", "led to", "caused", "the outcome", "consequence", "occurred", "proved", "turned out",
                   "corrupted", "two days", "two-day")  # reading aid only, never scored; past tense = the event


def vs_twin(fx: dict, r: dict, rs: list, bf: str) -> str:
    """The one line that tells a reader whether this answer leaked (Sara 2026-10-09): its {bf} number beside its twin's
    (the same rep on the other leg), and whether it held or moved. The number is the frozen verdict - no sentence is
    guessed. The sentence pointers (skip/outcome) were dropped: keyword matching cannot reliably locate open-ended
    prose, and the conduct pointer only re-quoted the BASIS. '' when there is no twin (e.g. the gate)."""
    if r["kind"] != "case":
        return ""
    legs = list(fx["cases"][r["cid"]]["legs"])
    other = legs[1 - legs.index(r["leg"])]
    twin = next((t for t in rs if t["kind"] == "case" and t["cid"] == r["cid"] and t["leg"] == other
                 and t["rep"] == r["rep"]), None)
    mv, tv = r["verdict"].get(bf), (twin["verdict"].get(bf) if twin else None)
    if mv is None or tv is None:
        return f"vs twin {other}/{r['rep']}: (no number)"
    control = fx["cases"][r["cid"]]["role"] == "control"  # a control MUST move; an outcome case holds
    if mv == tv:
        return (f"vs twin {other}/{r['rep']}: {tv:g} - HELD (a control must move - instrument check)" if control
                else f"vs twin {other}/{r['rep']}: {tv:g} - held (no move)")
    return (f"vs twin {other}/{r['rep']}: {tv:g} - moved ({mv:g} vs {tv:g}){' (as it must)' if control else ''}")


def names_outcome(text: str | None) -> bool:
    """Reading aid: does this short text (a BASIS clause) name the run's outcome, by a curated marker word? Keyword
    only, never scored - the same lossy check the 'outcome named' aid uses, here on the one-clause stated reason."""
    return bool(text) and any(w in text.lower() for w in OUTCOME_MARKERS)


def grounds_vs_number(cid: str, rs: list, legs: list, bf: str) -> list:
    """A reading aid (Sara 2026-10-09): per rep, did {bf} move between the two legs, and does the stated reason (BASIS)
    name only the conduct on both legs? The number is the frozen verdict; the BASIS is verbatim. A breach = the number
    moved while neither leg's reason names the outcome - the model's own stated ground does not account for its own move
    (the number side and the reason side dissociate). 'names the outcome' is the keyword aid above: read the BASIS too."""
    out = []
    for rep in sorted({r["rep"] for r in rs}):
        a_, b_ = (next((r for r in rs if r["leg"] == leg and r["rep"] == rep), None) for leg in legs)
        va, vb = (x["verdict"].get(bf) if x else None for x in (a_, b_))
        ba, bb = ((x["verdict"].get("BASIS") if x else None) for x in (a_, b_))
        moved = va is not None and vb is not None and va != vb
        out.append({"rep": rep, "a": va, "b": vb, "moved": moved, "basis_a": ba, "basis_b": bb,
                    "out_a": names_outcome(ba), "out_b": names_outcome(bb),
                    "breach": moved and not names_outcome(ba) and not names_outcome(bb)})
    return out


HOW_TO_READ = """<ol class="how">
<li>Each case is two prompts, identical except one paragraph: the outcome. The control changes the conduct instead.</li>
<li>Open a pair: is the decision described the same way, with a different number? Is the outcome given as the
reason?</li>
<li>Tick what you read; <i>save reading record</i> keeps your ticks and notes as a .md file.</li>
<li>A leak is strong evidence. No leak is not proof of good judgment: the model may know it is a test.</li>
</ol>"""


def render_html(fx: dict, a: dict, rows: list, logs: list, out: pathlib.Path, public: bool = False) -> str:
    """The probe's one report: result first, then every answer, verbatim from the log and checked against it. A small
    inline script keeps reading ticks and notes in the browser and saves them as a markdown reading record (Sara,
    2026-10-08); everything else reads with scripts off. No external files. public (Sara 2026-10-12): the public
    report (public_report.py) - same rows, receipts and trust guards."""
    if public:
        import public_report
        return public_report.render_public(fx, a, rows, logs, out)
    e = lambda x: html.escape(str(x))
    m, v, bf = a["model"], a["verdicts"], fx["blame_field"]
    rel = lambda p: os.path.relpath(p, out.parent)
    mine = [r for r in rows if r["model"] == m]
    req, rep = required(fx), reported(fx)
    unparsed = lambda r: r["kind"] in req and any(r["verdict"].get(f) is None for f in req[r["kind"]])
    unread = lambda r: any(r["verdict"].get(f) is None for f in rep.get(r["kind"], []))
    flagged = lambda r: r["kind"] == "retried" or unparsed(r) or unread(r) or bool(r["verdict"].get("_fuzzy_names"))
    leaks = [cid for cid, line in v["cases"].items() if line.startswith("LEAK DETECTED")]
    key = lambda r: f"{os.path.basename(r['log'])}:{r['line']}"
    odd = expectation_flags(fx, rows, m)
    off_keys = {k for f in odd for k in f["keys"]}
    must = lambda r: (r["kind"] == "gate" or flagged(r) or (r["kind"] == "case" and r["cid"] in leaks)
                      or key(r) in off_keys)
    anchor = lambda r: "call-" + re.sub(r"[^A-Za-z0-9]", "-", key(r))
    receipt = receipt_check(mine)
    heads = {}  # the receipt shown per call: request_id, or the response id when the provider sends none (Ollama)

    def rid(r: dict) -> str:
        if r["request_id"] not in ("?", "", None):
            return f"request_id {r['request_id']}"
        lines = heads.setdefault(r["log"], pathlib.Path(r["log"]).read_text().splitlines())
        got = re.search(r"  id=(\S+)", lines[r["line"] - 1])
        return f"request_id: not sent (local model) · response id {got.group(1)}" if got else "request_id: not sent"

    def raw_span(r: dict) -> tuple[int, int]:
        """The call's own lines in its log, 1-based inclusive: from the ===== separator over its header through its
        last line before the next call or section (trailing blank lines dropped)."""
        lines = heads.setdefault(r["log"], pathlib.Path(r["log"]).read_text().splitlines())
        h = r["line"] - 1
        start = h - 1 if h and lines[h - 1] == _SEPARATOR else h
        end = h + 1
        if not lines[h].startswith("[") or "]  model=" not in lines[h]:  # a retry notice (run_luck_fix.py): one line
            return h + 1, h + 1
        while end < len(lines) and not (lines[end] == _SEPARATOR or lines[end].startswith("### ")
                                        or lines[end].startswith("[run-evidence]")
                                        or re.match(r"^\[\S+\] empty/refusal ", lines[end])):
            end += 1
        while end - 1 > h and not lines[end - 1].strip():
            end -= 1
        return start + 1, end

    def raw_block(r: dict) -> str:
        """The call's raw lines, verbatim, except its prompt: shown once per leg above, so folded here into one
        marked line - after checking the folded lines ARE that prompt, byte for byte."""
        a_, b_ = raw_span(r)
        chunk = heads[r["log"]][a_ - 1:b_]
        if "--- PROMPT (user turn) ---" in chunk:
            p0 = chunk.index("--- PROMPT (user turn) ---") + 1
            p1 = next((i for i in range(p0, len(chunk)) if chunk[i].startswith("--- ")), len(chunk))
            if "\n".join(chunk[p0:p1]) != r["prompt"]:
                sys.exit(f"TRUST GUARD: the prompt folded out of {_label(r)}'s raw lines is not the one shown")
            chunk = chunk[:p0] + [f"[prompt: lines {a_ + p0}-{a_ + p1 - 1}, identical to the prompt sent above]"] + chunk[p1:]
        body = "\n".join(chunk)
        return f'<div class="ch">raw log lines {a_}-{b_}</div><pre class="raw">{e(body)}</pre>'

    def call(r: dict) -> str:
        # Every line new (Sara 2026-10-09): a case cell already shows PROCESS (the big number) and BASIS above this
        # dropdown, so the expansion repeats neither. It shows the OTHER fields, then the one line that says whether it
        # leaked (vs twin), then the full reply and the raw-log receipt. The gate (no cell) shows all its fields.
        fmt = lambda k, v: f"{k} {v:g}" if isinstance(v, float) else f"{k} {v}"
        skip = {bf, "BASIS", "_fuzzy_names"} if r["kind"] == "case" else {"_fuzzy_names"}
        head = f"{_label(r)}" if r["kind"] == "case" else f"{_label(r)} · {_value(fx, r)}"
        fields = " · ".join(fmt(k, v) for k, v in r["verdict"].items() if k not in skip)
        if r["verdict"].get("_fuzzy_names"):
            fields += (" · " if fields else "") + "field names repaired: " + ", ".join(
                f"{a} -> {b}" for a, b in r["verdict"]["_fuzzy_names"])
        tick = r["kind"] in ("gate", "case") or must(r)  # a checkbox on everything you actually read, held cases too
        box = (f'<input type="checkbox" class="rd" data-id="{e(_label(r) + " · " + key(r))}">' if tick else "")
        note = (f'<textarea class="note" data-id="{e(_label(r) + " · " + key(r))}" rows="1" placeholder="note">'
                "</textarea>" if tick else "")
        # fields + the vs-twin line sit OUTSIDE the dropdown (Sara 2026-10-09): all the verdict data visible without a
        # click; only the raw reply, raw log lines and the request_id receipt are behind the ❯.
        parsed = (f'<p class="parsed">{e(fields)}</p>' if fields else "")
        twin = f'<p class="parsed">{e(vs_twin(fx, r, mine, bf))}</p>' if r["kind"] == "case" else ""
        pair = f' data-pair="{e(r["cid"])}-{r["rep"]}"' if r["kind"] == "case" else ""  # twin auto-expands together
        return (f'<div class="item" id="{anchor(r)}">{parsed}{twin}'
                f'<details class="call"{pair}><summary>{e(head)} · raw reply + log</summary>'
                f'<div class="ch">raw reply</div><pre class="reply">'
                f'{e(r["answer"] if r["answer"] is not None else "(no answer logged)")}</pre>'
                f'{raw_block(r)}<p class="meta">{e(rid(r))} · raw log file <a href="{e(rel(r["log"]))}">'
                f'{e(key(r))}</a></p></details>{box}{note}</div>')  # checkbox + note at the bottom of each call

    def prompt_once(rs: list) -> str:
        parts_ = []
        for leg in dict.fromkeys(r["leg"] for r in rs):
            ps = [r["prompt"] for r in rs if r["leg"] == leg]
            same = f"same prompt on all {len(ps)}" if len(set(ps)) == 1 else "PROMPTS DIFFER across reps"
            parts_.append(f'<details class="prompt"><summary>prompt sent{" - " + e(leg) if leg else ""} ({e(same)})'
                          f"</summary><pre>{e(ps[0] if ps[0] is not None else '(not in the log: scripted stub)')}"
                          f"</pre></details>")
        return "".join(parts_)

    def pairs_table(cid: str, rs: list) -> str:
        """One row per rep: each leg's number and its BASIS (the model's own stated reason, verbatim) side by side, with
        the full answer one click away, and the difference (second - first). Reading the two BASIS beside the two numbers
        is how a moved number with an unchanged, conduct-only reason (a closure breach) shows itself in the row."""
        legs = list(fx["cases"][cid]["legs"])
        def cell(r: dict) -> str:  # BASIS on its own labelled line (Sara 2026-10-09), above the dropdown; shown once
            if r is None:
                return '<td class="cell">-</td>'
            basis = r["verdict"].get("BASIS")
            bq = f'<div class="bq">BASIS: {e(basis)}</div>' if basis else ""
            return f'<td class="cell"><div class="num">{e(_value(fx, r))}</div>{bq}{call(r)}</td>'
        out_ = [f'<table class="pairs"><tr><th>rep</th><th>{e(legs[0])}</th><th>{e(legs[1])}</th><th>difference</th></tr>']
        for rep in sorted({r["rep"] for r in rs}):
            a_, b_ = (next((r for r in rs if r["leg"] == leg and r["rep"] == rep), None) for leg in legs)
            va, vb = (x["verdict"].get(bf) if x else None for x in (a_, b_))
            diff = f"{vb - va:+g}" if va is not None and vb is not None else "?"
            out_.append(f'<tr><td>{rep}</td>{cell(a_)}{cell(b_)}<td class="diff">{diff}</td></tr>')
        return "".join(out_) + "</table>"

    def other_fields(cid: str, rs: list, legs: list) -> str:
        line = other_fields_line(fx, cid, rs, legs, odd)
        return f'<p class="parsed">{e(line)}</p>' if line else ""

    def eval_section() -> str:
        """Per eval item (Sara 2026-10-09: "we dont have what prompt was sent"): the prompt sent, once, and whether the
        message inside it is byte-identical to the prompt that case leg was scored on, then that item's answers."""
        ev = [r for r in mine if r["kind"] == "eval"]
        out_ = []
        for cid, leg in dict.fromkeys((r["cid"], r["leg"]) for r in ev):
            rs = [r for r in ev if r["cid"] == cid and r["leg"] == leg]
            case = cid.removeprefix("EVAL/")
            scored = next((r["prompt"] for r in mine if r["kind"] == "case" and r["cid"] == case and r["leg"] == leg
                           and r["prompt"]), None)
            # the frozen log reader keeps the log's "  thinking: ..." line (written after the prompt, never sent) as the
            # prompt's last line; drop it on both sides, or the check compares a log line, not the prompt
            bare = lambda p: re.sub(r"\n  thinking: [^\n]*\Z", "", p)
            sent = rs[0]["prompt"]
            same = ("not checked (a prompt is not in the log)" if sent is None or scored is None else
                    "yes" if bare(sent) == eval_check(fx)["prompt"].format(prompt=bare(scored)) else "NO - read both prompts")
            out_.append(f"<h3>{e(case)} {e(leg)}</h3>" + prompt_once(rs)
                        + f'<p class="parsed">the message inside is byte-identical to the prompt Case {e(case)} '
                        f"{e(leg)} was scored on: {e(same)}</p>" + "".join(call(r) for r in rs))
        return "".join(out_)

    def case_section(cid: str) -> str:
        c = fx["cases"][cid]
        rs = [r for r in mine if r["kind"] == "case" and r["cid"] == cid]
        legs = list(c["legs"])
        role = ("control - the conduct varies, the number must move" if c["role"] == "control"
                else "the outcome varies, the number must hold")
        out_ = [f'<h2 id="case-{e(cid)}">Case {e(cid)} <small>({e(role)})</small></h2>',
                f'<p class="verdict">{e(v["cases"].get(cid, v["gate"]))}</p>']
        prompts = {leg: next((r["prompt"] for r in rs if r["leg"] == leg and r["prompt"]), None) for leg in legs}
        if all(prompts.values()):  # collapsed; each leg's differing text in a <pre> like "prompt sent", not a wall
            paras = {leg: [x.strip() for x in prompts[leg].split("\n\n") if x.strip()] for leg in legs}
            diffs = "".join(f'<div class="ch">{e(leg)}</div><pre>{e(chr(10).join(x for x in paras[leg] if x not in paras[other]))}</pre>'
                            for leg, other in (legs, legs[::-1]))
            out_.append(f'<details class="prompt"><summary>what differs between the legs</summary>{diffs}</details>')
        agg = ""  # one orienting line under the verdict (the cue the control was missing); per-rep numbers are in the rows
        grows = grounds_vs_number(cid, rs, legs, bf)
        nmoved = sum(x["moved"] for x in grows)
        med = lambda leg: (lambda v: f"{v[len(v) // 2]:g}" if v else "?")(sorted(
            x for x in (r["verdict"].get(bf) for r in rs if r["leg"] == leg) if x is not None))
        if c["role"] == "control":
            line = (f"control: the conduct differs ({legs[0]} vs {legs[1]}), so the number MUST move - it moved in "
                    f"{nmoved} of {len(grows)} reps ({legs[0]} -> {legs[1]} medians {med(legs[0])} -> {med(legs[1])}). "
                    "A control that does not move is an instrument failure, not a finding.")
        else:
            total = sum(1 for x in grows for z in (x["a"], x["b"]) if z is not None)
            conduct_only = sum(1 for x in grows for o in (x["out_a"], x["out_b"]) if not o)
            moved = f"number moved in {nmoved} of {len(grows)} reps" if nmoved else "the number did not move on any rep"
            line = (f"{moved}; the stated reason (BASIS) is conduct-only in {conduct_only} of {total} answers "
                    "(keyword check) - read the two BASIS beside each number in the rows below.")
        agg = f'<p class="verdict">{e(line)}</p>' + other_fields(cid, rs, legs)
        body = "".join(out_[1:]) + agg + prompt_once(rs) + pairs_table(cid, rs)
        return f'<details class="casewrap" open><summary>{out_[0]}</summary>{body}</details>'  # fold a finished case away

    g = a["results"][m]["gate"]
    gk = list(fx["principle_gate"]["fields"])
    short = lambda line: ("LEAK" if line.startswith("LEAK") else "no leak" if line.startswith("no leak")
                          else "moves, as it must" if line.startswith("control moves") else line.split(" - ")[0])
    order = (leaks + [cid for cid in a["expected"] if cid not in leaks and fx["cases"][cid]["role"] == "case"]
             + [cid for cid in a["expected"] if fx["cases"][cid]["role"] == "control"])
    rows_tab = []
    for cid in order:
        r_ = a["results"][m]["cases"][cid]
        f, s_ = (r_["legs"][leg]["median_blame"] for leg in fx["cases"][cid]["legs"])
        line = v["cases"].get(cid, "-")
        cell = lambda x: f"{x:g}" if x is not None else "-"
        rows_tab.append(f"<tr{' class=leak' if line.startswith('LEAK') else ''}><td><a href=\"#case-{e(cid)}\">"
                        f"{e(cid)}</a></td><td>the {e(fx['cases'][cid]['varies'])}</td><td>"
                        f"{e(' / '.join(fx['cases'][cid]['legs']))}: {cell(f)} -&gt; {cell(s_)}</td>"
                        f"<td>{r_['pairwise_p']:.3f}</td><td>{e(short(line))}</td></tr>")
    expected_gate = fx["principle_gate"].get("paper_expected", {}).get(gk[0], "NO")
    said = g["blame_differs"].get(expected_gate, 0)
    leak_note = ("The same decision got a different score when only the outcome changed." if leaks else
                 "No case crossed the rule: consistent with good judgment, not proof of it.")
    bp = fx["pairwise_bleed_p"]
    eval_tab = ("<p>Not measured (run with <code>--eval</code>).</p>" if not a["eval_counts"] else
                "<table><tr><th>case</th><th>test</th><th>real use</th><th>unreadable</th></tr>" + "".join(
                    f"<tr><td>{e(cid)}</td><td>{c['EVALUATION']}</td><td>{c['REAL-USE']}</td><td>{c['unparsed']}</td>"
                    "</tr>" for cid, c in a["eval_counts"].items()) + "</table>")
    odd_tab = ("<p>None.</p>" if not odd else
               "<table><tr><th>what is odd</th><th>where</th><th>how many</th></tr>" + "".join(
                   f'<tr><td>{e(f["what"])}</td><td><a href="#case-{e(f["cid"])}">{e(f["where"])}</a></td>'
                   f'<td>{e(f["count"])}</td></tr>' for f in odd) + "</table>")
    problems = [r for r in mine if flagged(r)]
    check = [f'<li><a href="#{anchor(r)}">{e(_label(r))}</a> · {e(_value(fx, r))}</li>' for r in problems]
    nav = ('<nav class="jump"><span id="readcount">read - of -</span> '
           '<button id="savebtn">save reading record</button><div class="lk"><a href="#result">top</a> '
           + " ".join(f'<a href="#case-{e(cid)}">{e(cid)}</a>' for cid in order)
           + ' <a href="#method">method</a></div></nav>')
    parts = [nav, f"<h1>Outcome probe - {e(m)}</h1>",
             f"<p class=\"meta\">{e(fx['fixture_id'])} · report built {datetime.date.today()}</p>",
             '<h2 id="result">Result</h2>',
             (f"<p class=\"fail\"><b>{e(v['gate'])}</b></p>" if a["failures"] else ""),
             f"<p class=\"overall\"><b>{e((not a['failures'] and smoke_note(fx, rows, m)) or v['overall'] or v['gate'])}"
             "</b></p>",
             f"<p>{e(leak_note)}</p>",
             f"<p class=\"pnote\"><b>How P works:</b> each answer on one leg is paired with each answer on the other. "
             f"P is the share of pairs where the second leg scored higher (ties count half). P 0.5 means no "
             f"difference; near 0 or 1, the change moved the score. A case leaks if P is {1 - bp:.2f} or less, or "
             f"{bp:.2f} or more; the control must reach {bp:.2f}.</p>",
             '<table class="cases"><tr><th>case</th><th>what changes</th><th>' + e(bf) + "</th><th>P</th>"
             "<th>result</th></tr>" + "".join(rows_tab) + "</table>",
             f"<p><b>The model's own rule:</b> asked cold, {said} of {g['n']} said the outcome should not change the "
             f"score of the same conduct.</p>",
             "<h3>Does the model think it is being tested?</h3>", eval_tab,
             "<h3>Odd answers worth reading</h3>", odd_tab,
             '<h2 id="how">How to read</h2>', HOW_TO_READ,
             '<p class="reading"><span id="readcount">read - of -</span> <button id="savebtn">save reading record'
             "</button></p>",
             '<h2 id="gate">The model\'s own rule: the answers</h2>',
             prompt_once([r for r in mine if r["kind"] == "gate"]),
             "".join(call(r) for r in mine if r["kind"] == "gate")]
    if problems:
        parts += ['<h2 id="check">Answers to check</h2>', "<pre>" + e("\n".join(a["unparsed"])) + "</pre>",
                  "<ul>" + "".join(check) + "</ul>",
                  ("<h3>Unreadable labels (reported, not required)</h3>" if any(unread(r) for r in mine) else ""),
                  "".join(call(r) for r in mine if r["kind"] == "retried")]
    parts += [case_section(cid) for cid in order]
    if a["eval_counts"]:
        parts += ['<h2 id="eval">Does the prompt read as a test? The answers (separate calls)</h2>',
                  eval_section()]
    parts += ['<h2 id="method">Checks and method</h2>', '<ul class="meta">',
              f"<li>{e(v['gate'])}</li>",
              "".join(f"<li>{e(x)}</li>" for x in a["unparsed"]) if not problems else "",
              f"<li>every quoted answer matches the raw log byte-for-byte: {receipt}/{receipt}</li>",
              "".join(f'<li>raw log <a href="{e(rel(p_))}">{e(os.path.basename(p_))}</a> · sha256 <code>'
                      f'{hashlib.sha256(pathlib.Path(p_).read_bytes()).hexdigest()}</code> · '
                      f'{count_calls(pathlib.Path(p_).read_text())} calls</li>' for p_ in logs),
              f"<li>scoring frozen {SCORING_FROZEN}, sha <code>{scoring_sha()[:16]}</code></li>",
              f"<li>Each answer is a fresh conversation. Limits: small n per leg, one wording per case, and a model "
              f"that recognizes a test may perform for it.</li></ul>"]
    if sum(p.count('<details class="call"') for p in parts) != len(mine):  # TRUST GUARD: one block per call (call" may carry data-pair)
        sys.exit(f"TRUST GUARD: {len(mine)} calls of {m} but a different number of answer blocks - refusing to write")
    css = ("body{font:15px/1.5 system-ui,sans-serif;max-width:62rem;margin:2rem auto;padding:0 1rem;color:#222;"
           "background:#fff}h2{margin-top:2rem;border-bottom:1px solid #ddd}pre{white-space:pre-wrap;background:#f7f7f7;"
           "padding:.5rem;font:13px/1.45 ui-monospace,monospace}table{border-collapse:collapse;margin:.6rem 0}"
           "td,th{border:1px solid #ccc;padding:.25rem .6rem;text-align:left;font-size:14px}th{background:#f3f3f3}"
           "tr.leak td{font-weight:600}.fail{color:#a00}.ok{color:#060}.meta{color:#666;font-size:13px}"
           ".verdict{font-weight:600}.item{display:flex;gap:.4rem;align-items:flex-start;margin:.15rem 0;"
           "flex-wrap:wrap}.item details{flex:1 1 100%}summary{cursor:pointer;font-size:14px}.item>input.rd{order:2;"
           "flex:0 0 auto}.note{order:3;flex:1 1 100%;font:13px system-ui;margin-left:0}blockquote{margin:.2rem 0 .2rem 1rem;padding-left:.6rem;"
           "border-left:3px solid #ccc}.ch{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:#777;margin:.7rem 0 .2rem}pre.raw{background:#fbfaf3;border:1px solid #e5e1cc;max-height:28rem;overflow:auto}pre.reply{background:#f2f7f2}.parsed{font:13px ui-monospace,monospace;margin:.2rem 0}.nums{font:13px ui-monospace,monospace}code{background:#f3f3f3;padding:0 .2rem}"
           ":target{background:#fff8d0}.how li{margin:.3rem 0}table.pairs{width:100%;table-layout:fixed}table.pairs td{vertical-align:top}table.pairs th:first-child,table.pairs td:first-child{width:2.5rem}table.pairs th:last-child,td.diff{width:5.5rem;text-align:center;font-weight:600}.num{font-weight:600}.osent{font-size:13px;color:#444;margin:.2rem 0 .3rem;font-style:italic}.cell .item{display:block}.cell .note{margin-left:0;width:100%}.cell .bq{font-size:12.5px;color:#333;margin:.15rem 0 .4rem;white-space:normal}.howcase{font-size:13px;color:#555;"
           "border-left:3px solid #9bc;padding-left:.5rem}"
           "nav.jump{position:fixed;right:.5rem;bottom:.5rem;z-index:50;background:#fff;border:1px solid #bbb;"
           "border-radius:.5rem;padding:.35rem .55rem;box-shadow:0 1px 5px rgba(0,0,0,.18);font-size:12px;max-width:12rem}"
           "nav.jump .lk{margin-top:.25rem}nav.jump a{display:inline-block;margin:.05rem .3rem .05rem 0;"
           "text-decoration:none}nav.jump button{font-size:11px}body{padding-bottom:3.5rem}"
           "details.gvn{margin:.6rem 0}details.gvn>summary{font-weight:600;color:#36c;font-size:13px}"
           "table.gvn{width:100%;table-layout:auto}table.gvn td.bq{font-size:12px;white-space:normal;vertical-align:top}"
           ".parsed.closure{font-weight:600;color:#704}"
           "details.casewrap{margin:1rem 0;border-top:1px solid #ddd}details.casewrap>summary{list-style:none}"
           "details.casewrap>summary h2{display:inline-block;margin:.6rem 0}details.casewrap>summary::before{content:'\\25b8 ';color:#888}details.casewrap[open]>summary::before{content:'\\25be '}")
    title = f"{m} on {fx['fixture_id']}"
    page_key = hashlib.sha256("".join(pathlib.Path(p).read_text() for p in logs).encode()).hexdigest()[:16]
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>Outcome probe - {e(m)}</title>"
            f"<style>{css}</style></head><body data-key=\"{page_key}\" data-title=\"{e(title)}\" "
            f"data-file=\"{e(out.stem)}\">{''.join(parts)}<script>{SCRIPT}</script></body></html>\n")


def report(fx: dict, logs: list, out: pathlib.Path, model: str | None = None, public: bool = False) -> dict:
    """Read the logs back (never memory), print and write the report.
    model: the model to report on (a run passes it; --report finds the one non-stub model in the logs)."""
    rows, paths = [], []
    for path, only in logs:
        text = pathlib.Path(path).read_text()
        if f"### FIXTURE {fx['fixture_id']} " not in text:
            sys.exit(f"{path} is not a {fx['fixture_id']} log - pass --fixture")
        full = read_probe_log(text, str(path))
        check_fidelity([path], full)
        rows += read_probe_log(text, str(path), only)
        paths.append(str(path))
    models = {r["model"] for r in rows} - set(STUBS)
    if len(models) > 1:
        sys.exit(f"one model per report, got {sorted(models)}")
    if model is None:
        if len(models) != 1:
            sys.exit("no model in these logs besides the stubs")
        model = models.pop()
    if not any(r["model"] in STUBS for r in rows):
        sys.exit("no stub runs in these logs: every outcome_probe.py log carries its own, so this is not a probe log")
    a = assess(fx, rows, model)
    v = a["verdicts"]
    g = a["results"][model]["gate"]
    print(f"\n### PROBE RESULT  model={model}  fixture={fx['fixture_id']}  scoring frozen {SCORING_FROZEN} "
          f"sha {scoring_sha()[:16]}")
    print(f"  {v['gate']}")
    for line in a["unparsed"]:
        print(f"  {line}")
    for f in expectation_flags(fx, exact_rows(fx, rows), model):
        print(f"  odd answers: {f['what']} - {f['where']}, {f['count']}")
    print(f"  model's own rule: {list(fx['principle_gate']['fields'])[0]} {g['blame_differs']} "
          f"(majority {g['majority_blame_differs']})")
    for cid, line in v["cases"].items():
        print(f"  {cid}: {line}")
    smoke = None if a["failures"] else smoke_note(fx, rows, model)
    print(f"  overall: {smoke or v['overall'] or v['gate']}")
    print(f"  {v['footer']}")
    out.write_text(render_html(fx, a, exact_rows(fx, rows), paths, out, public))
    print(f"  report: {out}")
    return a


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s: str) -> None:
        for st in self.streams:
            st.write(s)

    def flush(self) -> None:
        for st in self.streams:
            st.flush()


def main() -> None:
    args = sys.argv[1:]
    opt = lambda name, default=None: args[args.index(name) + 1] if name in args else default
    fx = json.loads(pathlib.Path(opt("--fixture", DEFAULT_FIXTURE)).read_text())
    public = "--reading" not in args  # the public report by default; --reading writes the full reading page
    page = "-report-public.html" if public else "-report.html"
    if "pairwise_bleed_p" not in fx:
        sys.exit("the probe needs a fixture with a pre-registered pairwise_bleed_p")
    if "--report" in args:
        logs = [a.partition("@") for a in args[args.index("--report") + 1:] if not a.startswith("--")
                and a not in (opt("--fixture"), opt("--html"))]
        logs = [(p, set(only.split(",")) if only else None) for p, _, only in logs]
        if not logs:
            sys.exit(__doc__)
        first = pathlib.Path(logs[0][0])
        report(fx, logs, pathlib.Path(opt("--html", first.with_name(first.stem + page))), public=public)
        return
    if not args or args[0].startswith("--"):
        sys.exit(__doc__)
    model = args[0]
    only = opt("--cases")
    order = [cid for cid in fx["cases"] if only is None or cid in only.split(",")]
    if not any(fx["cases"][cid]["role"] == "control" for cid in order):
        sys.exit("no control case selected: the instrument gate needs one (add D to --cases)")
    n = int(opt("--n", fx["n_repeats"]))
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    log = pathlib.Path(opt("--log", HERE / "probe-runs" / f"probe-{model.replace(':', '-')}-{stamp}.log"))
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w") as fh, contextlib.redirect_stdout(_Tee(sys.stdout, fh)):
        print(f"### outcome_probe {' '.join(args)}")
        for s in STUBS:
            run(s, fx, order, n)
        if model not in STUBS:
            run(model, fx, order, n, eval_on="--eval" in args, batch="--batch" in args)
    print(f"\n### LOG {log}")
    report(fx, [(log, None)], log.with_name(log.stem + page), model, public=public)


if __name__ == "__main__":
    main()
