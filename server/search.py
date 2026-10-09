"""Paper search + PDF download.

arXiv is the primary source because every hit exposes a free PDF. Semantic
Scholar is a secondary source for non-arXiv work that happens to have an open
access PDF. Both are public and key-less; failures degrade to an empty list
rather than breaking the search UI.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from . import library

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_API = "https://export.arxiv.org/api/query"
S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"
UA = {"User-Agent": "paper-reader/1.0 (local research tool)"}

MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024


def _clean(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


STOPWORDS = {
    "a", "an", "the", "of", "for", "and", "or", "to", "in", "on", "with", "is",
    "are", "was", "were", "be", "by", "at", "as", "from", "that", "this", "it",
    "we", "you", "your", "our", "via", "using", "towards", "toward", "into",
    "can", "do", "does", "not", "no", "but", "than", "then", "so", "such",
}


def _queries(query: str) -> List[str]:
    """Ordered candidate arXiv queries, most precise first.

    A bare `AND` of every term fails on titles containing stopwords
    ("attention is all you need" -> "you" never appears in the abstract), so we
    start with a quoted phrase and fall back to progressively looser forms.
    """
    q = query.strip()
    if not q:
        return []
    # Respect explicit field prefixes (au:, ti:, cat:, ...) -- the user knows
    # what they are doing, so pass the query through untouched.
    if re.search(r"\b(all|ti|au|abs|cat|co|jr|rn):", q):
        return [q]

    out = ['all:"%s"' % q.replace('"', "")]
    terms = [t for t in re.split(r"[\s,]+", q) if t]
    meaningful = [t for t in terms if t.lower() not in STOPWORDS]
    if len(meaningful) > 1:
        out.append(" AND ".join("all:%s" % t for t in meaningful))
    if meaningful and len(meaningful) != len(terms):
        out.append(" ".join("all:%s" % t for t in meaningful))
    out.append(" ".join("all:%s" % t for t in terms) if terms else q)
    # De-dupe, keep order.
    seen = set()
    return [x for x in out if x and not (x in seen or seen.add(x))]


def _fetch_arxiv(query: str, limit: int) -> List[ET.Element]:
    params = {
        "search_query": query,
        "start": 0,
        "max_results": min(limit, 40),
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    try:
        with httpx.Client(timeout=25, headers=UA, follow_redirects=True) as client:
            resp = client.get(ARXIV_API, params=params)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
    except Exception:
        return []
    return root.findall(ATOM + "entry")


def search_arxiv(query: str, limit: int = 15) -> List[Dict[str, Any]]:
    entries: List[ET.Element] = []
    for candidate in _queries(query):
        entries = _fetch_arxiv(candidate, limit)
        if entries:
            break
        if len(candidate) > 200:  # guard against pathological expansions
            break

    results: List[Dict[str, Any]] = []
    for entry in entries:
        raw_id = _clean(entry.findtext(ATOM + "id"))
        # http://arxiv.org/abs/2401.12345v2 -> 2401.12345
        short = raw_id.rsplit("/", 1)[-1]
        pdf_url = ""
        for link in entry.findall(ATOM + "link"):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = link.get("href") or ""
        if not pdf_url and short:
            pdf_url = "https://arxiv.org/pdf/%s" % re.sub(r"v\d+$", "", short)
        authors = [
            _clean(a.findtext(ATOM + "name")) for a in entry.findall(ATOM + "author")
        ]
        results.append(
            {
                "source": "arxiv",
                "source_id": short,
                "title": _clean(entry.findtext(ATOM + "title")),
                "abstract": _clean(entry.findtext(ATOM + "summary"))[:1200],
                "authors": ", ".join([a for a in authors if a][:8]),
                "year": (_clean(entry.findtext(ATOM + "published")) or "")[:4],
                "venue": _clean(entry.findtext("{http://arxiv.org/schemas/atom}journal_ref"))
                or "arXiv",
                "url": raw_id,
                "pdf_url": pdf_url,
            }
        )
    return results


def _arxiv_query(query: str) -> str:
    """`all:` gives the widest match; keep explicit field prefixes intact."""
    q = query.strip()
    if re.search(r"\b(all|ti|au|abs|cat|co):", q):
        return q
    terms = [t for t in re.split(r"\s+", q) if t]
    if len(terms) <= 1:
        return "all:%s" % q
    return " AND ".join("all:%s" % t for t in terms)


def search_s2(query: str, limit: int = 15) -> List[Dict[str, Any]]:
    params = {
        "query": query,
        "limit": min(limit, 30),
        "fields": "title,abstract,authors,year,venue,externalIds,openAccessPdf,url",
    }
    try:
        with httpx.Client(timeout=25, headers=UA, follow_redirects=True) as client:
            resp = client.get(S2_API, params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception:
        return []

    results: List[Dict[str, Any]] = []
    for item in data.get("data") or []:
        oa = item.get("openAccessPdf") or {}
        results.append(
            {
                "source": "s2",
                "source_id": item.get("paperId") or "",
                "title": _clean(item.get("title")),
                "abstract": _clean(item.get("abstract"))[:1200],
                "authors": ", ".join(
                    a.get("name", "") for a in (item.get("authors") or [])[:8]
                ),
                "year": str(item.get("year") or ""),
                "venue": _clean(item.get("venue")) or "Semantic Scholar",
                "url": item.get("url") or "",
                "pdf_url": oa.get("url") or "",
            }
        )
    return results


def search(query: str, source: str = "all", limit: int = 15) -> Dict[str, Any]:
    query = (query or "").strip()
    if not query:
        return {"results": [], "errors": []}

    results: List[Dict[str, Any]] = []
    errors: List[str] = []

    if source in ("all", "arxiv"):
        hits = search_arxiv(query, limit)
        if not hits:
            errors.append("arXiv 无结果或请求失败")
        results.extend(hits)
    if source in ("all", "s2"):
        results.extend(search_s2(query, limit))

    # Dedupe on a normalised title, preferring entries that carry a PDF link.
    def norm(t: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (t or "").lower())[:70]

    seen: Dict[str, Dict[str, Any]] = {}
    for item in results:
        key = norm(item["title"])
        if not key:
            continue
        prev = seen.get(key)
        if prev is None or (not prev.get("pdf_url") and item.get("pdf_url")):
            seen[key] = item
    merged = list(seen.values())
    merged.sort(key=lambda r: (not r.get("pdf_url"), r.get("year") != ""))
    return {"results": merged[: max(limit, 20)], "errors": errors}


def download_pdf(
    pdf_url: str, dest: Path, on_progress: Optional[Callable[[int, int], None]] = None
) -> Dict[str, Any]:
    """Fetch a PDF to `dest`. Returns {ok, error, bytes}.

    `on_progress(done_bytes, total_bytes)` is called as data arrives; total is 0
    when the server sends no Content-Length. arXiv is frequently slow here, so
    the caller is expected to surface this progress to the user.
    """
    if not pdf_url:
        return {"ok": False, "error": "该结果没有可用的 PDF 链接"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    timeout = httpx.Timeout(connect=20.0, read=120.0, write=30.0, pool=20.0)
    try:
        with httpx.Client(timeout=timeout, headers=UA, follow_redirects=True) as client:
            with client.stream("GET", pdf_url) as resp:
                if resp.status_code >= 400:
                    return {"ok": False, "error": "下载失败：HTTP %d" % resp.status_code}
                expected = int(resp.headers.get("content-length") or 0)
                total = 0
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_bytes(65536):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            fh.close()
                            tmp.unlink(missing_ok=True)
                            return {"ok": False, "error": "文件超过 80MB，已中止"}
                        fh.write(chunk)
                        if on_progress:
                            on_progress(total, expected)
        head = tmp.open("rb").read(5)
        if head[:4] != b"%PDF":
            tmp.unlink(missing_ok=True)
            return {"ok": False, "error": "该链接返回的不是 PDF（可能需要机构登录）"}
        tmp.replace(dest)
        return {"ok": True, "bytes": dest.stat().st_size}
    except Exception as exc:  # noqa: BLE001
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "error": "下载出错：%s" % exc}


def _discard_dir(pid: str) -> None:
    """Remove a paper folder created for an import that never completed."""
    import shutil

    path = library.paper_dir(pid)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def open_from_search(
    hit: Dict[str, Any], on_progress: Optional[Callable[[int, int], None]] = None
) -> Dict[str, Any]:
    """Register a search hit in the library, downloading its PDF if needed."""
    existing = library.by_pdf_url(hit.get("pdf_url", "")) if hit.get("pdf_url") else None
    if existing and existing.get("has_pdf"):
        library.touch(existing["id"])
        return {"ok": True, "paper": existing, "reused": True}

    title = hit.get("title") or "untitled"
    pid = library.new_id(title)
    dest = library.pdf_path(pid)
    # 失败时不留空目录：pdf_path() 已经建好目录了。
    dl = download_pdf(hit.get("pdf_url", ""), dest, on_progress=on_progress)
    if not dl.get("ok"):
        _discard_dir(pid)
        return {"ok": False, "error": dl.get("error", "下载失败")}

    from . import pdf as pdf_mod  # local import: PyMuPDF is only needed here

    try:
        n_pages = pdf_mod.page_count(dest)
        readable = pdf_mod.has_text_layer(dest)
    except Exception as exc:  # noqa: BLE001
        _discard_dir(pid)
        return {"ok": False, "error": "PDF 解析失败：%s" % exc}

    meta = pdf_mod.metadata(dest)
    paper = library.upsert_paper(
        id=pid,
        title=title,
        authors=hit.get("authors", ""),
        abstract=hit.get("abstract", ""),
        venue=hit.get("venue", ""),
        year=hit.get("year", ""),
        source=hit.get("source", "url"),
        url=hit.get("url", ""),
        pdf_url=hit.get("pdf_url", ""),
        path=str(dest),
        n_pages=n_pages,
        readable=readable,
    )
    return {"ok": True, "paper": paper, "reused": False}
