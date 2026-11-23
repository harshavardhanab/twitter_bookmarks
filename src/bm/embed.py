"""Chunk items and store their embeddings in a sqlite-vec table."""

from __future__ import annotations

import hashlib
import sqlite3
from functools import lru_cache
from typing import Callable, Iterable

from . import db

MODEL = "BAAI/bge-small-en-v1.5"
DIM = 384
CHUNK = 1200
OVERLAP = 200
MAX_CHUNKS = 8
CONTENT_CHARS = 6000
BATCH = 32


def load_vec(conn: sqlite3.Connection) -> None:
    """Load sqlite-vec into this connection and create the vector table."""
    import sqlite_vec

    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[{DIM}])"
    )


@lru_cache(maxsize=1)
def model():
    """The embedding model, loaded once per process (first run downloads it)."""
    from fastembed import TextEmbedding

    cache = db.home() / "models"
    cache.mkdir(exist_ok=True)
    return TextEmbedding(MODEL, cache_dir=str(cache))


def to_blob(vec) -> bytes:
    import numpy as np

    return np.asarray(vec, dtype=np.float32).tobytes()


def query_vector(q: str) -> bytes:
    return to_blob(next(iter(model().query_embed(q))))


def document(conn: sqlite3.Connection, item_id: int) -> str:
    """Text to embed: title, author, text, link titles, start of the linked pages."""
    doc = db.item_document(conn, item_id)
    rows = conn.execute(
        "SELECT l.title, l.content FROM links l JOIN item_links il ON il.link_id = l.id "
        "WHERE il.item_id = ? ORDER BY l.id",
        (item_id,),
    ).fetchall()
    titles = [r["title"] for r in rows if r["title"]]
    content, budget = [], CONTENT_CHARS
    for r in rows:
        if budget <= 0:
            break
        if r["content"]:
            content.append(r["content"][:budget])
            budget -= len(content[-1])
    parts = [doc["title"], doc["author"] and f"by {doc['author']}", doc["text"], *titles, *content]
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def chunk(text: str) -> list[str]:
    """~1200-char chunks with 200 overlap, cut at whitespace where possible, max 8."""
    text = text.strip()
    if len(text) <= CHUNK:
        return [text] if text else []
    out, start = [], 0
    while start < len(text) and len(out) < MAX_CHUNKS:
        end = min(start + CHUNK, len(text))
        if end < len(text):
            cut = text.rfind(" ", start + CHUNK // 2, end)
            if cut > 0:
                end = cut
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - OVERLAP, start + 1)
        sp = text.find(" ", start, end)
        if 0 <= sp < start + 50:
            start = sp + 1
    return [c for c in out if c]


def _hash(doc: str) -> str:
    return hashlib.sha256(f"{MODEL}\n{CHUNK}/{OVERLAP}/{MAX_CHUNKS}\n{doc}".encode()).hexdigest()


def _batched(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def embed_all(conn: sqlite3.Connection, log: Callable[[str], None] = print) -> dict:
    """(Re)embed every item whose document changed since its last embedding."""
    load_vec(conn)
    known = dict(conn.execute("SELECT item_id, hash FROM item_embed").fetchall())
    todo = []  # (item_id, hash, chunks)
    for (item_id,) in conn.execute("SELECT id FROM items ORDER BY id").fetchall():
        doc = document(conn, item_id)
        h = _hash(doc)
        if known.get(item_id) != h:
            todo.append((item_id, h, chunk(doc)))
    stats = {"items": len(todo), "chunks": sum(len(c) for _, _, c in todo)}
    if not todo:
        log("embed: up to date")
        return stats

    log(f"embed: {stats['items']} items, {stats['chunks']} chunks ({MODEL})")
    m = model()
    done = 0
    for group in _batched(todo, BATCH):
        texts = [c for _, _, chunks in group for c in chunks]
        vectors = list(m.embed(texts, batch_size=BATCH)) if texts else []
        vi = 0
        for item_id, h, chunks in group:
            old = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE item_id = ?", (item_id,))]
            if old:
                marks = ",".join("?" * len(old))
                conn.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({marks})", old)
                conn.execute(f"DELETE FROM chunks WHERE id IN ({marks})", old)
            for text in chunks:
                cid = conn.execute(
                    "INSERT INTO chunks (item_id, text) VALUES (?, ?)", (item_id, text)
                ).lastrowid
                conn.execute(
                    "INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
                    (cid, to_blob(vectors[vi])),
                )
                vi += 1
            conn.execute(
                "INSERT INTO item_embed (item_id, hash) VALUES (?, ?) "
                "ON CONFLICT (item_id) DO UPDATE SET hash = excluded.hash",
                (item_id, h),
            )
        conn.commit()
        done += len(group)
        if len(todo) > BATCH:
            log(f"  {done}/{len(todo)}")
    log(f"embed: {stats['items']} items embedded")
    return stats


def has_vectors(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT EXISTS (SELECT 1 FROM chunks)").fetchone()[0] == 1

