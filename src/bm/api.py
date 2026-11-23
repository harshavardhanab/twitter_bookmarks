"""Read-side operations shared by the CLI and the MCP server."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from . import db

RECENT_ORDER = "first_seen_at DESC, CAST(bookmark_rank AS INTEGER) DESC, id DESC"


def _summary(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    meta = json.loads(row["meta"] or "{}")
    text = re.sub(r"\s+", " ", row["text"] or "").strip()
    return {
        "id": db.public_id(row),
        "source": row["source"],
        "lists": db.item_lists(conn, row["id"]),
        "kind": row["kind"],
        "author": row["author"],
        "created_at": row["created_at"],
        "saved_at": row["first_seen_at"],
        "title": row["title"] or meta.get("story_title"),
        "text": text[:240] + ("…" if len(text) > 240 else ""),
        "url": row["url"],
        "link_url": row["link_url"],
    }


def get_item(conn: sqlite3.Connection, ref: str, max_chars: int = 4000) -> dict | None:
    """Full item plus its linked pages; `max_chars` caps the linked page text in total."""
    row = db.find_item(conn, ref.strip())
    if row is None:
        return None
    meta = json.loads(row["meta"] or "{}")
    links, budget = [], max_chars
    for lr in conn.execute(
        "SELECT l.* FROM links l JOIN item_links il ON il.link_id = l.id "
        "WHERE il.item_id = ? ORDER BY l.id",
        (row["id"],),
    ):
        content = lr["content"] or ""
        take = content[: max(budget, 0)]
        budget -= len(take)
        links.append({
            "url": lr["url"],
            "final_url": lr["final_url"],
            "title": lr["title"],
            "status": lr["status"],
            "error": lr["error"],
            "fetched_at": lr["fetched_at"],
            "content": take or None,
            "content_truncated": len(take) < len(content),
            "content_chars": len(content),
            "snapshot_path": lr["snapshot_path"],
        })
    return {
        "id": db.public_id(row),
        "source": row["source"],
        "lists": db.item_lists(conn, row["id"]),
        "kind": row["kind"],
        "author": row["author"],
        "author_name": row["author_name"],
        "created_at": row["created_at"],
        "saved_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "title": row["title"] or meta.get("story_title"),
        "text": row["text"],
        "url": row["url"],
        "link_url": row["link_url"],
        "meta": meta,
        "links": links,
    }


def recent(
    conn: sqlite3.Connection, n: int = 20, source: str | None = None, in_list: str | None = None
) -> list[dict]:
    """Most recently saved first. With `in_list`, in that list's own order
    (e.g. most recently liked first for 'like')."""
    if in_list:
        where, args = "WHERE l.list = ?", [in_list]
        if source:
            where += " AND i.source = ?"
            args.append(source)
        rows = conn.execute(
            f"SELECT i.* FROM items i JOIN item_lists l ON l.item_id = i.id {where} "
            "ORDER BY l.first_seen_at DESC, CAST(l.rank AS INTEGER) DESC, i.id DESC LIMIT ?",
            (*args, n),
        ).fetchall()
    else:
        where, args = ("WHERE source = ?", [source]) if source else ("", [])
        rows = conn.execute(
            f"SELECT * FROM items {where} ORDER BY {RECENT_ORDER} LIMIT ?", (*args, n)
        ).fetchall()
    return [_summary(conn, r) for r in rows]


def stats(conn: sqlite3.Connection) -> dict:
    by_source = {}
    for r in conn.execute(
        "SELECT source, kind, count(*) n, min(created_at) oldest, max(created_at) newest "
        "FROM items GROUP BY source, kind ORDER BY source, kind"
    ):
        s = by_source.setdefault(r["source"], {"total": 0, "kinds": {}})
        s["total"] += r["n"]
        s["kinds"][r["kind"]] = r["n"]
        s["oldest"] = min(filter(None, [s.get("oldest"), r["oldest"]]), default=None)
        s["newest"] = max(filter(None, [s.get("newest"), r["newest"]]), default=None)
    links = conn.execute(
        "SELECT count(*) total, "
        "sum(fetched_at IS NULL) pending, "
        "sum(fetched_at IS NOT NULL AND error IS NULL AND content IS NOT NULL) with_text, "
        "sum(error IS NOT NULL) failed, "
        "sum(snapshot_path IS NOT NULL) snapshots FROM links"
    ).fetchone()
    settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    path = db.home() / "bookmarks.db"
    lists = {r[0]: r[1] for r in conn.execute(
        "SELECT list, count(*) FROM item_lists GROUP BY list ORDER BY list")}
    both = conn.execute(
        "SELECT count(*) FROM (SELECT item_id FROM item_lists WHERE list IN ('bookmark', 'like') "
        "GROUP BY item_id HAVING count(*) = 2)").fetchone()[0]
    return {
        "items": sum(s["total"] for s in by_source.values()),
        "sources": by_source,
        "lists": lists,
        "x_bookmarked_and_liked": both,
        "links": {k: links[k] or 0 for k in links.keys()},
        "embedded_items": conn.execute("SELECT count(*) FROM item_embed").fetchone()[0],
        "chunks": conn.execute("SELECT count(*) FROM chunks").fetchone()[0],
        "last_sync": {k.removeprefix("last_sync_"): v for k, v in settings.items()
                      if k.startswith("last_sync_")},
        "hn_user": settings.get("hn_user"),
        "x_user": settings.get("x_user"),
        "db_path": str(path),
        "db_mb": round(path.stat().st_size / 2**20, 2) if path.exists() else 0,
    }


def to_markdown(item: dict) -> str:
    """One bookmark as a Markdown document."""
    def fm(v):
        return json.dumps(v, ensure_ascii=False) if v is not None else '""'

    head = {
        "id": item["id"], "source": item["source"], "lists": item["lists"], "kind": item["kind"],
        "author": item["author"], "created_at": item["created_at"],
        "saved_at": item["saved_at"], "url": item["url"], "link_url": item["link_url"],
    }
    out = ["---", *(f"{k}: {fm(v)}" for k, v in head.items()), "---", ""]
    title = item["title"] or (f"@{item['author']}" if item["source"] == "x" else item["id"])
    out += [f"# {title}", ""]
    who = item["author"] + (f" ({item['author_name']})" if item.get("author_name") else "") if item["author"] else ""
    out += [f"*{who}, {item['created_at'] or ''}* · [source]({item['url']})", ""]
    if item["text"]:
        out += [item["text"], ""]
    meta = item["meta"]
    if meta.get("quoted"):
        q = meta["quoted"]
        quoted = (q.get("text") or "").replace("\n", "\n> ")
        out += [f"> Quoting @{q.get('author')}: {quoted}", ""]
    if meta.get("article"):
        out += [f"**Article:** {meta['article'].get('title') or ''}", "",
                meta["article"].get("preview_text") or "", ""]
    for m in meta.get("media") or []:
        out.append(f"- {m.get('type')}: {m.get('url')}")
    if meta.get("media"):
        out.append("")
    for link in item["links"]:
        out += [f"## Link: {link['title'] or link['final_url'] or link['url']}", "",
                f"<{link['final_url'] or link['url']}>", ""]
        if link["content"]:
            out += [link["content"], ""]
            if link["content_truncated"]:
                out += ["*(truncated)*", ""]
        elif link["error"]:
            out += [f"*(not fetched: {link['error']})*", ""]
    return "\n".join(out).rstrip() + "\n"


def export_md(conn: sqlite3.Connection, outdir: str, max_chars: int = 20000) -> int:
    """Write one .md per bookmark (`x-<id>.md`, `hn-<id>.md`). Returns the count."""
    out = Path(outdir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for (ref,) in conn.execute(f"SELECT source || ':' || source_id FROM items ORDER BY {RECENT_ORDER}"):
        item = get_item(conn, ref, max_chars=max_chars)
        (out / f"{ref.replace(':', '-')}.md").write_text(to_markdown(item), encoding="utf-8")
        n += 1
    return n
