#!/usr/bin/env python3
"""Exact input-token count of recorded traces / views for the agent-run-judge-02 cost gate, via the Messages API
count_tokens endpoint (counts only, no generation). Counts each file as one user message, per judge model
(tokenizers differ by model). Stdlib only. The key is read from ANTHROPIC_API_KEY and never
printed. Run in your own terminal:

  python3 scripts/count_trace_tokens.py traces/ajr02-dryrun.jsonl traces/ajr02-dryrun-view.md"""
import json
import os
import sys
import urllib.error
import urllib.request

URL = "https://api.anthropic.com/v1/messages/count_tokens"
MODELS = ["claude-haiku-4-5", "claude-opus-5-5"]  # the two 02 judge tiers (decided 2026-10-09)


def count(key, model, text):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": text}]}).encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)["input_tokens"], resp.headers.get("request-id")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode()[:200]}", e.headers.get("request-id")


def main(paths):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY not set")
    for p in paths:
        text = open(p).read()
        for m in MODELS:
            n, rid = count(key, m, text)
            print(f"{p}\t{len(text)} chars\t{m}\tinput_tokens={n}\trequest_id={rid}")


if __name__ == "__main__":
    main(sys.argv[1:])
