"""SQLite storage: items, linked pages, full-text index and settings.

Nothing is ever deleted. An item that disappears from a source keeps its row;
`last_seen_at` shows when a sync last saw it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id            INTEGER PRIMARY KEY,
  source        TEXT NOT NULL,          -- 'x' | 'hn'
  source_id     TEXT NOT NULL,
  kind          TEXT,                   -- tweet | story | comment
  url           TEXT,                   -- permalink on the source
  link_url      TEXT,                   -- main external link (HN story URL)
  title         TEXT,
  text          TEXT,
  author        TEXT,
  author_name   TEXT,
  created_at    TEXT,                   -- ISO 8601 UTC
  bookmark_rank TEXT,                   -- X sortIndex: higher = bookmarked later
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  meta          TEXT,                   -- JSON: counts, quoted tweet, media, parent story
  raw           TEXT,                   -- source payload as received
  UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS items_seen ON items (first_seen_at);

CREATE TABLE IF NOT EXISTS links (
  id            INTEGER PRIMARY KEY,
  url           TEXT NOT NULL UNIQUE,
  final_url     TEXT,
  status        INTEGER,
  content_type  TEXT,
  title         TEXT,
  content       TEXT,
  error         TEXT,
  fetched_at    TEXT,
  snapshot_path TEXT
);

CREATE TABLE IF NOT EXISTS item_links (
  item_id INTEGER NOT NULL REFERENCES items(id),
  link_id INTEGER NOT NULL REFERENCES links(id),
  PRIMARY KEY (item_id, link_id)
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

-- Which lists an item is on: X 'bookmark' / 'like', HN 'favorite'. One item
-- (x:<id>) can be on several. rank = the list's sortIndex (higher = added later).
CREATE TABLE IF NOT EXISTS item_lists (
  item_id       INTEGER NOT NULL REFERENCES items(id),
  list          TEXT NOT NULL,
  rank          TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  PRIMARY KEY (item_id, list)
);
CREATE INDEX IF NOT EXISTS item_lists_list ON item_lists (list, first_seen_at);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, text, author, links, tokenize = 'porter unicode61'
);

-- Semantic search bookkeeping; the vector table itself is created by embed.py
CREATE TABLE IF NOT EXISTS chunks (
  id      INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_item ON chunks (item_id);
CREATE TABLE IF NOT EXISTS item_embed (item_id INTEGER PRIMARY KEY, hash TEXT NOT NULL);
"""


def home() -> Path:
    """Data directory.

    $BM_HOME if set; else <repo>/data when running from a source checkout (editable
    install); else $XDG_DATA_HOME/bm (default ~/.local/share/bm).
    """
    env = os.environ.get("BM_HOME")
    if env:
        path = Path(env).expanduser()
    else:
        repo = Path(__file__).resolve().parents[2]
        if (repo / "pyproject.toml").is_file() and (repo / "src" / "bm").is_dir():
            path = repo / "data"
        else:
            base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
            path = Path(base).expanduser() / "bm"
    path.mkdir(parents=True, exist_ok=True)
    return path


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(home() / "bookmarks.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    # Items stored before lists existed: X rows were bookmarks, HN rows favorites
    conn.execute(
        "INSERT OR IGNORE INTO item_lists (item_id, list, rank, first_seen_at, last_seen_at) "
        "SELECT id, CASE source WHEN 'hn' THEN 'favorite' ELSE 'bookmark' END, bookmark_rank, "
        "first_seen_at, last_seen_at FROM items "
        "WHERE NOT EXISTS (SELECT 1 FROM item_lists l WHERE l.item_id = items.id)"
    )
    conn.commit()
    return conn


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


# Meta added after capture (by `bm x threads`); a re-capture of the tweet keeps it
PRESERVED_META = ("thread", "thread_checked_at", "thread_error")


def upsert_item(conn: sqlite3.Connection, item: dict, touch: bool = True) -> tuple[int, bool]:
    """Insert or refresh one item. Returns (row id, was_new).

    `links` in the dict (list of URLs) are attached to the item for fetching.
    `list` ('bookmark' | 'like' | 'favorite') and `list_rank` record list membership.
    `touch=False` keeps `last_seen_at` of an existing row (re-parsing old captures).
    `seen_at` (ISO) replaces "now" as the first/last-seen time (imports of old data).
    """
    ts = item.get("seen_at") or now()
    existing = conn.execute(
        "SELECT id, meta FROM items WHERE source = ? AND source_id = ?",
        (item["source"], item["source_id"]),
    ).fetchone()
    fields = {
        k: item.get(k)
        for k in (
            "kind", "url", "link_url", "title", "text", "author",
            "author_name", "created_at", "bookmark_rank",
        )
    }
    meta = dict(item.get("meta") or {})
    if existing:
        old = json.loads(existing["meta"] or "{}")
        for k in PRESERVED_META:
            if k in old and k not in meta:
                meta[k] = old[k]
    fields["meta"] = json.dumps(meta, ensure_ascii=False)
    fields["raw"] = json.dumps(item.get("raw"), ensure_ascii=False) if item.get("raw") is not None else None

    if existing:
        item_id = existing["id"]
        # Keep an earlier bookmark_rank if this payload has none
        sets = ", ".join(
            f"{k} = COALESCE(?, {k})" if k == "bookmark_rank" else f"{k} = ?" for k in fields
        )
        conn.execute(
            f"UPDATE items SET {sets}, last_seen_at = {'?' if touch else 'last_seen_at'} WHERE id = ?",
            (*fields.values(), *([ts] if touch else []), item_id),
        )
        new = False
    else:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = conn.execute(
            f"INSERT INTO items (source, source_id, {cols}, first_seen_at, last_seen_at) "
            f"VALUES (?, ?, {marks}, ?, ?)",
            (item["source"], item["source_id"], *fields.values(), ts, ts),
        )
        item_id = cur.lastrowid
        new = True

    if item.get("list"):
        conn.execute(
            "INSERT INTO item_lists (item_id, list, rank, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (item_id, list) DO UPDATE SET "
            "rank = COALESCE(excluded.rank, rank), "
            f"last_seen_at = {'excluded.last_seen_at' if touch else 'last_seen_at'}",
            (item_id, item["list"], item.get("list_rank"), ts, ts),
        )

    for url in item.get("links") or []:
        conn.execute("INSERT OR IGNORE INTO links (url) VALUES (?)", (url,))
        link_id = conn.execute("SELECT id FROM links WHERE url = ?", (url,)).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO item_links (item_id, link_id) VALUES (?, ?)", (item_id, link_id)
        )
    refresh_fts(conn, item_id)
    return item_id, new


def item_document(conn: sqlite3.Connection, item_id: int) -> dict:
    """Searchable text for one item, including quoted tweets and linked pages."""
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    meta = json.loads(row["meta"] or "{}")
    text = row["text"] or ""
    quoted = meta.get("quoted")
    if quoted:
        text += f"\n\nQuoting @{quoted.get('author')}: {quoted.get('text') or ''}"
    if meta.get("story_title"):
        text += f"\n\nOn: {meta['story_title']}"
    thread = [t.get("text") for t in meta.get("thread") or [] if t.get("text")]
    if thread:
        text += "\n\nThread:\n" + "\n\n".join(thread)
    link_rows = conn.execute(
        "SELECT l.title, l.content, l.final_url, l.url FROM links l "
        "JOIN item_links il ON il.link_id = l.id WHERE il.item_id = ?",
        (item_id,),
    ).fetchall()
    links = "\n\n".join(
        "\n".join(filter(None, [lr["title"], lr["final_url"] or lr["url"], lr["content"]]))
        for lr in link_rows
    )
    author = " ".join(filter(None, [row["author"], row["author_name"]]))
    return {"title": row["title"] or "", "text": text, "author": author, "links": links}


def refresh_fts(conn: sqlite3.Connection, item_id: int) -> None:
    doc = item_document(conn, item_id)
    conn.execute("DELETE FROM items_fts WHERE rowid = ?", (item_id,))
    conn.execute(
        "INSERT INTO items_fts (rowid, title, text, author, links) VALUES (?, ?, ?, ?, ?)",
        (item_id, doc["title"], doc["text"], doc["author"], doc["links"]),
    )


def item_lists(conn: sqlite3.Connection, item_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT list FROM item_lists WHERE item_id = ? ORDER BY list", (item_id,))]


def public_id(row: sqlite3.Row) -> str:
    return f"{row['source']}:{row['source_id']}"


def find_item(conn: sqlite3.Connection, ref: str) -> sqlite3.Row | None:
    """Look up an item by 'x:123' / 'hn:456', or by a bare internal id."""
    if ":" in ref:
        source, source_id = ref.split(":", 1)
        return conn.execute(
            "SELECT * FROM items WHERE source = ? AND source_id = ?", (source, source_id)
        ).fetchone()
    if ref.isdigit():
        return conn.execute("SELECT * FROM items WHERE id = ?", (int(ref),)).fetchone()
    return None
