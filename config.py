"""
router_proxy/config.py — Load and validate hermes-routing.yaml

All model slugs / tiers / settings come from the YAML file.
No hardcoded model IDs — the config file is the single source of truth.
"""

from pathlib import Path
import yaml
import os
from typing import Any

CONFIG_PATH = os.environ.get("ROUTING_CONFIG", str(Path(__file__).parent.parent / "hermes-routing.yaml"))

_config: dict[str, Any] = {}
_config_mtime: float = 0.0


def load_config(path: str | None = None, force_reload: bool = False) -> dict[str, Any]:
    """Load and return the routing config. Caches until file changes or force_reload."""
    global _config, _config_mtime
    p = path or CONFIG_PATH
    f = Path(p)
    if not f.exists():
        raise FileNotFoundError(f"Routing config not found: {p}")

    mtime = f.stat().st_mtime
    if _config and not force_reload and mtime == _config_mtime:
        return _config

    with open(f) as fh:
        _config = yaml.safe_load(fh)

    _config_mtime = mtime
    _validate(_config)
    return _config


def _validate(cfg: dict):
    """Basic structural validation — fail fast on broken config."""
    assert "go" in cfg, "Missing 'go' section"
    assert "roles" in cfg, "Missing 'roles' section"
    assert "go_backed_roles" in cfg, "Missing 'go_backed_roles'"
    assert "escalate_to_premium_after_failures" in cfg, "Missing 'escalate_to_premium_after_failures'"
    for role in cfg["roles"]:
        r = cfg["roles"][role]
        # Every role must have at least one model slot
        has_model = any(
            k for k in r
            if k.startswith("go_") or k.startswith("fallback_") or
               k.startswith("escalation_") or k.startswith("free_") or
               k in ("primary", "cheap_alt")
        )
        assert has_model, f"Role '{role}' has no model slots"


def get_go_config() -> dict:
    """Return the 'go' block from the config."""
    cfg = load_config()
    return cfg["go"]


def get_roles_config() -> dict:
    """Return the 'roles' block from the config."""
    cfg = load_config()
    return cfg["roles"]


def get_go_backed_roles() -> list[str]:
    """Roles that can use the Go backend."""
    cfg = load_config()
    return cfg["go_backed_roles"]


def get_escalation_threshold() -> int:
    """How many failures before escalating to $5-cap tier."""
    cfg = load_config()
    return cfg["escalate_to_premium_after_failures"]


def get_role_tiers(role: str, complex_flag: bool, failure_count: int) -> list[dict]:
    """
    Build the ordered fallback ladder for a role.

    Returns a list of dicts:
      [{"name": "go_default", "provider": "go", "model": "opencode-go/deepseek-v4-flash", "tier": 0}, ...]

    Each entry has:
      - provider: "go" or "openrouter"
      - model: real slug to use
      - tier: tier number for logging
    """
    cfg = load_config()
    roles = cfg["roles"]
    escalate_threshold = cfg["escalate_to_premium_after_failures"]
    go_backed = set(cfg["go_backed_roles"])

    r = roles.get(role)
    if not r:
        raise ValueError(f"Unknown role: {role}")

    tiers: list[dict] = []
    uses_go = role in go_backed

    # ── Tier 0: Go (only for go_backed roles) ──
    if uses_go:
        go_prefix = cfg["go"].get("model_prefix", "opencode-go/")

        if complex_flag or failure_count >= 1:
            # Use premium if available, else default
            go_slot = r.get("go_premium") or r.get("go_default") or r.get("go_primary")
        else:
            go_slot = r.get("go_default") or r.get("go_premium") or r.get("go_primary")

        if go_slot:
            models = go_slot if isinstance(go_slot, list) else [go_slot]
            for m in models:
                tiers.append({"name": "go", "provider": "go", "model": m, "tier": 0})

    # ── Tier 1: OpenRouter $2-cap ──
    if complex_flag:
        fallback = r.get("fallback_2cap_heavy") or r.get("fallback_2cap")
    else:
        fallback = r.get("fallback_2cap_default") or r.get("fallback_2cap") or r.get("primary") or r.get("fallback_cheap")

    # For roles without Go-backed fallback: use primary/cheap_alt
    if not fallback and not uses_go:
        fallback = r.get("primary")

    if fallback == "none":
        fallback = None

    if fallback:
        models = fallback if isinstance(fallback, list) else [fallback]
        for m in models:
            tiers.append({"name": "2cap", "provider": "openrouter", "model": m, "tier": 1})

    # ── Tier 1b: $5-cap escalation (only when failure_count ≥ threshold) ──
    # tool_use/simple_tasks skip this (per YAML they don't have escalation keys)
    if failure_count >= escalate_threshold:
        esc = r.get("escalation_5cap")
        if esc:
            models = esc if isinstance(esc, list) else [esc]
            # Clamp to failure_count index
            idx = min(failure_count - escalate_threshold, len(models) - 1)
            tiers.append({"name": "5cap_escalation", "provider": "openrouter", "model": models[idx], "tier": 1.5})

    # ── Tier 2: Free models ──
    free = (r.get("free_active") or r.get("free_long_context") or
            r.get("free_backup") or r.get("cheap_alt"))
    if free:
        models = free if isinstance(free, list) else [free]
        for m in models:
            tiers.append({"name": "free", "provider": "openrouter", "model": m, "tier": 2})

    return tiers
