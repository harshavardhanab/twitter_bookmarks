"""Hacker News favorites sync.

Scrapes the public favorites pages for item ids, then fetches each item from the
official Firebase API (and Algolia for a comment's parent story).
"""

from __future__ import annotations

import html
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx

from . import db

HN = "https://news.ycombinator.com/"
FIREBASE = "https://hacker-news.firebaseio.com/v0/item/{}.json"
ALGOLIA = "https://hn.algolia.com/api/v1/items/{}"
UA = "bm-bookmarks-mirror/0.1"
PAGE_DELAY = 1.5
MAX_PAGES = 500

ID_RE = re.compile(r"""<tr\s+class=['"]athing[^'"]*['"]\s+id=['"](\d+)['"]""")
MORE_RE = re.compile(r"""<a\b[^>]*\bclass=['"]morelink['"][^>]*>""")
HREF_RE = re.compile(r"""\bhref=['"]([^'"]+)['"]""")
TAG_RE = re.compile(r"<[^>]+>")
P_RE = re.compile(r"<p\s*/?>", re.I)


class HNError(RuntimeError):
    pass


def client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": UA}, timeout=20, follow_redirects=True
    )


def _get(http: httpx.Client, url: str, tries: int = 4) -> httpx.Response:
    """GET with backoff on rate limiting / transient server errors."""
    delay = 5.0
    for attempt in range(tries):
        try:
            resp = http.get(url)
        except httpx.TransportError:
            if attempt == tries - 1:
                raise
        else:
            slow = "not able to serve your requests this quickly" in resp.text[:2000]
            if resp.status_code not in (429, 502, 503, 504) and not slow:
                resp.raise_for_status()
                return resp
            if attempt == tries - 1:
                raise HNError(f"HN is rate-limiting requests ({resp.status_code}) for {url}")
        time.sleep(delay)
        delay *= 2
    raise AssertionError("unreachable")


# --- favorites pages ---------------------------------------------------------

def favorite_pages(http: httpx.Client, user: str, comments: bool):
    """Yield the list of item ids on each favorites page, newest favorites first."""
    url = f"{HN}favorites?id={user}" + ("&comments=t" if comments else "")
    for n in range(MAX_PAGES):
        if n:
            time.sleep(PAGE_DELAY)
        page = _get(http, url).text
        if "No such user." in page[:3000]:
            raise HNError(f"HN user {user!r} not found")
        ids = ID_RE.findall(page)
        if ids:
            yield ids
        more = MORE_RE.search(page)
        if not more or not ids:
            return
        href = HREF_RE.search(more.group(0))
        if not href:
            return
        url = urljoin(HN, html.unescape(href.group(1)))


# --- item conversion ---------------------------------------------------------

def html_to_text(s: str | None) -> tuple[str, list[str]]:
    """HN item HTML to plain text, plus outbound links found in it."""
    if not s:
        return "", []
    links = []
    for href in HREF_RE.findall(s):
        href = html.unescape(href)
        host = urlparse(href).hostname or ""
        if href.startswith(("http://", "https://")) and host != "news.ycombinator.com":
            if href not in links:
                links.append(href)
    text = P_RE.sub("\n\n", s)
    text = html.unescape(TAG_RE.sub("", text))
    return text.strip(), links


def _iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _external(url: str | None) -> bool:
    return bool(url) and (urlparse(url).hostname or "") != "news.ycombinator.com"


def fetch_item(http: httpx.Client, item_id: str, story_titles: dict) -> dict | None:
    """Fetch one HN item and convert it to an `upsert_item` dict."""
    fb = _get(http, FIREBASE.format(item_id)).json()
    if not fb:
        return None
    kind = fb.get("type") or "story"
    text, links = html_to_text(fb.get("text"))
    meta = {
        k: fb[k] for k in ("score", "descendants", "parent", "deleted", "dead") if k in fb
    }
    raw = {"firebase": fb}
    link_url = fb.get("url") if _external(fb.get("url")) else None

    if kind == "comment":
        try:
            al = _get(http, ALGOLIA.format(item_id)).json()
        except (httpx.HTTPError, HNError, ValueError):
            al = None
        if al:
            al.pop("children", None)
            raw["algolia"] = al
            story_id = al.get("story_id")
            if story_id:
                meta["story_id"] = story_id
                if story_id not in story_titles:
                    story = _get(http, FIREBASE.format(story_id)).json() or {}
                    story_titles[story_id] = (story.get("title"), story.get("url"))
                title, url = story_titles[story_id]
                if title:
                    meta["story_title"] = title
                if url:
                    meta["story_url"] = url

    if link_url and link_url not in links:
        links.insert(0, link_url)

    return {
        "source": "hn",
        "source_id": str(fb.get("id", item_id)),
        "kind": kind,
        "url": f"{HN}item?id={fb.get('id', item_id)}",
        "link_url": link_url,
        "title": fb.get("title"),
        "text": text,
        "author": fb.get("by"),
        "author_name": None,
        "created_at": _iso(fb.get("time")),
        "meta": meta,
        "raw": raw,
        "links": links,
        "list": "favorite",
    }


# --- sync --------------------------------------------------------------------

def sync(
    conn: sqlite3.Connection,
    user: str | None = None,
    full: bool = False,
    log: Callable[[str], None] = print,
) -> dict:
    """Mirror the user's favorite stories and comments. Returns counts."""
    user = user or db.get_setting(conn, "hn_user")
    if not user:
        raise HNError("No HN username set. Run: bm config hn_user <name>")

    known = {r["source_id"] for r in conn.execute("SELECT source_id FROM items WHERE source = 'hn'")}
    stats = {"seen": 0, "new": 0, "updated": 0, "missing": 0}
    ts = db.now()

    with client() as http:
        to_fetch: list[str] = []  # newest favorite first
        seen: set[str] = set()
        for comments in (False, True):
            label = "comments" if comments else "stories"
            for p, ids in enumerate(favorite_pages(http, user, comments), 1):
                fresh = [i for i in ids if i not in seen]
                seen.update(fresh)
                new_here = [i for i in fresh if i not in known]
                to_fetch.extend(fresh if full else new_here)
                log(f"hn {label} page {p}: {len(ids)} ids, {len(new_here)} new")
                if not full and not new_here:
                    break
            time.sleep(PAGE_DELAY)

        stats["seen"] = len(seen)
        # Touch items we saw but are not refetching
        conn.executemany(
            "UPDATE items SET last_seen_at = ? WHERE source = 'hn' AND source_id = ?",
            [(ts, i) for i in seen - set(to_fetch)],
        )

        story_titles: dict = {}
        with ThreadPoolExecutor(8) as pool:
            items = list(pool.map(lambda i: fetch_item(http, i, story_titles), to_fetch))

    # Insert oldest favorite first so row ids follow favorite order
    for item_id, item in reversed(list(zip(to_fetch, items))):
        if item is None:
            log(f"hn {item_id}: not returned by the API, skipped")
            stats["missing"] += 1
            continue
        _, new = db.upsert_item(conn, item)
        stats["new" if new else "updated"] += 1
    conn.commit()
    log(f"hn: {stats['seen']} favorites seen, {stats['new']} new, {stats['updated']} updated")
    return stats
