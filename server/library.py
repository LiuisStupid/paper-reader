"""Reading history + chat log, persisted in SQLite.

A "paper" here is anything the user opened: a local PDF, or one downloaded
after searching arXiv / Semantic Scholar.  Each paper owns a folder under
data/papers/<id>/ holding the PDF, the translation cache and a cover thumbnail.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    authors      TEXT NOT NULL DEFAULT '',
    abstract     TEXT NOT NULL DEFAULT '',
    venue        TEXT NOT NULL DEFAULT '',
    year         TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT 'local',   -- local | arxiv | s2 | url
    url          TEXT NOT NULL DEFAULT '',
    pdf_url      TEXT NOT NULL DEFAULT '',
    path         TEXT NOT NULL DEFAULT '',
    n_pages      INTEGER NOT NULL DEFAULT 0,
    last_page    INTEGER NOT NULL DEFAULT 0,
    readable     INTEGER NOT NULL DEFAULT 1,
    added_at     REAL NOT NULL,
    opened_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id   TEXT NOT NULL,
    role       TEXT NOT NULL,          -- user | assistant
    content    TEXT NOT NULL,
    context    TEXT NOT NULL DEFAULT '', -- quoted selection, if any
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_paper ON messages(paper_id, id);
CREATE INDEX IF NOT EXISTS idx_papers_opened ON papers(opened_at DESC);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DB_PATH), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    with _conn() as conn:
        conn.executescript(SCHEMA)


def slugify(text: str, limit: int = 48) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return (text[:limit].strip("-") or "paper")


def new_id(title: str) -> str:
    return "%s-%d" % (slugify(title), int(time.time() * 1000) % 100000)


def paper_dir(paper_id: str) -> Path:
    d = config.PAPERS_DIR / paper_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def pdf_path(paper_id: str) -> Path:
    return paper_dir(paper_id) / "paper.pdf"


# --------------------------------------------------------------------------- papers


def upsert_paper(**fields: Any) -> Dict[str, Any]:
    now = time.time()
    pid = fields.get("id") or new_id(fields.get("title", "paper"))
    cols = {
        "id": pid,
        "title": fields.get("title", "") or "",
        "authors": fields.get("authors", "") or "",
        "abstract": fields.get("abstract", "") or "",
        "venue": fields.get("venue", "") or "",
        "year": str(fields.get("year", "") or ""),
        "source": fields.get("source", "local") or "local",
        "url": fields.get("url", "") or "",
        "pdf_url": fields.get("pdf_url", "") or "",
        "path": fields.get("path", "") or "",
        "n_pages": int(fields.get("n_pages", 0) or 0),
        "readable": 1 if fields.get("readable", True) else 0,
    }
    with _conn() as conn:
        existing = conn.execute("SELECT id, added_at FROM papers WHERE id=?", (pid,)).fetchone()
        if existing:
            sets = ", ".join("%s=?" % k for k in cols if k != "id")
            conn.execute(
                "UPDATE papers SET %s WHERE id=?" % sets,
                [cols[k] for k in cols if k != "id"] + [pid],
            )
            added_at = existing["added_at"]
        else:
            cols["added_at"] = now
            cols["opened_at"] = now
            conn.execute(
                "INSERT INTO papers (%s) VALUES (%s)"
                % (", ".join(cols), ", ".join("?" * len(cols))),
                list(cols.values()),
            )
            added_at = now
    rec = get_paper(pid)
    if rec:
        rec["added_at"] = added_at
    return rec or {}


def get_paper(paper_id: str) -> Optional[Dict[str, Any]]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    return _row(row) if row else None


def by_pdf_url(pdf_url: str) -> Optional[Dict[str, Any]]:
    if not pdf_url:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM papers WHERE pdf_url=? ORDER BY opened_at DESC LIMIT 1", (pdf_url,)
        ).fetchone()
    return _row(row) if row else None


def by_path(path: str) -> Optional[Dict[str, Any]]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM papers WHERE path=? ORDER BY opened_at DESC LIMIT 1", (path,)
        ).fetchone()
    return _row(row) if row else None


def list_papers(limit: int = 200, query: str = "") -> List[Dict[str, Any]]:
    sql = "SELECT * FROM papers"
    args: List[Any] = []
    if query:
        sql += " WHERE title LIKE ? OR authors LIKE ? OR abstract LIKE ?"
        like = "%" + query + "%"
        args += [like, like, like]
    sql += " ORDER BY opened_at DESC LIMIT ?"
    args.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row(r) for r in rows]


def touch(paper_id: str, page: Optional[int] = None) -> None:
    with _conn() as conn:
        if page is None:
            conn.execute("UPDATE papers SET opened_at=? WHERE id=?", (time.time(), paper_id))
        else:
            conn.execute(
                "UPDATE papers SET opened_at=?, last_page=? WHERE id=?",
                (time.time(), int(page), paper_id),
            )


def delete_paper(paper_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM papers WHERE id=?", (paper_id,))
        conn.execute("DELETE FROM messages WHERE paper_id=?", (paper_id,))
    d = config.PAPERS_DIR / paper_id
    if d.exists():
        for child in d.iterdir():
            try:
                child.unlink()
            except OSError:
                pass
        try:
            d.rmdir()
        except OSError:
            pass


def _row(row: sqlite3.Row) -> Dict[str, Any]:
    rec = dict(row)
    rec["readable"] = bool(rec.get("readable", 1))
    rec["has_pdf"] = bool(rec.get("path")) and Path(rec["path"]).exists()
    return rec


# --------------------------------------------------------------------------- messages


def add_message(
    paper_id: str, role: str, content: str, context: str = ""
) -> Dict[str, Any]:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (paper_id, role, content, context, created_at) VALUES (?,?,?,?,?)",
            (paper_id, role, content, context or "", time.time()),
        )
        mid = cur.lastrowid
    return {
        "id": mid,
        "paper_id": paper_id,
        "role": role,
        "content": content,
        "context": context or "",
    }


def get_messages(paper_id: str, limit: int = 500) -> List[Dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE paper_id=? ORDER BY id ASC LIMIT ?",
            (paper_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_messages(paper_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM messages WHERE paper_id=?", (paper_id,))


# --------------------------------------------------------------------------- misc

TRANSLATION_CACHE = "translation.json"


def load_translation(paper_id: str) -> Dict[str, Any]:
    p = paper_dir(paper_id) / TRANSLATION_CACHE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_translation(paper_id: str, page_key: str, blocks: List[Dict[str, Any]]) -> None:
    data = load_translation(paper_id)
    data[page_key] = blocks
    tmp = paper_dir(paper_id) / (TRANSLATION_CACHE + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(paper_dir(paper_id) / TRANSLATION_CACHE)
