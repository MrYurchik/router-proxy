"""
router_proxy/router.py — Core routing logic.

Handles the actual upstream calls with tiered fallback:
Tier 0 → Go; Tier 1 → $2-cap; Tier 1b → $5-cap; Tier 2 → free.

Manages Go health state transitions and notifications.
"""

import json
import logging
import os
import time
from typing import AsyncGenerator

import httpx

from . import config as cfg
from . import notify
from . import state

logger = logging.getLogger("router_proxy.router")

GO_API_KEY = os.environ.get("OPENCODE_GO_API_KEY", "") or os.environ.get("GO_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

GO_BASE_URL = "https://opencode.ai/zen/go/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Track last go_down notification time to avoid spamming
_last_go_notify: float = 0.0
_NOTIFY_COOLDOWN = 3600  # 1 hour between repeated go_down notifications


async def _call_upstream(
    base_url: str,
    api_key: str,
    model: str,
    body: dict,
    provider: str,
    headers: dict | None = None,
    stream: bool = False,
) -> httpx.Response:
    """Make a chat completions call to an upstream provider."""
    url = f"{base_url}/chat/completions"

    req_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider == "openrouter":
        req_headers["HTTP-Referer"] = "https://hermes-agent.nousresearch.com"
        req_headers["X-Title"] = "Hermes Agent Routing Proxy"

    if headers:
        req_headers.update({k: v for k, v in headers.items()
                           if k.lower().startswith("x-")})

    request_body = dict(body)
    request_body["model"] = model
    if stream:
        request_body["stream"] = True

    # DeepSeek models produce internal reasoning that eats into the max_tokens budget.
    # If max_tokens is too low, reasoning consumes everything and finish_reason="length"
    # with empty content. Bump to at least 4096 for DeepSeek models routed via Go.
    if "deepseek" in model and provider == "go":
        cur = request_body.get("max_tokens", 0)
        if not cur or cur < 4096:
            logger.info(f"Bumping max_tokens {cur} -> 4096 for DeepSeek model {model}")
            request_body["max_tokens"] = 4096

    timeout = httpx.Timeout(600.0, connect=10.0, read=300.0)
    client = httpx.AsyncClient(timeout=timeout)

    resp = await client.post(url, json=request_body, headers=req_headers)
    # Don't close client — the response body streaming needs it alive
    # Client is closed after response is consumed in app.py
    return resp


async def _try_tier(
    tier_info: dict,
    body: dict,
    stream: bool,
) -> dict | None:
    """Try one tier. Returns {"response": httpx.Response} on success, None on failure."""
    provider = tier_info["provider"]
    model = tier_info["model"]

    if provider == "go":
        if not GO_API_KEY:
            logger.debug("Skipping Go tier — no GO_API_KEY set")
            return None
        base_url = GO_BASE_URL
        api_key = GO_API_KEY
        # Strip opencode-go/ prefix — Go API uses bare model slugs
        upstream_model = model.replace("opencode-go/", "")
    else:
        if not OPENROUTER_API_KEY:
            logger.debug("Skipping OpenRouter tier — no OPENROUTER_API_KEY set")
            return None
        base_url = OPENROUTER_BASE_URL
        api_key = OPENROUTER_API_KEY
        upstream_model = model

    try:
        resp = await _call_upstream(base_url, api_key, upstream_model, body, provider, stream=stream)
        status = resp.status_code

        if status < 300:
            logger.info(
                f"Upstream OK: provider={provider} model={model} status={status}"
            )
            return {"response": resp, "provider": provider, "model": model, "status": status}
        else:
            logger.warning(
                f"Upstream failed: provider={provider} model={model} status={status}"
            )
            # Try to read error body and close
            try:
                await resp.aread()
            except Exception:
                pass
            return None

    except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as e:
        logger.warning(f"Upstream connection error: provider={provider} model={model} err={e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error: provider={provider} model={model} err={e}")
        return None


async def route_and_call(
    role: str,
    body: dict,
    subtask_id: str | None = None,
    failure_count: int = 0,
    complex_flag: bool = False,
    stream: bool = False,
) -> dict | AsyncGenerator:
    """
    Main routing entry point.

    Returns dict with response info (for non-stream) or the raw Response for streaming.
    """
    tiers = cfg.get_role_tiers(role, complex_flag, failure_count)
    original_role = role

    if not tiers:
        # No tiers configured — error
        return {"error": f"No model tiers configured for role '{role}'", "status": 500}

    go_healthy = state.get_go_healthy()
    go_backed = role in cfg.get_go_backed_roles()

    logger.info(
        f"Routing: role={original_role} subtask={subtask_id} "
        f"complex={complex_flag} failures={failure_count} "
        f"go_healthy={go_healthy} tiers={len(tiers)}"
    )

    for tier_info in tiers:
        provider = tier_info["provider"]
        tier_name = tier_info["name"]

        # Skip Go tiers if Go is unhealthy
        if provider == "go" and not go_healthy:
            logger.debug(f"Skipping Go tier (unhealthy): {tier_info['model']}")
            continue

        # Go-backed roles only use Go if healthy
        if provider == "go" and go_backed and not go_healthy:
            continue

        result = await _try_tier(tier_info, body, stream)

        if result is not None:
            resp = result["response"]
            status_code = result["status"]

            # Check for Go auth errors — mark unhealthy
            if provider == "go" and status_code in (401, 402, 403, 429):
                global _last_go_notify
                prev_healthy = state.get_go_healthy()
                state.set_go_healthy(False)
                go_healthy = False

                now = time.time()
                if prev_healthy or (now - _last_go_notify > _NOTIFY_COOLDOWN):
                    _last_go_notify = now
                    await notify.notify_go_down(status_code, original_role)

                # Continue to next tier — Go failed
                try:
                    await resp.aread()
                except Exception:
                    pass
                continue

            return {
                "response": resp,
                "provider": provider,
                "model": tier_info["model"],
                "tier": tier_info["tier"],
                "status": status_code,
            }

    # All tiers exhausted
    return {"error": "All upstream tiers exhausted", "status": 502}
