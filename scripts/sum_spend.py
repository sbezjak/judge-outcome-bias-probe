"""Sum every logged API call in evidence/raw-logs into tokens and list-price dollars.

Usage: python3 scripts/sum_spend.py [--rows] [log-name-substring]
  no filter: the whole repo; e.g. `agent-run-judge` for the article's runs.

Each call in a raw log is a header line `[...] model=X request_id=... id=MSGID` followed by
`  usage={json}`. Paid calls are de-duplicated by message id across logs (a -collect log
re-prints a batch already paid). Ollama and stub calls cost 0 (Ollama reuses short ids, so
they are not de-duplicated). `service_tier: batch` is half price. Claude Code recordings
(traces/, gitignored) carry their own total_cost_usd and are not counted here.
"""
import collections, glob, json, os, re, sys

# $ per MTok: input, output, cache write 5m, cache write 1h, cache read (claude-api skill, cached 2026-10-06)
CLAUDE = {
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 2.0, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.20, 0.01),  # prompts up to 100K; cache at the usual 1.25x/2x/0.1x
    "claude-sonnet-5": (2.0, 10.0, 2.5, 4.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 4.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-opus-5-5": (4.0, 20.0, 5.0, 8.0, 0.20),
}
# $ per MTok: input, cached input, output - third-party price pages, not the providers' own (2026-10-13)
OTHER = {
    "openai:gpt-5.4-mini": (0.75, 0.075, 4.5),
    "deepseek:deepseek-flash": (0.14, 0.028, 0.28),
    "deepseek:deepseek-chat": (0.14, 0.028, 0.28),
}

args = [a for a in sys.argv[1:] if a != "--rows"]
only = args[0] if args else ""
root = os.path.join(os.path.dirname(__file__), "..", "evidence", "raw-logs")
hdr = re.compile(r"model=(\S+)\s+request_id=(\S+)\s+id=(\S+)")
seen, rows = set(), []
for path in sorted(glob.glob(os.path.join(root, "*.log"))):
    name = os.path.basename(path)[:-4]
    model = msgid = None
    agg = collections.defaultdict(collections.Counter)
    for line in open(path, errors="replace"):
        m = hdr.search(line)
        if m:
            model, msgid = m.group(1).rstrip(")"), m.group(3)
            continue
        s = line.strip()
        if not s.startswith("usage={") or s == "usage={}":
            continue
        u = json.loads(s[len("usage="):])
        local = (model or "").startswith(("ollama:", "stub:"))
        if not local and msgid not in ("?", None):
            if msgid in seen:
                continue
            seen.add(msgid)
        base = re.sub(r"-\d{8}$", "", model or "?")
        c = agg[(base, u.get("service_tier", "standard"))]
        c["calls"] += 1
        if "input_tokens" in u:  # Anthropic shape
            cc = u.get("cache_creation") or {}
            c["in"] += u["input_tokens"]
            c["out"] += u["output_tokens"]
            c["cw5"] += cc.get("ephemeral_5m_input_tokens", 0)
            c["cw1h"] += cc.get("ephemeral_1h_input_tokens", 0)
            c["cr"] += u.get("cache_read_input_tokens", 0)
        else:  # OpenAI / DeepSeek shape
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or u.get("prompt_cache_hit_tokens", 0)
            c["in"] += u["prompt_tokens"] - cached
            c["cr"] += cached
            c["out"] += u["completion_tokens"]
    for (base, tier), c in agg.items():
        if base in CLAUDE:
            pi, po, pw5, pw1, pr = CLAUDE[base]
            usd = (c["in"] * pi + c["out"] * po + c["cw5"] * pw5 + c["cw1h"] * pw1 + c["cr"] * pr) / 1e6
            usd *= 0.5 if tier == "batch" else 1
        elif base in OTHER:
            pi, pr, po = OTHER[base]
            usd = (c["in"] * pi + c["cr"] * pr + c["out"] * po) / 1e6
        elif base.startswith(("ollama:", "stub:")):
            usd = 0.0
        else:
            sys.exit(f"no price for {base} in {name}")
        if only in name:
            rows.append((name, base, tier, c, usd))

if "--rows" in sys.argv:
    for name, base, tier, c, usd in rows:
        print(f"{name}\t{base}\t{tier}\t{c['calls']}\t${usd:.4f}")
by = collections.defaultdict(lambda: [set(), 0, 0.0])
for name, base, tier, c, usd in rows:
    k = "local (free)" if base.startswith("ollama:") else "stub" if base.startswith("stub:") else base
    by[k][0].add(name); by[k][1] += c["calls"]; by[k][2] += usd
for k, (logs, calls, usd) in sorted(by.items()):
    print(f"{k:28s} logs={len(logs):4d} calls={calls:5d} ${usd:7.2f}")
paid = [r for r in rows if not r[1].startswith(("ollama:", "stub:"))]
smoke = [r for r in paid if "smoke" in r[0]]
print(f"{'PAID TOTAL':28s} logs={len({r[0] for r in paid}):4d} calls={sum(r[3]['calls'] for r in paid):5d} "
      f"${sum(r[4] for r in paid):7.2f}  (smokes: {len({r[0] for r in smoke})} logs, ${sum(r[4] for r in smoke):.2f})")
