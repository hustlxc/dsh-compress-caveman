# Findings behind this repository

Raw evidence for the two fixes. All measurements from one deployment:
**DeepSeek Harness 0.1.5-rc.2**, local **Qwen3.8-27B (Q8_0)** on **llama.cpp**
with `--reasoning on --reasoning-budget 4096`, one session using the shipped
compaction policy (`maxTokens: 8192`).

## 1. Compaction outcomes, before the fix

| time | event | `outputTokens` | reasoning chars | summary chars |
|---|---|---|---|---|
| 12:58:38 | summary OK | 7268 | 9790 | 11229 |
| 13:36:08 … 14:33:12 | 14 failures | 8192 (ceiling) | — | 0 |
| 14:37:24 | summary OK | 7200 | 8811 | 13193 |
| 15:30:03 … 16:35:03 | 16 failures | 8192 (ceiling) | — | 0 |

* Every failure stopped exactly at the ceiling (`finish_reason: length`), i.e. the
  summarizer's `max_tokens`, not a server limit.
* The two successes sat ~900 tokens below the ceiling. The margin was never large;
  variance in the reasoning phase is what decided success.
* `compaction/end` recorded the failure verbatim:
  `"summarization truncated at the token cap (incomplete checkpoint)"`.

Code path: `packages/compaction/compaction-basic/src/summarizer.ts:203-207`
(`finish.kind === 'max-tokens'` → throw, `code = 'MAX_TOKENS'`), budget from
`packages/compaction/compaction-basic/src/config.ts:91` (`maxTokens: … ?? 8192`).

## 2. Endpoint behaviour

Same model, same flags, small synthetic prompts.

| case | `max_tokens` | finish | completion tokens | reasoning chars | answer chars |
|---|---|---|---|---|---|
| `cap` | 1500 | `length` | 1500 | 5572 | **0** |
| `big` | 20000 | `length` | 20000 | 5572 | 18265 |
| `think` | 8192 | `length` | 8192 | 16917 | 15024 |
| `thinkbudget` (`thinking_budget_tokens=512`) | 8192 | `length` | 8192 | 16705 | 14110 |
| `nothink` (`enable_thinking=false`) | 8192 | `stop` | 6938 | **0** | 26391 |

Conclusions:

1. **The client `max_tokens` is the effective ceiling.** 20000 was honoured; there is
   no hidden 8192 wall to fight.
2. **A client reasoning budget does nothing here.** `thinking_budget_tokens=512`
   changed neither the reasoning length nor the finish reason. On this server the
   reasoning phase is bounded only by `--reasoning-budget`, and the model can
   overshoot even that (16917 reasoning chars ≈ 4.2k tokens against a 4096 budget).
3. **`enable_thinking=false` works** and removes the reasoning phase entirely. It is
   the only deterministic cure; it is not reachable from configuration because
   `summarizeWithLlm` passes no reasoning option, so raising `maxTokens` is the
   config-only remedy.

Reproduce with `scripts/probe_llama_endpoint.py <base-url> all`.

## 3. Where the persona has to live

A patch on the deployment-wide persona row is composed correctly and still does not
reach a session, because every shipped agent preset registers its own `persona` row
and an agent-scoped prompt section shadows the global one.

Observed system prompts:

| session created | preset | system prompt chars | contains persona |
|---|---|---|---|
| 10:21 | standard | 6875 | no |
| 14:36 | standard | 6868 | no |
| 15:05 | cordis | 18433 | no |
| 15:07 | cordis | 18433 | no |
| 16:40 (**after** moving the rule into a user preset) | qwen-caveman | **7837** | **yes** (rule at char 110) |

The `qwen-caveman` figure is the shipped `standard` prompt plus ~960 characters of
persona, with `{{model}}` and `{{cwd}}` interpolated correctly.

The same reasoning explains why patching `compaction-basic` on the host plane is a
no-op: each preset mounts its own instance inside an `isolate` realm, and the
host-plane row is disabled by the web bundle.

## 4. Verification checklist actually used

```bash
# persona reached the new session
zstd -dc ~/.dsh/sessions/<scope>/<session>/session.v3.jsonl.zstd | grep -c caveman   # > 0

# the raised ceiling reached the compaction call (the decisive check:
# a config dump only proves the file is right, not that a session used it)
zstd -dc …/session.v3.jsonl.zstd | grep -o '"maxTokens":16384'

# no further truncation in sessions created after the change
zstd -dc …/session.v3.jsonl.zstd | grep -c 'summarization truncated'                  # 0
```

Status at the time of writing: the persona check passes; the `maxTokens: 16384`
field has **not yet been observed** in a `compaction/summary` event, because no
compaction has run in a session created after the change. Treat the ceiling fix as
measured-motivated but not yet field-confirmed.

## 5. Incident note

While testing patch application, a `patch -p1` invocation was pointed at the shipped
preset inside the DSH checkout and modified it. The 13 added lines were removed
immediately and the file was verified back to its original 255 lines / 12928 bytes
with no `caveman` content.

Lesson: never run patch experiments against the install tree. Always copy the file
to a temporary directory first — which is what `scripts/check_presets_after_upgrade.sh`
and the patch verification in this repo do.
