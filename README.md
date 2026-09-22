# dsh-compress-caveman

Two fixes for **DeepSeek Harness (DSH)** deployments that run a *local reasoning model*
behind an OpenAI-compatible endpoint (llama.cpp, vLLM, …):

1. **Stop `summarization truncated at the token cap (incomplete checkpoint)`** —
   the context-compaction summarizer was hitting its `max_tokens` ceiling because
   *reasoning and the checkpoint summary share one output budget*.
2. **Make the model think and answer terse ("caveman")** — including the compaction
   pass — so both halves of that shared budget get smaller.

Verified against `@deepseek-ai/dsh-root` **0.1.5-rc.2**.

---

## 1. The failure

```
compaction/end {"turn":4,"error":"summarization truncated at the token cap (incomplete checkpoint)"}
```

### Root cause

`@deepseek-ai/dsh-compaction-basic` summarizes the shadowed region with one extra
model call and **fails closed** when that call ends on `finish_reason: length`:

```ts
// packages/compaction/compaction-basic/src/summarizer.ts:203-207
case 'max-tokens': {
  const error = new Error('summarization truncated at the token cap (incomplete checkpoint)')
  error.code = 'MAX_TOKENS'
  return error
}
```

Its output budget defaults to **8192** tokens:

```ts
// packages/compaction/compaction-basic/src/config.ts:91
maxTokens: config.maxTokens ?? 8192,
```

That ceiling is shared by `reasoning` and the summary text. A local reasoning model
spends 2.4k–4.5k tokens of it thinking before writing anything, and the checkpoint
summary itself is 5k–7k tokens, so the two occasionally do not fit.

### Measurements

Local deployment: Qwen3.8-27B (Q8_0) on llama.cpp, `--reasoning on --reasoning-budget 4096`,
one session, `default` compaction policy (`maxTokens: 8192`).

| outcome | count | `usage.outputTokens` | reasoning chars | summary chars |
|---|---|---|---|---|
| success | 2 | 7268, 7200 | 9790, 8811 | 11229, 13193 |
| failure | 20+ | 8192 (ceiling, `finish_reason: length`) | — | 0 |

Two successes landed ~900 tokens below the ceiling; every failure stopped exactly at it.

Server-side probes (same model/config) confirm the mechanism:

| probe | `max_tokens` | result |
|---|---|---|
| long numbered list | 1500 | `finish=length`, 1500 completion tokens, **5572 reasoning chars, 0 chars of answer** |
| long numbered list | 20000 | `finish=length` at 20000 — the endpoint honours the client ceiling, no hidden 8192 wall |
| complex design prompt | 8192 | `finish=length`, reasoning + answer together consumed all 8192 |
| complex design prompt + `chat_template_kwargs.thinking_budget_tokens=512` | 8192 | **no effect** — llama.cpp ignores the client-side budget |
| complex design prompt + `chat_template_kwargs.enable_thinking=false` | 8192 | `finish=stop`, **0 reasoning chars** |

Two conclusions that shape the fix:

* The ceiling is the client's `max_tokens`; raising it is sufficient and safe.
* There is no client-side *budget* control on this endpoint. Either the request
  does not think at all, or thinking is bounded only by the server's
  `--reasoning-budget` (which the model may overshoot).

---

## 2. Why a global persona does **not** reach your sessions

A patch targeting the deployment-wide persona looks like the obvious place to put a
style rule:

```yaml
# ~/.dsh/cordis.patch.yml  — does NOT work for preset-backed sessions
- id: system-prompt
  config:
    personaPrefix: |
      Respond and think terse like smart caveman. …
```

Every shipped agent preset registers **its own** `persona` row, and an agent-scoped
prompt section *shadows* the global `deployment:persona-prefix` for that session:

```
packages/preset/agent-presets/presets/
├── standard/agent.cordis.yml   - id: persona …
├── cordis/agent.cordis.yml     - id: persona …
├── ptc/agent.cordis.yml        - id: persona …
└── minimal/agent.cordis.yml    - id: persona …
```

Observed: sessions created after the patch had a system prompt of 18433 chars
(cordis preset) with **no** persona text, while the composed config tree *did* carry
it. The rule was in the right file and the wrong plane.

The same applies to compaction tuning: each preset mounts `compaction-basic` inside
its own `isolate` realm, so the host-plane `compaction-basic` row (which the web
bundle disables) is not the instance a session uses. Patching it there is a no-op.

**A preset is therefore the only layer that can change both things.**

---

## 3. The fix

### 3.1 User preset (both changes)

Copy the shipped preset you want and edit the copy — `~/.dsh/.agent-presets/<id>/`
is the writable preset root.

```bash
SRC=<dsh-checkout>/packages/preset/agent-presets/presets/standard
DST=~/.dsh/.agent-presets/qwen-caveman
mkdir -p "$DST" && cp -r "$SRC/." "$DST/"
find "$DST" -type d -exec chmod 700 {} +
find "$DST" -type f -exec chmod 600 {} +
```

Then apply the two patches with `git apply` (they apply cleanly and reproduce the
deployed file byte-for-byte):

```bash
cd "$DST"
git apply /path/to/dsh-compress-caveman/patches/01-persona-caveman.diff
git apply /path/to/dsh-compress-caveman/patches/02-compaction-maxtokens.diff
```

`patches/00-all-changes.diff` is both hunks in one file, for reading. On this host
GNU `patch -p1` reported success while changing nothing, so prefer `git apply`.

Or edit by hand:

```yaml
# ~/.dsh/.agent-presets/qwen-caveman/agent.cordis.yml

- id: persona
  name: '@deepseek-ai/dsh-persona'
  config:
    suffix: Your working directory is {{cwd}}.
    prefix: >-
      You are a coding agent powered by the {{model}} model.

      Respond and think terse like smart caveman. All technical substance stay; only fluff die.
      Drop articles, filler, pleasantries, and hedging. Fragments fine. Short synonyms. No
      tool-call narration, no decorative tables or emoji, no dumping long raw log output unless
      asked. Never invent abbreviations. No causal arrows. Technical terms exact, code blocks
      unchanged, error strings quoted exact. Never drop not/never/no/only/except. Numbers and
      units exact. Never add a word just to sound caveman: if caveman phrasing is not shorter
      than plain phrasing, use plain phrasing. Reply in the user's dominant language and
      compress the style, not the language. The rule applies to reasoning as well as final
      output. During context compaction, apply the same terse style to both your reasoning and
      the checkpoint text: keep every required section heading, but fill each with caveman
      bullets. Write files, code comments, commits, docs, and messages to other humans in
      normal prose.

# later in the same file, inside the `compaction` group:
    - id: compaction-basic
      name: '@deepseek-ai/dsh-compaction-basic'
      config:
        maxTokens: 16384
```

`maxTokens: 16384` leaves room for both halves: server-side thinking (≤ 4096 in
practice) plus a 5k–7k summary, with ~7k of slack. See `patches/` for the exact diff.

### 3.2 Make it the default preset

```yaml
# ~/.dsh/settings.yaml
agent-presets:
  default: qwen-caveman
```

Read at boot, so this one needs a restart. Preset *contents* are read when a session
is created, so later edits to the composition need no restart — but sessions already
created keep the composition they were composed from.

### 3.3 Optional: keep the deployment persona (minimal preset only)

`~/.dsh/cordis.patch.yml` is applied after every profile's patch layer and before
`--patch` overlays, so it is the right place for machine-wide settings — and it does
reach the `minimal` preset, which takes its persona from the deployment:

```yaml
- id: system-prompt
  config:
    includeHarnessIdentity: true
    includeRuntimeContext: true
    personaPrefix: |
      Respond and think terse like smart caveman. …
    personaSuffix: 'Your working directory is {{cwd}}.'
```

`includeHarnessIdentity` / `includeRuntimeContext` and the `{{cwd}}` suffix must be
restated: a patch replaces the targeted row's whole `config` rather than deep-merging.

---

## 4. Verify

**No restart needed for preset contents; a new session is required.**

```bash
# 1. the new session's system prompt carries the persona
zstd -dc ~/.dsh/sessions/<workspace-scope>/<session-id>/session.v3.jsonl.zstd \
  | grep -m1 -o '"type":"system/message"' >/dev/null && \
  zstd -dc ~/.dsh/sessions/<workspace-scope>/<session-id>/session.v3.jsonl.zstd \
  | grep -c caveman
```

Expected: non-zero. A `standard`-derived preset goes from ~6875 to ~7837 chars —
the persona contributes ~960. In the observed run the rule landed at char 110 of the
system prompt, with `{{model}}` and `{{cwd}}` correctly interpolated.

```bash
# 2. compaction used the raised ceiling
zstd -dc …/session.v3.jsonl.zstd | grep -o '"maxTokens":16384'
```

The `maxTokens` field of `compaction/summary` must read `16384`. **This is the only
evidence that the preset config reached the running compaction call** — the config
dump alone proves the file is right, not that a session used it.

```bash
# 3. no further truncation
zstd -dc …/session.v3.jsonl.zstd | grep -c 'summarization truncated'
```

Expected: `0` for sessions created after the change. A *pre-existing* session keeps
its old preset and will keep failing; only a new session picks up the fix.

Full post-upgrade / post-change check: `scripts/check_presets_after_upgrade.sh`.
Raw measurements behind every claim here: [`docs/findings.md`](docs/findings.md).

### Server-side reproduction

To re-measure the endpoint behaviour the fix depends on:

```bash
LLM_API_KEY=… python3 scripts/probe_llama_endpoint.py http://127.0.0.1:8098/v1 all
```

---

## 5. Upgrade notes

`~/.dsh` lives outside the install tree, so upgrading DSH (swapping the checkout)
does not touch these files, and the launcher heals the profile→install symlinks
itself (`healProfilesModuleFallback`, `packages/boot/app-boot/src/profile.ts:552`).

What can break after an upgrade, all *loudly*:

| change upstream | symptom | action |
|---|---|---|
| the `system-prompt` row id is renamed/removed | `patch: entry "system-prompt" not found` on stderr of `--dump-config` and at boot | retarget the patch |
| the `persona` or `compaction-basic` row id inside presets changes | the preset copy's edits orphan; persona/ceiling stop taking effect | realign the copy |
| the shipped `standard` preset gains rows | the copy is a snapshot and **will not inherit** them | re-copy, then re-apply the two diffs |

`scripts/check_presets_after_upgrade.sh` checks all of the above in one run.

---

## 6. Residual risk

`16384` is headroom, not a proof. The llama.cpp `--reasoning-budget` is advisory —
the model was observed to overshoot it — so a compaction whose reasoning exceeds
~10k tokens could still truncate. A conditional fix is to make the compaction call
not think at all: `enable_thinking=false` was verified effective, but
`summarizeWithLlm` sends no reasoning option, so `maxTokens` headroom is the only
config-only remedy. Asserting `enable_thinking=false` for `purpose: 'compaction'`
requires an engine subclass overriding `BasicCompactionEngine.summarize()`.

## License

MIT — see `LICENSE`.
