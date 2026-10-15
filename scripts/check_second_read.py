#!/usr/bin/env python3
"""Check an agent's second read against the raw log, then diff its codes against a reference reading.

  python3 scripts/check_second_read.py <log> <section T1|T2|J> <agent.md> [<reference-table.md>]

The agent file is the one-row-per-call table written by .claude/agents/log-second-reader.md. Structural checks
(each failure listed, non-zero exit if any): every call of the section has exactly one row; header line, choice and
harm equal the raw log (header line, PARSED VERDICT); each quote is a verbatim piece of its cited line and that line
is in the same call's THINKING or RESPONSE; NO-SUMMARY exactly where the log has no thinking text. Then, as data:
summary numbers that differ from the answer's, and every code that differs from the reference table."""
import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from read_view import entries  # noqa: E402

REF_SECTION = {"Blind": "T1", "Reveal": "T2", "Joint": "J"}


def rows(path):
    """markdown pipe-table rows whose first cell looks like a call id -> list of cell lists."""
    out = []
    for s in open(path).read().split("\n"):
        cells = [c.strip().strip("`") for c in s.strip().strip("|").split("|")]
        if s.startswith("|") and re.match(r"(T1|T2|J)/", cells[0]):
            out.append(cells)
    return out


def numbers(verdict, key):
    """parsed verdict -> 'n' (single) or 'a/b' (joint, in verdict order); '-' when the key is absent."""
    v = ast.literal_eval(verdict) if verdict else {}
    if all(isinstance(x, dict) for x in v.values()) and v:
        vals = [p.get(key) for p in v.values()]
    else:
        vals = [v.get(key)]
    return "-" if None in vals else "/".join(str(int(x)) for x in vals)


def reference(path, section):
    """reference table -> {call id: (code, log:line, quote)} for the one section."""
    out, sec = {}, None
    for s in open(path).read().split("\n"):
        m = re.match(r"## (\w+)", s)
        if m:
            sec = REF_SECTION.get(m[1])
            continue
        cells = [c.strip() for c in s.strip().strip("|").split("|")]
        if sec == section and s.startswith("|") and len(cells) >= 5 and "/" in cells[0]:
            code = re.sub(r"\s*\(.*", "", cells[2]).strip().upper().replace(" ", "-")
            out[f"{section}/{cells[0]}"] = (code, cells[3], cells[4])
    return out


def main():
    if len(sys.argv) not in (4, 5):
        sys.exit(__doc__)
    log, section, agent = sys.argv[1:4]
    lines = open(log).read().split("\n")
    calls = [e for e in entries(log) if e["label"].split()[0].startswith(section + "/")]
    starts = sorted(e["line"] for e in entries(log))
    end_of = {a: b - 1 for a, b in zip(starts, starts[1:] + [len(lines) + 1])}
    by_id = {e["label"].split()[0]: e for e in calls}
    got = rows(agent)
    fail, mism = [], []

    seen = {}
    for r in got:
        seen[r[0]] = seen.get(r[0], 0) + 1
    fail += [f"missing row: {c}" for c in by_id if c not in seen]
    fail += [f"duplicate row x{n}: {c}" for c, n in seen.items() if n > 1]
    fail += [f"row for a call not in section {section}: {c}" for c in seen if c not in by_id]

    for r in got:
        if r[0] not in by_id:
            continue
        if len(r) < 9:
            fail.append(f"{r[0]}: {len(r)} cells, expected 9")
            continue
        call, header, choice, harm, sch, sha, code, line, quote = r[:9]
        e = by_id[call]
        think = "\n".join(e["thinking"]).strip()
        if header != str(e["line"]):
            fail.append(f"{call}: header {header}, log has {e['line']}")
        for name, key, val in (("choice", "BLAME-FOR-CHOICE", choice), ("harm", "BLAME-FOR-HARM", harm)):
            want = numbers(e["verdict"], key)
            if val != want:
                fail.append(f"{call}: {name} {val!r}, log has {want!r}")
        if (code == "NO-SUMMARY") != (not think):
            fail.append(f"{call}: code {code} but log thinking is {'empty' if not think else 'present'}")
        if code == "NO-SUMMARY":
            continue
        if not line.isdigit():
            fail.append(f"{call}: line {line!r} is not a line number")
            continue
        n = int(line)
        text = lines[n - 1] if 0 < n <= len(lines) else ""
        if not e["line"] < n <= end_of[e["line"]]:
            fail.append(f"{call}: cited line {n} is outside the call (:{e['line']}-{end_of[e['line']]})")
        elif text not in e["thinking"] and text not in e["answer"]:
            fail.append(f"{call}: cited line {n} is not THINKING or RESPONSE text")
        short_ok = quote.endswith((".", "!", "?"))  # brief: >= 30 chars, or the whole sentence if shorter
        if len(quote) < min(30, len(text)) and not short_ok or quote.replace("/", "|") not in text and quote not in text:
            fail.append(f"{call}: quote not verbatim on :{n}: {quote[:80]!r}")
        for slot, ans, summ in (("choice", choice, sch), ("harm", harm, sha)):
            if summ in ("-", "none") or summ == ans:
                continue
            a_parts, s_parts = ans.split("/"), summ.split("/")
            if len(s_parts) == 1:  # joint summary giving one number for both people
                s_parts = s_parts * len(a_parts)
            kinds = []
            for a, s in zip(a_parts, s_parts):
                if s == "-" or s == a:
                    continue
                lo_hi = re.fullmatch(r"(\d+)-(\d+)", s)  # "5-6" a range, "6 or 4" a set
                inside = (lo_hi and a.isdigit() and int(lo_hi[1]) <= int(a) <= int(lo_hi[2])
                          or " or " in s and a in re.findall(r"\d+", s))
                kinds.append("in range" if inside else "MISMATCH")
            if kinds:
                kind = "MISMATCH" if "MISMATCH" in kinds else "in range"
                mism.append(f"| {call} | {slot} | {ans} | {summ} | {kind} | :{e['line']} |")

    base = os.path.basename(log)
    print(f"# Second-read check: {os.path.basename(agent)} vs {base}, section {section}\n")
    print(f"{len(by_id)} calls in section; {len(got)} rows; {len(fail)} structural failures.\n")
    codes = {}
    for r in got:
        if len(r) > 6:
            codes[r[6]] = codes.get(r[6], 0) + 1
    print("Codes: " + ", ".join(f"{k} {v}" for k, v in sorted(codes.items())) + "\n")
    print("## Structural failures\n")
    print("\n".join(f"- {f}" for f in fail) or "none")
    print("\n## Summary number differs from the answer (agent's reading of the summary)\n")
    print("| call | slot | answer | summary | kind | header |\n|---|---|---|---|---|---|\n" + "\n".join(mism)
          if mism else "none")

    if len(sys.argv) == 5:
        ref = reference(sys.argv[4], section)
        agree, diff = 0, []
        for r in got:
            if len(r) < 9 or r[0] not in ref:
                continue
            rc, rl, rq = ref[r[0]]
            if r[6] == rc:
                agree += 1
            else:
                diff.append(f"| {r[0]} | {rc} | {r[6]} | [`{base}:{r[7]}`](../../evidence/raw-logs/{base}#L{r[7]}) "
                            f"| {r[8]} | {rq} |")
        print(f"\n## Codes vs reference ({os.path.basename(sys.argv[4])})\n")
        print(f"{len(ref)} reference rows; agree {agree}; differ {len(diff)}; "
              f"no reference row {sum(1 for r in got if r[0] not in ref)}.\n")
        if diff:
            print("| call | reference | agent | agent's line | agent's quote | reference quote |\n"
                  "|---|---|---|---|---|---|\n" + "\n".join(diff))
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
