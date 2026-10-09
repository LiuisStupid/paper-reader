"""Dependency detection + installation.

Runs before anything else, so it may only use the standard library. Given a
bare Python 3.8+ interpreter it can create the virtualenv, install everything
in requirements.txt and re-exec into the venv -- which is what makes the
reader startable with a single command on a fresh machine.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import venv
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"

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


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def in_venv() -> bool:
    """True when the *running* interpreter is this project's venv."""
    try:
        return Path(sys.executable).resolve() == venv_python().resolve()
    except OSError:
        return False


def missing() -> List[Tuple[str, str, str]]:
    out = []
    for mod, pkg, why in REQUIRED:
        if importlib.util.find_spec(mod) is None:
            out.append((mod, pkg, why))
    return out


def create_venv() -> bool:
    if venv_python().exists():
        return True
    print("· 未找到虚拟环境，正在创建 .venv ...", flush=True)
    try:
        venv.EnvBuilder(with_pip=True, upgrade_deps=False).create(str(VENV_DIR))
    except Exception as exc:  # noqa: BLE001
        print("✗ 创建虚拟环境失败：%s" % exc)
        print("  请手动执行： python3 -m venv %s" % VENV_DIR)
        return False
    return venv_python().exists()


def reexec_in_venv(argv: Optional[List[str]] = None) -> None:
    """Replace the current process with the venv's interpreter."""
    args = [str(venv_python())] + (argv if argv is not None else sys.argv)
    os.execv(str(venv_python()), args)


def _pip(args: List[str], index: Optional[str]) -> int:
    cmd = [str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check"] + args
    if index:
        cmd += ["-i", index, "--trusted-host", index.split("//", 1)[-1].split("/", 1)[0]]
    env = dict(os.environ, PIP_PROGRESS_BAR="off")
    return subprocess.call(cmd, env=env)


def install_requirements(verbose: bool = True) -> bool:
    if not REQUIREMENTS.exists():
        print("✗ 缺少 requirements.txt")
        return False
    override = os.environ.get("PAPER_READER_PIP_INDEX")
    mirrors = [override] if override else MIRRORS
    for index in mirrors:
        label = index or "PyPI 官方源"
        if verbose:
            print("· 正在通过 %s 安装依赖 ..." % label, flush=True)
        code = _pip(["-q", "-r", str(REQUIREMENTS)], index)
        if code == 0:
            return True
        if verbose:
            print("  ↳ %s 失败，尝试下一个源" % label)
    return False


def preflight(verbose: bool = True) -> Tuple[bool, str]:
    """Full dependency story. Returns (ok, message)."""
    if not in_venv() and not venv_python().exists():
        if not create_venv():
            return False, "虚拟环境创建失败"
    if not in_venv():
        return False, "需要切换到虚拟环境（内部使用）"

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
