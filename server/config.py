"""Runtime configuration: paths + LLM endpoint resolution.

The LLM endpoint is resolved from (highest priority first):
  1. data/settings.json      -- saved from the in-app settings panel
  2. environment variables   -- ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / ...
  3. ~/.claude/settings.json -- the Claude Code CLI config
  4. built-in default        -- api.anthropic.com
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PAPER_READER_DATA") or (ROOT / "data"))
PAPERS_DIR = DATA_DIR / "papers"
DB_PATH = DATA_DIR / "library.db"
WEB_DIR = ROOT / "web"
SETTINGS_PATH = DATA_DIR / "settings.json"

DEFAULT_MODEL = "deepseek-v4-flash"
# Model names offered in the UI. The gateway is a passthrough, so any name is
# accepted; these are the ones actually routed to a backend here.
KNOWN_MODELS = [
    "deepseek-v4-flash",
    "deepseek-chat",
    "deepseek-reasoner",
]

for _d in (DATA_DIR, PAPERS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_settings_file() -> Dict[str, Any]:
    return _read_json(SETTINGS_PATH)


def save_settings_file(patch: Dict[str, Any]) -> Dict[str, Any]:
    current = load_settings_file()
    current.update(patch)
    SETTINGS_PATH.write_text(
        json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return current


def _claude_cli_env() -> Dict[str, Any]:
    """Pull the env block out of ~/.claude/settings.json (Claude Code CLI)."""
    cfg = _read_json(Path.home() / ".claude" / "settings.json")
    env = cfg.get("env") or {}
    return {
        "base_url": env.get("ANTHROPIC_BASE_URL"),
        "auth_token": env.get("ANTHROPIC_AUTH_TOKEN"),
        "api_key": env.get("ANTHROPIC_API_KEY"),
        "model": env.get("ANTHROPIC_MODEL") or cfg.get("model"),
    }


def resolve_llm() -> Dict[str, Any]:
    """Return the effective LLM config dict used by server/llm.py."""
    cli = _claude_cli_env()
    saved = load_settings_file()

    base_url = (
        saved.get("base_url")
        or os.environ.get("ANTHROPIC_BASE_URL")
        or cli.get("base_url")
        or "https://api.anthropic.com"
    )
    auth_token = (
        saved.get("auth_token")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or cli.get("auth_token")
    )
    api_key = (
        saved.get("api_key")
        or os.environ.get("ANTHROPIC_API_KEY")
        or cli.get("api_key")
    )
    model = (
        saved.get("model")
        or os.environ.get("ANTHROPIC_MODEL")
        or cli.get("model")
        or DEFAULT_MODEL
    )
    protocol = saved.get("protocol") or "anthropic"

    return {
        "base_url": str(base_url).rstrip("/"),
        "auth_token": auth_token,
        "api_key": api_key,
        "model": model,
        "protocol": protocol,
        # Anthropic's public API rejects `thinking` on non-reasoning models;
        # this gateway accepts it and it makes translation much faster.
        "supports_thinking_toggle": bool(saved.get("supports_thinking_toggle", True)),
    }


def public_llm_config() -> Dict[str, Any]:
    """Same as resolve_llm() but with secrets masked -- safe to send to the UI."""
    cfg = resolve_llm()
    secret = cfg.get("auth_token") or cfg.get("api_key") or ""
    return {
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "protocol": cfg["protocol"],
        "configured": bool(secret),
        "secret_hint": ("*" * max(0, len(secret) - 4) + secret[-4:]) if secret else "",
        "models": KNOWN_MODELS,
    }
