# Context & Historical Decisions

This document captures the rationale behind key architectural decisions and
historical context that shaped the router proxy. It is useful for onboarding
and for understanding why things are the way they are.

---

## Why the proxy exists

Hermes Agent originally sent all requests directly to a single model provider.
This had several problems:

1. **No role separation** — the same model handled coding, research, and tool
   calls, which is inefficient (tool calls don't need a reasoning model).
2. **No fallback** — if the primary provider went down (e.g. Go subscription
   expired), Hermes would fail entirely.
3. **No escalation** — when a complex task kept failing, there was no mechanism
   to try a stronger model automatically.
4. **No visibility** — there was no record of which provider handled which
   request, making debugging difficult.

The proxy solves all four: it routes **by role**, falls back **by tier**,
escalates **on repeated failures**, and logs everything to SQLite.

---

## DeepSeek Reasoning Overflow

### The problem

`deepseek-v4-flash` and `deepseek-v4-pro` (routed via OpenCode Go) generate
extensive **internal reasoning** chains before producing visible output. These
reasoning tokens count toward `max_tokens`. When `max_tokens` was set to a low
value (e.g. 1024 or 2048), the model would:

1. Fill the entire output budget with reasoning
2. Hit `finish_reason: "length"`
3. Produce **zero visible content**
4. Hermes would see an empty response and retry — more failures, same result

### The fix

The proxy automatically **bumps `max_tokens` to at least 4096** for any
DeepSeek model routed via the Go backend:

```python
if "deepseek" in model and provider == "go":
    if not max_tokens or max_tokens < 4096:
        max_tokens = 4096
```

This gives DeepSeek enough room to reason **and** produce output. The bump is
applied only on the Go path — OpenRouter handles DeepSeek models differently
(or the user sets their own `max_tokens` for those endpoints).

---

## Model Provider History

### Phase 1: DeepSeek-only (initial)

The original Hermes Agent setup used DeepSeek models exclusively, routed
directly without a proxy. This worked for simple tasks but had:
- No fallback
- No role separation
- Occasional reasoning overflow issues

### Phase 2: DeepSeek → GLM (mid-stream issues)

DeepSeek's streaming responses sometimes exhibited problems:
- Choppy or incomplete SSE chunks
- Delayed first-token latency
- Occasional mid-stream disconnections

The team experimented with **GLM models** (via OpenRouter) as an alternative.
GLM had:
- More stable streaming
- Lower cost
- But less capable reasoning for complex coding tasks

### Phase 3: GLM → DeepSeek (with fixes)

The reasoning overflow fix (`max_tokens ≥ 4096`) was identified and implemented.
Combined with the proxy's tiered routing:

- **DeepSeek** is used as the Go primary (Tier 0) for `coder`, `orchestrator`,
  and `research` roles
- **GLM** (and other $2-cap models) serve as Tier 1 fallback
- **Kimi K2 / GLM-4 5.2** serve as Tier 1b escalation models

This gives the best of both worlds: DeepSeek's reasoning power when it works,
with graceful fallback to alternatives when it doesn't.

---

## Orchestration Flow

```
Orchestrator (orchestrator role)
  │
  ├── decompose_task() → list of subtasks
  │     │
  │     ├── assign subtask to coder role
  │     │     └── delegate_task(coder, ...)
  │     │           │
  │     │           ├── POST /v1/chat/completions (model="coder")
  │     │           ├── review output (reviewer role)
  │     │           │     └── POST /v1/feedback (status=approved|needs_revision)
  │     │           │
  │     │           └── if needs_revision:
  │     │                 ├── X-Failure-Count incremented
  │     │                 ├── proxy escalates to Tier 1b
  │     │                 └── retry
  │     │
  │     ├── assign subtask to research role
  │     │     └── delegate_task(research, ...)
  │     │
  │     └── assign subtask to tool_use role
  │           └── delegate_task(tool_use, ...)
  │
  └── collect_results() → final response
```

### Key design decisions

- **Single endpoint** (`/v1/chat/completions`) — Hermes sends all requests to
  the same proxy URL, differentiated only by the `model` field.
- **Roles are models** — Hermes treats role names as model IDs. The proxy maps
  them to real upstream model slugs.
- **Stateless from Hermes' perspective** — Hermes doesn't know about tiers or
  fallback. It just sends headers and gets a response. The proxy handles all
  complexity.
- **Review loop** — After a subtask completes, Hermes reviews the output using
  the `reviewer` role. If it needs revision, it sends feedback and retries with
  a higher failure count, which may trigger escalation.

---

## Proxy version history

| Version | Date | Change |
|---------|------|--------|
| 1.0.0 | — | Initial implementation with Go → OpenRouter routing |
| 2.0.0 | — | Added SQLite state persistence, feedback endpoint, escalation tiers, DeepSeek max_tokens fix, Telegram notifications |

---

## Related files

| File | Purpose |
|------|---------|
| [README.md](./README.md) | Quick start and basic usage |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Full architecture documentation |
| [DEPLOY.md](./DEPLOY.md) | Production deployment with systemd |
| [SETUP.md](./SETUP.md) | Telegram bot setup guide |
| [config.py](./config.py) | YAML config loader and role tier builder |
| [router.py](./router.py) | Core routing logic and upstream calls |
| [state.py](./state.py) | SQLite persistent state |
| [notify.py](./notify.py) | Telegram notification sender |
| [app.py](./app.py) | FastAPI application and endpoints |
