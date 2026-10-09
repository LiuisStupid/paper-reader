#!/usr/bin/env python3
"""Paper Reader 的内置 agent —— 一步完成全部启动工作。

它把通常写进 README 的那串「getting started」步骤全部接管了：

  1. 检查/创建虚拟环境
  2. 检查/安装 requirements.txt 里的依赖
  3. 探测可用的大模型 API（环境变量 → Claude CLI 配置 → 本地运行时）
  4. 启动服务并在浏览器中打开

用法（唯一的命令，无需任何其他脚本）：

    python3 agent/boot.py

常用参数：

    --port 8765      指定端口
    --check          只做检测，不启动服务
    --no-browser     不自动打开浏览器
    --recheck-api    忽略已保存的配置，重新探测大模型
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import deps  # noqa: E402  (path bootstrap must come first)

DEFAULT_PORT = 8765
SETTINGS_PATH = ROOT / "data" / "settings.json"

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"
)


def say(text: str = "") -> None:
    print(text, flush=True)


def step(n: int, total: int, title: str) -> None:
    say("\n%s[%d/%d]%s %s%s%s" % (DIM, n, total, RESET, BOLD, title, RESET))


def free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


# --------------------------------------------------------------------------- LLM


def _saved_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _persist(cfg: dict) -> None:
    """Merge the detected endpoint into data/settings.json (never clobbers a
    key the user typed in by hand, but does refresh a working base_url/model)."""
    current = _saved_settings()
    current.update(
        {
            "base_url": cfg["base_url"],
            "model": cfg["model"],
            "protocol": cfg.get("protocol", "anthropic"),
        }
    )
    if cfg.get("auth_token"):
        current["auth_token"] = cfg["auth_token"]
    if cfg.get("api_key"):
        current["api_key"] = cfg["api_key"]
    if "supports_thinking_toggle" in cfg:
        current["supports_thinking_toggle"] = cfg["supports_thinking_toggle"]
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")


def resolve_llm(recheck: bool = False) -> dict:
    from agent import detect

    saved = _saved_settings()
    secret = saved.get("auth_token") or saved.get("api_key")
    if saved.get("base_url") and saved.get("model") and secret and not recheck:
        say("· 复用已保存的大模型配置：%s @ %s" % (saved["model"], saved["base_url"]))
        cfg = {
            "base_url": saved["base_url"],
            "model": saved["model"],
            "protocol": saved.get("protocol", "anthropic"),
            "auth_token": saved.get("auth_token", ""),
            "api_key": saved.get("api_key", ""),
            "supports_thinking_toggle": saved.get("supports_thinking_toggle", True),
        }
        ok, err = detect.check(cfg)
        if ok:
            say("  %s✓ 连接正常%s" % (GREEN, RESET))
            return cfg
        say("  %s✗ 已保存的配置失效（%s），重新探测%s" % (YELLOW, err or "无法连接", RESET))

    cfg = detect.detect(verbose=True)
    if cfg:
        _persist(cfg)
        say("  %s✓ 已写入 data/settings.json，下次启动会直接复用%s" % (GREEN, RESET))
    return cfg or {}


# --------------------------------------------------------------------------- serve


def wait_and_open(url: str, timeout: float = 30.0) -> None:
    """Open the browser only once the server actually answers."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=2) as resp:
                if resp.status == 200:
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(0.35)
    say("  %s! 服务已启动但探测器未收到响应，请手动打开 %s%s" % (YELLOW, url, RESET))


def serve(host: str, port: int, open_browser: bool) -> int:
    import uvicorn

    url = "http://%s:%d" % ("127.0.0.1" if host in ("0.0.0.0", "::") else host, port)

    say("")
    say("%s%s Paper Reader 已就绪 %s" % (BOLD, GREEN, RESET))
    say("  地址：%s%s%s" % (BOLD, url, RESET))
    say("  数据：%s" % (ROOT / "data"))
    say("  %s按 Ctrl+C 退出%s" % (DIM, RESET))
    say("")

    if open_browser:
        threading.Thread(target=wait_and_open, args=(url,), daemon=True).start()

    try:
        uvicorn.run(
            "server.main:app",
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
    except KeyboardInterrupt:
        pass
    finally:
        try:
            from server import pdf as pdf_mod

            pdf_mod.close_all()
        except Exception:
            pass
    return 0


# --------------------------------------------------------------------------- main


def main(argv: list = None) -> int:
    parser = argparse.ArgumentParser(
        description="Paper Reader 一键启动 agent", add_help=True
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check", action="store_true", help="只检测环境，不启动服务")
    parser.add_argument("--recheck-api", action="store_true", help="重新探测大模型 API")
    args = parser.parse_args(argv)

    say("%s%s╭──────────────────────────────────────────╮%s" % (BOLD, GREEN, RESET))
    say("%s%s│   Paper Reader · 论文阅读器 启动 agent   │%s" % (BOLD, GREEN, RESET))
    say("%s%s╰──────────────────────────────────────────╯%s" % (BOLD, GREEN, RESET))

    # -- 1. venv ------------------------------------------------------------
    step(1, 4, "检查运行环境")
    if not deps.venv_python().exists():
        if not deps.create_venv():
            return 1
    if not deps.in_venv():
        # Hand the rest of the work to the venv interpreter.
        say("· 切换到虚拟环境解释器 ...")
        deps.reexec_in_venv([str(Path(__file__).resolve())] + sys.argv[1:])
        return 0  # not reached
    say("· 解释器：%s" % sys.executable)

    # -- 2. dependencies ----------------------------------------------------
    step(2, 4, "检查并安装依赖")
    ok, message = deps.preflight(verbose=True)
    if not ok:
        say("%s✗ %s%s" % (RED, message, RESET))
        return 1
    say("· %s%s" % (message, ""))

    # -- 3. LLM api ---------------------------------------------------------
    step(3, 4, "探测大模型 API")
    cfg = resolve_llm(recheck=args.recheck_api)
    if not cfg:
        say("%s! 没有探测到可用的大模型接口。%s" % (YELLOW, RESET))
        say("  阅读与检索功能仍可正常使用；翻译和 AI 对话需要先配置模型。")
        say("  你可以：")
        say("    · 在应用内「设置」里填写 Base URL / API Key")
        say("    · 或设置环境变量 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN 后重启")
        say("    · 或启动本地 ollama（默认 http://localhost:11434）后重启")

    if args.check:
        step(4, 4, "检测完成（--check，未启动服务）")
        return 0 if cfg else 0

    # -- 4. serve -----------------------------------------------------------
    step(4, 4, "启动服务")
    port = free_port(args.port)
    if port != args.port:
        say("· 端口 %d 被占用，改用 %d" % (args.port, port))
    return serve(args.host, port, open_browser=not args.no_browser)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\n已退出。")
        sys.exit(0)
