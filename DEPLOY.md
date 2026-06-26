# Hermes Routing Proxy — Deployment Guide

## Prerequisites

- Python 3.11+
- Git access to the repository
- API keys (see below)
- Systemd (Linux) — for production deployment

---

## 1. API Keys

The proxy requires these environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `GO_API_KEY` | Yes | OpenCode Go API key (`sk-...`) |
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key (`sk-or-...`) |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token for notifications |
| `TELEGRAM_CHAT_ID` | No | Telegram chat/user ID for notifications |

### Getting keys

- **OpenCode Go**: Go to [opencode.ai](https://opencode.ai) → create account →
  subscribe → API keys section.
- **OpenRouter**: Go to [openrouter.ai/keys](https://openrouter.ai/keys).

See [SETUP.md](./SETUP.md) for Telegram bot setup instructions.

---

## 2. Quick Start (Development)

```bash
# Clone
git clone git@github.com:MrYurchik/router-proxy.git
cd router_proxy

# Python virtual environment
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt

# Export API keys
export GO_API_KEY="sk-..."
export OPENROUTER_API_KEY="sk-or-..."
export ROUTING_CONFIG="/path/to/hermes-routing.yaml"

# Run
uvicorn router_proxy.app:app --host 0.0.0.0 --port 8787
```

Or use the helper script:

```bash
./run_proxy.sh
```

---

## 3. Production Deployment (systemd)

### 3.1 Create the systemd service file

Create `~/.config/systemd/user/hermes-routing-proxy.service`:

```ini
[Unit]
Description=Hermes Routing Proxy — Role-based model router
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/router_proxy/.venv/bin/uvicorn router_proxy.app:app \
    --host 0.0.0.0 --port 8787

WorkingDirectory=%h/router_proxy
Environment=PYTHONPATH=%h

# ── API keys (edit these or source from .env) ──
Environment=GO_API_KEY=sk-your-go-api-key
Environment=OPENROUTER_API_KEY=sk-or-your-openrouter-key

# ── Optional: Telegram notifications ──
# Environment=TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234gh
# Environment=TELEGRAM_CHAT_ID=-1001234567890

# ── Config paths (adjust as needed) ──
Environment=ROUTING_CONFIG=%h/hermes-routing.yaml
Environment=PROXY_STATE_DB=%h/proxy_state.db

# ── Security ──
Restart=on-failure
RestartSec=5
NoNewPrivileges=true

# ── Logging ──
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

> **Note:** Replace `%h` with your home directory or use the literal path.
> `%h` expands to the user's home directory when running under systemd --user.

### 3.2 Alternative: Source API keys from `.env` file

If you keep secrets in a `.env` file, use an `ExecStartPre` or a wrapper script
instead of putting keys in the unit file directly. The provided `run_proxy.sh`
script already handles this:

```ini
ExecStart=%h/router_proxy/run_proxy.sh
```

Make sure `run_proxy.sh` is executable (`chmod +x run_proxy.sh`) and that your
API keys are in `~/.hermes/.env` or wherever the script sources them from.

### 3.3 Enable linger (for user services to start on boot)

```bash
sudo loginctl enable-linger $(whoami)
```

This allows user systemd services to start automatically at boot, even when
no user is logged in.

### 3.4 Start the service

```bash
# Reload systemd user daemon
systemctl --user daemon-reload

# Enable auto-start at boot
systemctl --user enable hermes-routing-proxy

# Start now
systemctl --user start hermes-routing-proxy

# Check status
systemctl --user status hermes-routing-proxy
```

### 3.5 Verify it's running

```bash
curl http://localhost:8787/healthz
# Expected: {"status":"ok","go_healthy":true}
```

---

## 4. Logging

All logs go to the systemd journal:

```bash
# Follow latest logs
journalctl --user -u hermes-routing-proxy -f

# Last 100 lines
journalctl --user -u hermes-routing-proxy -n 100 --no-pager

# Since last boot
journalctl --user -u hermes-routing-proxy --since today

# Filter by log level
journalctl --user -u hermes-routing-proxy -p info
```

---

## 5. Managing the Service

```bash
# Restart
systemctl --user restart hermes-routing-proxy

# Stop
systemctl --user stop hermes-routing-proxy

# Disable auto-start
systemctl --user disable hermes-routing-proxy

# Check if it's running
systemctl --user is-active hermes-routing-proxy
```

---

## 6. Pointing Hermes Agent at the Proxy

In Hermes Agent configuration:

```bash
hermes config set model.base_url http://localhost:8787/v1
```

Or in `~/.hermes/.env`:

```bash
export HERMES_BASE_URL=http://localhost:8787/v1
```

Then use role names as models:

```bash
hermes chat --model coder -q "Write a hello world script"
hermes chat --model research -q "Compare Kubernetes vs Nomad"
```

---

## 7. Health Checks (Monitoring)

| Endpoint | Purpose |
|----------|---------|
| `GET /healthz` | Liveness + Go status check |
| `GET /admin/state` | Full state dump for debugging |
| `GET /admin/routing?limit=50` | Recent routing history |

Example monitoring script:

```bash
#!/bin/bash
if ! curl -sf http://localhost:8787/healthz > /dev/null; then
    echo "Hermes Routing Proxy is down!" | mail -s "Proxy Alert" admin@example.com
fi
```

---

## 8. Troubleshooting

### Proxy won't start

```bash
systemctl --user status hermes-routing-proxy
journalctl --user -u hermes-routing-proxy -n 50 --no-pager
```

Common issues:

- **Missing API keys**: Ensure `GO_API_KEY` and `OPENROUTER_API_KEY` are set.
- **Config file not found**: Check `ROUTING_CONFIG` path or `hermes-routing.yaml`
  location.
- **Port in use**: Change `PORT` environment variable or stop the conflicting
  process.
- **Permission denied**: Ensure `.venv/bin/uvicorn` is executable and
  `WorkingDirectory` is accessible.

### Go backend not used

```bash
curl http://localhost:8787/admin/state
```

Check `go_healthy` field. If `false`, the Go backend will be skipped for all
requests until the daily health check or a manual fix.

Force Go recovery:

```bash
curl -X POST http://localhost:8787/admin/reload
```

### Reload configuration without restart

```bash
curl -X POST http://localhost:8787/admin/reload
```

This re-reads the YAML routing config file without stopping the server.
