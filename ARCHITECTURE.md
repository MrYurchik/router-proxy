# Hermes Routing Proxy — Architecture

## Overview

The **Hermes Routing Proxy** is an OpenAI-compatible HTTP proxy that intercepts
chat completion requests from Hermes Agent and routes them to different upstream
LLM providers based on the **role** specified in the `model` field. It implements
a tiered fallback ladder, failure escalation, per-role model selection, and
persistent state tracking.

```
┌──────────┐     model="coder"     ┌───────────────────┐
│  Hermes   │ ──────────────────▶  │  Router Proxy     │
│  Agent    │   X-Subtask-Id: t1   │  (port 8787)      │
│           │   X-Failure-Count: 2 │                   │
│           │   X-Complex: true    │  ┌─────────────┐  │
└──────────┘                      │  │ YAML config  │  │
                                   │  │ route logic  │  │
                                   │  │ state+health │  │
                                   │  └─────────────┘  │
                                   └────────┬──────────┘
                                            │
                      ┌─────────────────────┼─────────────────────┐
                      ▼                     ▼                     ▼
               ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐
               │  OpenCode Go  │    │  OpenRouter  │    │  OpenRouter Free  │
               │  (Tier 0)     │    │  $2-cap      │    │  (Tier 2)         │
               │               │    │  (Tier 1)    │    │                   │
               │  flat fee     │    │  cheap paid  │    │  rate-limited     │
               └──────┬───────┘    └──────┬───────┘    └──────────────────┘
                      │                   │
                      ▼                   ▼
               ┌──────────────────────────────────┐
               │  OpenRouter $5-cap (Tier 1b)     │
               │  escalation on repeated failures  │
               └──────────────────────────────────┘
```

---

## 1. Tiered Routing Ladder

When a request arrives, the proxy builds a **fallback ladder** from the
[YAML routing configuration](./SETUP.md) and walks through it until one tier
succeeds:

| Tier | Provider | Cost Profile | Description |
|------|----------|-------------|-------------|
| **0** | OpenCode Go | Flat subscription (unlimited) | Primary tier. Fast reasoning models included in subscription. Used only by `go_backed_roles`. |
| **1** | OpenRouter $2-cap | Cheap paid models (e.g. GPT-4o Mini, Claude 3 Haiku) | First fallback when Go is down or unavailable. |
| **1b** | OpenRouter $5-cap | Premium paid models (e.g. Kimi K2, GLM-4 5.2) | **Escalation tier** — activated only when `X-Failure-Count ≥ escalate_to_premium_after_failures` (default: 2). |
| **2** | OpenRouter Free | Rate-limited free models | Last resort fallback. Slower, subject to tier limits. |

### Escalation mechanism

- When a subtask keeps failing (Hermes sends `X-Failure-Count` header or records
  `needs_revision` via `POST /v1/feedback`), the proxy automatically inserts
  Tier 1b into the ladder.
- The specific model is selected from `escalation_5cap` list, indexed by how
  many failures beyond the threshold occurred.
- `tool_use` and `simple_tasks` roles typically skip Go entirely and also have
  no escalation tier — they use $2-cap → Free.

---

## 2. Roles

Each role maps to a different set of model slots in the YAML config. Hermes
sends the role name as the `model` field:

| Role | Go-backed | Escalation | Typical Use |
|------|-----------|------------|-------------|
| `orchestrator` | ✅ Yes | ✅ Yes | Decomposes tasks into subtasks, manages workflow |
| `coder` | ✅ Yes | ✅ Yes | Code generation, refactoring, debugging |
| `research` | ✅ Yes | ✅ Yes | Web research, summarization, analysis |
| `reviewer` | ✅ Yes | ✅ Yes | Code review, validation, quality checks |
| `tool_use` | ❌ No | ❌ No | Function/tool calling (lightweight models) |
| `simple_tasks` | ❌ No | ❌ No | Trivial queries (fast, cheap) |
| `passthrough` | N/A | N/A | Direct model slug in `model` field bypasses routing entirely |

When the proxy receives an unknown role name, it falls back to `default_role`
if configured in the YAML.

---

## 3. X-Headers (Routing Metadata)

Hermes Agent sends the following custom headers to drive routing decisions:

| Header | Type | Source | Effect |
|--------|------|--------|--------|
| `X-Subtask-Id` | string | Hermes orchestrator | Stable subtask identifier across retries. Used to look up persistent failure counters in SQLite. |
| `X-Failure-Count` | int | Hermes orchestrator | Explicit failure count. If provided, used directly; otherwise the proxy queries its own counters by subtask ID. |
| `X-Complex` | `true`/`false` | Hermes orchestrator | When `true`, selects premium Go models (if available) and heavier fallback models. |

### Fallback: `routing_meta` in request body

If the caller cannot set HTTP headers (some client libraries), the same fields
can be embedded in the JSON request body:

```json
{
  "model": "coder",
  "messages": [...],
  "routing_meta": {
    "subtask_id": "t1",
    "failure_count": 2,
    "complex": true
  }
}
```

The proxy merges headers and body fields, preferring explicit headers when both
are present.

---

## 4. Feedback Loop (`POST /v1/feedback`)

Hermes Agent sends feedback after a subtask completes:

```json
{"subtask_id": "t1", "status": "needs_revision"}
```

- **`needs_revision`**: Increments the failure counter for that subtask in SQLite.
  Subsequent requests with the same `X-Subtask-Id` will see a higher failure count
  and may escalate to Tier 1b.
- **`approved`**: Resets the failure counter to zero.

This enables **progressive escalation**: a task that keeps failing is
automatically routed to stronger (more expensive) models.

---

## 5. SSE Streaming + Routing Events

When the upstream request uses `stream: true`, the proxy passes the SSE stream
through to the client transparently. The response headers contain routing
metadata:

```
X-Routed-Role: coder
X-Routed-Model: opencode-go/deepseek-v4-flash
X-Routed-Backend: go
X-Routed-Tier: 0
```

For non-streaming responses, the proxy **embeds a `routing` JSON block** in the
response body:

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "choices": [...],
  "routing": {
    "role": "coder",
    "model": "opencode-go/deepseek-v4-flash",
    "backend": "go",
    "tier": "0"
  }
}
```

This allows Hermes Agent to know exactly which model and provider handled the
request for logging and debugging.

---

## 6. Health Check Mechanism

The proxy runs a background health checker that **periodically probes the Go
backend** when it's known to be unhealthy.

### Flow

```
┌──────────┐     daily (86400s)     ┌────────────────┐
│  Checker  │ ─────────────────────▶│  Is Go healthy? │
│  loop     │                       └────────┬───────┘
└──────────┘                                │
                                   No ───────┤ Yes ──▶ skip
                                     │
                                     ▼
                            ┌──────────────────┐
                            │ Ping Go via API   │
                            │ model="ping"      │
                            │ max_tokens=1      │
                            └────────┬─────────┘
                                     │
                           success ──┤ failure
                            │         │
                            ▼         ▼
                    ┌────────────┐  (skip, log)
                    │ Mark Go UP │
                    │ Notify     │
                    │ recovered  │
                    └────────────┘
```

### Go-down detection (inline)

When a **live request** fails on Go with HTTP 401/402/403/429, the proxy
immediately marks Go as unhealthy and switches to fallback tiers:

1. Sets `go_healthy = false` in SQLite (persists across restarts)
2. Sends a Telegram notification: `⚠️ OpenCode Go down (HTTP ...) on role '...'`
3. Continues to the next tier (Tier 1+)
4. Cooldown: repeated Go-down notifications are suppressed for 1 hour

### Go-recovery detection (background)

1. The background health check runs every 86400 seconds (daily)
2. It sends a minimal ping request to Go
3. On success, it sets `go_healthy = true` and sends notification:
   `✅ OpenCode Go back up. Paid roles switched back to Go.`

### State persistence

`go_healthy` is stored in SQLite, so the proxy remembers Go's health across
restarts without needing an initial probe on every startup.

---

## 7. SQLite State (persistent)

The proxy uses a local SQLite database (`proxy_state.db` by default) for three
purposes:

### Table: `state` — key-value store

| Key | Value | Purpose |
|-----|-------|---------|
| `go_healthy` | `"true"` / `"false"` | Persist Go health across restarts |

### Table: `counters` — failure tracking

| Column | Type | Description |
|--------|------|-------------|
| `subtask_id` | TEXT PRIMARY KEY | Stable subtask identifier |
| `failures` | INTEGER | Running failure count |

Updated via `POST /v1/feedback` or directly by the proxy on Go failures.

### Table: `routing_log` — request audit trail

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER AUTO | Auto-increment ID |
| `ts` | REAL | Unix timestamp |
| `subtask_id` | TEXT | Subtask identifier (nullable) |
| `role` | TEXT | Role that was routed |
| `model` | TEXT | Actual upstream model slug |
| `backend` | TEXT | `"go"` or `"openrouter"` |
| `tier` | TEXT | Tier label (`0`, `1`, `1b`, `2`) |
| `status` | INTEGER | Upstream HTTP status code |
| `complex` | INTEGER | Boolean: was complex flag set? |
| `failures` | INTEGER | Failure count at routing time |

Query via `GET /admin/routing?subtask_id=...&limit=...`.

---

## 8. Admin Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/healthz` | Liveness check + `go_healthy` status |
| `GET` | `/admin/state` | Full state dump (state keys, counters, recent routing log) |
| `POST` | `/admin/reload` | Reload YAML config without restart |
| `GET` | `/admin/routing` | Query routing log with optional `subtask_id` filter |

---

## 9. DeepSeek Reasoning Overflow Fix

When routing DeepSeek models (`deepseek-v4-flash`, `deepseek-v4-pro`) via the Go
backend, these models generate extensive internal reasoning tokens that consume
the `max_tokens` budget. If `max_tokens` is too low, the model fills its entire
output with reasoning and produces `finish_reason: "length"` with **empty visible
content**.

The proxy automatically detects DeepSeek models routed via Go and **bumps
`max_tokens` to at least 4096**:

```python
if "deepseek" in model and provider == "go":
    cur = request_body.get("max_tokens", 0)
    if not cur or cur < 4096:
        request_body["max_tokens"] = 4096
```

This ensures reasoning tokens don't crowd out the actual response.

---

## 10. Data Flow Diagram (Full Request Lifecycle)

```
Hermes Agent
  │
  │ POST /v1/chat/completions
  │ model="coder", stream=true
  │ X-Subtask-Id: "t1", X-Failure-Count: 1, X-Complex: true
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│                     app.py :: chat_completions               │
│                                                             │
│  1. Extract role from model field                           │
│  2. Parse routing metadata from headers / body              │
│  3. Call router.route_and_call(role, body, ...)              │
│                                                             │
│  ┌────────────────────────────────────────────────────┐     │
│  │           router.py :: route_and_call               │     │
│  │                                                    │     │
│  │  4. cfg.get_role_tiers(role, complex, failures)     │     │
│  │     → [go_deepseek, 2cap_gpt4o_mini, 5cap_kimi, …] │     │
│  │                                                    │     │
│  │  5. For each tier:                                  │     │
│  │     - Skip Go if go_healthy=False                   │     │
│  │     - _try_tier(tier_info) → upstream POST          │     │
│  │     - On Go 401/402/403/429: mark unhealthy,notify  │     │
│  │     - On success: return response + provider info   │     │
│  │                                                    │     │
│  │  6. All tiers exhausted → 502 error                 │     │
│  └────────────────────────────────────────────────────┘     │
│                                                             │
│  7. Embed routing info in response (headers + body.routing) │
│  8. state.log_routing(...) → SQLite                        │
│  9. Return StreamingResponse or JSON Response               │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
Response to Hermes Agent
  Headers: X-Routed-Role, X-Routed-Model, X-Routed-Backend, X-Routed-Tier
  Body:    { ..., "routing": { "role":"coder", "model":"...", ... } }
```
