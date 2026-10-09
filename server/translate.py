"""Paragraph-level translation with an on-disk cache.

Blocks are translated one at a time but concurrently: a per-block request has
no output-parsing risk (no JSON to repair), and with thinking disabled each
call comes back in well under a second, so a whole page lands in a few
seconds.  Results stream to the UI as they arrive so the page fills in
progressively instead of blocking on the slowest block.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from . import config, library, llm

CONCURRENCY = 6
MAX_BLOCK_CHARS = 4000

SYSTEM = (
    "你是一位专业的学术论文翻译，精通人工智能、计算机科学、数学与物理等领域的术语。"
    "把用户提供的英文（或中英混排）论文片段翻译成准确、通顺、符合中文学术表达习惯的简体中文。"
    "要求：\n"
    "1. 只输出译文本身，不要输出原文、不要解释、不要加任何前缀或后缀；\n"
    "2. 保留公式、变量名、数字、单位、引用标记（如 [12]、\\cite{...}）、LaTeX 命令原样；\n"
    "3. 专业术语使用该领域通用译法，首次出现时可在括号内保留英文原词；\n"
    "4. 保持段落结构与原文一致，不要合并或拆分段落。"
)

# The model occasionally still prefixes its answer; strip those lead-ins.
_PREFIX_RE = re.compile(
    r"^\s*(译文|翻译|中文翻译|以下是译文|以下为译文|译文如下)\s*[:：]?\s*", re.I
)
_CJK_RE = re.compile(r"[一-鿿]")


def _cache_key(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


#: Public alias -- callers outside this module look translations up by it.
cache_key = _cache_key


def _needs_translation(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 4:
        return False
    # Already mostly Chinese (e.g. a CJK paper) -> nothing to do.
    latin = sum(1 for c in stripped if c.isascii() and c.isalpha())
    cjk = len(_CJK_RE.findall(stripped))
    return latin >= max(8, cjk * 1.5)


def _clean(text: str) -> str:
    text = _PREFIX_RE.sub("", text.strip())
    return text.strip().strip('"').strip()


async def translate_block(
    text: str, cfg: Dict[str, Any], sem: asyncio.Semaphore
) -> str:
    async with sem:
        out = await llm.complete(
            text[:MAX_BLOCK_CHARS],
            system=SYSTEM,
            max_tokens=min(4096, max(512, len(text) * 3)),
            temperature=0.1,
            thinking=False,
            cfg=cfg,
        )
    return _clean(out)


def cached_entries(paper_id: str) -> Dict[str, str]:
    data = library.load_translation(paper_id)
    page = data.get("_entries")
    return page if isinstance(page, dict) else {}


def _store(paper_id: str, entries: Dict[str, str]) -> None:
    data = library.load_translation(paper_id)
    data["_entries"] = entries
    library.save_translation(paper_id, "_entries", entries)


async def translate_blocks(
    paper_id: str,
    blocks: List[Dict[str, Any]],
    cfg: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> AsyncIterator[Dict[str, Any]]:
    """Yield {i, text, zh} for each block, cache hits first and instantly."""
    cfg = cfg or config.resolve_llm()
    entries = cached_entries(paper_id)
    sem = asyncio.Semaphore(CONCURRENCY)

    pending: List[Tuple[int, Dict[str, Any], str]] = []
    for block in blocks:
        idx = int(block.get("i", 0))
        src = block.get("text", "")
        if not _needs_translation(src):
            yield {"i": idx, "text": src, "zh": src, "cached": True, "skipped": True}
            continue
        key = _cache_key(src)
        hit = entries.get(key)
        if hit and not force:
            yield {"i": idx, "text": src, "zh": hit, "cached": True}
            continue
        pending.append((idx, block, src))

    if not pending:
        return

    queue: "asyncio.Queue[Any]" = asyncio.Queue()
    done = 0
    total = len(pending)

    async def worker(idx: int, src: str) -> None:
        try:
            zh = await translate_block(src, cfg, sem)
            await queue.put((idx, src, zh, None))
        except Exception as exc:  # noqa: BLE001 - reported per block
            await queue.put((idx, src, None, str(exc)))

    tasks = [asyncio.ensure_future(worker(i, s)) for i, _b, s in pending]

    try:
        while done < total:
            idx, src, zh, err = await queue.get()
            done += 1
            if err:
                yield {"i": idx, "text": src, "zh": "", "error": err, "progress": done, "total": total}
            else:
                entries[_cache_key(src)] = zh
                yield {
                    "i": idx,
                    "text": src,
                    "zh": zh,
                    "cached": False,
                    "progress": done,
                    "total": total,
                }
            # Persist periodically so an interrupted run keeps most of its work.
            if done % 8 == 0:
                _store(paper_id, entries)
    finally:
        for task in tasks:
            task.cancel()
        _store(paper_id, entries)
