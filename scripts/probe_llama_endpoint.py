#!/usr/bin/env python3
"""Probe an OpenAI-compatible local endpoint for the behaviour this repo relies on.

Answers three questions about a llama.cpp / vLLM style server:

  1. Does it honour a client `max_tokens` value (small and large)?
  2. Does it honour a per-request reasoning budget (`chat_template_kwargs`)?
  3. Does `enable_thinking=false` actually suppress the reasoning phase?

Usage:
  probe_llama_endpoint.py URL [CASE ...]

  URL   base URL of the endpoint, including /v1
        e.g. http://127.0.0.1:8098/v1
  CASE  any of: cap big think thinkbudget nothink all   (default: all)

Environment:
  LLM_API_KEY   bearer token               (default: none)
  LLM_MODEL     model id                   (default: qwen3.8-27b)

Notes:
  * A local reasoning model is slow. `big` alone can run for minutes; the point
    is whether the endpoint stops at the requested ceiling, not how fast it is.
  * `cap` uses a cheap counting task so the answer is trivially checkable.
  * `think` / `thinkbudget` / `nothink` use a complex prompt, because a trivial
    one does not provoke a long reasoning phase.

Interpretation, from a real run against llama.cpp with
`--reasoning on --reasoning-budget 4096`:

  cap         finish=length, completion_tokens=1500, reasoning only, 0 chars answer
  big         generation continues past 8192 (endpoint has no hidden 8192 wall)
  thinkbudget reasoning length UNCHANGED -> the client budget is ignored
  nothink     finish=stop, 0 reasoning chars -> the flag works
"""

import json
import os
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8098/v1"
CASES = sys.argv[2:] or ["all"]
URL = BASE.rstrip("/") + "/chat/completions"
KEY = os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "qwen3.8-27b")

COUNT = [
    {"role": "system", "content": "Answer with a numbered list only. No preamble, no commentary."},
    {"role": "user", "content": "Count from 1 to 3000 as a numbered list, one number per line."},
]

COMPLEX = [
    {"role": "system", "content": "You are a careful analyst. Think step by step."},
    {"role": "user", "content": (
        "Design a fault-tolerant pipeline that ingests 4 TB/day of gzipped FASTQ, validates every "
        "record, runs three alignment stages, and writes partitioned Parquet. Enumerate failure "
        "modes, give concrete retry and backoff policies, sketch the state machine, and justify "
        "every storage-layout decision. Be thorough; consider consistency, idempotency, cost, and "
        "observability.")},
]


def call(label, messages, max_tokens, extra=None):
    payload = {"model": MODEL, "messages": messages, "max_tokens": max_tokens,
               "stream": False, "temperature": 0.5, "top_p": 0.95, "top_k": 20}
    if extra:
        payload.update(extra)
    headers = {"Content-Type": "application/json"}
    if KEY:
        headers["Authorization"] = "Bearer " + KEY
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=headers)
    started = time.time()
    with urllib.request.urlopen(req, timeout=3600) as response:
        body = json.load(response)
    elapsed = time.time() - started
    choice = body["choices"][0]
    message = choice.get("message", {})
    content = message.get("content") or ""
    reasoning_field = message.get("reasoning_content") or ""
    think_chars = 0
    if "<think>" in content:
        end = content.find("</think>")
        think_chars = (end + 8) if end >= 0 else len(content)
    usage = body.get("usage", {})
    print(f"[{label}]")
    print(f"  finish_reason     : {choice.get('finish_reason')}")
    print(f"  completion_tokens : {usage.get('completion_tokens')}   prompt_tokens: {usage.get('prompt_tokens')}")
    print(f"  reasoning in text : {think_chars} chars")
    print(f"  reasoning_content : {len(reasoning_field)} chars")
    print(f"  answer            : {len(content) - think_chars} chars")
    print(f"  wall              : {elapsed:.1f}s")
    print(f"  answer_head       : {content[think_chars:think_chars + 100]!r}")
    sys.stdout.flush()


def wants(case):
    return "all" in CASES or case in CASES


if wants("cap"):
    call("max_tokens=1500 (expect stop at the ceiling)", COUNT, 1500)
if wants("big"):
    call("max_tokens=20000 (expect generation past 8192)", COUNT, 20000)
if wants("think"):
    call("default thinking", COMPLEX, 8192)
if wants("thinkbudget"):
    call("thinking_budget_tokens=512", COMPLEX, 8192,
         {"chat_template_kwargs": {"thinking_budget_tokens": 512}})
if wants("nothink"):
    call("enable_thinking=false", COMPLEX, 8192,
         {"chat_template_kwargs": {"enable_thinking": False}})
