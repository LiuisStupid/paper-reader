"""PDF access layer built on PyMuPDF.

Everything the reader needs comes from here:
  * rendered page images (server-side, so the browser needs no pdf.js)
  * a word-level text layer with exact bounding boxes, used for
    select-to-ask and for translating a region in place
  * paragraph blocks, which are the unit of translation

All coordinates are PDF points with a top-left origin (PyMuPDF's convention).
The frontend scales them by the same zoom factor used to render the image.
"""
from __future__ import annotations

import io
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF

# Rendering DPI: 2.0 == 144dpi, crisp on retina without huge PNGs.
DEFAULT_ZOOM = 2.0
MAX_ZOOM = 4.0
MAX_IMAGE_PX = 5000

_docs: Dict[str, "fitz.Document"] = {}
_lock = threading.Lock()

# Blocks that are almost certainly furniture rather than prose.
_NUMERIC_RE = re.compile(r"^[\d\s\.\-–—/:]+$")
_JUNK_RE = re.compile(r"^(arxiv|preprint|doi|https?://|www\.)", re.I)


class PDFError(RuntimeError):
    pass


def _open(path: Path) -> "fitz.Document":
    key = str(path)
    with _lock:
        doc = _docs.get(key)
        if doc is None:
            if not path.exists():
                raise PDFError("PDF 文件不存在：%s" % path)
            doc = fitz.open(str(path))
            _docs[key] = doc
        return doc


def close_all() -> None:
    with _lock:
        for doc in _docs.values():
            try:
                doc.close()
            except Exception:
                pass
        _docs.clear()


def _r(value: float) -> float:
    return round(float(value), 2)


def page_count(path: Path) -> int:
    return _open(path).page_count


def metadata(path: Path) -> Dict[str, Any]:
    doc = _open(path)
    meta = doc.metadata or {}
    title = (meta.get("title") or "").strip()
    # A lot of arXiv PDFs carry the LaTeX jobname as the title; ignore those.
    if not title or title.lower().endswith((".tex", ".dvi", ".pdf")) or len(title) < 4:
        title = ""
    return {
        "title": title,
        "author": (meta.get("author") or "").strip(),
        "n_pages": doc.page_count,
    }


def first_page_text(path: Path, max_chars: int = 600) -> str:
    """Used to guess a title when the PDF has no usable metadata."""
    try:
        doc = _open(path)
        if not doc.page_count:
            return ""
        return doc[0].get_text("text").strip()[:max_chars]
    except Exception:
        return ""


def has_text_layer(path: Path, sample_pages: int = 3) -> bool:
    """False for scanned/image-only PDFs, which we cannot translate or search."""
    doc = _open(path)
    for i in range(min(sample_pages, doc.page_count)):
        if len(doc[i].get_text("text").strip()) > 40:
            return True
    return False


def render_page(path: Path, page_no: int, zoom: float = DEFAULT_ZOOM) -> Tuple[bytes, int, int]:
    """Return (png_bytes, width_px, height_px) for a 0-based page index."""
    doc = _open(path)
    if not (0 <= page_no < doc.page_count):
        raise PDFError("页码越界：%d（共 %d 页）" % (page_no, doc.page_count))
    zoom = max(0.5, min(float(zoom), MAX_ZOOM))
    page = doc[page_no]
    rect = page.rect
    # Keep the raster sane for very large pages.
    longest = max(rect.width, rect.height) * zoom
    if longest > MAX_IMAGE_PX:
        zoom *= MAX_IMAGE_PX / longest
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png"), pix.width, pix.height


def page_size(path: Path, page_no: int) -> Tuple[float, float]:
    doc = _open(path)
    rect = doc[page_no].rect
    return _r(rect.width), _r(rect.height)


def page_sizes(path: Path) -> List[Dict[str, Any]]:
    """Every page's size in points.

    The frontend builds all page containers up front from this, so the document
    has a stable layout (no jump as pages lazy-load) and page navigation can
    scroll straight to a target offset.
    """
    doc = _open(path)
    out: List[Dict[str, Any]] = []
    for page in doc:
        rect = page.rect
        # Portrait pages are sometimes stored rotated; report the visual size.
        w, h = rect.width, rect.height
        if page.rotation in (90, 270):
            w, h = h, w
        out.append({"w": _r(w), "h": _r(h)})
    return out


def page_layout(path: Path, page_no: int) -> Dict[str, Any]:
    """Word boxes for the selectable text layer + paragraph blocks.

    `words` is intentionally flat and in reading order: the browser lays them
    out as absolutely positioned inline spans, which makes native text
    selection (and therefore select-to-ask) work over the page image.
    """
    doc = _open(path)
    if not (0 <= page_no < doc.page_count):
        raise PDFError("页码越界：%d" % page_no)
    page = doc[page_no]
    width, height = _r(page.rect.width), _r(page.rect.height)

    raw = page.get_text("words")  # (x0, y0, x1, y1, word, block, line, word_no)
    raw.sort(key=lambda w: (w[5], w[6], w[7]))

    words: List[Dict[str, Any]] = []
    for x0, y0, x1, y1, text, block_no, line_no, _word_no in raw:
        text = text.strip()
        if not text:
            continue
        if x1 - x0 <= 0.4 or y1 - y0 <= 0.4:
            continue
        words.append(
            {
                "x0": _r(x0),
                "y0": _r(y0),
                "x1": _r(x1),
                "y1": _r(y1),
                "t": text,
                "b": int(block_no),
                "l": int(line_no),
            }
        )

    return {
        "page": page_no,
        "width": width,
        "height": height,
        "words": words,
        "blocks": _blocks(page),
        "has_text": len(words) > 8,
    }


def _blocks(page: "fitz.Page") -> List[Dict[str, Any]]:
    """Paragraph-level blocks, the unit we translate."""
    spans: List[Dict[str, Any]] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:  # 1 == image
            continue
        lines = []
        for line in block.get("lines", []):
            text = "".join(s.get("text", "") for s in line.get("spans", []))
            if text.strip():
                sizes = [s.get("size", 10) for s in line.get("spans", []) if s.get("text", "").strip()]
                lines.append(
                    {
                        "text": text,
                        "bbox": line.get("bbox"),
                        "size": max(sizes) if sizes else 10.0,
                    }
                )
        if not lines:
            continue
        # A "block" in PyMuPDF is often a single line; group consecutive lines
        # into a paragraph so translation sees real sentences.
        spans.append({"lines": lines, "bbox": block.get("bbox")})

    blocks: List[Dict[str, Any]] = []
    for item in _merge_paragraphs(spans):
        text = " ".join(l["text"] for l in item["lines"]).strip()
        text = re.sub(r"\s+", " ", text)
        if _is_junk(text):
            continue
        x0, y0, x1, y1 = item["bbox"]
        blocks.append(
            {
                "x0": _r(x0),
                "y0": _r(y0),
                "x1": _r(x1),
                "y1": _r(y1),
                "text": text,
                "size": _r(max(l["size"] for l in item["lines"])),
                "n_lines": len(item["lines"]),
            }
        )
    blocks.sort(key=lambda b: (b["y0"], b["x0"]))
    for i, b in enumerate(blocks):
        b["i"] = i
    return blocks


def _merge_paragraphs(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Join vertically-adjacent single-line blocks that share a column."""
    merged: List[Dict[str, Any]] = []
    for item in sorted(items, key=lambda b: (b["bbox"][1], b["bbox"][0])):
        if not merged:
            merged.append({"lines": list(item["lines"]), "bbox": list(item["bbox"])})
            continue
        prev = merged[-1]
        px0, py0, px1, py1 = prev["bbox"]
        cx0, cy0, cx1, cy1 = item["bbox"]
        gap = cy0 - py1
        line_h = max(1.0, (py1 - py0) / max(1, len(prev["lines"])))
        same_column = abs(cx0 - px0) < max(24.0, (px1 - px0) * 0.18)
        prev_ends_sentence = prev["lines"][-1]["text"].rstrip().endswith((".", "?", "!", ":", ";"))
        if same_column and -2.0 < gap < line_h * 0.75 and not prev_ends_sentence:
            prev["lines"].extend(item["lines"])
            prev["bbox"] = [
                min(px0, cx0),
                min(py0, cy0),
                max(px1, cx1),
                max(py1, cy1),
            ]
        else:
            merged.append({"lines": list(item["lines"]), "bbox": list(item["bbox"])})
    return merged


def _is_junk(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 3:
        return True
    if _NUMERIC_RE.match(stripped):
        return True
    if _JUNK_RE.match(stripped) and len(stripped) < 60:
        return True
    # Require a reasonable ratio of letters/CJK to total length.
    meaningful = sum(1 for ch in stripped if ch.isalpha() or "一" <= ch <= "鿿")
    return meaningful < max(3, len(stripped) * 0.4)


def extract_text(path: Path, max_chars: int = 200000) -> str:
    """Whole-document plain text, used as LLM context."""
    doc = _open(path)
    out: List[str] = []
    total = 0
    for i in range(doc.page_count):
        chunk = doc[i].get_text("text").strip()
        if not chunk:
            continue
        out.append("\n\n--- page %d ---\n\n%s" % (i + 1, chunk))
        total += len(chunk)
        if total >= max_chars:
            out.append("\n\n[... 文档过长，已截断 ...]")
            break
    return "".join(out)


def detect_title(path: Path, fallback: str) -> str:
    """Title from page 1: the largest-font block in the upper part of the page.

    Naive "first long line" heuristics pick up arXiv's attribution banner, which
    on many preprints sits above the real title in a *smaller* font -- so rank
    by font size instead.
    """
    try:
        doc = _open(path)
        if doc.page_count == 0:
            return fallback
        page = doc[0]
        height = page.rect.height
        best: Optional[Tuple[float, str]] = None
        for block in _blocks(page):
            if block["y0"] > height * 0.45 or block["y1"] - block["y0"] > height * 0.25:
                continue
            text = block["text"].strip()
            if not (12 <= len(text) <= 250) or _is_junk(text):
                continue
            score = block["size"] * (1.0 + min(len(text), 120) / 400.0)
            if best is None or score > best[0]:
                best = (score, text)
        if best:
            return best[1]
    except Exception:
        pass
    return guess_title(first_page_text(path), fallback)


def guess_title(text: str, fallback: str) -> str:
    """Best-effort title from the top of the first page."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for line in lines[:12]:
        if 20 <= len(line) <= 200 and not _JUNK_RE.match(line) and not _NUMERIC_RE.match(line):
            letters = sum(1 for c in line if c.isalpha())
            if letters >= len(line) * 0.5:
                return re.sub(r"\s+", " ", line)
    return fallback
