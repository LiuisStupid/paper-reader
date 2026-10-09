"""FastAPI application: the paper reader's HTTP surface.

Run it via `agent/boot.py` (which handles API detection, dependency install and
launching) rather than by hand.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, library, llm, pdf as pdf_mod, search as search_mod, translate as tr

app = FastAPI(title="Paper Reader", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    library.init()


# --------------------------------------------------------------------------- models


class OpenLocalRequest(BaseModel):
    path: str


class ProgressRequest(BaseModel):
    page: int


class SettingsRequest(BaseModel):
    base_url: Optional[str] = None
    auth_token: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    protocol: Optional[str] = None
    supports_thinking_toggle: Optional[bool] = None


class ChatRequest(BaseModel):
    paper_id: str
    message: str
    page: int = 0
    selection: str = ""
    model: Optional[str] = None
    use_full_text: bool = False
    history_limit: int = 12


# --------------------------------------------------------------------------- helpers


def _dump(model: BaseModel) -> Dict[str, Any]:
    """Pydantic v1/v2 compatible dict dump."""
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _paper_or_404(paper_id: str) -> Dict[str, Any]:
    paper = library.get_paper(paper_id)
    if not paper:
        raise HTTPException(404, "找不到该论文")
    return paper


def _pdf_or_404(paper: Dict[str, Any]) -> Path:
    path = Path(paper.get("path") or "")
    if not path.exists():
        raise HTTPException(410, "PDF 文件已丢失，请重新导入")
    return path


def _sse(obj: Dict[str, Any]) -> str:
    return "data: %s\n\n" % json.dumps(obj, ensure_ascii=False)


def _sse_response(gen: AsyncIterator[Dict[str, Any]]) -> StreamingResponse:
    async def wrapper() -> AsyncIterator[str]:
        try:
            async for item in gen:
                yield _sse(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - last-resort error surface
            yield _sse({"type": "error", "message": "%s: %s" % (type(exc).__name__, exc)})
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        wrapper(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _register_pdf(path: Path, **meta: Any) -> Dict[str, Any]:
    """Create/refresh a library entry for a PDF already on disk."""
    pm = pdf_mod.metadata(path)
    n_pages = pm["n_pages"]
    readable = pdf_mod.has_text_layer(path)
    title = meta.get("title") or pm.get("title") or ""
    if not title:
        title = pdf_mod.detect_title(path, path.stem)
    pid = meta.get("id") or library.new_id(title)
    dest = library.pdf_path(pid)
    if path.resolve() != dest.resolve():
        shutil.copy2(path, dest)
    paper = library.upsert_paper(
        id=pid,
        title=title,
        authors=meta.get("authors") or pm.get("author") or "",
        abstract=meta.get("abstract", ""),
        venue=meta.get("venue", ""),
        year=meta.get("year", ""),
        source=meta.get("source", "local"),
        url=meta.get("url", ""),
        pdf_url=meta.get("pdf_url", ""),
        path=str(dest),
        n_pages=n_pages,
        readable=readable,
    )
    _make_cover(pid, dest)
    return paper


def _make_cover(paper_id: str, path: Path) -> None:
    cover = library.paper_dir(paper_id) / "cover.png"
    if cover.exists():
        return
    try:
        data, _w, _h = pdf_mod.render_page(path, 0, zoom=0.35)
        cover.write_bytes(data)
    except Exception:
        pass


# --------------------------------------------------------------------------- health / settings


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    cfg = config.public_llm_config()
    deps = {"pymupdf": True}
    return {"ok": True, "llm": cfg, "deps": deps, "data_dir": str(config.DATA_DIR)}


@app.post("/api/settings/test")
async def settings_test() -> Dict[str, Any]:
    return await llm.check_connection()


@app.get("/api/settings")
async def get_settings() -> Dict[str, Any]:
    return config.public_llm_config()


@app.put("/api/settings")
async def put_settings(req: SettingsRequest) -> Dict[str, Any]:
    patch = {k: v for k, v in _dump(req).items() if v is not None}
    if patch:
        config.save_settings_file(patch)
    return config.public_llm_config()


# --------------------------------------------------------------------------- papers


@app.get("/api/papers")
async def list_papers(q: str = "", limit: int = 200) -> Dict[str, Any]:
    return {"papers": library.list_papers(limit=limit, query=q)}


@app.get("/api/papers/{paper_id}")
async def get_paper(paper_id: str) -> Dict[str, Any]:
    paper = _paper_or_404(paper_id)
    library.touch(paper_id)
    return {"paper": paper, "messages": library.get_messages(paper_id)}


@app.delete("/api/papers/{paper_id}")
async def delete_paper(paper_id: str) -> Dict[str, Any]:
    _paper_or_404(paper_id)
    library.delete_paper(paper_id)
    return {"ok": True}


@app.post("/api/papers/{paper_id}/progress")
async def set_progress(paper_id: str, req: ProgressRequest) -> Dict[str, Any]:
    _paper_or_404(paper_id)
    library.touch(paper_id, page=req.page)
    return {"ok": True}


@app.post("/api/papers/upload")
async def upload_pdf(file: UploadFile = File(...)) -> Dict[str, Any]:
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "只支持 PDF 文件")
    tmp_dir = Path(tempfile.mkdtemp(prefix="paper-reader-"))
    tmp = tmp_dir / "upload.pdf"
    try:
        with tmp.open("wb") as fh:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
        paper = _register_pdf(tmp, title=Path(file.filename).stem, source="local")
        return {"paper": paper, "reused": False}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, "导入失败：%s" % exc)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/papers/open-local")
async def open_local(req: OpenLocalRequest = Body(...)) -> Dict[str, Any]:
    path = Path(req.path).expanduser()
    if not path.exists():
        raise HTTPException(404, "路径不存在：%s" % path)
    if path.is_dir():
        raise HTTPException(400, "请选择 PDF 文件而不是目录")
    existing = library.by_path(str(path))
    if existing and existing.get("has_pdf"):
        library.touch(existing["id"])
        return {"paper": existing, "reused": True}
    try:
        return {"paper": _register_pdf(path, source="local"), "reused": False}
    except pdf_mod.PDFError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/papers/open-result")
async def open_result(hit: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    result = await asyncio.to_thread(search_mod.open_from_search, hit)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "打开失败"))
    return result


@app.post("/api/papers/open-result/stream")
async def open_result_stream(hit: Dict[str, Any] = Body(...)) -> StreamingResponse:
    """Same as open-result, but reports download progress.

    arXiv regularly serves PDFs at a few tens of KB/s, so a silent multi-minute
    request looks like a hang. Progress bytes are pushed onto an asyncio queue
    from the worker thread and drained here as SSE.
    """
    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()

    def on_progress(done: int, total: int) -> None:
        loop.call_soon_threadsafe(
            queue.put_nowait, {"type": "progress", "done": done, "total": total}
        )

    async def gen() -> AsyncIterator[Dict[str, Any]]:
        task = asyncio.ensure_future(
            asyncio.to_thread(search_mod.open_from_search, hit, on_progress)
        )
        try:
            while not task.done() or not queue.empty():
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=0.4)
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat"}
            result = await task
        except asyncio.CancelledError:
            task.cancel()
            raise
        if result.get("ok"):
            yield {"type": "done", "paper": result["paper"], "reused": result.get("reused", False)}
        else:
            yield {"type": "error", "message": result.get("error", "打开失败")}

    return _sse_response(gen())


@app.get("/api/papers/{paper_id}/cover.png")
async def cover(paper_id: str) -> Response:
    p = library.paper_dir(paper_id) / "cover.png"
    if not p.exists():
        return Response(status_code=404)
    return FileResponse(str(p), media_type="image/png")


@app.get("/api/papers/{paper_id}/file")
async def raw_file(paper_id: str) -> Response:
    paper = _paper_or_404(paper_id)
    path = _pdf_or_404(paper)
    return FileResponse(str(path), media_type="application/pdf")


@app.get("/api/papers/{paper_id}/pages")
async def page_sizes(paper_id: str) -> Dict[str, Any]:
    paper = _paper_or_404(paper_id)
    path = _pdf_or_404(paper)
    sizes = pdf_mod.page_sizes(path)
    return {"count": len(sizes), "pages": sizes}


@app.get("/api/papers/{paper_id}/pages/{page_no}.png")
async def page_image(paper_id: str, page_no: int, zoom: float = pdf_mod.DEFAULT_ZOOM) -> Response:
    paper = _paper_or_404(paper_id)
    path = _pdf_or_404(paper)
    try:
        data, _w, _h = pdf_mod.render_page(path, page_no, zoom=zoom)
    except pdf_mod.PDFError as exc:
        raise HTTPException(400, str(exc))
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/papers/{paper_id}/pages/{page_no}/layout")
async def page_layout(paper_id: str, page_no: int) -> Dict[str, Any]:
    paper = _paper_or_404(paper_id)
    path = _pdf_or_404(paper)
    try:
        layout = pdf_mod.page_layout(path, page_no)
    except pdf_mod.PDFError as exc:
        raise HTTPException(400, str(exc))
    cached = tr.cached_entries(paper_id)
    for block in layout["blocks"]:
        hit = cached.get(tr.cache_key(block["text"]))
        if hit:
            block["zh"] = hit
    return layout


@app.get("/api/papers/{paper_id}/text")
async def paper_text(paper_id: str, max_chars: int = 200000) -> Dict[str, Any]:
    paper = _paper_or_404(paper_id)
    path = _pdf_or_404(paper)
    return {"text": pdf_mod.extract_text(path, max_chars=max_chars), "n_pages": paper["n_pages"]}


# --------------------------------------------------------------------------- search


@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1), source: str = "all", limit: int = 15
) -> Dict[str, Any]:
    return await asyncio.to_thread(search_mod.search, q, source, limit)


# --------------------------------------------------------------------------- translation


@app.get("/api/papers/{paper_id}/pages/{page_no}/translate")
async def translate_page(
    paper_id: str, page_no: int, force: bool = False
) -> StreamingResponse:
    paper = _paper_or_404(paper_id)
    path = _pdf_or_404(paper)

    async def gen() -> AsyncIterator[Dict[str, Any]]:
        layout = pdf_mod.page_layout(path, page_no)
        if not layout["blocks"]:
            yield {"type": "page_done", "page": page_no, "total": 0}
            return
        yield {"type": "page_start", "page": page_no, "total": len(layout["blocks"])}
        async for item in tr.translate_blocks(paper_id, layout["blocks"], force=force):
            yield {"type": "block", "page": page_no, **item}
        yield {"type": "page_done", "page": page_no}

    return _sse_response(gen())


# --------------------------------------------------------------------------- chat


def _build_system_prompt(
    paper: Dict[str, Any],
    page: int,
    page_text: str,
    selection: str,
    full_text: str = "",
) -> str:
    parts = [
        "你是 Paper Reader 内置的论文阅读助手，帮助用户理解正在阅读的论文。",
        "回答要求：用简洁、准确的中文回答；涉及公式或术语时给出必要解释；"
        "如果论文内容不足以回答，明确说明而不是编造。",
        "",
        "【当前论文】",
        "标题：%s" % (paper.get("title") or "(未知)"),
    ]
    if paper.get("authors"):
        parts.append("作者：%s" % paper["authors"])
    if paper.get("venue") or paper.get("year"):
        parts.append("出处：%s %s" % (paper.get("venue", ""), paper.get("year", "")))
    if paper.get("url"):
        parts.append("链接：%s" % paper["url"])
    if paper.get("abstract"):
        parts.append("摘要：%s" % paper["abstract"][:2000])

    if full_text:
        parts += ["", "【论文全文（可能已截断）】", full_text]
    elif page_text:
        parts += ["", "【当前页（第 %d 页）内容】" % (page + 1), page_text[:12000]]

    if selection.strip():
        parts += [
            "",
            "【用户选中的片段（用户的问题多半针对这段内容）】",
            selection.strip()[:4000],
        ]
    return "\n".join(parts)


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    paper = _paper_or_404(req.paper_id)
    path = _pdf_or_404(paper)
    cfg = config.resolve_llm()

    page_text = ""
    try:
        if 0 <= req.page < paper["n_pages"]:
            page_text = pdf_mod._open(path)[req.page].get_text("text").strip()
    except Exception:
        page_text = ""

    full_text = ""
    if req.use_full_text:
        try:
            full_text = pdf_mod.extract_text(path, max_chars=120000)
        except Exception:
            full_text = ""

    history = library.get_messages(req.paper_id)
    prior = history[-max(0, req.history_limit) :] if req.history_limit else []

    messages: List[Dict[str, str]] = []
    for msg in prior:
        if msg["role"] in ("user", "assistant") and msg["content"].strip():
            content = msg["content"]
            if msg["role"] == "user" and msg.get("context"):
                content = "[选中内容]\n%s\n\n[问题]\n%s" % (msg["context"], content)
            messages.append({"role": msg["role"], "content": content})

    user_content = req.message.strip()
    if req.selection.strip():
        user_content = "[选中内容]\n%s\n\n[问题]\n%s" % (req.selection.strip(), user_content)
    messages.append({"role": "user", "content": user_content})

    system = _build_system_prompt(paper, req.page, page_text, req.selection, full_text)

    library.add_message(req.paper_id, "user", req.message.strip(), req.selection.strip())
    library.touch(req.paper_id, page=req.page)

    async def gen() -> AsyncIterator[Dict[str, Any]]:
        collected: List[str] = []
        yield {
            "type": "meta",
            "model": req.model or cfg["model"],
            "page": req.page,
            "has_selection": bool(req.selection.strip()),
        }
        async for evt in llm.stream_chat(
            messages,
            system=system,
            model=req.model or cfg["model"],
            max_tokens=4096,
            temperature=0.3,
            thinking=True,
        ):
            if evt["type"] == "text":
                collected.append(evt["text"])
            yield evt
        answer = "".join(collected).strip()
        if answer:
            msg = library.add_message(req.paper_id, "assistant", answer)
            yield {"type": "saved", "id": msg["id"]}

    return _sse_response(gen())


@app.get("/api/papers/{paper_id}/messages")
async def get_messages(paper_id: str) -> Dict[str, Any]:
    _paper_or_404(paper_id)
    return {"messages": library.get_messages(paper_id)}


@app.post("/api/papers/{paper_id}/messages/clear")
async def clear_messages(paper_id: str) -> Dict[str, Any]:
    _paper_or_404(paper_id)
    library.clear_messages(paper_id)
    return {"ok": True}


# --------------------------------------------------------------------------- static


@app.get("/")
async def index() -> Response:
    return FileResponse(str(config.WEB_DIR / "index.html"))


app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")


@app.exception_handler(HTTPException)
async def http_error(_request: Any, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
