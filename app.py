"""
router_proxy/app.py — FastAPI application.

OpenAI-compatible HTTP proxy that encodes all role-based model routing.
Part A: routing headers, JSON routing field, streaming SSE routing, routing_log persistence.
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import uvicorn
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse

from . import config as cfg
from . import notify
from . import router as rt
from . import state

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("router_proxy")

# ── Background Go health checker ──
HEALTH_CHECK_INTERVAL = 86400  # daily


async def _go_health_check_loop():
    """Background task: periodically ping Go if it's unhealthy."""
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        go_healthy = state.get_go_healthy()
        if go_healthy:
            continue

        go_config = cfg.get_go_config()
        base_url = go_config.get("base_url", rt.GO_BASE_URL)
        go_api_key = os.environ.get("OPENCODE_GO_API_KEY", "") or os.environ.get("GO_API_KEY", "")
        if not go_api_key:
            continue

        roles = cfg.get_roles_config()
        coder = roles.get("coder", {})
        model = (coder.get("go_default") or coder.get("go_premium") or
                 coder.get("go_primary", ""))
        if not model:
            continue
        model_slug = model.replace("opencode-go/", "")

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    json={
                        "model": model_slug,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    },
                    headers={"Authorization": f"Bearer {go_api_key}"},
                )
                if resp.status_code < 300:
                    old = state.get_go_healthy()
                    state.set_go_healthy(True)
                    if not old:
                        logger.info("Go health: RECOVERED")
                        await notify.notify_go_recovered()
                else:
                    logger.debug(f"Go health check: still down (HTTP {resp.status_code})")
        except Exception as e:
            logger.debug(f"Go health check: still unreachable ({e})")


async def _health_check_task():
    while True:
        try:
            await _go_health_check_loop()
        except Exception as e:
            logger.error(f"Health check loop crashed: {e}", exc_info=True)
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Router proxy starting...")
    try:
        cfg.load_config()
        logger.info(f"Config loaded. Roles: {list(cfg.get_roles_config().keys())}")
    except Exception as e:
        logger.warning(f"Could not load config on startup: {e}")
    state.init_db()
    task = asyncio.create_task(_health_check_task())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("Router proxy stopped.")


app = FastAPI(title="Hermes Routing Proxy", version="2.0.0", lifespan=lifespan)


# ── Helpers ──

def _extract_routing_meta(request: Request, body: dict) -> tuple[str | None, int, bool]:
    """Extract routing metadata from headers, falling back to body.routing_meta."""
    subtask_id = request.headers.get("X-Subtask-Id")
    failure_count_str = request.headers.get("X-Failure-Count", "")
    complex_flag = request.headers.get("X-Complex", "").lower() == "true"

    # Fallback: read from body.routing_meta
    has_explicit_failure = bool(failure_count_str)
    rm = body.get("routing_meta")
    if rm and isinstance(rm, dict):
        if not subtask_id:
            subtask_id = rm.get("subtask_id")
        if not has_explicit_failure:
            fc = rm.get("failure_count")
            if fc is not None and fc > 0:
                failure_count_str = str(fc)
                has_explicit_failure = True
        if not request.headers.get("X-Complex"):
            complex_flag = complex_flag or rm.get("complex", False) in (True, "true", "True")

    if failure_count_str:
        try:
            failure_count = int(failure_count_str)
        except ValueError:
            failure_count = 0
    elif subtask_id and not has_explicit_failure:
        failure_count = state.get_failure_count(subtask_id)
    else:
        failure_count = 0

    return subtask_id, failure_count, complex_flag


def _tier_label(tier: float) -> str:
    """Convert numeric tier to human label."""
    if tier == 0:
        return "0"
    elif tier == 1:
        return "1"
    elif tier == 1.5:
        return "1b"
    elif tier == 2:
        return "2"
    return str(tier)


# ── Main endpoint ──

@dataclass
class RoutingContext:
    """Track original model and resolved roles for audit logging."""
    original_model: str
    resolved_role: str = ""
    role_source: str = "model"
    fallback_reason: str = ""

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions with role-based routing."""
    body = await request.json()

    # Determine role/model
    model_field = body.get("model", "")

    # Track original model for audit logging
    routing_ctx = RoutingContext(original_model=model_field)

    # Extract all X-* headers for routing
    headers = {k: v for k, v in request.headers.items() if k.lower().startswith("x-")}

    # The router now handles both logical roles and direct model slugs
    role = model_field  # Pass as-is, router will detect type

    subtask_id, failure_count, complex_flag = _extract_routing_meta(request, body)
    stream = body.get("stream", False)

    try:
        # Pass original model to router for alias/fallback tracking
        # Don't resolve aliases here - let router do it
        result = await rt.route_and_call(
            role=role, body=body, subtask_id=subtask_id,
            failure_count=failure_count, complex_flag=complex_flag, stream=stream,
            headers=headers,  # Pass headers for manual override detection
            original_model=routing_ctx.original_model,
        )
    except ValueError:
        # Unknown role — apply unknown_role_policy
        unknown_policy = cfg.get_unknown_role_policy()
        unknown_fallback = cfg.get_unknown_role_fallback()

        if unknown_policy == "error":
            return JSONResponse(
                status_code=400,
                content={"error": {"message": f"Unknown role: '{role}' — configure the role in hermes-routing.yaml or set unknown_role_policy to 'fallback'", "type": "invalid_request_error"}},
            )
        elif unknown_policy == "fallback":
            logger.info(f"Unknown role '{role}', falling back to '{unknown_fallback}' per unknown_role_policy")
            routing_ctx.resolved_role = unknown_fallback
            routing_ctx.role_source = "unknown_fallback"
            routing_ctx.fallback_reason = "unknown_role_to_simple_tasks"
            role = unknown_fallback
            result = await rt.route_and_call(
                role=role, body=body, subtask_id=subtask_id,
                failure_count=failure_count, complex_flag=complex_flag, stream=stream,
                headers=headers,
                original_model=routing_ctx.original_model,
            )
        else:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": f"Unknown role: '{role}' and unknown_role_policy='{unknown_policy}' is invalid", "type": "invalid_request_error"}},
            )

    if "error" in result:
        return JSONResponse(
            status_code=result.get("status", 502),
            content={"error": {"message": result["error"], "type": "routing_error"}},
        )

    resp = result["response"]
    status_code = result.get("status", 200)
    real_model = result.get("model", "?")
    backend = result.get("provider", "?")
    tier = result.get("tier", -1)
    tier_str = _tier_label(tier)

    # Use resolved role from router for audit logging
    fallback_reason = result.get("fallback_reason", routing_ctx.fallback_reason)
    role_source = result.get("role_source", routing_ctx.role_source)
    requested_model = result.get("requested_model", routing_ctx.original_model)
    resolved_role = result.get("resolved_role", routing_ctx.resolved_role or role)
    log_role = resolved_role if resolved_role and resolved_role != role else role

    # Build routing info (shows what was actually used)
    routing_info = {
        "role": log_role,
        "model": real_model,
        "backend": backend,
        "tier": tier_str,
    }

    # Persist routing log with enhanced fields
    assert isinstance(result, dict), "route_and_call must return dict at this point"
    state.log_routing(
        subtask_id=subtask_id, role=log_role, model=real_model,
        backend=backend, tier=tier, status=status_code,
        complex_flag=complex_flag, failure_count=failure_count,
        role_source=role_source, fallback_reason=fallback_reason,
        requested_model=requested_model, resolved_role=resolved_role,
    )

    logger.info(
        f"role={role} tier={tier_str} model={real_model} "
        f"upstream_status={status_code} subtask={subtask_id} complex={complex_flag}"
    )

    # Response headers
    resp_headers = {
        "X-Routed-Role": role,
        "X-Routed-Model": real_model,
        "X-Routed-Backend": backend,
        "X-Routed-Tier": tier_str,
    }

    if stream:
        async def stream_generator():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            except Exception:
                pass
            finally:
                await resp.aclose()

        return StreamingResponse(
            stream_generator(),
            status_code=status_code,
            headers={**resp_headers, "Content-Type": "text/event-stream"},
        )
    else:
        content = await resp.aread()
        await resp.aclose()

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = {"raw": content.decode("utf-8", errors="replace")}

        # Embed routing info in JSON body
        if isinstance(data, dict):
            data["routing"] = routing_info

        return Response(
            content=json.dumps(data),
            status_code=status_code,
            headers=resp_headers,
            media_type="application/json",
        )


# ── Feedback endpoint ──

@app.post("/v1/feedback")
async def feedback(request: Request):
    """Record subtask feedback. Increments failure counter on 'needs_revision'."""
    body = await request.json()
    subtask_id = body.get("subtask_id", "")
    feedback_status = body.get("status", "")

    if not subtask_id:
        return JSONResponse(status_code=400, content={"error": "subtask_id required"})

    if feedback_status == "needs_revision":
        new_count = state.increment_failure(subtask_id)
        return {"subtask_id": subtask_id, "failures": new_count, "action": "incremented"}
    elif feedback_status == "approved":
        state.set_failure_count(subtask_id, 0)
        return {"subtask_id": subtask_id, "failures": 0, "action": "reset"}
    else:
        return JSONResponse(status_code=400, content={"error": f"Unknown status: {feedback_status}"})


# ── Health & debug endpoints ──

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "go_healthy": state.get_go_healthy()}


@app.get("/admin/state")
async def admin_state():
    return state.get_all_state()


@app.post("/admin/reload")
async def admin_reload():
    try:
        cfg.load_config(force_reload=True)
        return {"status": "ok", "roles": list(cfg.get_roles_config().keys())}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@app.get("/admin/routing")
async def admin_routing(subtask_id: str | None = Query(None), limit: int = Query(100)):
    """Query routing log, optionally filtered by subtask_id."""
    entries = state.get_routing_log(subtask_id=subtask_id, limit=limit)
    return {"entries": entries, "count": len(entries)}


def main():
    port = int(os.environ.get("PORT", "8787"))
    uvicorn.run("router_proxy.app:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
