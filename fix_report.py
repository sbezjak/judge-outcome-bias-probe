"""One HTML report for a fix-arm log (run_luck_fix.py on agent-run-judge-01-fix), in the probe report's simple layout.

Built from the log only: every answer is re-parsed from its raw reply with the run's own rules (exact values, field
names within field_name_tolerance), and each reply is checked byte for byte against the log (outcome_probe's TRUST
GUARD). The 01 baseline, when given, is read with the probe's reader. Layout only - no verdict here feeds a result.

Usage: python3 fix_report.py <fix log> <fixture.json> [--baseline=<01 probe log>] [--reading]
(writes the public report, <log>-report-public.html; --reading writes the full reading page, <log>-report.html)
"""

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

from outcome_probe import PUBLIC_SCRIPT, SCRIPT, expectation_flags, other_fields_line, receipt_check
from run_luck_fix import probe_baseline
from scorer import _two_sided, pairwise_p, parse_fields

SEP = "=" * 78
HEAD = re.compile(r"^\[([^\]]+)\]  model=(\S+)  request_id=(\S+)")

HOW = """<ol class="how">
<li>Turn 1: the judge reads the run without the monitoring note and scores PROCESS only. Turn 2, same conversation: the
note is shown and it scores the full rubric again.</li>
<li>In the cases, each turn-1 answer is continued with both notes, so both reveals start from the same number.</li>
<li>A number that changes after the note is the strong result: open it - is the outcome given as the reason?</li>
<li>A kept number means the fix works. It cannot tell good judgment from sticking to an earlier answer.</li>
</ol>"""


def read_fix_log(path: str) -> list[dict]:
    """One row per GATE / T1 / T2 call: turn, cid, leg (T1: its stem), rep, t1 (the turn-1 call a T2 continues),
    request_id, line (the header's 1-based line), prompt, answer, log. Verdicts are added by parse()."""
    lines = pathlib.Path(path).read_text().splitlines()
    rows, cur, sec = [], None, None
    for i, line in enumerate(lines, 1):
        m = HEAD.match(line) if i > 1 and lines[i - 2] == SEP else None
        if m and m.group(1) != "step0":
            label = m.group(1).split()
            parts = label[0].split("/")
            cur = {"log": path, "line": i, "model": m.group(2), "request_id": m.group(3), "prompt": [], "answer": [],
                   "turn": "gate" if parts[0] == "GATE" else parts[0].lower(),
                   "cid": None if parts[0] == "GATE" else parts[1], "leg": None if parts[0] == "GATE" else parts[2],
                   "rep": int(parts[-1])}
            if len(label) == 3 and label[1] == "continues":
                t = label[2].split("/")
                cur["t1"] = (t[1], t[2], int(t[3]))
            rows.append(cur)
            sec = None
        elif m:
            cur = sec = None
        elif cur and line == "--- PROMPT (user turn) ---":
            sec = "prompt"
        elif cur and line == "--- RESPONSE (visible text) ---":
            sec = "answer"
        elif cur and line.startswith("--- PARSED VERDICT: "):
            cur, sec = None, None
        elif cur and line.startswith("--- "):
            sec = None
        elif cur and sec:
            cur[sec].append(line)
    for r in rows:
        r["prompt"], r["answer"] = "\n".join(r["prompt"]), "\n".join(r["answer"])
        r["kind"] = "gate" if r["turn"] == "gate" else "case"  # for outcome_probe's receipt check and labels
    return rows


def parse(fx: dict, rows: list) -> list[dict]:
    tol, strict = fx.get("field_name_tolerance", 0), fx.get("exact_values", False)
    fields = {"gate": fx["principle_gate"]["fields"], "t1": fx["reveal"]["turn1_fields"], "t2": fx["fields"]}
    return [r | {"verdict": parse_fields(r["answer"], fields[r["turn"]], name_tol=tol, strict=strict)} for r in rows]


def render(fx: dict, log: str, baseline: str | None, out: pathlib.Path, rows: list | None = None,
           public: bool = False) -> str:
    e = lambda x: html.escape(str(x))
    rows = parse(fx, rows if rows is not None else read_fix_log(log))
    receipt = receipt_check(rows)  # TRUST GUARD: every answer shown is the one under its header in the log
    model = rows[0]["model"]
    bf, hf, lf, bp = fx["blame_field"], fx["harm_field"], fx.get("liability_field", "LIABILITY"), fx["pairwise_bleed_p"]
    base = probe_baseline(fx, pathlib.Path(baseline).read_text(), model) if baseline else {}
    num = lambda r: r["verdict"].get(bf)
    med = lambda xs: f"{statistics.median(xs):g}" if xs else "-"
    t1 = {(r["cid"], r["leg"], r["rep"]): r for r in rows if r["turn"] == "t1"}
    t2 = [r for r in rows if r["turn"] == "t2"]
    changed = [r for r in t2 if num(r) is not None and num(t1[r["t1"]]) is not None and num(r) != num(t1[r["t1"]])]
    cases = [c for c in fx["cases"] if any(r["cid"] == c for r in t2)]
    key = lambda r: f"{os.path.basename(r['log'])}:{r['line']}"
    odd = expectation_flags(fx, [r | {"kind": "case"} for r in t2], model)
    anchor = lambda r: "call-" + re.sub(r"[^A-Za-z0-9]", "-", key(r))

    def label(r):
        return "GATE/%d" % r["rep"] if r["turn"] == "gate" else f"{r['turn'].upper()}/{r['cid']}/{r['leg']}/{r['rep']}"

    lines = pathlib.Path(log).read_text().splitlines()

    def raw_block(r):
        """The call's own raw log lines, verbatim, its prompt folded into one marked line after checking the folded
        lines ARE that prompt (it is shown once above: the turn-1 prompt, or the note)."""
        a_, b_ = r["line"] - 1, r["line"]
        while b_ < len(lines) and lines[b_] != SEP and not lines[b_].startswith("### "):
            b_ += 1
        while b_ - 1 > r["line"] and not lines[b_ - 1].strip():
            b_ -= 1
        chunk = lines[a_ - 1:b_]
        if "--- PROMPT (user turn) ---" in chunk:
            p0 = chunk.index("--- PROMPT (user turn) ---") + 1
            p1 = next((i for i in range(p0, len(chunk)) if chunk[i].startswith("--- ")), len(chunk))
            if "\n".join(chunk[p0:p1]) != r["prompt"]:
                sys.exit(f"TRUST GUARD: the prompt folded out of {label(r)}'s raw lines is not the one read")
            chunk = chunk[:p0] + [f"[prompt: lines {a_ + p0}-{a_ + p1 - 1}, shown above]"] + chunk[p1:]
        return f'<div class="ch">raw log lines {a_}-{b_}</div><pre class="raw">{e(chr(10).join(chunk))}</pre>'

    def call(r):
        # the probe report's layout (Sara 2026-10-09: "same functionality"): the verdict data visible without a click,
        # a tick and a note on every answer, only the raw reply and raw log lines behind the dropdown, and the blind
        # answer and both notes of one rep opening together
        v = r["verdict"]
        n = num(r)
        big = f"{bf} {n:g}" if n is not None else f"{bf} (not parsed)"
        rest = " · ".join(f"{k} {x:g}" if isinstance(x, float) else f"{k} {x}" for k, x in v.items()
                          if k not in (bf, "BASIS", "_fuzzy_names") and x is not None)
        if r["turn"] == "gate":
            big, rest = rest or "(nothing parsed)", ""
        basis = f'<div class="bq">BASIS: {e(v["BASIS"])}</div>' if v.get("BASIS") else ""
        vs = ""
        if r["turn"] == "t2":
            b = num(t1[r["t1"]])
            vs = (f'<p class="parsed">vs blind (turn 1): {b:g} - ' + ("kept" if b == n else f"CHANGED {b:g} -&gt; {n:g}")
                  + "</p>" if b is not None and n is not None else "")
        rid = f"{e(label(r) + ' · ' + key(r))}"
        pair = f' data-pair="{e(r["cid"])}-{r["rep"]}"' if r["turn"] != "gate" else ""
        return (f'<div class="item" id="{anchor(r)}"><div class="num">{e(big)}</div>{basis}'
                + (f'<p class="parsed">{e(rest)}</p>' if rest else "") + vs
                + f'<details class="call"{pair}><summary>{e(label(r))} · raw reply + log</summary>'
                f'<div class="ch">raw reply</div><pre class="reply">{e(r["answer"])}</pre>{raw_block(r)}'
                f'<p class="meta">request_id {e(r["request_id"])} · raw log '
                f'<a href="{e(os.path.relpath(log, out.parent))}">{e(key(r))}</a></p></details>'
                + ("" if public else f'<input type="checkbox" class="rd" data-id="{rid}"> '
                   f'<textarea class="note" data-id="{rid}" rows="1" placeholder="your note"></textarea>') + "</div>")

    def verdict(cid):
        legs = list(fx["cases"][cid]["legs"])
        a_, b_ = ([num(r) for r in t2 if r["cid"] == cid and r["leg"] == leg and num(r) is not None] for leg in legs)
        p = pairwise_p(a_, b_)
        ok = p is not None and (p >= bp if fx["cases"][cid]["role"] == "control" else not _two_sided(p, bp))
        word = (("moves, as it must" if ok else "DOES NOT MOVE") if fx["cases"][cid]["role"] == "control"
                else ("no leak" if ok else "LEAK"))
        return legs, a_, b_, p, word

    trs, leaks = [], []
    for cid in cases:
        c = fx["cases"][cid]
        legs, a_, b_, p, word = verdict(cid)
        leaks += [cid] if word == "LEAK" else []
        b = base.get(cid, {})
        b1, b2 = ([x[bf] for x in b.get(leg, []) if x.get(bf) is not None] for leg in legs)
        before = f"{med(b1)} -&gt; {med(b2)} ({pairwise_p(b1, b2):.3f})" if b1 and b2 else "-"
        stems = sorted({r["leg"] for r in t1.values() if r["cid"] == cid})
        blind = " -&gt; ".join(med([num(r) for r in t1.values() if r["cid"] == cid and r["leg"] == s]) for s in
                               (legs if len(stems) > 1 else stems))
        n_ch = sum(r["cid"] == cid for r in changed)
        n_all = sum(r["cid"] == cid for r in t2)
        trs.append(f'<tr{" class=leak" if word == "LEAK" else ""}><td><a href="#case-{e(cid)}">{e(cid)}</a></td>'
                   f"<td>the {e(c['varies'])}</td><td>{before}</td><td>{blind}</td>"
                   f"<td>{med(a_)} -&gt; {med(b_)} ({p:.3f})</td><td>{n_ch} of {n_all}</td><td>{e(word)}</td></tr>")
    gate = [r for r in rows if r["turn"] == "gate"]
    gk = list(fx["principle_gate"]["fields"])[0]
    want = fx["principle_gate"].get("paper_expected", {}).get(gk, "NO")
    said = sum(r["verdict"].get(gk) == want for r in gate)
    overall = (f"LEAK on {len(leaks)} of {sum(fx['cases'][c]['role'] == 'case' for c in cases)} cases after the fix"
               if leaks else "No leak left on any case after the fix")
    odd_tab = ("<p>None.</p>" if not odd else
               "<table><tr><th>what is odd</th><th>where</th><th>how many</th></tr>" + "".join(
                   f'<tr><td>{e(f["what"])}</td><td><a href="#case-{e(f["cid"])}">{e(f["where"])}</a></td>'
                   f'<td>{e(f["count"])}</td></tr>' for f in odd) + "</table>")
    # the probe report's floating jump nav (Sara 2026-10-09: "navigation ... same as the other one"); it carries the one
    # read count and save button, so the page has no second copy (the script updates the first id="readcount" only)
    nav = ('<nav class="jump">' + ("" if public else '<span id="readcount">read - of -</span> <button id="savebtn">'
           'save reading record</button>') + '<div class="lk"><a href="#result">top</a> ' + " ".join(f'<a href="#case-{e(c)}">{e(c)}</a>' for c in cases)
           + (' <a href="#changed">changed</a>' if changed else "") + ' <a href="#method">method</a></div></nav>')
    parts = [nav, f"<h1>Fix test - {e(model)}</h1>",
             f'<p class="meta">{e(fx["fixture_id"])} · report built {datetime.date.today()}</p>',
             '<h2 id="result">Result</h2>', f'<p class="overall"><b>{e(overall)}</b></p>',
             f"<p>The fix: score {e(bf)} before the outcome is known, then show it. Numbers "
             f"changed after the note: {len(changed)} of {len(t2)}.</p>",
             f'<p class="pnote"><b>How P works:</b> each answer on one leg is paired with each answer on the other. '
             f"P is the share of pairs where the second leg scored higher (ties count half). P 0.5 means no "
             f"difference; near 0 or 1, the change moved the score. A case leaks if P is {1 - bp:.2f} or less, or "
             f"{bp:.2f} or more; the control must reach {bp:.2f}.</p>",
             f'<table class="cases"><tr><th>case</th><th>what changes</th><th>before the fix (01): {e(bf)} (P)</th>'
             f"<th>blind (no note)</th><th>after the note (P)</th><th>changed</th><th>result</th></tr>"
             + "".join(trs) + "</table>",
             '<p class="meta">The blind column is not the 01 prompt minus the note (it adds "not available yet" and asks '
             f"{e(bf)} only), so it cannot say which way the 01 leak ran.</p>",
             f"<p><b>The model's own rule:</b> asked cold, {said} of {len(gate)} said the outcome should not change the "
             "score of the same conduct.</p>",
             "<h3>Odd answers worth reading</h3>", odd_tab,
             '<h2 id="how">How to read</h2>', HOW,
             '<h2 id="gate">The model\'s own rule: the answers</h2>', "".join(call(r) for r in gate)]
    if changed:
        parts += ['<h2 id="changed">Numbers that changed after the note</h2><ul>' + "".join(
            f'<li><a href="#{anchor(r)}">{e(label(r))}</a>: {num(t1[r["t1"]]):g} -&gt; {num(r):g}</li>'
            for r in changed) + "</ul>"]
    for cid in cases:
        c, legs = fx["cases"][cid], list(fx["cases"][cid]["legs"])
        parts.append(f'<h2 id="case-{e(cid)}">Case {e(cid)} <small>(the {e(c["varies"])} changes)</small></h2>')
        other = other_fields_line(fx, cid, [r for r in t2 if r["cid"] == cid], legs, odd)  # after the note (turn 2)
        parts.append(f"<p>{e(other.replace('other fields - ', 'other fields after the note - ', 1))}</p>" if other else "")
        stem = next(r for r in t1.values() if r["cid"] == cid)
        parts.append(f'<details class="prompt"><summary>turn-1 prompt (the run, no note)</summary><pre>'
                     f'{e(stem["prompt"])}</pre></details>')
        # the probe report's "what differs between the legs" (Sara 2026-10-09: same format): each leg's paragraphs that
        # the other leg does not have, in <pre>. A case differs in the note (turn 2); the control in the run (turn 1),
        # and its note, the same on both legs, is shown once under it.
        turn = "t2" if c["role"] == "case" else "t1"
        sent = {leg: next((r["prompt"] for r in rows if r["turn"] == turn and r["cid"] == cid and r["leg"] == leg), "")
                for leg in legs}
        paras = {leg: [x.strip() for x in sent[leg].split("\n\n") if x.strip()] for leg in legs}
        diffs = "".join(f'<div class="ch">{e(leg)}</div><pre>{e(chr(10).join(x for x in paras[leg] if x not in paras[o]))}'
                        "</pre>" for leg, o in (legs, legs[::-1]))
        if c["role"] != "case":
            shared = next((r["prompt"] for r in t2 if r["cid"] == cid), "")
            diffs += ('<div class="ch">the note (same on both legs)</div><pre>' + e(next(
                (x for x in shared.split("\n\n") if x.startswith("<monitoring_note")), "")) + "</pre>")
        parts.append(f'<details class="prompt"><summary>what differs between the legs</summary>{diffs}</details>')
        reps = sorted({r["rep"] for r in t2 if r["cid"] == cid})
        cell = lambda r: f'<td class="cell">{call(r)}</td>' if r else "<td>-</td>"
        find = lambda turn, leg, rep: next((r for r in rows if r["turn"] == turn and r["cid"] == cid
                                            and r["leg"] == leg and r["rep"] == rep), None)
        # the row's numbers side by side in its last column (Sara 2026-10-09: "those 2 2 2 numbers ... all together", as
        # the probe's difference column); bold when a number changed after the note
        fmt = lambda r: f"{num(r):g}" if r and num(r) is not None else "?"
        moved = lambda *rs: any(r and r["turn"] == "t2" and num(r) is not None and num(t1[r["t1"]]) is not None
                                and num(r) != num(t1[r["t1"]]) for r in rs)
        sums = lambda txt, *rs: f'<td class="diff">{"<b>" + txt + "</b>" if moved(*rs) else txt}</td>'
        if c["role"] == "case":
            head = (f"<tr><th>rep</th><th>blind</th><th>{e(legs[0])} note</th><th>{e(legs[1])} note</th>"
                    f"<th>blind -&gt; {e(legs[0])} / {e(legs[1])}</th></tr>")
            body = ""
            for k in reps:
                b_, n0, n1 = find("t1", "shared", k), find("t2", legs[0], k), find("t2", legs[1], k)
                body += (f"<tr><td>{k}</td>{cell(b_)}{cell(n0)}{cell(n1)}"
                         + sums(f"{fmt(b_)} -&gt; {fmt(n0)} / {fmt(n1)}", n0, n1) + "</tr>")
        else:
            head = ("<tr><th>rep</th>" + "".join(f"<th>{e(leg)} blind</th><th>{e(leg)} after the note</th>"
                                                for leg in legs) + "<th>blind -&gt; after</th></tr>")
            body = ""
            for k in reps:
                rs = [(find("t1", leg, k), find("t2", leg, k)) for leg in legs]
                body += (f"<tr><td>{k}</td>" + "".join(cell(a) + cell(b) for a, b in rs)
                         + sums("<br>".join(f"{e(leg)} {fmt(a)} -&gt; {fmt(b)}" for leg, (a, b) in zip(legs, rs)),
                                *(b for _, b in rs)) + "</tr>")
        parts.append(f'<table class="pairs">{head}{body}</table>')
    text = pathlib.Path(log).read_text()
    parts += ['<h2 id="method">Checks and method</h2><ul class="meta">',
              f"<li>every quoted answer matches the raw log byte-for-byte: {receipt}/{receipt}</li>",
              f'<li>raw log <a href="{e(os.path.relpath(log, out.parent))}">{e(os.path.basename(log))}</a> · sha256 '
              f"<code>{hashlib.sha256(text.encode()).hexdigest()}</code> · {len(rows)} calls</li>",
              f"<li>before the fix: {e(os.path.basename(baseline)) if baseline else 'no baseline given'}</li>",
              "<li>Each answer is a fresh conversation (turn 2 continues its own turn 1). Limits: small n per leg, one "
              "wording per case, hand-written transcripts, and a kept number cannot tell judgment from anchoring.</li>"
              "</ul>"]
    if sum(p.count('<details class="call"') for p in parts) != len(rows):
        sys.exit(f"TRUST GUARD: {len(rows)} calls but a different number of answer blocks - refusing to write")
    css = ("body{font:15px/1.5 system-ui,sans-serif;max-width:68rem;margin:2rem auto;padding:0 1rem;color:#222;"
           "background:#fff}h2{margin-top:2rem;border-bottom:1px solid #ddd}pre{white-space:pre-wrap;background:#f7f7f7;"
           "padding:.5rem;font:13px/1.45 ui-monospace,monospace}table{border-collapse:collapse;margin:.6rem 0}"
           "td,th{border:1px solid #ccc;padding:.25rem .6rem;text-align:left;font-size:14px;vertical-align:top}"
           "th{background:#f3f3f3}tr.leak td{font-weight:600}.meta{color:#666;font-size:13px}"
           ".item{display:flex;gap:.4rem;flex-wrap:wrap;align-items:flex-start;margin:.15rem 0}.item>.num,.item>.bq,"
           ".item>.parsed,.item details{flex:1 1 100%}summary{cursor:pointer;font-size:14px}.item>input.rd{order:2}"
           ".note{order:3;flex:1 1 100%;font:13px system-ui}.num{font-weight:600}.bq{font-size:12.5px;color:#333;"
           "margin:.15rem 0 .3rem}.parsed{font:13px ui-monospace,monospace;margin:.2rem 0}.ch{font-size:11px;"
           "font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:#777;margin:.7rem 0 .2rem}"
           "pre.raw{background:#fbfaf3;border:1px solid #e5e1cc;max-height:28rem;overflow:auto}pre.reply{background:#f2f7f2}"
           "table.pairs{width:100%;table-layout:fixed}table.pairs th:first-child,table.pairs td:first-child{width:2.5rem}table.pairs td{vertical-align:top}.cell .item{display:block}.cell .note{width:100%;box-sizing:border-box}pre{overflow-wrap:anywhere}details.prompt{margin:.4rem 0}table.pairs th:last-child,td.diff{width:7.5rem;text-align:center;font-weight:600}"
           ":target{background:#fff8d0}"
           "nav.jump{position:fixed;right:.5rem;bottom:.5rem;z-index:50;background:#fff;border:1px solid #bbb;"
           "border-radius:.5rem;padding:.35rem .55rem;box-shadow:0 1px 5px rgba(0,0,0,.18);font-size:12px;max-width:12rem}"
           "nav.jump .lk{margin-top:.25rem}nav.jump a{display:inline-block;margin:.05rem .3rem .05rem 0;"
           "text-decoration:none}nav.jump button{font-size:11px}body{padding-bottom:3.5rem}")
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Fix test - {e(model)}</title>'
            f'<style>{css}</style></head><body data-key="{e(os.path.basename(str(out)))}" data-title="Fix test - '
            f'{e(model)}" data-file="{e(out.stem)}">' + "".join(parts) + f"<script>{PUBLIC_SCRIPT if public else SCRIPT}</script></body></html>")
    out.write_text(page)
    return page


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        sys.exit(__doc__)
    base = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--baseline=")), None)
    log = args[0]
    public = "--reading" not in sys.argv
    out = pathlib.Path(log).with_name(pathlib.Path(log).stem + ("-report-public.html" if public else "-report.html"))
    render(json.loads(pathlib.Path(args[1]).read_text()), log, base, out, public=public)
    print(f"report: {out}")


if __name__ == "__main__":
    main()
