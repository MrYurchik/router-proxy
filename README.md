# Hermes Routing Proxy

OpenAI-compatible HTTP proxy that adds role-based model routing on top of Hermes
(OpenCode Go primary → OpenRouter fallback).

Hermes points its `base_url` at this proxy and sends `model` = a ROLE name
(`coder`, `research`, `tool_use`, etc.). The proxy reads `hermes-routing.yaml`,
picks the right real model per role, walks the fallback ladder on failures,
escalates to premium models after repeated revisions, and notifies you on
Telegram when the Go subscription goes down.

## Quick start

```bash
cd where/this/repo/is/router_proxy

# Install dependencies (use `uv` — the system Python has PEP 668)
uv pip install -r requirements.txt

# Set env vars (copy .env.example → .env, fill keys)
export GO_API_KEY="sk-..."
export OPENROUTER_API_KEY="sk-or-..."
# Optional: Telegram
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."

# Point to your routing YAML
export ROUTING_CONFIG=/path/to/hermes-routing.yaml

# Run
python -m router_proxy.app
# or
uvicorn router_proxy.app:app --host 0.0.0.0 --port 8787
```

## Point Hermes at it

```bash
hermes config set model.base_url http://localhost:8787/v1
```

Then use **role names** as model IDs:

```bash
hermes chat --model coder -q "Write a hello world script"
hermes chat --model research -q "Compare Kubernetes vs Nomad"
```

Or explicitly tell Hermes the model via `/model` in chat:

```
/model coder
```

## How routing works

```
Hermes: model="coder"
  │
  ▼
[router_proxy]
  │ reads hermes-routing.yaml
  │ checks go_healthy state
  │ checks X-Complex / X-Failure-Count headers
  ▼
Tier 0 → OpenCode Go        (flat fee)
  │ failure → Tier 1
Tier 1 → OpenRouter $2-cap  (cheap paid)
  │ failure ≥ 2 → Tier 1b
Tier 1b → OpenRouter $5-cap  (escalation: Kimi/GLM-5.2)
  │ failure → Tier 2
Tier 2 → Free models
```

- `tool_use` and `simple_tasks` skip Go and $5-cap entirely.
- `coder` uses premium Go models when `X-Complex: true`.

## Escalation headers (X-headers)

Hermes (or any caller) sends these to drive escalation:

| Header | Type | Meaning |
|--------|------|---------|
| `X-Subtask-Id` | string | Stable ID for one subtask across retries |
| `X-Failure-Count` | int | How many times this subtask already failed |
| `X-Complex` | `true`/`false` | Complex flag from orchestrator |

The proxy also stores counters via `POST /v1/feedback`:

```bash
curl -X POST http://localhost:8787/v1/feedback \
  -H "Content-Type: application/json" \
  -d '{"subtask_id":"t1","status":"needs_revision"}'
```

## Admin endpoints

```bash
# Liveness
curl http://localhost:8787/healthz

# Full state dump
curl http://localhost:8787/admin/state

# Reload routing config without restart
curl -X POST http://localhost:8787/admin/reload
```

## curl examples

```bash
# Basic coder request (Go primary → OpenRouter fallback)
curl -X POST http://localhost:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "coder",
    "messages": [{"role":"user","content":"Write a Python hello world in one line"}]
  }'

# With escalation headers (forces $5-cap tier)
curl -X POST http://localhost:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Subtask-Id: t1" \
  -H "X-Failure-Count: 2" \
  -H "X-Complex: true" \
  -d '{
    "model": "coder",
    "messages": [{"role":"user","content":"Refactor this entire codebase"}]
  }'

# tool_use (never hits Go)
curl -X POST http://localhost:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "tool_use",
    "messages": [{"role":"user","content":"Call an external API"}]
  }'
```
