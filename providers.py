"""The HTTP layer: one call() for every judge model, the Anthropic batch lane, and the log format.

Two adapters: Anthropic native (/v1/messages) and one OpenAI-compatible adapter (/v1/chat/completions) for Ollama
(free, local), OpenAI and DeepSeek. Model spec:
    claude-opus-5-5 | claude-sonnet-5-5 | claude-haiku-4-5-20251001   (Anthropic native)
    ollama:qwen2.5:7b | openai:<model> | deepseek:<model>             (OpenAI-compatible)
The runs in evidence/ used Anthropic and Ollama only.

Stdlib only (urllib/json), so the probe needs nothing but python3. log_call prints every prompt and full response,
so a run captured by scripts/run-evidence.sh is an untruncated log with the receipts (request_id / id / usage) each
verdict rests on. Keys are read from the environment or a .env at the repo root into a header, and never printed.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

from scorer import parse_verdict

# Env-overridable so a slow local model can be given a smaller budget / longer wait without
# editing code; frontier runs keep the full defaults (thinking needs the headroom).
MAX_TOKENS = int(os.environ.get("RUN_MAX_TOKENS", "4096"))
TIMEOUT = int(os.environ.get("RUN_TIMEOUT", "300"))
ANTHROPIC_VERSION = "2023-06-01"
# Anthropic prompt caching, opt-in: the top-level automatic cache_control (caches the whole prompt; a leg sent n times
# reads it from cache after the first). The message content is unchanged. Off unless RUN_PROMPT_CACHE=1.
PROMPT_CACHE = os.environ.get("RUN_PROMPT_CACHE") == "1"

# OpenAI-compatible providers: prefix -> (base url, env-var name or None for keyless local).
OPENAI_COMPAT = {
    "ollama": ("http://localhost:11434/v1/chat/completions", None),
    "openai": ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1/chat/completions", "DEEPSEEK_API_KEY"),
}


def load_key(var: str) -> str:
    """Key from the process environment first, then a .env at the repo root (never one above it)."""
    val = os.environ.get(var)
    if val:
        return val
    envf = pathlib.Path(__file__).resolve().parent / ".env"
    if envf.is_file():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{var}=") and not line.startswith("#"):
                key = line.split("=", 1)[1].strip().strip("'\"")
                if key:
                    return key
    sys.exit(f"{var} not set (checked the environment and {envf})")


def resolve(model: str) -> tuple[str, str, str | None]:
    """(kind, real_model, provider_prefix) - kind is 'anthropic' or 'openai-compat'."""
    prefix, _, rest = model.partition(":")
    if prefix in OPENAI_COMPAT and rest:
        return "openai-compat", rest, prefix
    if prefix == "stub" and rest:  # the probe's scripted stubs (stub:leaking / stub:governed): no network, but
        # log_call still resolves their model spec
        return "stub", rest, prefix
    if model.startswith("claude") or prefix == "anthropic":
        return "anthropic", (rest if prefix == "anthropic" else model), None
    sys.exit(f"Unrecognized model spec {model!r} (use claude-*, or ollama:/openai:/deepseek:<name>)")


def _post(url: str, body: dict, headers: dict) -> tuple[dict, str]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            rid = resp.headers.get("request-id") or resp.headers.get("x-request-id", "?")
            return json.loads(resp.read()), rid
    except urllib.error.HTTPError as e:
        print("\n!!! HTTP ERROR", e.code, "request-id:", e.headers.get("request-id", "?"))
        print(e.read().decode(errors="replace"))
        raise


def _anthropic_headers() -> dict:
    return {"x-api-key": load_key("ANTHROPIC_API_KEY"), "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json"}


def _anthropic_body(real: str, messages: list, cache: bool = PROMPT_CACHE) -> dict:
    """One request body for both lanes, so a batch call sends byte-identical settings."""
    body = {"model": real, "max_tokens": MAX_TOKENS, "messages": messages}
    if cache:
        body["cache_control"] = {"type": "ephemeral"}
    if messages and messages[0]["role"] == "system":  # a leading system turn is the
        body["system"], body["messages"] = messages[0]["content"], messages[1:]  # top-level field here
    if not real.startswith("claude-haiku"):  # Haiku 4.5 uses budget_tokens; run it thinking-off
        # thinking stays on (runs compare) but its text is not requested: since 2026-10-02 no Claude reasoning
        # summaries are collected; verdicts never rested on them (scorer.py)
        body["thinking"] = {"type": "adaptive", "display": "omitted"}
    if real.startswith("claude-haiku-5-5"):  # Sara 2026-10-13: off, as Haiku 4.5 ran and Sonnet 5.5 behaved (0 thinking
        body["thinking"] = {"type": "disabled"}  # tokens on every 01/02 call); 4.5 -> 5.5 still differs in effort (high below; 4.5 takes none)
    if real.startswith(("claude-opus-5-5", "claude-haiku-5-5")):  # default effort medium on both; pin high so runs
        # compare with Opus 5 / Sonnet 5.5 (Haiku 5.5 accepts thinking off at high or below)
        body["output_config"] = {"effort": "high"}
    return body


def _anthropic_reply(data: dict, rid: str) -> dict:
    content = data.get("content", [])
    join = lambda k: "\n".join(b.get(k if k == "thinking" else "text", "")  # a thinking block keeps its
                               for b in content if b.get("type") == k).strip()  # text under "thinking"
    stop = data.get("stop_reason")
    if stop == "refusal":  # a safeguard decline names its category (e.g. reasoning_extraction); log it verbatim
        stop = f"refusal: {json.dumps(data.get('stop_details'))}"
    return {"answer": join("text"), "thinking": join("thinking"), "usage": data.get("usage", {}),
            "id": data.get("id"), "request_id": rid, "stop_reason": stop}


def call(model: str, messages: list) -> dict:
    """Normalized reply: {answer, thinking, usage, id, request_id}. messages = [{role, content:str}]."""
    kind, real, prefix = resolve(model)
    if kind == "anthropic":
        data, rid = _post_live("https://api.anthropic.com/v1/messages", _anthropic_body(real, messages),
                               _anthropic_headers())
        return _anthropic_reply(data, rid)

    url, keyvar = OPENAI_COMPAT[prefix]
    headers = {"content-type": "application/json"}
    if keyvar:
        headers["authorization"] = f"Bearer {load_key(keyvar)}"
    cap = "max_completion_tokens" if prefix == "openai" else "max_tokens"  # current OpenAI models reject max_tokens
    data, rid = _post_live(url, {"model": real, cap: MAX_TOKENS, "messages": messages}, headers)
    msg = (data.get("choices") or [{}])[0].get("message", {})
    return {"answer": (msg.get("content") or "").strip(),
            "thinking": (msg.get("reasoning_content") or "").strip(),  # DeepSeek reasoner, if present
            "usage": data.get("usage", {}), "id": data.get("id"), "request_id": rid}


def _post_live(url: str, body: dict, headers: dict) -> tuple[dict, str]:
    """_post for a live call: a dropped connection or a server-side 5xx is retried once, loudly. Never used for a
    batch submit, where a retry could submit the job twice."""
    try:
        return _post(url, body, headers)
    except (ConnectionError, TimeoutError, urllib.error.URLError) as e:
        if isinstance(e, urllib.error.HTTPError) and e.code < 500:
            raise
        print(f"\n!!! TRANSIENT ERROR {type(e).__name__}: {e} - retrying once", flush=True)
        return _post(url, body, headers)


def _get(url: str, headers: dict) -> str:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        print("\n!!! HTTP ERROR", e.code, "request-id:", e.headers.get("request-id", "?"))
        print(e.read().decode(errors="replace"))
        raise


def batch_call(model: str, requests: dict, poll_s: int = 30, max_wait_s: int = 86400,
               resume_id: str | None = None) -> dict:
    """Message Batches lane (half price, Anthropic only): {custom_id: messages} -> {custom_id: reply}.

    Ported from llm-benchmark's AnthropicBatchProvider: submit one job, poll until it ends,
    fetch the JSONL results keyed by custom_id (they come back in ANY order). Same body as
    call() via _anthropic_body. The batch_id is printed the moment the job is accepted, so a
    crash later cannot lose the receipt. A per-item request_id does not exist in this lane;
    each reply carries batch=<id>/<custom_id> plus the message id as its receipt.
    resume_id collects an already-submitted batch (e.g. after the poller was killed) instead of
    submitting again; `requests` must be the same jobs, since results are matched by custom_id."""
    import time
    kind, real, _ = resolve(model)
    if kind != "anthropic":
        sys.exit(f"batch lane is Anthropic-only, got {model!r}")
    url = "https://api.anthropic.com/v1/messages/batches"
    body = {"requests": [{"custom_id": cid, "params": _anthropic_body(real, msgs)}
                         for cid, msgs in requests.items()]}
    if resume_id:
        obj = json.loads(_get(f"{url}/{resume_id}", _anthropic_headers()))
        batch_id = obj["id"]
        print(f"\n### BATCH RESUMED  batch_id={batch_id}  n={len(requests)}  "
              f"status={obj.get('processing_status')}", flush=True)
    else:
        obj, rid = _post(url, body, _anthropic_headers())  # not _post_live: a retry could submit (and bill) twice
        batch_id = obj["id"]
        print(f"\n### BATCH SUBMITTED  batch_id={batch_id}  request_id={rid}  n={len(requests)}  "
              f"status={obj.get('processing_status')}", flush=True)
    start = time.monotonic()
    while obj.get("processing_status") != "ended":
        if time.monotonic() - start > max_wait_s:
            sys.exit(f"batch {batch_id} not ended after {max_wait_s}s - still {obj.get('processing_status')}")
        time.sleep(poll_s)
        obj = json.loads(_get(f"{url}/{batch_id}", _anthropic_headers()))
        print(f"### BATCH POLL  batch_id={batch_id}  status={obj.get('processing_status')}  "
              f"counts={json.dumps(obj.get('request_counts', {}))}", flush=True)
    print(f"### BATCH ENDED  batch_id={batch_id}  turnaround_s={time.monotonic() - start:.0f}  "
          f"counts={json.dumps(obj.get('request_counts', {}))}", flush=True)
    results = {}
    for line in _get(obj["results_url"], _anthropic_headers()).splitlines():
        if line.strip():
            entry = json.loads(line)
            results[entry["custom_id"]] = entry["result"]
    replies = {}
    for cid in requests:
        r = results.get(cid, {"type": "missing"})
        receipt = f"batch={batch_id}/{cid}"
        if r.get("type") == "succeeded":
            replies[cid] = _anthropic_reply(r["message"], receipt)
        else:  # errored / canceled / expired / missing: logged verbatim, never dropped silently
            replies[cid] = {"answer": "", "thinking": "", "usage": {}, "id": None, "request_id": receipt,
                            "stop_reason": f"{r.get('type')}: {json.dumps(r.get(r.get('type'), r))}"}
    return replies


def log_call(label: str, model: str, prompt: str | None, reply: dict, labels: tuple, parse=None):
    print("\n" + "=" * 78)
    print(f"[{label}]  model={model}  request_id={reply['request_id']}  id={reply.get('id')}")
    print(f"  usage={json.dumps(reply.get('usage', {}))}")
    if reply.get("stop_reason") not in (None, "end_turn"):  # refusal / max_tokens / batch error, loud
        print(f"  !!! stop_reason={reply['stop_reason']}")
    if prompt is not None:
        print("--- PROMPT (user turn) ---")
        print(prompt)
    if reply.get("thinking"):
        print("--- THINKING ---")
        print(reply["thinking"])
    else:  # reasoning billed but not captured must be loud, never a silent absence
        u = reply.get("usage", {})
        billed = ((u.get("output_tokens_details") or {}).get("thinking_tokens")
                  or (u.get("output_tokens_details") or {}).get("reasoning_tokens")
                  or (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        if billed and resolve(model)[0] == "anthropic":  # display=omitted: absent on purpose, said so in the log
            print(f"  thinking: {billed} tokens billed, text not requested (display=omitted)")
        elif billed:
            print(f"  !!! THINKING MISSING: {billed} reasoning tokens billed, no text captured")
        elif resolve(model)[0] == "anthropic" and not resolve(model)[1].startswith("claude-haiku"):
            print("  thinking: 0 tokens - thinking on (adaptive), the model chose not to think")  # visible, not silent
    print("--- RESPONSE (visible text) ---")
    print(reply["answer"] or "(no text)")
    # parse overrides the single-label read for a multi-field contract (run_stakes)
    verdict = parse(reply["answer"]) if parse else parse_verdict(reply["answer"], labels)
    print(f"--- PARSED VERDICT: {verdict} ---")
    return verdict
