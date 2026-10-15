#!/usr/bin/env python3
"""Re-download a finished Anthropic message batch's results (free: no new generation) and print, per request,
the content block types with their text length, plus every block verbatim for the requests named.

  python3 scripts/batch_blocks.py <msgbatch_id> [custom_id ...]

Answers "reasoning billed but not returned": shows which block types the reply actually carried (results are kept
29 days after the batch was created)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from providers import _anthropic_headers, _get  # noqa: E402


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    bid, show = sys.argv[1], set(sys.argv[2:])
    url = f"https://api.anthropic.com/v1/messages/batches/{bid}/results"
    print(f"### GET {url}")
    for line in _get(url, _anthropic_headers()).splitlines():
        r = json.loads(line)
        res = r["result"]
        if res["type"] != "succeeded":
            print(f"[{r['custom_id']}] result={res['type']}")
            continue
        msg = res["message"]
        blocks = [f"{b['type']}({len(b.get('thinking') or b.get('text') or b.get('data') or '')})"
                  for b in msg["content"]]
        print(f"[{r['custom_id']}] id={msg['id']} stop={msg.get('stop_reason')} "
              f"usage={json.dumps(msg.get('usage', {}).get('output_tokens_details'))} blocks={' '.join(blocks)}")
        if r["custom_id"] in show:
            print(json.dumps(msg["content"], indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
