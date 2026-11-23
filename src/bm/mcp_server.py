"""MCP server (stdio) exposing bookmark search to any MCP client.

Nothing here may print to stdout: stdout is the MCP channel.
"""

from __future__ import annotations

import sys
import threading
import warnings
from typing import Literal

warnings.filterwarnings("ignore", message=".*SOCKS support in urllib3.*")

from mcp.server.fastmcp import FastMCP  # noqa: E402

from . import api, db, search as search_mod  # noqa: E402

mcp = FastMCP(
    "bookmarks",
    instructions=(
        "Searches the owner's saved X (Twitter) bookmarks and likes and Hacker News favorites, "
        "including the text of the pages they link to. Use search_bookmarks when the owner "
        "asks about something they saved, bookmarked, favorited or read; try two or three "
        "phrasings, then get_bookmark for detail. Cite the url (and link_url) of what you use. "
        "An empty result is not proof: check the returned mode and note (keyword-only means "
        "semantic search was unavailable), retry with mode='semantic', fewer and more distinctive "
        "words, no filters, and the key term in the post's likely original language. Posts may "
        "be in other languages; translate what you quote."
    ),
)


@mcp.tool()
def search_bookmarks(
    query: str,
    limit: int = 10,
    source: Literal["x", "hn"] | None = None,
    since: str | None = None,
    until: str | None = None,
    author: str | None = None,
    in_list: Literal["bookmark", "like", "favorite"] | None = None,
    mode: Literal["hybrid", "keyword", "semantic"] = "hybrid",
) -> dict:
    """Search saved X bookmarks and likes and HN favorites (keyword + semantic).

    Args:
        query: what to look for, in plain words.
        limit: maximum results (default 10).
        source: "x" or "hn" to search one source only.
        since: only posts created on or after this date (YYYY-MM-DD).
        until: only posts created on or before this date (YYYY-MM-DD, inclusive).
        author: X handle or HN username (a leading @ is fine).
        in_list: only "bookmark" or "like" (X) or "favorite" (HN) items.
        mode: "hybrid" (default), "keyword" or "semantic".

    Returns {"mode", "results": [{id, source, lists, kind, author, created_at, title, snippet,
    url, link_url, score}], "note"?}. "mode" is the mode actually used: "keyword" with a
    "note" means semantic search was unavailable, so an empty result is inconclusive.
    Snippets are in the post's own language. Pass a result's id to get_bookmark for the
    full text.
    """
    conn = db.connect()
    try:
        return search_mod.search(
            conn, query, mode=mode, limit=max(1, min(limit, 100)),
            source=source, since=since, until=until, author=author, in_list=in_list,
        )
    finally:
        conn.close()


@mcp.tool()
def get_bookmark(id: str, max_chars: int = 4000) -> dict:
    """Full bookmark by id ("x:<tweet id>" or "hn:<item id>"), with linked page text.

    max_chars caps the total linked-page text returned (default 4000).
    """
    conn = db.connect()
    try:
        item = api.get_item(conn, id, max_chars=max(0, max_chars))
    finally:
        conn.close()
    if item is None:
        raise ValueError(f"No bookmark {id!r}; ids look like x:123 or hn:456")
    return item


@mcp.tool()
def recent_bookmarks(
    n: int = 20,
    source: Literal["x", "hn"] | None = None,
    in_list: Literal["bookmark", "like", "favorite"] | None = None,
) -> list[dict]:
    """The most recently saved items, newest first (with in_list: in that list's order,
    e.g. most recently liked first)."""
    conn = db.connect()
    try:
        return api.recent(conn, max(1, min(n, 200)), source, in_list)
    finally:
        conn.close()


@mcp.tool()
def bookmark_stats() -> dict:
    """Counts per source and list, link fetch status, embedding coverage, last sync times."""
    conn = db.connect()
    try:
        return api.stats(conn)
    finally:
        conn.close()


def _warm() -> None:
    """Load the embedding model in the background so the first search is fast."""
    try:
        from . import embed

        conn = db.connect()
        try:
            embed.load_vec(conn)
            if embed.has_vectors(conn):
                embed.model()
        finally:
            conn.close()
    except Exception as e:
        print(f"bm mcp: embedding model not loaded ({e}); keyword search only", file=sys.stderr)


def main() -> None:
    threading.Thread(target=_warm, daemon=True).start()
    mcp.run("stdio")


if __name__ == "__main__":
    main()
