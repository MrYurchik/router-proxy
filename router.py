"""
router_proxy/router.py — Core routing logic.

Handles upstream calls with tiered fallback:

Tiers are built from hermes-routing.yaml.
For orchestrator: primary (OpenRouter FREE NVIDIA) → fallback (OpenCode Go)
For go-backed roles: default model → premium model (if complex/failures >= 2)
No OpenRouter paid/capped fallbacks — only NVIDIA FREE for orchestrator.

Manages Go health state transitions and notifications.
"""

import json
import logging
import os
import time
import uuid
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


# ─── Direct Model Detection & Policy ──────────────────────────────────

def _get_direct_model_config() -> dict:
    """Get direct_model_passthrough config from YAML."""
    config = cfg.load_config()
    return config.get("direct_model_passthrough", {
        "enabled": True,
        "policy": "explicit_only",
        "always_allow": [],
        "manual_allow": {
            "provider_prefixes": ["openrouter/"],
            "require_header": {"name": "X-Manual-Model-Override", "value": "true"}
        },
        "deny_by_default": True
    })


def _is_logical_role(model: str) -> bool:
    """Check if model is a logical role name (orchestrator, coder, etc.)."""
    roles_config = cfg.get_roles_config()
    aliases = cfg.get_role_aliases()
    return model in roles_config or model in aliases


def _is_always_allowed_direct_model(model: str, direct_cfg: dict) -> bool:
    """Check if model is in always_allow list."""
    always_allow = direct_cfg.get("always_allow", [])
    return model in always_allow


def _is_openrouter_model(model: str) -> bool:
    """
    Heuristic: detect if model slug is an OpenRouter model.
    OpenRouter models often have provider/ prefix like:
    - openai/gpt-4.1
    - anthropic/claude-...
    - google/gemini-...
    - qwen/...
    - nvidia/... (but :free ones are handled separately)
    """
    # If it has a slash and doesn't start with opencode-go/, it's likely OpenRouter
    if "/" in model and not model.startswith("opencode-go/"):
        # Check if it's a known OpenRouter provider prefix
        known_prefixes = (
            "openai/", "anthropic/", "google/", "qwen/", "meta-llama/",
            "mistralai/", "cohere/", "x-ai/", "nvidia/", "deepseek/",
            "z-ai/", "moonshot/", "minimax/", "01-ai/", "allenai/",
            "nous/", "gryphe/", "undi/", "mancer/", "cognitive/",
            "liquid/", "shisa/", "aether/", "nvidia/"
        )
        return model.startswith(known_prefixes)
    return False


def _is_direct_model(model: str) -> bool:
    """Check if model is a direct model slug (not a logical role)."""
    return not _is_logical_role(model) and ("/" in model or model.startswith("opencode-go/"))


async def _route_direct_model(
    model: str,
    body: dict,
    stream: bool,
    headers: dict,
    direct_cfg: dict,
    has_manual_override: bool,
    direct_provider: str | None
) -> dict:
    """
    Route a direct model slug through policy.
    Returns routing result dict or error dict.
    """
    provider = None
    upstream_model = model

    # Determine provider and upstream model
    if model.startswith("opencode-go/"):
        provider = "go"
        upstream_model = model.replace("opencode-go/", "")
        base_url = GO_BASE_URL
        api_key = GO_API_KEY
    elif _is_openrouter_model(model) or direct_provider == "openrouter":
        provider = "openrouter"
        upstream_model = model
        base_url = OPENROUTER_BASE_URL
        api_key = OPENROUTER_API_KEY
    else:
        # Unknown provider prefix
        return {
            "error": f"Unknown provider for direct model: {model}",
            "status": 400
        }

    # Check policy
    if not direct_cfg.get("enabled", True):
        return {
            "error": "Direct model pass-through is disabled",
            "status": 403
        }

    # 1. Always allowed models (free NVIDIA, Go models)
    if _is_always_allowed_direct_model(model, direct_cfg):
        logger.info(f"Direct model allowed (always_allow): {model}")
        resp = await _call_upstream(base_url, api_key, upstream_model, body, provider, headers, stream)
        if resp and resp.status_code < 300:
            return {
                "response": resp,
                "provider": provider,
                "model": model,
                "tier": 0,
                "status": resp.status_code,
                "fallback_reason": "direct_allowlist",
                "role_source": "direct_allowlist",
                "requested_model": model
            }
        return {"error": f"Upstream failed: {resp.status_code if resp else 'connection'}", "status": 502}

    # 2. OpenRouter paid models - require manual override
    if provider == "openrouter" and not model.endswith(":free"):
        manual_allow = direct_cfg.get("manual_allow", {})
        require_header = manual_allow.get("require_header", {})
        header_name = require_header.get("name", "X-Manual-Model-Override")
        header_value = require_header.get("value", "true")

        if not has_manual_override:
            logger.warning(f"Direct OpenRouter paid model blocked (no manual override): {model}")
            return {
                "error": f"Direct OpenRouter paid models require manual override header: {header_name}: {header_value}",
                "status": 403
            }

        logger.info(f"Direct model allowed (manual override): {model}")
        resp = await _call_upstream(base_url, api_key, upstream_model, body, provider, headers, stream)
        if resp and resp.status_code < 300:
            return {
                "response": resp,
                "provider": provider,
                "model": model,
                "tier": 0,
                "status": resp.status_code,
                "fallback_reason": "manual_override",
                "role_source": "manual_direct_openrouter",
                "manual_paid_override": True,
                "cost_risk": "manual_paid"
            }
        return {"error": f"Upstream failed: {resp.status_code if resp else 'connection'}", "status": 502}

    # 3. OpenRouter free models (already caught by always_allow if listed)
    if provider == "openrouter" and model.endswith(":free"):
        if not direct_cfg.get("deny_by_default", True):
            logger.info(f"Direct OpenRouter free model allowed (deny_by_default=false): {model}")
            resp = await _call_upstream(base_url, api_key, upstream_model, body, provider, headers, stream)
            if resp and resp.status_code < 300:
                return {
                    "response": resp,
                    "provider": provider,
                    "model": model,
                    "tier": 0,
                    "status": resp.status_code,
                    "fallback_reason": "direct_free_model",
                    "role_source": "direct_free_model",
                    "requested_model": model
                }
            return {"error": f"Upstream failed: {resp.status_code if resp else 'connection'}", "status": 502}
        else:
            logger.warning(f"Direct OpenRouter free model blocked (deny_by_default): {model}")
            return {
                "error": "Direct OpenRouter free models not in allowlist",
                "status": 403
            }

    # 4. Go models not in always_allow
    if provider == "go":
        if not direct_cfg.get("deny_by_default", True):
            logger.info(f"Direct Go model allowed (deny_by_default=false): {model}")
            resp = await _call_upstream(base_url, api_key, upstream_model, body, provider, headers, stream)
            if resp and resp.status_code < 300:
                return {
                    "response": resp,
                    "provider": provider,
                    "model": model,
                    "tier": 0,
                    "status": resp.status_code,
                    "fallback_reason": "direct_go_model",
                    "role_source": "direct_go_model",
                    "requested_model": model
                }
            return {"error": f"Upstream failed: {resp.status_code if resp else 'connection'}", "status": 502}
        else:
            logger.warning(f"Direct Go model blocked (not in allowlist): {model}")
            return {
                "error": "Direct Go model not in allowlist",
                "status": 403
            }

    # Default deny
    if direct_cfg.get("deny_by_default", True):
        return {
            "error": f"Direct model not allowed by policy: {model}",
            "status": 403
        }

    # Fallback allow (open policy)
    logger.info(f"Direct model allowed (open policy): {model}")
    resp = await _call_upstream(base_url, api_key, upstream_model, body, provider, headers, stream)
    if resp and resp.status_code < 300:
        return {
            "response": resp,
            "provider": provider,
            "model": model,
            "tier": 0,
            "status": resp.status_code,
            "fallback_reason": "direct_open_policy",
            "role_source": "direct_open_policy",
            "requested_model": model
        }
    return {"error": f"Upstream failed: {resp.status_code if resp else 'connection'}", "status": 502}


# ─── Upstream Call ──────────────────────────────────

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
    if provider == "go":
        # OpenCode Go requires a session identifier on every request.
        req_headers["x-opencode-session"] = f"hermes-{uuid.uuid4().hex}"
    if provider == "openrouter":
        req_headers["HTTP-Referer"] = "https://hermes-agent.nousresearch.com"
        req_headers["X-Title"] = "Hermes Agent Routing Proxy"

    if headers:
        req_headers.update({k: v for k, v in headers.items() if k.lower().startswith("x-")})

    request_body = dict(body)
    request_body["model"] = model
    if stream:
        request_body["stream"] = True

    # Reasoning models produce internal reasoning that eats into the max_tokens budget.
    # If max_tokens is too low, reasoning consumes everything and finish_reason="length"
    # with empty content. Bump to at least 4096 for these models routed via Go.
    _reasoning_models = ("deepseek", "mimo", "minimax")
    if any(m in model for m in _reasoning_models) and provider == "go":
        cur = request_body.get("max_tokens", 0)
        if not cur or cur < 4096:
            logger.info(f"Bumping max_tokens {cur} -> 4096 for reasoning model {model}")
            request_body["max_tokens"] = 4096

    timeout = httpx.Timeout(600.0, connect=10.0, read=300.0)
    client = httpx.AsyncClient(timeout=timeout)

    resp = await client.post(url, json=request_body, headers=req_headers)
    # Don't close client — the response body streaming needs it alive
    return resp


async def _try_tier(
    tier_info: dict,
    body: dict,
    stream: bool,
) -> dict | None:
    """Try one tier. Returns {\"response\": httpx.Response} on success, None on failure."""
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
    elif provider == "openrouter":
        if not OPENROUTER_API_KEY:
            logger.debug("Skipping OpenRouter tier — no OPENROUTER_API_KEY set")
            return None
        base_url = OPENROUTER_BASE_URL
        api_key = OPENROUTER_API_KEY
        upstream_model = model
    else:
        logger.warning(f"Unknown provider: {provider}")
        return None

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


# ─── Main Routing Entry Point ──────────────────────────────────

async def route_and_call(
    role: str,
    body: dict,
    subtask_id: str | None = None,
    failure_count: int = 0,
    complex_flag: bool = False,
    stream: bool = False,
    headers: dict | None = None,
    original_model: str | None = None,
) -> dict | AsyncGenerator:
    """
    Main routing entry point.

    Returns dict with response info (for non-stream) or the raw Response for streaming.
    """
    # Track original model for audit logging
    if original_model is None:
        original_model = role

    # Extract direct model headers
    direct_provider = None
    has_manual_override = False
    if headers:
        # Check for manual override header
        manual_header_name = "X-Manual-Model-Override"
        if manual_header_name.lower() in {k.lower() for k in headers}:
            has_manual_override = True
        # Check for explicit provider header
        if "X-Direct-Provider" in headers:
            direct_provider = headers["X-Direct-Provider"]

    # 1. Check if it's a logical role → use role routing
    if _is_logical_role(role):
        # Pass original_model to get_role_tiers for alias/fallback tracking
        tiers = cfg.get_role_tiers(role, complex_flag, failure_count, original_model=original_model)
        original_role = role

        if not tiers:
            return {"error": f"No model tiers configured for role '{role}'", "status": 500}

        go_healthy = state.get_go_healthy()

        logger.info(
            f"Routing: role={original_role} subtask={subtask_id} "
            f"complex={complex_flag} failures={failure_count} "
            f"go_healthy={go_healthy} tiers=({len(tiers)}) "
            f"tier_plan={[t.get('model','?') for t in tiers]}"
        )

        for tier_info in tiers:
            provider = tier_info["provider"]
            tier_name = tier_info.get("name", "?")
            tier_model = tier_info["model"]

            # Skip Go tiers if Go is unhealthy
            if provider == "go" and not go_healthy:
                logger.debug(f"Skipping Go tier (unhealthy): {tier_model}")
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

                # Success — log which fallback was used for routing diversity tracking
                actual_provider = result.get("provider", provider)
                actual_model = result.get("model", tier_model)
                tier_name = tier_info.get("name", "?")
                tier_number = tier_info.get("tier", 0)
                fallback_reason = tier_info.get("fallback_reason", "")
                # Use tier metadata for audit fields
                role_source = tier_info.get("role_source", "role_routing")
                resolved_role = tier_info.get("resolved_role", original_role)

                logger.info(
                    f"Routed: role={original_role} provider={actual_provider} "
                    f"model={actual_model} tier_name={tier_name} "
                    f"fallback_reason='{fallback_reason}' role_source={role_source} resolved_role={resolved_role}"
                )

                return {
                    "response": resp,
                    "provider": actual_provider,
                    "model": actual_model,
                    "tier": tier_number,
                    "status": status_code,
                    "fallback_reason": fallback_reason,
                    "role_source": role_source,
                    "resolved_role": resolved_role
                }

        # All tiers exhausted — provide detailed diagnostics
        return {
            "error": "All upstream tiers exhausted — "
                     f"Go API key set={'yes' if GO_API_KEY else 'no'}, "
                     f"OpenRouter API key set={'yes' if OPENROUTER_API_KEY else 'no'}, "
                     f"go_healthy={go_healthy}",
            "status": 502,
        }

    # 2. Direct model slug → apply direct model policy
    elif _is_direct_model(role):
        direct_cfg = _get_direct_model_config()
        logger.info(f"Direct model request: model={role} manual_override={has_manual_override} direct_provider={direct_provider}")
        return await _route_direct_model(role, body, stream, headers or {}, direct_cfg, has_manual_override, direct_provider)

    # 3. Unknown model/role
    else:
        # Check unknown role policy
        unknown_policy = cfg.get_unknown_role_policy()
        fallback_role = cfg.get_unknown_role_fallback()

        if unknown_policy == "fallback":
            logger.info(f"Unknown model '{role}' → fallback to role '{fallback_role}' (unknown_fallback)")
            # Recursively route to fallback, but capture the result to enrich logging
            result = await route_and_call(
                fallback_role, body, subtask_id, failure_count, complex_flag, stream, headers
            )
            # Enrich result with original request info for audit
            if isinstance(result, dict) and "error" not in result:
                result["requested_model"] = role
                result["resolved_role"] = fallback_role
                result["role_source"] = "unknown_fallback"
                result["fallback_reason"] = "unknown_role_to_simple_tasks"
            return result
        else:
            return {"error": f"Unknown model/role: {role}", "status": 400}
