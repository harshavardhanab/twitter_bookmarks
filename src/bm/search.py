"""Hybrid search: FTS5 keyword ranking fused with vector similarity (RRF)."""

from __future__ import annotations

import json
import re
import sqlite3
import sys

from . import db, translate

RRF_K = 60
SNIPPET = 240
TOKEN_RE = re.compile(r"\w+", re.UNICODE)


# Ignored when measuring how many query terms a post matches (they still match)
STOPWORDS = frozenset(
    "a an and are as at be by did do does for from how i in is it me my of on or that the "
    "this to was what when where which who why with you your about saved save bookmarked "
    "liked".split()
)


def _term(token: str) -> str:
    """One query word as an FTS5 string; CJK runs become a phrase of single characters."""
    if db.CJK_RE.search(token):
        token = db.cjk_split(token).replace(db.ZWSP, " ")
    return f'"{token}"'


def terms(q: str) -> list[str]:
    """Distinct query words as quoted FTS5 strings, in order."""
    out: list[str] = []
    for t in TOKEN_RE.findall(q):
        term = _term(t)
        if term not in out:
            out.append(term)
    return out


def fts_query(q: str, raw: bool = False) -> str | None:
    """Free text to an FTS5 query: quoted terms joined by OR (raw passes it through)."""
    if raw:
        q = db.cjk_split(q.strip()).replace(db.ZWSP, " ")
        return q or None
    return " OR ".join(terms(q)) or None


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


def _match_ids(conn, query: str, ids: list[int]) -> set[int]:
    marks = ",".join("?" * len(ids))
    return {r[0] for r in conn.execute(
        f"SELECT rowid FROM items_fts WHERE items_fts MATCH ? AND rowid IN ({marks})",
        (query, *ids))}


def keyword(conn, q, n=50, raw=False, **filters) -> list[tuple[int, str, float]]:
    """[(item id, snippet, weight)] best first.

    Free-text queries are ranked by: exact phrase match, then the share of the query's
    words the post contains (stopwords aside), then bm25. `weight` is that share (1.0 for
    a phrase or full match) and scales the keyword vote in hybrid ranking, so a post that
    shares one word with the query counts for little.
    """
    query = fts_query(q, raw)
    if not query:
        return []
    where, args = _filters(**filters)
    sql = (
        "SELECT f.rowid AS id, snippet(items_fts, -1, '', '', '…', 40) AS snip "
        "FROM items_fts f JOIN items i ON i.id = f.rowid "
        f"WHERE items_fts MATCH ?{where} "
        "ORDER BY bm25(items_fts, 5, 3, 2, 1) LIMIT ?"
    )
    ts = [] if raw else terms(q)
    if len(ts) < 2:
        rows = conn.execute(sql, (query, *args, n)).fetchall()
        return [(r["id"], db.cjk_join(r["snip"]), 1.0) for r in rows]

    # Candidates: posts with every word (so none are cut off by the OR list), then the OR list
    cand: dict[int, tuple[int, str]] = {}  # id -> (bm25 order, snippet)
    for qq, lim in ((" AND ".join(ts), n), (query, n * 3)):
        for r in conn.execute(sql, (qq, *args, lim)).fetchall():
            cand.setdefault(r["id"], (len(cand), r["snip"]))
    ids = list(cand)
    if not ids:
        return []
    content = [t for t in ts if t.strip('"').lower() not in STOPWORDS] or ts
    hits = {t: _match_ids(conn, t, ids) for t in content}
    phrase = _match_ids(conn, '"' + " ".join(t.strip('"') for t in ts) + '"', ids)

    def weight(i: int) -> float:
        return 1.0 if i in phrase else sum(i in hits[t] for t in content) / len(content)

    ranked = sorted(ids, key=lambda i: (i not in phrase, -weight(i), cand[i][0]))
    return [(i, db.cjk_join(cand[i][1]), weight(i)) for i in ranked[:n]]


def semantic(conn, q, n=50, **filters) -> list[tuple[int, str]]:
    """[(item id, best chunk text)] nearest first. Needs vectors (bm embed).

    With filters, distances are computed over the matching posts' chunks only, so a
    filter can never empty the result by cutting the nearest-neighbour list short.
    """
    from . import embed

    embed.load_vec(conn)
    qv = embed.query_vector(q)
    if any(filters.values()):
        where, args = _filters(**filters)
        rows = conn.execute(
            "SELECT c.item_id, c.text, vec_distance_l2(v.embedding, ?) AS distance "
            "FROM chunks c JOIN vec_chunks v ON v.rowid = c.id "
            f"WHERE c.item_id IN (SELECT i.id FROM items i WHERE 1 = 1{where}) "
            "ORDER BY distance LIMIT ?",
            (qv, *args, n * 8),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT c.item_id, c.text, v.distance FROM vec_chunks v "
            "JOIN chunks c ON c.id = v.rowid WHERE v.embedding MATCH ? AND k = ? "
            "ORDER BY v.distance",
            (qv, min(n * 4, 4096)),
        ).fetchall()
    best: dict[int, str] = {}
    for r in rows:
        best.setdefault(r["item_id"], r["text"])
    return list(best.items())[:n]


def result(conn, item_id: int, snippet: str, score: float) -> dict:
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    meta = json.loads(row["meta"] or "{}")
    title = row["title"] or meta.get("story_title")
    if not title and row["link_url"]:
        # Title of the main linked page (not of other links, which would mislabel the post)
        lt = conn.execute("SELECT title FROM links WHERE url = ?", (row["link_url"],)).fetchone()
        title = lt["title"] if lt else None
    snippet = snippet or row["text"]
    if row["source"] == "x":
        language = None
        if snippet and snippet.strip(" …") in (row["text"] or ""):
            language = translate.tweet_language(row["raw"])
        title, snippet = translate.english(title), translate.english(snippet, language)
    return {
        "id": db.public_id(row),
        "source": row["source"],
        "lists": db.item_lists(conn, item_id),
        "kind": row["kind"],
        "author": row["author"],
        "created_at": row["created_at"],
        "title": title,
        "snippet": _clip(snippet),
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
            problem = embed.not_ready(conn)
            if problem:
                raise RuntimeError(problem)
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
        ranked = [(i, s, 1.0 / (RRF_K + r)) for r, (i, s, _) in enumerate(kw, 1)]
    elif mode == "semantic":
        ranked = [(i, s, 1.0 / (RRF_K + r)) for r, (i, s) in enumerate(sem or [], 1)]
    else:
        scores: dict[int, float] = {}
        snips: dict[int, str] = {}
        for r, (i, s, w) in enumerate(kw, 1):
            scores[i] = scores.get(i, 0) + w / (RRF_K + r)
            snips[i] = s  # keyword snippet shows the matching words
        for r, (i, s) in enumerate(sem or [], 1):
            scores[i] = scores.get(i, 0) + 1.0 / (RRF_K + r)
            snips.setdefault(i, s)
        ranked = sorted(((i, snips[i], sc) for i, sc in scores.items()), key=lambda t: -t[2])

    out = {"mode": mode, "results": [result(conn, i, s, sc) for i, s, sc in ranked[:limit]]}
    if note:
        out["note"] = note
    return out
