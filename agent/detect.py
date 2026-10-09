"""Find a working LLM endpoint without asking the user anything.

Probes, in priority order:

  1. data/settings.json        -- whatever the user last saved in the app
  2. environment variables     -- ANTHROPIC_* / OPENAI_*
  3. ~/.claude/settings.json   -- the Claude Code CLI's own config
  4. local runtimes            -- ollama / LM Studio / vLLM on their default ports

Every candidate is *live-tested* with a tiny completion before being accepted,
so a stale key or a dead local server is skipped rather than silently used.

Implemented on stdlib `urllib` only: this module has to run *before* the deps in
`agent/deps.py` are installed, so it cannot import httpx.

入口：`python3 -m agent.detect [--save]`。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "data" / "settings.json"

PROBE_TIMEOUT = 12
ANTHROPIC_HEADERS = {"anthropic-version": "2023-06-01", "content-type": "application/json"}

# Ports worth trying for a local model server, with the protocol each speaks.
LOCAL_CANDIDATES = [
    ("http://localhost:11434/v1", "openai", "ollama"),
    ("http://localhost:1234/v1", "openai", "LM Studio"),
    ("http://localhost:8080/v1", "openai", "vLLM/LocalAI"),
    ("http://localhost:8000/v1", "openai", "vLLM"),
    ("http://localhost:5000/v1", "openai", "LocalAI"),
]


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.35)
        return sock.connect_ex((host, port)) == 0


def _post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - connection refused, DNS, timeout...
        return 0, str(exc)


def _get_json(url: str, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def _check_anthropic(base_url: str, secret: str, model: str) -> Tuple[bool, str]:
    url = base_url.rstrip("/") + "/v1/messages"
    payload = {
        "model": model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "ping"}],
        "thinking": {"type": "disabled"},
    }
    for headers in (
        dict(ANTHROPIC_HEADERS, **{"x-api-key": secret}),
        dict(ANTHROPIC_HEADERS, **{"authorization": "Bearer " + secret}),
    ):
        status, body = _post_json(url, payload, headers)
        if status == 200:
            return True, ""
        if status == 400:
            # The endpoint is alive; it just disliked `thinking` (public API).
            payload.pop("thinking", None)
            status2, body2 = _post_json(url, payload, headers)
            if status2 == 200:
                return True, ""
            body = body2
        if status in (401, 403):
            return False, "鉴权失败 (HTTP %d)" % status
        last = body
    return False, (last or "").strip()[:200]


def _check_openai(base_url: str, secret: str, model: str) -> Tuple[bool, str]:
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"content-type": "application/json"}
    if secret:
        headers["authorization"] = "Bearer " + secret
    payload = {
        "model": model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "ping"}],
    }
    status, body = _post_json(url, payload, headers)
    if status == 200:
        return True, ""
    if status in (401, 403):
        return False, "鉴权失败 (HTTP %d)" % status
    return False, (body or "").strip()[:200]


def check(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    secret = cfg.get("auth_token") or cfg.get("api_key") or ""
    if cfg.get("protocol") == "openai":
        return _check_openai(cfg["base_url"], secret, cfg["model"])
    return _check_anthropic(cfg["base_url"], secret, cfg["model"])


def _list_openai_models(base_url: str, secret: str) -> List[str]:
    headers = {"authorization": "Bearer " + secret} if secret else {}
    status, data = _get_json(base_url.rstrip("/") + "/models", headers)
    if status != 200 or not isinstance(data, dict):
        return []
    return [m.get("id", "") for m in (data.get("data") or []) if m.get("id")]


def candidates() -> List[Dict[str, Any]]:
    """Every endpoint worth testing, best guess first."""
    out: List[Dict[str, Any]] = []
    seen = set()

    def add(cfg: Dict[str, Any], label: str) -> None:
        key = (cfg.get("base_url"), cfg.get("protocol"), cfg.get("model"))
        if key in seen or not cfg.get("base_url"):
            return
        seen.add(key)
        out.append(dict(cfg, _label=label))

    # 1. previously saved settings
    if SETTINGS_PATH.exists():
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            add(
                {
                    "base_url": saved.get("base_url"),
                    "auth_token": saved.get("auth_token"),
                    "api_key": saved.get("api_key"),
                    "model": saved.get("model") or "",
                    "protocol": saved.get("protocol") or "anthropic",
                    "supports_thinking_toggle": saved.get("supports_thinking_toggle", True),
                },
                "已保存的设置",
            )
        except Exception:
            pass

    # 2. environment
    env_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
    if os.environ.get("ANTHROPIC_BASE_URL") or env_key:
        add(
            {
                "base_url": os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com",
                "auth_token": os.environ.get("ANTHROPIC_AUTH_TOKEN"),
                "api_key": os.environ.get("ANTHROPIC_API_KEY"),
                "model": os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5",
                "protocol": "anthropic",
                "supports_thinking_toggle": True,
            },
            "环境变量 ANTHROPIC_*",
        )
    if os.environ.get("OPENAI_BASE_URL"):
        add(
            {
                "base_url": os.environ["OPENAI_BASE_URL"],
                "api_key": os.environ.get("OPENAI_API_KEY"),
                "model": os.environ.get("OPENAI_MODEL") or "gpt-4o-mini",
                "protocol": "openai",
            },
            "环境变量 OPENAI_*",
        )

    # 3. the Claude Code CLI's config
    cli_path = Path.home() / ".claude" / "settings.json"
    if cli_path.exists():
        try:
            cli = json.loads(cli_path.read_text(encoding="utf-8"))
            env = cli.get("env") or {}
            if env.get("ANTHROPIC_BASE_URL") or env.get("ANTHROPIC_AUTH_TOKEN"):
                add(
                    {
                        "base_url": env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com",
                        "auth_token": env.get("ANTHROPIC_AUTH_TOKEN"),
                        "api_key": env.get("ANTHROPIC_API_KEY"),
                        "model": env.get("ANTHROPIC_MODEL") or cli.get("model") or "claude-sonnet-5",
                        "protocol": "anthropic",
                        "supports_thinking_toggle": True,
                    },
                    "Claude Code CLI 配置",
                )
        except Exception:
            pass

    # 4. local runtimes, only if the port is actually listening
    for base_url, protocol, label in LOCAL_CANDIDATES:
        host_port = base_url.split("//", 1)[1].split("/", 1)[0]
        host, _, port = host_port.partition(":")
        if not _port_open(host, int(port or 80)):
            continue
        for model in _list_openai_models(base_url, "") or ["llama3.1"]:
            add(
                {
                    "base_url": base_url,
                    "auth_token": "",
                    "api_key": "",
                    "model": model,
                    "protocol": protocol,
                },
                "本地运行时 %s" % label,
            )
            break

    return out


def detect(verbose: bool = True) -> Optional[Dict[str, Any]]:
    """Return the first candidate that answers, or None."""
    found = candidates()
    if not found:
        if verbose:
            print("  ✗ 没有找到任何可用的大模型配置")
        return None
    for cfg in found:
        label = cfg.pop("_label", "?")
        if verbose:
            print("  · 测试 %s → %s (%s)" % (label, cfg["base_url"], cfg["model"]), flush=True)
        ok, err = check(cfg)
        if ok:
            if verbose:
                print("  ✓ 可用：%s @ %s" % (cfg["model"], cfg["base_url"]))
            return cfg
        if verbose:
            print("    ✗ %s" % (err or "无法连接"))
    return None


def main(argv: Optional[List[str]] = None) -> int:
    """CLI：`python3 -m agent.detect [--save] [--json]`。

    非 0 退出码表示一个能用的接口都没探到，调用方（skill）据此转入人工配置，
    而不是硬编一个 base_url 进去。
    """
    import argparse

    parser = argparse.ArgumentParser(description="探测可用的大模型接口")
    parser.add_argument("--save", action="store_true", help="探到的配置写入 data/settings.json")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（默认人话）")
    args = parser.parse_args(argv)

    cfg = detect(verbose=not args.json)
    if not cfg:
        if args.json:
            print(json.dumps({"ok": False}, ensure_ascii=False))
        else:
            print("✗ 没有探测到可用的大模型接口")
        return 1

    saved = False
    if args.save:
        # 复用 server/config.py 里的写入逻辑，避免 settings.json 的字段和路径
        # 在两个地方各维护一份。
        sys.path.insert(0, str(ROOT))
        from server.config import save_settings_file

        patch = {
            "base_url": cfg["base_url"],
            "model": cfg["model"],
            "protocol": cfg.get("protocol", "anthropic"),
        }
        if cfg.get("auth_token"):
            patch["auth_token"] = cfg["auth_token"]
        if cfg.get("api_key"):
            patch["api_key"] = cfg["api_key"]
        if "supports_thinking_toggle" in cfg:
            patch["supports_thinking_toggle"] = bool(cfg["supports_thinking_toggle"])
        save_settings_file(patch)
        saved = True

    if args.json:
        # 只输出非敏感字段：这个结果可能会被打印进对话记录。
        print(json.dumps(
            {
                "ok": True,
                "base_url": cfg["base_url"],
                "model": cfg["model"],
                "protocol": cfg.get("protocol", "anthropic"),
                "saved": saved,
            },
            ensure_ascii=False,
        ))
    elif saved:
        print("✓ 已写入 data/settings.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
