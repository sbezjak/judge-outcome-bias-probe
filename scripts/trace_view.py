#!/usr/bin/env python3
"""Reading view of a recorded Claude Code stream-json trace (agent-run-judge-02): a summary from the result line,
then one block per event - the agent's text, each tool call in full, each tool result in full, every permission
denial - each tagged with the raw trace line it came from. A view only - the raw trace stays the ground truth.
Run only after scripts/ajr02-check-trace.sh says CLEAN. Writes next to the trace (gitignored traces/).

  python3 scripts/trace_view.py traces/ajr02-<name>.jsonl     # -> traces/ajr02-<name>-view.md"""
import json
import os
import sys


def fence(text):
    return "```\n" + text.rstrip("\n") + "\n```"


def main(path):
    out = path[:-len(".jsonl")] + "-view.md"
    raw = os.path.basename(path)
    rows = []
    for n, line in enumerate(open(path), 1):
        if line.strip():
            rows.append((n, json.loads(line)))
    result = next((d for _, d in rows if d.get("type") == "result"), None)
    init = next((d for _, d in rows if d.get("subtype") == "init"), {})

    md = [f"# Trace view: {raw}", "",
          f"A view of `{raw}` ({len(rows)} lines). `L<n>` = line n of the raw trace, the ground truth.", ""]
    if result:
        u, mu = result.get("usage", {}), result.get("modelUsage", {})
        md += ["| field | value |", "|---|---|",
               f"| model | {init.get('model')} |",
               f"| tools / permission mode | {init.get('tools')} / {init.get('permissionMode')} |",
               f"| outcome | {result.get('subtype')}, {result.get('num_turns')} turns, is_error={result.get('is_error')} |",
               f"| cost (USD) | {result.get('total_cost_usd')} |",
               f"| duration | {result.get('duration_ms', 0) / 1000:.0f} s (API {result.get('duration_api_ms', 0) / 1000:.0f} s) |",
               f"| tokens | input {u.get('input_tokens')}, cache read {u.get('cache_read_input_tokens')}, "
               f"cache write {u.get('cache_creation_input_tokens')}, output {u.get('output_tokens')} |",
               f"| permission denials | {len(result.get('permission_denials', []))} |",
               f"| models used | {', '.join(mu)} |", ""]
    else:
        md += ["**No result line: the run did not finish (or the trace is cut).**", ""]

    step, progress = 0, None
    for n, d in rows:
        t, st = d.get("type"), d.get("subtype", "")
        if t == "tool_progress":
            progress = (n, d.get("elapsed_time_seconds"))
            continue
        if progress:
            md += [f"*... still running, last heartbeat {progress[1]} s (L{progress[0]})*", ""]
            progress = None
        if t == "system" and st == "init":
            md += [f"## L{n} - session start", "",
                   f"cwd `{d.get('cwd')}`, model `{d.get('model')}`, tools {d.get('tools')}, "
                   f"MCP {d.get('mcp_servers')}, skills {d.get('skills')}, slash commands {d.get('slash_commands')}, "
                   f"agents {d.get('agents')}, plugins {[p.get('name') for p in d.get('plugins', [])]}", ""]
        elif t == "system" and st == "permission_denied":
            md += [f"## L{n} - PERMISSION DENIED ({d.get('tool_name')})", "", fence(d.get("message", "")), ""]
        elif t == "system" and st in ("thinking_tokens",):
            continue
        elif t == "system":
            md += [f"*L{n} - system: {st} {json.dumps({k: v for k, v in d.items() if k in ('kind', 'branch')})}*", ""]
        elif t == "assistant":
            for c in d["message"]["content"]:
                if c["type"] == "text":
                    md += [f"## L{n} - agent says", "", c["text"], ""]
                elif c["type"] == "tool_use":
                    step += 1
                    inp = dict(c["input"])
                    cmd = inp.pop("command", None)
                    md += [f"## L{n} - step {step}: {c['name']}", ""]
                    if cmd is not None:
                        md += [fence(cmd), ""]
                    if inp:
                        md += [fence(json.dumps(inp, indent=1, ensure_ascii=False)), ""]
                elif c["type"] == "thinking":
                    md += [f"*L{n} - thinking (text not returned, {len(c.get('thinking', ''))} chars)*", ""]
        elif t == "user":
            for c in d["message"]["content"]:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    body = c["content"] if isinstance(c["content"], str) else json.dumps(c["content"], indent=1)
                    md += [f"### L{n} - result{' (ERROR)' if c.get('is_error') else ''}", "", fence(body), ""]
        elif t == "result":
            md += [f"## L{n} - final answer", "", str(d.get("result")), ""]
            for x in d.get("permission_denials", []):
                md += [f"- denied: `{json.dumps(x.get('tool_input'), ensure_ascii=False)}`"]
            md += [""]
        else:
            md += [f"*L{n} - {t} {st}*", ""]
    open(out, "w").write("\n".join(md))
    print(f"{out}: {step} tool calls, {sum(1 for _, d in rows if d.get('subtype') == 'permission_denied')} denials")


if __name__ == "__main__":
    main(sys.argv[1])
