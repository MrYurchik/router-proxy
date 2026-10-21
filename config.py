"""
router_proxy/config.py — Load and validate hermes-routing.yaml

All model slugs / roles / providers come from the YAML file.
No hardcoded model IDs — the config file is the single source of truth.

Config structure:
  default_role: str
  unknown_role_policy: str  # "error" or "fallback"
  unknown_role_fallback: str  # role name to fallback to
  role_aliases: dict[str, str]  # e.g. dispatcher -> orchestrator
  go: Go config
  go_backed_roles: list[str]  # Roles that use OpenCode Go
  roles:
    <rolename>:
      primary: {provider, models}     # orchestrator: OpenRouter FREE
      fallback: {provider, models}    # orchestrator: Go fallback
      provider: opencode-go           # Go-only roles
      default: [models...]            # default tier
      premium: [models...]            # premium tier (complex=true or failures>=2)
  direct_model_passthrough:
    enabled: true
    policy: explicit_only
    always_allow: [...]
    manual_allow: {provider_prefixes: [...], require_header: {name, value}}
    deny_by_default: true
"""

from pathlib import Path
import yaml
import os
from typing import Any

CONFIG_PATH = os.environ.get("ROUTING_CONFIG", str(Path(__file__).parent / "hermes-routing.yaml"))

_config: dict[str, Any] = {}
_config_mtime: float = 0.0

# Failures threshold before escalating to premium tier
ESCALATE_THRESHOLD = 2  # same for all roles


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
    for role in cfg["roles"]:
        r = cfg["roles"][role]
        # Every role must have either:
        # a) primary + fallback (orchestrator pattern), or
        # b) provider + default/premium (go-only pattern)
        has_orchestrator = all(k in r for k in ["primary", "fallback"])
        has_go_only = "provider" in r or "default" in r
        if not has_orchestrator and not has_go_only:
            assert False, f"Role '{role}' has no valid config structure. Must have 'primary'/'fallback' or 'provider'"

    # Validate role_aliases if present
    aliases = cfg.get("role_aliases", {})
    if aliases:
        assert isinstance(aliases, dict), "'role_aliases' must be a dict"

    # Validate direct_model_passthrough if present
    direct_cfg = cfg.get("direct_model_passthrough")
    if direct_cfg:
        assert isinstance(direct_cfg, dict), "'direct_model_passthrough' must be a dict"


def get_go_config() -> dict:
    cfg = load_config()
    return cfg.get("go", {})


def get_roles_config() -> dict:
    cfg = load_config()
    return cfg.get("roles", {})


def get_role_aliases() -> dict:
    cfg = load_config()
    return cfg.get("role_aliases", {})


def get_go_backed_roles() -> set[str]:
    cfg = load_config()
    return set(cfg.get("go_backed_roles", []))


def get_direct_model_config() -> dict:
    cfg = load_config()
    return cfg.get("direct_model_passthrough", {
        "enabled": True,
        "policy": "explicit_only",
        "always_allow": [],
        "manual_allow": {
            "provider_prefixes": ["openrouter/"],
            "require_header": {"name": "X-Manual-Model-Override", "value": "true"}
        },
        "deny_by_default": True
    })


def get_unknown_role_policy() -> str:
    cfg = load_config()
    return cfg.get("unknown_role_policy", "error")


def get_unknown_role_fallback() -> str:
    cfg = load_config()
    return cfg.get("unknown_role_fallback", "simple_tasks")


def get_default_role() -> str:
    cfg = load_config()
    return cfg.get("default_role", "orchestrator")


def is_logical_role(model: str) -> bool:
    """Check if model is a logical role name (orchestrator, coder, etc.)."""
    roles_config = get_roles_config()
    aliases = get_role_aliases()
    return model in roles_config or model in aliases


def get_role_tiers(role: str, complex_flag: bool, failure_count: int, original_model: str | None = None) -> list[dict]:
    """
    Build the ordered fallback ladder for a role.

    Returns a list of dicts:
      [{"name": "go_default", "provider": "go", "model": "deepseek-v4-flash", "tier": 0}, ...]

    Each entry has:
      - provider: "go" or "openrouter"
      - model: model slug to use
      - tier: tier number for logging
      - name: descriptive name
      - fallback_reason: reason for this tier
      - role_source: "role_routing" | "alias" | "unknown_fallback"
      - resolved_role: the actual role being used (for alias/fallback tracking)
    """
    cfg = load_config()
    roles = cfg["roles"]
    go_prefix = cfg["go"].get("model_prefix", "opencode-go/")
    go_backed = set(cfg.get("go_backed_roles", []))

    r = roles.get(role)
    resolved_role = role
    role_source = "role_routing"

    # Track original model for audit
    if original_model is None:
        original_model = role

    if not r:
        # Check if there's an alias
        aliases = get_role_aliases()
        if role in aliases:
            target = aliases[role]
            r = roles.get(target)
            if not r:
                raise ValueError(f"Alias '{role}' -> '{target}' but target role not found")
            resolved_role = target
            role_source = "alias"
        if not r:
            raise ValueError(f"Unknown role: {role}")

    tiers: list[dict] = []
    uses_go = resolved_role in go_backed

    # ── Pattern A: orchestrator (primary + fallback) ──
    if "primary" in r and "fallback" in r:
        # Primary tier (usually OpenRouter FREE)
        primary = r["primary"]
        for i, model in enumerate(primary.get("models", [])):
            provider_prefix = primary["provider"]
            tiers.append({
                "name": f"primary_{i}",
                "provider": provider_prefix,
                "model": model,
                "tier": 0 + (i * 0.1),
                "fallback_reason": "primary",
                "role_source": role_source,
                "resolved_role": resolved_role,
            })

        # Fallback tier
        fallback = r["fallback"]
        for i, model in enumerate(fallback.get("models", [])):
            provider_name = fallback["provider"]
            is_first = i == 0
            tiers.append({
                "name": f"fallback_{i}",
                "provider": provider_name,
                "model": go_prefix + model if provider_name == "go" else model,
                "tier": 1 + (i * 0.1),
                "fallback_reason": "fallback" if is_first else "fallback_alt",
                "role_source": role_source,
                "resolved_role": resolved_role,
            })
    # ── Pattern B: go-only roles (default + premium) ──
    elif uses_go:
        # Default tier
        for i, model in enumerate(r.get("default", [])):
            tiers.append({
                "name": f"default_{i}",
                "provider": "go",
                "model": go_prefix + model,
                "tier": 0 + (i * 0.1),
                "fallback_reason": "default",
                "role_source": role_source,
                "resolved_role": resolved_role,
            })

        # Premium tier (if complex or failures >= 2)
        if complex_flag or failure_count >= ESCALATE_THRESHOLD:
            for i, model in enumerate(r.get("premium", [])):
                tiers.append({
                    "name": f"premium_{i}",
                    "provider": "go",
                    "model": go_prefix + model,
                    "tier": 1.5 + (i * 0.1),
                    "fallback_reason": "premium" if complex_flag else "escalation",
                    "role_source": role_source,
                    "resolved_role": resolved_role,
                })
    else:
        raise ValueError(f"Role '{role}' has unsupported config structure")

    return tiers
