#!/usr/bin/env python3
"""Send the project sources to DeepSeek (OpenAI-compatible API) for a code review.

The key is read from ``DEEPSEEK_API_KEY`` or ``--key-file`` and is never printed.

    python3 tools/review_with_deepseek.py --paths terraform_mirror tests
    python3 tools/review_with_deepseek.py --files terraform_mirror/cache.py --model deepseek-v4-pro

Output: one block per file with the model's findings, plus a summary line.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-v4-pro"

SYSTEM = (
    "You are a strict senior reviewer of Python code that implements a caching reverse "
    "proxy for the Terraform module registry protocol (standard library only, S3 storage, "
    "distributed locks). Report only defects that matter: correctness bugs, protocol "
    "violations, race conditions, resource leaks, security problems (path traversal, "
    "injection, credential handling), and API misuse. For each finding give the file, an "
    "approximate line, severity (blocker/major/minor), why it is wrong and a concrete fix. "
    "Do not restyle working code, do not suggest new dependencies, do not repeat the code "
    "back. If a file is fine, say NO FINDINGS.\n\n"
    "OUTPUT FORMAT - this is the whole answer, nothing else:\n"
    "1. [severity] line N - one line saying what is wrong and how to fix it\n"
    "2. ...\n"
    "Keep every item on one or two lines. Do not narrate your reading of the file, do not "
    "list what is correct, do not restate the code. Start the answer with the first item."
)


def read_key(path: str) -> str:
    value = os.environ.get("DEEPSEEK_API_KEY")
    if value:
        return value.strip()
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def collect(paths: list[str], files: list[str]) -> list[str]:
    out: list[str] = []
    for name in files:
        out.append(name)
    for root in paths:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".git"}]
            for filename in sorted(filenames):
                if filename.endswith((".py", ".tf", "Dockerfile")) or filename.endswith((".md", ".yml")):
                    out.append(os.path.join(dirpath, filename))
    seen, unique = set(), []
    for name in out:
        if name not in seen and os.path.isfile(name):
            seen.add(name)
            unique.append(name)
    return unique


def ask(url: str, model: str, key: str, path: str, source: str, timeout: int, max_tokens: int, think: bool = False) -> str:
    prompt = f"File: {path}\n\n```python\n{source}\n```\n\nReview it."
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "temperature": 0.1,
            # deepseek-v4-pro is a reasoning model and its thinking block is billed from
            # the same max_tokens budget: with thinking on, long files spend the whole
            # budget on reasoning and answer with an empty content. The findings list is
            # what we want, so reasoning is off unless --think is passed.
            "max_tokens": max_tokens,
            "thinking": {"type": "enabled" if think else "disabled"},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}: {exc.read()[:300]!r}"
    except urllib.error.URLError as exc:
        return f"transport error: {exc.reason}"
    choices = payload.get("choices") or [{}]
    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if content:
        return content
    # The reasoning block ate the whole budget: keep a trace, but say so loudly.
    reasoning = (message.get("reasoning_content") or "").strip()
    finish = choices[0].get("finish_reason") or "?"
    if reasoning:
        return f"(NO FINDINGS EMITTED - only reasoning, finish_reason={finish})\n{reasoning[:1200]}"
    return f"(empty answer, finish_reason={finish})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", nargs="*", default=None, help="directories to review")
    parser.add_argument("--files", nargs="*", default=[], help="individual files")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--think", action="store_true", help="keep the model's reasoning on")
    parser.add_argument("--parallel", type=int, default=1, help="ask this many files at a time")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--key-file", default=os.path.expanduser("~/.nanobot/secrets/deepseek.key"))
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--limit", type=int, default=0, help="max files per run")
    parser.add_argument("--out", default="", help="write the report to this file too")
    args = parser.parse_args()

    key = read_key(args.key_file)
    paths = args.paths if args.paths is not None else ([] if args.files else ["terraform_mirror"])
    if not paths and not args.files:
        parser.error("nothing to review: pass --paths and/or --files")
    targets = collect(paths, args.files)
    if args.limit:
        targets = targets[: args.limit]
    wanted = [path for path in targets if open(path, encoding="utf-8").read().strip()]

    def review(path: str) -> str:
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        time.sleep(args.sleep)
        return ask(args.url, args.model, key, path, source, args.timeout, args.max_tokens, args.think)

    if args.parallel > 1:
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            reports = list(pool.map(review, wanted))
    else:
        reports = [review(path) for path in wanted]

    lines: list[str] = []
    findings = 0
    for path, report in zip(wanted, reports):
        interesting = "NO FINDINGS" not in report.upper()
        findings += 1 if interesting else 0
        lines.append(f"\n{'=' * 78}\n{path}{'  <-- findings' if interesting else ''}\n{'=' * 78}\n{report}\n")
        print(lines[-1], flush=True)
    lines.append(f"\n--- reviewed {len(wanted)} file(s), {findings} with findings (model {args.model}) ---")
    print(lines[-1])
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write("".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
