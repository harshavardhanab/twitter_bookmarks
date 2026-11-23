"""One-way, insert-only import from a Siftly database (prisma/dev.db).

Siftly stores a reduced copy of each tweet: no author ("unknown"), and mostly
unexpanded t.co links. So this only *adds* tweets that bm doesn't already have on
that list, never overwrites anything captured from X, and marks what it adds with
meta.imported_from = "siftly". A later `bm x sync --full` that reaches the same
tweet replaces the Siftly copy with the full X data.
"""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import db
from .x import _is_internal

# Siftly Bookmark.source -> bm list
LISTS = {"bookmarks": ("bookmark", "bookmark"), "likes": ("like", "like")}


class SiftlyError(RuntimeError):
    pass


def _dt(v) -> datetime | None:
    """Prisma DateTime: text ('2026-03-12T15:41:56.050+00:00') or epoch milliseconds."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            return datetime.fromtimestamp(int(v) / 1000, timezone.utc)
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _post_date(v) -> str | None:  # same format as tweets captured from X
    dt = _dt(v)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _seen(v) -> str | None:  # same format as db.now()
    dt = _dt(v)
    return dt.replace(microsecond=0).isoformat() if dt else None


def _item(row: sqlite3.Row, media: list[dict], list_name: str) -> dict:
    raw = json.loads(row["rawJson"] or "{}")
    text = html.unescape(row["text"] or raw.get("full_text") or "")
    links = []
    for u in ((raw.get("entities") or {}).get("urls") or []):
        full = u.get("expanded_url")
        if full and not _is_internal(full) and full not in links:
            links.append(full)
    handle = row["authorHandle"] if row["authorHandle"] not in (None, "", "unknown") else None
    name = row["authorName"] if row["authorName"] not in (None, "", "Unknown") else None
    meta = {"imported_from": "siftly"}
    if media:
        meta["media"] = media
    seen = _seen(row["importedAt"])
    return {
        "source": "x",
        "source_id": str(row["tweetId"]),
        "kind": "tweet",
        "url": f"https://x.com/{handle or 'i'}/status/{row['tweetId']}",
        "link_url": links[0] if links else None,
        "title": None,
        "text": text,
        "author": handle,
        "author_name": name,
        "created_at": _post_date(row["tweetCreatedAt"]),
        "meta": meta,
        "raw": {"siftly": raw},
        "links": links,
        "list": list_name,
        "seen_at": seen,
    }


def import_db(
    conn: sqlite3.Connection,
    path: str,
    what: str = "bookmarks",
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> dict:
    """Add Siftly bookmarks (and/or likes) that bm lacks. Returns counts per list."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise SiftlyError(f"{p}: no such Siftly database")
    src = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        src.execute("SELECT tweetId, source, rawJson FROM Bookmark LIMIT 1")
    except sqlite3.Error as e:
        raise SiftlyError(f"{p}: not a Siftly database ({e})") from e

    media: dict[str, list[dict]] = {}
    for m in src.execute("SELECT bookmarkId, type, url, thumbnailUrl FROM MediaItem"):
        entry = {"type": m["type"], "url": m["url"]}
        if m["thumbnailUrl"] and m["thumbnailUrl"] != m["url"]:
            entry["poster"] = m["thumbnailUrl"]
        media.setdefault(m["bookmarkId"], []).append(entry)

    stats = {}
    for w in (("bookmarks", "likes") if what == "all" else (what,)):
        siftly_source, list_name = LISTS[w]
        on_list = {r[0] for r in conn.execute(
            "SELECT i.source_id FROM items i JOIN item_lists l ON l.item_id = i.id "
            "WHERE i.source = 'x' AND l.list = ?", (list_name,))}
        existing = {r[0]: r[1] for r in conn.execute("SELECT source_id, id FROM items WHERE source = 'x'")}
        rows = src.execute(
            "SELECT * FROM Bookmark WHERE source = ? ORDER BY importedAt", (siftly_source,)
        ).fetchall()
        s = {"in_siftly": len(rows), "already": 0, "added": 0, "list_only": 0}
        for row in rows:
            tid = str(row["tweetId"])
            if tid in on_list:
                s["already"] += 1
                continue
            if dry_run:
                s["list_only" if tid in existing else "added"] += 1
                continue
            if tid in existing:
                # Tweet captured from X for another list: add the membership, keep X's data
                ts = _seen(row["importedAt"]) or db.now()
                conn.execute(
                    "INSERT OR IGNORE INTO item_lists (item_id, list, rank, first_seen_at, last_seen_at) "
                    "VALUES (?, ?, NULL, ?, ?)", (existing[tid], list_name, ts, ts))
                s["list_only"] += 1
            else:
                db.upsert_item(conn, _item(row, media.get(row["id"], []), list_name))
                s["added"] += 1
        conn.commit()
        stats[w] = s
        verb = "would add" if dry_run else "added"
        log(
            f"siftly {w}: {s['in_siftly']} in Siftly, {s['already']} already in bm, "
            f"{verb} {s['added']} new tweets"
            + (f" and {s['list_only']} list entries for tweets bm already has" if s["list_only"] else "")
        )
    src.close()
    return stats
