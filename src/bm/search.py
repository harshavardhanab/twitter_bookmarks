"""Hybrid search: FTS5 keyword ranking fused with vector similarity (RRF)."""

from __future__ import annotations

import json
import re
import sqlite3
import sys

from . import db

RRF_K = 60
SNIPPET = 240
TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def fts_query(q: str, raw: bool = False) -> str | None:
    """Free text to an FTS5 query: quoted tokens joined by OR (raw passes it through)."""
    if raw:
        return q.strip() or None
    tokens = TOKEN_RE.findall(q)
    return " OR ".join(f'"{t}"' for t in tokens) or None


def _filters(source=None, since=None, until=None, author=None, in_list=None) -> tuple[str, list]:
    where, args = [], []
    if in_list:
        where.append("EXISTS (SELECT 1 FROM item_lists l WHERE l.item_id = i.id AND l.list = ?)")
        args.append(in_list)
    if source:
        where.append("i.source = ?")
        args.append(source)
    if since:
        where.append("i.created_at >= ?")
        args.append(since)
    if until:
        # Prefix compare makes a bare date inclusive: 2024-03-05 keeps 2024-03-05T23:00Z
        where.append("substr(i.created_at, 1, ?) <= ?")
        args += [len(until), until]
    if author:
        a = author.lstrip("@").lower()
        where.append("(lower(i.author) = ? OR lower(i.author_name) LIKE ?)")
        args += [a, f"%{a}%"]
    return (" AND " + " AND ".join(where)) if where else "", args


def _clip(s: str | None, n: int = SNIPPET) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def keyword(conn, q, n=50, raw=False, **filters) -> list[tuple[int, str]]:
    """[(item id, snippet)] best first."""
    query = fts_query(q, raw)
    if not query:
        return []
    where, args = _filters(**filters)
    rows = conn.execute(
        "SELECT f.rowid AS id, snippet(items_fts, -1, '', '', '…', 40) AS snip "
        "FROM items_fts f JOIN items i ON i.id = f.rowid "
        f"WHERE items_fts MATCH ?{where} "
        "ORDER BY bm25(items_fts, 5, 3, 2, 1) LIMIT ?",
        (query, *args, n),
    ).fetchall()
    return [(r["id"], r["snip"]) for r in rows]


def semantic(conn, q, n=50, **filters) -> list[tuple[int, str]]:
    """[(item id, best chunk text)] nearest first. Needs vectors (bm embed)."""
    from . import embed

    embed.load_vec(conn)
    filtered = any(filters.values())
    k = n * 8 if filtered else n * 3  # several chunks per item; filters drop some
    rows = conn.execute(
        "SELECT v.rowid AS cid, v.distance, c.item_id, c.text FROM vec_chunks v "
        "JOIN chunks c ON c.id = v.rowid WHERE v.embedding MATCH ? AND k = ? "
        "ORDER BY v.distance",
        (embed.query_vector(q), min(k, 4096)),
    ).fetchall()
    best: dict[int, str] = {}
    for r in rows:
        best.setdefault(r["item_id"], r["text"])
    ids = list(best)
    if filtered and ids:
        where, args = _filters(**filters)
        marks = ",".join("?" * len(ids))
        keep = {r[0] for r in conn.execute(
            f"SELECT i.id FROM items i WHERE i.id IN ({marks}){where}", (*ids, *args))}
        ids = [i for i in ids if i in keep]
    return [(i, best[i]) for i in ids[:n]]


def result(conn, item_id: int, snippet: str, score: float) -> dict:
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    meta = json.loads(row["meta"] or "{}")
    title = row["title"] or meta.get("story_title")
    if not title and row["link_url"]:
        # Title of the main linked page (not of other links, which would mislabel the post)
        lt = conn.execute("SELECT title FROM links WHERE url = ?", (row["link_url"],)).fetchone()
        title = lt["title"] if lt else None
    return {
        "id": db.public_id(row),
        "source": row["source"],
        "lists": db.item_lists(conn, item_id),
        "kind": row["kind"],
        "author": row["author"],
        "created_at": row["created_at"],
        "title": title,
        "snippet": _clip(snippet or row["text"]),
        "url": row["url"],
        "link_url": row["link_url"],
        "score": round(score, 4),
    }


def search(
    conn: sqlite3.Connection,
    q: str,
    mode: str = "hybrid",
    limit: int = 10,
    raw: bool = False,
    source: str | None = None,
    since: str | None = None,
    until: str | None = None,
    author: str | None = None,
    in_list: str | None = None,
) -> dict:
    """Search bookmarks. Returns {"mode": mode actually used, "results": [...]}.

    `in_list` keeps only items on one list: 'bookmark' | 'like' (X) or 'favorite' (HN).
    """
    filters = dict(source=source, since=since, until=until, author=author, in_list=in_list)
    depth = max(50, limit * 5)
    note = None

    if mode in ("hybrid", "semantic"):
        from . import embed

        try:
            embed.load_vec(conn)
            if not embed.has_vectors(conn):
                raise RuntimeError("no embeddings yet (run: bm embed)")
            sem = semantic(conn, q, depth, **filters)
        except Exception as e:  # missing model, no network, sqlite-vec unavailable
            note = f"semantic search unavailable, keyword only: {e}"
            print(f"bm: {note}", file=sys.stderr)
            sem, mode = None, "keyword"
    else:
        sem = None

    try:
        kw = keyword(conn, q, depth, raw, **filters) if mode in ("hybrid", "keyword") else []
    except sqlite3.OperationalError as e:
        if not raw:
            raise
        raise ValueError(f"bad FTS query: {e}") from e

    if mode == "keyword":
        ranked = [(i, s, 1.0 / (RRF_K + r)) for r, (i, s) in enumerate(kw, 1)]
    elif mode == "semantic":
        ranked = [(i, s, 1.0 / (RRF_K + r)) for r, (i, s) in enumerate(sem or [], 1)]
    else:
        scores: dict[int, float] = {}
        snips: dict[int, str] = {}
        for r, (i, s) in enumerate(kw, 1):
            scores[i] = scores.get(i, 0) + 1.0 / (RRF_K + r)
            snips[i] = s  # keyword snippet shows the matching words
        for r, (i, s) in enumerate(sem or [], 1):
            scores[i] = scores.get(i, 0) + 1.0 / (RRF_K + r)
            snips.setdefault(i, s)
        ranked = sorted(((i, snips[i], sc) for i, sc in scores.items()), key=lambda t: -t[2])

    out = {"mode": mode, "results": [result(conn, i, s, sc) for i, s, sc in ranked[:limit]]}
    if note:
        out["note"] = note
    return out
