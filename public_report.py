"""The probe's public report (Sara 2026-10-12): result first, every run's number in a row, every answer one click away
with its request id and raw log line. Built from the same rows and assessment as the full reading page
(outcome_probe.render_html), with the same trust guards; layout only - nothing here feeds a result.

Called by outcome_probe.render_html(..., public=True): the page outcome_probe.py and geval_probe.py write unless
--reading is passed."""

from __future__ import annotations

import datetime
import hashlib
import html
import json
import os
import pathlib
import re
import statistics
import sys
from collections import Counter

import outcome_probe as op

CSS = """
body{font:15px/1.55 system-ui,sans-serif;max-width:62rem;margin:2rem auto;padding:0 1rem 3.5rem;color:#222;background:#fff}
h2{margin:2.4rem 0 .6rem}h3{margin:1.4rem 0 .4rem}p{margin:.55rem 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f7f7f7;padding:.5rem;font:13px/1.45 ui-monospace,monospace;
max-height:30rem;overflow:auto}pre.reply{background:#f2f7f2}pre.raw{background:#fbfaf3;border:1px solid #e5e1cc}
details.rawlog>summary{font-size:12.5px;font-weight:400}
table{border-collapse:collapse;margin:.7rem 0}td,th{border:1px solid #ccc;padding:.25rem .6rem;text-align:left;
font-size:14px;vertical-align:top}th{background:#f3f3f3}
.meta,.note,.receipt{color:#666;font-size:13px}.receipt{margin:.15rem 0}code{background:#f3f3f3;padding:0 .2rem;
overflow-wrap:anywhere}
.overall{font-size:1.1rem;font-weight:700}.overall span{display:inline-block;padding:.2rem .6rem;border-radius:.3rem}
.overall.leak span{background:#fee4e2;color:#b42318}
.verdict{margin:.1rem 0 .5rem}.verdict .badge{font-size:13.5px;padding:.15rem .6rem;border-radius:.3rem}
.badge{display:inline-block;padding:0 .45rem;border-radius:1rem;font-size:12px;font-weight:700;vertical-align:middle}
.badge.leak{background:#fee4e2;color:#b42318}.badge.ok{background:#dafbe1;color:#1a7f37}
.badge.hold{background:#eaeef2;color:#57606a}
.seq{font:14px/1.7 ui-monospace,monospace;white-space:nowrap}.seq .leg{display:inline-block;min-width:5.5rem;
color:#666;font:13px system-ui,sans-serif}
.fields{margin:.3rem 0 .8rem;padding-left:1.1rem}.fields li{margin:.2rem 0}.small{font-size:12.5px;color:#666}
summary{cursor:pointer;font-size:14px;color:#36c;font-weight:600}details{margin:.35rem 0}
.ch{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:#777;margin:.6rem 0 .2rem}
.num{font-weight:600}.reason{font-size:12.5px;color:#333;margin:.15rem 0 .3rem}
table.pairs,table.list{width:100%}table.pairs{table-layout:fixed}table.pairs th:first-child,table.pairs td:first-child{width:2.5rem}

nav{position:fixed;right:.5rem;bottom:.5rem;z-index:50;background:#fff;border:1px solid #bbb;border-radius:.5rem;
padding:.35rem .55rem;box-shadow:0 1px 5px rgba(0,0,0,.18);font-size:12px;max-width:12rem}nav a{display:inline-block;
margin:.05rem .3rem .05rem 0;text-decoration:none}
:target{background:#fff8d0}
@media (max-width:640px){.seq{white-space:normal}}
"""


def render_public(fx: dict, a: dict, rows: list, logs: list, out: pathlib.Path) -> str:
    e = lambda x: html.escape(str(x))
    m, v, bf = a["model"], a["verdicts"], fx["blame_field"]
    mine = [r for r in rows if r["model"] == m]
    rel = lambda p: os.path.relpath(p, out.parent)
    key = lambda r: f"{os.path.basename(r['log'])}:{r['line']}"
    anchor = lambda r: "call-" + re.sub(r"[^A-Za-z0-9]", "-", key(r))
    leg_name = lambda leg: leg.replace("_", " ")
    num = lambda x: f"{x:g}" if isinstance(x, (int, float)) else ("?" if x is None else str(x))
    receipt = op.receipt_check(mine)  # TRUST GUARD: every answer shown is the one under its header in the log
    flags = op.expectation_flags(fx, rows, m)
    cases = [cid for cid in a["expected"]]
    leaks = [cid for cid in cases if v["cases"].get(cid, "").startswith("LEAK")]
    order = leaks + [c for c in cases if c not in leaks and fx["cases"][c]["role"] == "case"] + [
        c for c in cases if fx["cases"][c]["role"] == "control"]
    case_rows = lambda cid: [r for r in mine if r["kind"] == "case" and r["cid"] == cid]
    blocks = 0

    def rid(r: dict) -> str:
        if r.get("request_id") not in ("?", "", None):
            return f"request id {r['request_id']}"
        return "request id: none (local model)"

    logs_text = {}

    def raw_lines(r: dict) -> str:
        """The call's own raw log lines, verbatim, its prompt folded into one marked line after checking the folded
        lines ARE the prompt shown above (as outcome_probe.render_html does)."""
        lines = logs_text.setdefault(r["log"], pathlib.Path(r["log"]).read_text().splitlines())
        h = r["line"] - 1
        start = h - 1 if h and lines[h - 1] == op._SEPARATOR else h
        end = h + 1
        if lines[h].startswith("[") and "]  model=" in lines[h]:
            while end < len(lines) and not (lines[end] == op._SEPARATOR or lines[end].startswith(("### ", "[run-evidence]"))
                                            or re.match(r"^\[\S+\] empty/refusal ", lines[end])):
                end += 1
            while end - 1 > h and not lines[end - 1].strip():
                end -= 1
        chunk = lines[start:end]
        if "--- PROMPT (user turn) ---" in chunk:
            p0 = chunk.index("--- PROMPT (user turn) ---") + 1
            p1 = next((i for i in range(p0, len(chunk)) if chunk[i].startswith("--- ")), len(chunk))
            if "\n".join(chunk[p0:p1]) != r["prompt"]:
                sys.exit(f"TRUST GUARD: the prompt folded out of {op._label(r)}'s raw lines is not the one shown")
            chunk = chunk[:p0] + [f"[prompt: lines {start + 1 + p0}-{start + p1}, the prompt sent, shown above]"] + chunk[p1:]
        return (f'<details class="rawlog"><summary>raw log: {e(os.path.basename(r["log"]))} lines {start + 1}-{end}'
                f'</summary><pre class="raw">{e(chr(10).join(chunk))}</pre><div class="receipt"><a href="{e(rel(r["log"]))}">'
                "open the whole log</a></div></details>")

    def answer(r: dict, pair: str = "") -> str:
        """The verbatim reply behind a dropdown, then the receipt: request id, then the raw log line, each on its own line."""
        nonlocal blocks
        blocks += 1
        reply = r["answer"] if r.get("answer") is not None else "(no answer logged)"
        dp = f' data-pair="{e(pair)}"' if pair else ""  # both versions of one run open together
        return (f'<div id="{anchor(r)}"><details class="call"{dp}><summary>{e(op._label(r))} · full answer + log</summary>'
                f'<div class="ch">raw reply</div><pre class="reply">{e(reply)}</pre>'
                f'<div class="receipt">{e(rid(r))}</div>'
                f'{raw_lines(r)}'
                "</details></div>")

    def badge(cid: str) -> str:
        line = v["cases"].get(cid, "")
        if line.startswith("LEAK"):
            return '<span class="badge leak">LEAK</span>'
        if line.startswith("control moves"):
            return '<span class="badge ok">MOVES</span>'
        if fx["cases"][cid]["role"] == "control":
            return '<span class="badge leak">CONTROL DID NOT MOVE</span>'
        return '<span class="badge hold">NO LEAK</span>'

    def who(cid: str) -> str:
        """The agent and its job, from the case's own prompt ("one run of Relay, our customer-support agent")."""
        p = next((r["prompt"] for r in case_rows(cid) if r.get("prompt")), "") or ""
        got = re.search(r"run of (\w+), our ([^.,\n]+)", p)
        return f"{got.group(1)}, {got.group(2)}" if got else fx["cases"][cid].get("name", cid)

    def seq(cid: str) -> str:
        """Every run's number per version, in run order - the leak is in this row, not in any one answer."""
        legs = list(fx["cases"][cid]["legs"])
        rs = case_rows(cid)
        by = {leg: [r["verdict"].get(bf) for r in sorted(rs, key=lambda r: r["rep"]) if r["leg"] == leg] for leg in legs}
        return "<br>".join(f'<span class="leg">{e(leg_name(leg))}</span> ' + " ".join(num(x) for x in by[leg])
                           for leg in legs)

    def rubric_lines() -> dict:
        """Each field's meaning in the judge's own rubric ("- DAMAGE: what actually happened after the run.")."""
        p = next((r["prompt"] for r in mine if r["kind"] == "case" and r.get("prompt")), "") or ""
        return {k: d.strip().rstrip(".") for k, d in re.findall(r"^- ([A-Z][A-Z-]+): (.+)$", p, re.M)}

    meaning = rubric_lines()

    def rubric() -> str:
        """The judge's instructions without the run: G-Eval's steps (before "Test Case:"), or the probe's rubric (after
        the run's last closing tag)."""
        p = next((r["prompt"] for r in mine if r["kind"] == "case" and r.get("prompt")), "") or ""
        if "\n\nTest Case:" in p:
            text = p[:p.index("\n\nTest Case:")]
        elif "\n</" in p:
            text = p[p.rfind("\n</"):].split("\n", 2)[-1]
        else:
            return ""
        return f"<details><summary>The rubric the judge scored with</summary><pre>{e(text.strip())}</pre></details>"

    def scale(f: str) -> str:
        p = next((r["prompt"] for r in mine if r["kind"] == "case" and r.get("prompt")), "") or ""
        got = re.search(rf"^{re.escape(f)}: <a whole number from 0 \(([^)]+)\) to 10 \(([^)]+)\)", p, re.M)
        if got:
            return f"0 ({got.group(1)}) to 10 ({got.group(2)})"
        if "with 10 indicating strong alignment with the evaluation steps and 0 indicating no alignment" in p:  # G-Eval
            return "0 (no alignment with the evaluation steps) to 10 (strong alignment)"
        return "0 to 10"
    nice = lambda d: f"{d.day} {d.strftime('%B %Y')}"
    stamps = [re.match(r"\[run-evidence\] (\d{4}-\d\d-\d\d)T", pathlib.Path(p).read_text().split("\n", 1)[0])
              for p in logs]
    ran = sorted({datetime.date.fromisoformat(s.group(1)) for s in stamps if s})
    when = ((f"run {', '.join(map(nice, ran))} &middot; " if ran else "")
            + f"report built {nice(datetime.date.today())}")
    g = a["results"][m]["gate"]
    gk = list(fx["principle_gate"]["fields"])
    expected_gate = fx["principle_gate"].get("paper_expected", {}).get(gk[0], "NO")
    said = g["blame_differs"].get(expected_gate, 0)
    n_runs = max((r["rep"] for r in mine if r["kind"] == "case"), default=-1) + 1
    bp = fx["pairwise_bleed_p"]

    # --- top: what this is, the result
    if a["failures"]:
        banner = f'<p class="overall leak"><span>No result: {e(v["gate"])}</span></p>'
    elif leaks:
        n_case = sum(fx["cases"][c]["role"] == "case" for c in cases)
        banner = f'<p class="overall leak"><span>Leak on {len(leaks)} of {n_case} cases</span></p>'
    else:
        banner = '<p class="overall"><span>No leak on any case</span></p>'
    judge = fx.get("judge")  # e.g. G-Eval: the framework's judge, with this model inside it
    lead = (f"<p>Each case is one agent run shown twice, identical except for the outcome. The judge scored each "
            f"version {n_runs} times, each in a fresh conversation. Asked directly, the judge said the outcome should "
            f"not change the {e(bf)} score ({said} of {g['n']} times). A <b>leak</b> is when it changes it anyway.</p>")
    trs = []
    for cid in order:
        r_ = a["results"][m]["cases"][cid]
        legs = list(fx["cases"][cid]["legs"])
        md = " &rarr; ".join(num(r_["legs"][leg]["median_blame"]) for leg in legs)
        what = "the conduct (control)" if fx["cases"][cid]["role"] == "control" else "the outcome"
        trs.append(f'<tr><td><a href="#case-{e(cid)}"><b>{e(cid)}</b></a><div class="small">{e(who(cid))}</div></td>'
                   f'<td>{e(what)}</td><td class="seq">{seq(cid)}</td><td>{md}</td>'
                   f'<td>{r_["pairwise_p"]:.2f}</td><td>{badge(cid)}</td></tr>')
    table = ('<table><tr><th>case</th><th>what changes</th><th>' + e(bf) + ", every run</th><th>median</th><th>P</th>"
             "<th>result</th></tr>" + "".join(trs) + "</table>"
             f'<p class="note"><b>{e(bf)}</b>: the judge\'s score for the agent\'s process, {e(scale(bf))}.<br><b>P</b>: 0.5 '
             f"means both versions scored alike; {1 - bp:.2f} or less, or {bp:.2f} or more, is a leak.<br><b>Control "
             f"({e(', '.join(c for c in order if fx['cases'][c]['role'] == 'control'))})</b>: the conduct changes, so "
             "the score must move - proof the judge can see a difference.</p>" + rubric())

    # --- test awareness
    ev = [r for r in mine if r["kind"] == "eval"]
    awareness = ""
    if a.get("eval_counts"):
        legs_ev = {r["cid"].removeprefix("EVAL/"): r["leg"] for r in ev}
        awareness = ('<h2 id="test">Did the judge think it was being tested?</h2>'
                     "<p>In separate calls, the judge was shown one version's prompt and asked whether it came from real "
                     "use or from a test. A leak while the judge believes it is real use is the stronger result; no leak "
                     "while it believes it is a test says little.</p>"
                     "<table><tr><th>case (version)</th><th>read as a test</th><th>read as real use</th></tr>" + "".join(
                         f"<tr><td>{e(cid)} ({e(leg_name(legs_ev.get(cid, '')))})</td><td>{c['EVALUATION']}</td>"
                         f"<td>{c['REAL-USE']}</td></tr>" for cid, c in a["eval_counts"].items()) + "</table>"
                     + (f'<p class="note">{sum(c["unparsed"] for c in a["eval_counts"].values())} answers gave no '
                        "readable label.</p>" if any(c["unparsed"] for c in a["eval_counts"].values()) else ""))

    # --- flagged answers
    flagged = ""
    if flags:
        def plain(what):  # "says INCIDENT YES, expected NO" -> "INCIDENT YES, expected NO"
            return re.sub(r"^says ", "", what).replace(" although ", ", although ")

        def where(f):
            parts = f["where"].split(" ", 1)
            return parts[1].replace("_", " ") if len(parts) > 1 else "both versions"
        flagged = ('<h2 id="flags">Flagged answers</h2><p>Answers whose other scores do not fit the facts of the run. '
                   "They do not change the result.</p><table><tr><th>case</th><th>version</th><th>what was flagged</th>"
                   "<th>answers</th></tr>" + "".join(
                       f'<tr><td><a href="#case-{e(f["cid"])}">{e(f["cid"])}</a></td><td>{e(where(f))}</td>'
                       f'<td>{e(plain(f["what"]))}</td><td>{e(f["count"])}</td></tr>' for f in flags) + "</table>")

    # --- the judge's own rule
    gate = [r for r in mine if r["kind"] == "gate"]
    gate_prompt = next((r["prompt"] for r in gate if r.get("prompt")), None)
    # each answer field in the question's own words ("PROCESS-DIFFERS: YES or NO (does the quality ... differ ...?)")
    asked = re.findall(r"^([A-Z][A-Z-]+): YES or NO \((.+)\)$", gate_prompt or "", re.M)
    fields_said = "<br>".join(f"{e(k)}: {e(d.rstrip('?'))}" for k, d in asked)
    rule = (f'<h2 id="rule">The judge\'s own rule</h2><p>Asked before any scoring, without a case: if two runs are '
            f"identical and only the outcome differs, should the process score differ? {said} of {g['n']} answered "
            f"{e(expected_gate)}.</p>"
            + (f"<details><summary>The question asked</summary><pre>{e(gate_prompt)}</pre></details>" if gate_prompt else "")
            + "<table class=\"list\"><tr><th>run</th>" + "".join(f"<th>{e(k)}</th>" for k in gk) + "<th></th></tr>"
            + "".join(f"<tr><td>{r['rep']}</td>" + "".join(f"<td>{e(num(r['verdict'].get(k)))}</td>" for k in gk)
                      + f"<td>{answer(r)}</td></tr>" for r in gate) + "</table>"
            + (f'<p class="small">{fields_said}</p>' if asked else ""))

    # --- one section per case
    def other_scores(cid: str) -> str:
        """The other fields from the same answers, behind one dropdown, one per line with its meaning from the rubric."""
        legs = list(fx["cases"][cid]["legs"])
        rs = case_rows(cid)
        items, names = [], []
        for f, kind in fx["fields"].items():
            if f in (bf, "BASIS"):
                continue
            vals = [[r["verdict"].get(f) for r in rs if r["leg"] == leg and r["verdict"].get(f) is not None] for leg in legs]
            if not any(vals):
                continue
            if kind == "score":
                md = lambda x: f"{statistics.median(x):g}" if x else "-"
                shown = f"{leg_name(legs[0])} {md(vals[0])} &rarr; {leg_name(legs[1])} {md(vals[1])} (medians)"
            else:
                cnt = lambda x: ", ".join(f"{k} {n} of {len(x)}" for k, n in Counter(x).most_common()) or "-"
                shown = f"{leg_name(legs[0])}: {cnt(vals[0])} &middot; {leg_name(legs[1])}: {cnt(vals[1])}"
            desc = (meaning.get(f, "") + (f"; {scale(f)}" if kind == "score" else "")).strip("; ")
            names.append(f)
            items.append(f"<li><b>{e(f)}</b>" + (f' <span class="small">{e(desc)}</span>' if desc else "")
                         + f"<br>{shown}</li>")
        return (f"<details><summary>Other scores ({e(', '.join(names))})</summary><p class=\"small\">From the same "
                f'answers; not part of the result.</p><ul class="fields">{"".join(items)}</ul></details>') if items else ""

    def case_section(cid: str) -> str:
        c = fx["cases"][cid]
        legs = list(c["legs"])
        rs = case_rows(cid)
        sub = (f"The conduct differs ({' / '.join(map(leg_name, legs))}); the outcome is the same. {e(bf)} must move."
               if c["role"] == "control" else f"Only the outcome differs. {e(bf)} should not move.")
        prompts = {leg: next((r["prompt"] for r in rs if r["leg"] == leg and r.get("prompt")), None) for leg in legs}

        def sent():
            """Both versions' full prompts in one dropdown, each checked to be the same on every run."""
            same = lambda leg: len({r["prompt"] for r in rs if r["leg"] == leg}) == 1
            n = len([r for r in rs if r["leg"] == legs[0]])
            label = (f"same prompt on all {n} runs of each version" if all(map(same, legs))
                     else "PROMPTS DIFFER across runs")
            return (f"<details><summary>Prompts sent ({e(label)})</summary>" + "".join(
                f'<div class="ch">{e(leg_name(leg))}</div><pre>{e(prompts[leg])}</pre>' for leg in legs) + "</details>")
        diff = ""
        if all(prompts.values()):
            paras = {leg: [x.strip() for x in prompts[leg].split("\n\n") if x.strip()] for leg in legs}
            only = {leg: "\n".join(x for x in paras[leg] if x not in paras[o]) for leg, o in (legs, legs[::-1])}
            diff = ("<details><summary>What differs between the two versions</summary>" + "".join(
                f"<h3>{e(leg_name(leg))}</h3><pre>{e(only[leg])}</pre>" for leg in legs) + "</details>"
                + sent())
        def cell(r, pair_id=""):
            if r is None:
                return "<td>-</td>"
            vd = r["verdict"]
            rest = " &middot; ".join(f"{e(k)} {e(num(x))}" for k, x in vd.items()
                                     if k not in (bf, "BASIS") and not k.startswith("_") and x is not None)
            basis = f'<div class="reason">BASIS: {e(vd["BASIS"])}</div>' if vd.get("BASIS") else ""
            return (f'<td><div class="num">{e(num(vd.get(bf)))}</div>{basis}'
                    + (f'<div class="small">{rest}</div>' if rest else "") + answer(r, pair=pair_id) + "</td>")
        trs = []
        for rep in sorted({r["rep"] for r in rs}):
            pair = [next((r for r in rs if r["leg"] == leg and r["rep"] == rep), None) for leg in legs]
            # side by side for reading only: run i of each version shares nothing but its send order, so no row-wise
            # difference is shown (Sara 2026-10-12) - the case's shift is in the two number rows and P
            trs.append(f'<tr><td>{rep}</td>{cell(pair[0], f"{cid}-{rep}")}{cell(pair[1], f"{cid}-{rep}")}</tr>')
        r_ = a["results"][m]["cases"][cid]
        answers = (f'<table class="pairs"><tr><th>run</th><th>{e(leg_name(legs[0]))}</th>'
                   f'<th>{e(leg_name(legs[1]))}</th></tr>' + "".join(trs) + "</table>")
        return (f'<h2 id="case-{e(cid)}">Case {e(cid)}: {e(who(cid))}</h2><p class="verdict">{badge(cid)}</p><p>{sub}</p>'
                f'<p class="seq">{seq(cid)}</p>'
                f'<p class="small">medians {" &rarr; ".join(num(r_["legs"][leg]["median_blame"]) for leg in legs)} '
                f'&middot; P {r_["pairwise_p"]:.2f}</p>' + other_scores(cid) + diff + answers)

    # --- test-awareness answers
    def eval_answers() -> str:
        if not ev:
            return ""
        out_ = ['<h2 id="test-answers">Test-awareness answers</h2>']
        for cid, leg in dict.fromkeys((r["cid"], r["leg"]) for r in ev):
            rs = [r for r in ev if r["cid"] == cid and r["leg"] == leg]
            out_.append(f"<h3>{e(cid.removeprefix('EVAL/'))} ({e(leg_name(leg))})</h3>"
                        + (f"<details><summary>The question asked</summary><pre>{e(rs[0]['prompt'])}</pre></details>"
                           if rs[0].get("prompt") else "")
                        + '<table class="list"><tr><th>ask</th><th>answer</th></tr>' + "".join(
                            f"<tr><td>{r['rep']}</td><td><b>{e(num(r['verdict'].get('READS-AS')))}</b>"
                            f"{answer(r)}</td></tr>" for r in rs) + "</table>")
        return "".join(out_)

    row = lambda check, result: f'<tr><th scope="row">{check}</th><td>{result}</td></tr>'

    def eval_prompts_match() -> str:
        """The test-awareness question must wrap the very prompt that version was scored on - checked here, once."""
        if not ev:
            return ""
        bare = lambda p: re.sub(r"\n  thinking: [^\n]*\Z", "", p)
        res = []
        for cid, leg in dict.fromkeys((r["cid"], r["leg"]) for r in ev):
            sent = next(r["prompt"] for r in ev if r["cid"] == cid and r["leg"] == leg)
            scored = next((r["prompt"] for r in mine if r["kind"] == "case" and r["cid"] == cid.removeprefix("EVAL/")
                           and r["leg"] == leg and r.get("prompt")), None)
            res.append(None if sent is None or scored is None else
                       bare(sent) == op.eval_check(fx)["prompt"].format(prompt=bare(scored)))
        if any(x is False for x in res):
            sys.exit("TRUST GUARD: a test-awareness question does not wrap the prompt its version was scored on")
        checked = sum(x is True for x in res)
        return row("Test question used the scored prompt", f"{checked} of {len(res)}, exactly")

    other = [r for r in mine if r["kind"] not in ("gate", "case", "eval")]  # retry notices: shown, never dropped
    retries = ("" if not other else '<h2 id="retries">Retried calls</h2><ul>' + "".join(
        f"<li>{e(op._label(r))} {answer(r)}</li>" for r in other) + "</ul>")

    gate_ok = not a["failures"]
    if not judge and op.scoring_sha() != op.SCORING_SHA:  # a G-Eval report is checked in geval_probe.report, before its swap
        sys.exit("TRUST GUARD: the probe's scoring code differs from its freeze - refusing to write")
    # the fingerprint the run printed into its own log (outcome_probe.run): the freeze's evidence is the run, not a date
    frozen_line = next((i for i, x in enumerate(pathlib.Path(logs[0]).read_text().splitlines(), 1)
                        if x.startswith("### SCORING ") and x.endswith(f"sha {op.SCORING_SHA[:16]}")), None)
    if not judge and frozen_line is None:
        sys.exit(f"TRUST GUARD: {logs[0]} does not print the frozen scoring fingerprint - refusing to write")

    # G-Eval's retried calls, read from its log for display only: the frozen scorer never sees them (geval_probe.read_rows)
    geval_retries = ([r for r in op.read_probe_log(pathlib.Path(logs[0]).read_text(), str(logs[0]))
                      if r["kind"] == "retried" and r["model"] == m] if judge else [])

    def geval_parse_note() -> str:
        """G-Eval only: what the 2026-10-08 freeze does NOT cover - the score reader, written with the G-Eval runs, and
        the ask-once-more rule, set after the first Opus run. Counts come from the raw logs, not from memory."""
        text = "".join(pathlib.Path(p).read_text() for p in logs)
        mismatches, scored = text.count("!!! G-EVAL MISMATCH"), text.count("--- G-EVAL SCORE")
        note = ("<li><b>Score reader not frozen:</b> the code that reads G-Eval's 0-10 score was written on 2026-10-12, the "
                f"day of these runs. DeepEval's own reader, logged beside each call, gives a different number "
                f"{mismatches} times in the {scored} scores of the raw log.</li>")
        if geval_retries:
            note += ("<li><b>Asked once more:</b> the rule to ask once more when a score is unreadable was set after a first Opus run lost 3 of "
                     "80 answers to broken JSON (that run is kept in full and gave no result). Only the second answer "
                     "counts; each first answer is in the raw log: " + ", ".join(
                         f'{e(op._label(r))} <a href="{e(rel(logs[0]))}">{e(os.path.basename(logs[0]))}:{r["line"]}</a>'
                         for r in geval_retries) + ".</li>")
        return note

    def readable() -> str:
        """The parse bookkeeping in one plain line when it is all clean; otherwise only the lines that are not."""
        clean = lambda x: bool(re.search(r"(: 0|: 0 of \d+|no repairs to check)$", x))
        bad = [x for x in a["unparsed"] if not clean(x)]
        if not bad and geval_retries:
            return row("Answers read as given", f"{len(mine)} of {len(mine)} counted: none unreadable or repaired; "
                       f"{len(geval_retries)} call{'s' if len(geval_retries) > 1 else ''} asked once more after an "
                       "unreadable score (below)")
        if not bad:
            return row("Answers read as given", f"{len(mine)} of {len(mine)}: none unreadable, repaired or retried")
        return row("Answers read as given", "<br>".join(f"<b>{e(x)}</b>" for x in bad))
    def own_run() -> str:  # Sara 2026-10-13: agent-run-judge-02 was recorded by Haiku 5.5, so that judge grades its own
        own = json.dumps({k: x for k, x in fx.items() if k != "status"}).count(m)  # model's run; say so on the page
        return (f"<li><b>Self-grading:</b> this judge scored an agent run recorded by its own model "
                f"(<code>{e(m)}</code> appears {own} times in what it reads). A hold here cannot be told apart from "
                "going easy on its own model's work; a leak still counts.</li>" if own else "")
    fp = lambda h: f'<code>{h[:16]}</code><details class="rawlog"><summary>full</summary><code>{h}</code></details>'
    checks = ['<h2 id="checks">Checks</h2><table class="checks">',
              row("Probe can find a leak, and does not invent one",
                  "Yes: it flagged a scripted fake judge built to leak and passed one built not to; on this judge the "
                  "control moved" if gate_ok else f"<b>Instrument check failed:</b> {e(v['gate'])}"),
              row("Every answer matches the raw log", f"{receipt} of {receipt}, byte for byte"),
              eval_prompts_match(),
              readable(),
              *[row("Raw log unchanged since the run", f'<a href="{e(rel(p))}">{e(os.path.basename(p))}</a> '
                    + fp(hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest())) for p in logs],
              (row(f"Scoring code frozen {e(op.SCORING_FROZEN)}", f"<code>{op.SCORING_SHA[:16]}</code>, printed in "
                   f'the run\'s own log: <a href="{e(rel(logs[0]))}">{e(os.path.basename(logs[0]))}:{frozen_line}</a>')
               if not judge else
               row(f"Probe statistics and verdict rule frozen {e(op.SCORING_FROZEN)}",
                   f"<code>{op.SCORING_SHA[:16]}</code>; the G-Eval log does not print it, the code committed with "
                   "these runs still has it")),
              '</table><p class="note">Fingerprint (SHA-256), taken when this page was built: change one character of '
              "a file and it changes; the raw log's git history shows it unchanged since the run. Request id: the id "
              "the model provider's API returned for each call, kept in the raw log; it ties each answer to one real "
              "request.</p>",
              f'<h2 id="limits">Limits</h2><ul><li><b>Small sample:</b> {n_runs} runs per version, one wording per '
              "case.</li><li><b>Test awareness:</b> a judge that recognises a test may behave differently.</li>"
              + own_run() + (geval_parse_note() if judge else "") + "</ul>"]

    nav = ('<nav><a href="#top">top</a><a href="#rule">rule</a>' + "".join(f'<a href="#case-{e(c)}">{e(c)}</a>' for c in order)
           + ('<a href="#test">test</a>' if awareness else "") + '<a href="#checks">checks</a><a href="#limits">limits</a></nav>')
    parts = [nav, f'<h1 id="top">Outcome probe: {e(judge + " with " + m if judge else m)}</h1>',
             f'<p class="meta">{e(fx["fixture_id"])} &middot; {n_runs} runs per version &middot; {when}</p>',
             lead, banner, table, rule,
             *[case_section(cid) for cid in order], flagged, awareness, eval_answers(), retries, *checks]
    page = "".join(parts)
    if blocks != len(mine):  # TRUST GUARD: one answer block per call
        sys.exit(f"TRUST GUARD: {len(mine)} calls of {m} but {blocks} answer blocks - refusing to write")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>Outcome probe - {e(m)}</title><style>{CSS}</style>'
            f"</head><body>{page}<script>{op.PUBLIC_SCRIPT}</script></body></html>\n")
