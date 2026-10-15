#!/usr/bin/env python3
"""Mechanical check of raw run logs: one row per model call, so a call nobody checked is a missing row.

  python3 scripts/check_logs.py <out.md> <log>[@A,B,...] [<log>[@F,G] ...]

Same log specs as read_view.py. Per call: prompt identical to the other reps of its leg; parsed verdict equal to
the answer's own "KEY: value" lines (re-read here by a plain line split, not by scorer.py); `!!!` flags; answer
empty or ending without terminal punctuation; reasoning text length and billed reasoning tokens. Per case: the
lines that differ between the legs' prompts, printed once, for a human to confirm it is only the outcome."""
import difflib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from read_view import entries  # noqa: E402

END = tuple('.!?)"\'”’*`')


def usage(path):
    """line number of each call header -> billed reasoning tokens (None when the usage line has no such field)."""
    out, lines = {}, open(path).read().split("\n")
    for i, s in enumerate(lines):
        if re.match(r"\[[^\]]+\]\s+model=", s) and i + 1 < len(lines):
            m = re.search(r'"(?:reasoning|thinking)_tokens":\s*(\d+)', lines[i + 1])
            out[i + 1] = int(m[1]) if m else None
    return out


def flat(verdict):
    """parsed verdict dict -> {KEY: value}; joint verdicts {'Jesse': {...}} become JESSE-KEY."""
    out = {}
    for k, v in verdict.items():
        if isinstance(v, dict):
            out.update({f"{k.upper()}-{kk}": vv for kk, vv in v.items()})
        else:
            out[k] = v
    return out


def answer_fields(answer):
    out = {}
    for s in answer:
        m = re.match(r"^[\s*_#>`-]*([A-Z][A-Z0-9-]+)[\s*_`]*:[\s*_`]*(.*?)[\s*_`]*$", s)
        if m and m[1] not in out:
            out[m[1]] = m[2]
    return out


def same(parsed, seen):
    if parsed is None or seen is None:
        return parsed is seen
    if isinstance(parsed, (int, float)):
        m = re.match(r"(\d+(?:\.\d+)?)", seen)
        return bool(m) and float(m[1]) == float(parsed)
    return str(parsed).strip().upper() == seen.strip().upper()


def main():
    out, specs = sys.argv[1], sys.argv[2:]
    if not specs:
        sys.exit(__doc__)
    rows, legs, problems = [], {}, []
    for spec in specs:
        path, _, cases = spec.partition("@")
        keep = set(cases.split(",")) if cases else None
        toks = usage(path)
        for e in entries(path):
            label = e["label"].split("  ")[0]
            if keep is not None and label.split("/")[0] not in keep or label == "step0":
                continue
            parts = label.split("/")
            group = "/".join(parts[:-1])
            prompt = "\n".join(e["prompt"]).strip()
            first = legs.setdefault((path, group), {"prompt": prompt, "label": label})
            p_ok = prompt == first["prompt"]
            try:
                verdict = flat(eval(e["verdict"], {"__builtins__": {}})) if e["verdict"] else {}
            except Exception:
                verdict = {"<unparseable>": e["verdict"]}
            seen = answer_fields(e["answer"])
            bad = [f"{k}: parsed `{v}` vs answer `{seen.get(k)}`" for k, v in verdict.items() if not same(v, seen.get(k))]
            text = "\n".join(e["answer"]).strip()
            end = "EMPTY" if not text else ("ok" if text.endswith(END) else f"ends `...{text[-25:]}`")
            think = len("\n".join(e["thinking"]).strip())
            where = f"{os.path.basename(path)}:{e['line']}"
            row = {"label": label, "where": where, "prompt": "same" if p_ok else f"DIFFERS from {first['label']}",
                   "verdict": "ok" if not bad else "MISMATCH " + "; ".join(bad), "flags": " / ".join(e["flags"]),
                   "end": end, "think": think, "tok": toks.get(e["line"])}
            rows.append(row)
            if not p_ok or bad or e["flags"] or end != "ok" or (row["tok"] and not think):
                problems.append(row)

    # cross-leg diff: per log and turn/case, each leg's first prompt against the first leg's
    diffs, by_case = [], {}
    for (path, group), v in legs.items():
        case = "/".join(group.split("/")[:-1]) or group
        by_case.setdefault((path, case), []).append((group, v))
    for (path, case), groups in by_case.items():
        base_g, base = groups[0]
        for g, v in groups[1:]:
            a, b = base["prompt"].split(), v["prompt"].split()
            spans = [f"- `{' '.join(a[i1:i2])}`\n+ `{' '.join(b[j1:j2])}`"
                     for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
                     if op != "equal"]
            diffs.append(f"\n**{os.path.basename(path)} - `{g}` vs `{base_g}`** ({len(spans)} changed spans)\n\n"
                         + ("\n\n".join(spans) or "_identical_") + "\n")

    cells = lambda r: [r["label"], f"`{r['where']}`", r["prompt"], r["verdict"], r["flags"] or "-", r["end"],
                       str(r["think"]), "-" if r["tok"] is None else str(r["tok"])]
    head = "| call | raw log | prompt vs leg | verdict vs answer | flags | end | reasoning chars | reasoning tok |\n" \
           "|---|---|---|---|---|---|---|---|\n"
    table = lambda rs: head + "".join("| " + " | ".join(c.replace("|", "/") for c in cells(r)) + " |\n" for r in rs)
    doc = [f"# Log check: {', '.join(os.path.basename(s) for s in specs)}\n\n",
           "Generated by `scripts/check_logs.py`. Mechanical only - the raw log is the ground truth.\n\n",
           f"{len(rows)} calls checked; {len(problems)} with something to look at.\n\n",
           "## Calls to look at\n\n", table(problems) if problems else "_none_\n",
           "\n## Leg differences (confirm each is only the outcome / order)\n", *diffs,
           "\n## Every call\n\n", table(rows)]
    open(out, "w").write("".join(doc))
    print(f"{out}: {len(rows)} calls, {len(problems)} to look at")


if __name__ == "__main__":
    main()
