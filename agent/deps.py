"""Dependency detection + installation.

Runs before anything else, so it may only use the standard library.

这里刻意**不建虚拟环境**：依赖只有 5 个，直接装进「正在跑这个脚本的解释器」即可。
非 venv 的解释器一律加 `--user`，装到用户目录而不是系统 site-packages，
所以既不需要 sudo，也不会污染 Homebrew / 发行版自带的 Python。

这样 `python3 agent/boot.py` 就真的是唯一一条命令；多一层 venv 只会多一个
要解释、要清理、还要在文档里写两遍的解释器。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"

MIN_PYTHON = (3, 9)

# import name -> (pip name, what it is for)
REQUIRED = [
    ("fastapi", "fastapi", "HTTP 服务"),
    ("uvicorn", "uvicorn", "ASGI 服务器"),
    ("httpx", "httpx", "大模型 / arXiv 请求"),
    ("fitz", "PyMuPDF", "PDF 解析与渲染"),
    ("multipart", "python-multipart", "本地上传 PDF"),
]

# PyPI from some networks is ~25x slower than the domestic mirrors; try the
# fast ones first and fall back to the default index.
MIRRORS = [
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://pypi.tuna.tsinghua.edu.cn/simple/",
    None,  # None == PyPI default
]


def in_venv() -> bool:
    """True when the *running* interpreter lives inside a virtualenv."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def missing() -> List[Tuple[str, str, str]]:
    out = []
    for mod, pkg, why in REQUIRED:
        if importlib.util.find_spec(mod) is None:
            out.append((mod, pkg, why))
    return out


def _ensure_pip() -> bool:
    """Make sure `python -m pip` works; bootstrap it with ensurepip if not."""
    if importlib.util.find_spec("pip") is not None:
        return True
    cmd = [sys.executable, "-m", "ensurepip", "--upgrade"]
    if not in_venv():
        cmd.append("--user")
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        return False
    importlib.invalidate_caches()
    return importlib.util.find_spec("pip") is not None


def check_interpreter() -> Tuple[bool, str]:
    """Version + pip sanity check. Returns (ok, message-to-print)."""
    if sys.version_info < MIN_PYTHON:
        return False, "需要 Python %d.%d 以上，当前是 %d.%d.%d" % (
            MIN_PYTHON[0], MIN_PYTHON[1], *sys.version_info[:3]
        )
    if not _ensure_pip():
        return False, "这个 Python 里没有 pip，自动安装也没成功（可手动执行：python3 -m ensurepip --user）"
    where = "虚拟环境" if in_venv() else "系统 Python"
    return True, "%s（%s，Python %d.%d.%d）" % (
        sys.executable, where, *sys.version_info[:3]
    )


def _pip(args: List[str], index: Optional[str], flags: List[str]) -> Tuple[int, str]:
    """Run pip, returning (exit code, combined output)."""
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", "--progress-bar", "off",
    ] + flags + args
    if index:
        host = index.split("//", 1)[-1].split("/", 1)[0]
        cmd += ["-i", index, "--trusted-host", host]
    env = dict(os.environ, PIP_PROGRESS_BAR="off", PIP_DISABLE_PIP_VERSION_CHECK="1")
    try:
        proc = subprocess.run(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)
    return proc.returncode, proc.stdout or ""


def _tail(output: str, lines: int = 12) -> str:
    kept = [ln for ln in output.strip().splitlines() if ln.strip()][-lines:]
    return "\n".join(kept)


def install_requirements(verbose: bool = True) -> bool:
    if not REQUIREMENTS.exists():
        print("✗ 缺少 requirements.txt")
        return False
    if not _ensure_pip():
        print("✗ 当前解释器没有 pip")
        return False

    override = os.environ.get("PAPER_READER_PIP_INDEX")
    mirrors = [override] if override else MIRRORS
    # 装到用户目录，不碰系统 site-packages（也顺带避开大部分权限问题）。
    flags = [] if in_venv() else ["--user"]

    last_output = ""
    for index in mirrors:
        label = index or "PyPI 官方源"
        if verbose:
            print("· 正在通过 %s 安装依赖 ..." % label, flush=True)
        code, output = _pip(["-r", str(REQUIREMENTS)], index, flags)
        if code == 0:
            return True
        last_output = output

        # PEP 668：新版 pip 会直接拒绝往发行版 / Homebrew 自带的 Python 里装。
        # 上面已经用 --user 避开了系统目录，这里只是让 pip 放行。
        if "externally-managed" in output and "--break-system-packages" not in flags:
            if verbose:
                print("  ↳ 该 Python 受 PEP 668 保护，改用 --user --break-system-packages 重试")
            flags = flags + ["--break-system-packages"]
            code, output = _pip(["-r", str(REQUIREMENTS)], index, flags)
            if code == 0:
                return True
            last_output = output

        if verbose:
            reason = _tail(output, 1)
            print("  ↳ %s 失败%s，尝试下一个源" % (label, ("：" + reason) if reason else ""))

    if last_output:
        print(_tail(last_output))
    return False


def preflight(verbose: bool = True) -> Tuple[bool, str]:
    """Full dependency story. Returns (ok, message)."""
    gone = missing()
    if not gone:
        return True, "依赖完整"

    if verbose:
        print("· 缺少 %d 个依赖：%s" % (len(gone), ", ".join(p for _m, p, _w in gone)), flush=True)
    if not install_requirements(verbose=verbose):
        return False, "依赖安装失败，请检查网络后重试"

    still = missing()
    if still:
        return False, "安装后仍缺少：%s" % ", ".join(p for _m, p, _w in still)
    return True, "依赖已安装完成"
