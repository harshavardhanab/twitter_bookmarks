"""Capture the author's own replies (threads) of saved X tweets.

X's Bookmarks/Likes data holds only the saved tweet, but the thing worth saving
often lives in the author's replies ("link in the first reply", the rest of a 🧵).
For each candidate tweet this opens the tweet page in the logged-in browser,
records X's TweetDetail responses, and keeps the author's own tweets in that
conversation as meta.thread. Their links join the pages `bm links` fetches and
their text is indexed with the tweet.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable

from . import db, x

# Text that suggests the useful part is in a thread or the replies
CANDIDATE_RE = re.compile(
    r"🧵|👇|⬇️?|\bthread\b|(?:^|[\s(])1\s?/\s?(?:\d+|n)?(?=[\s).:]|$)"
    r"|\bin (?:the )?(?:replies|reply|comments)\b|\b(?:first|1st) (?:reply|comment)\b"
    r"|\b(?:see|link|links|repo|code|paper|details|more|everything|full|sources?)\b[^.\n]{0,25}\bbelow\b",
    re.I,
)
TWEETDETAIL_RE = re.compile(r"/graphql/[^/]+/TweetDetail(?:\?|$)")


def raw_dir() -> Path:
    d = db.home() / "raw" / "x-threads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def looks_like_thread(text: str | None) -> bool:
    return bool(text) and bool(CANDIDATE_RE.search(x.URL_RE.sub(" ", text)))


def candidates(conn: sqlite3.Connection, all_: bool = False, recheck: bool = False) -> list[sqlite3.Row]:
    """X items to check, newest saved first."""
    rows = conn.execute(
        "SELECT id, source_id, text, meta FROM items WHERE source = 'x' "
        "ORDER BY first_seen_at DESC, CAST(bookmark_rank AS INTEGER) DESC, id DESC"
    ).fetchall()
    out = []
    for r in rows:
        meta = json.loads(r["meta"] or "{}")
        if meta.get("thread_checked_at") and not recheck:
            continue
        if all_ or looks_like_thread(r["text"]):
            out.append(r)
    return out


def extract(root_id: str, bodies: list) -> tuple[dict | None, list[dict]]:
    """(root tweet, the author's other tweets in its conversation, oldest first)."""
    parsed: dict[str, dict] = {}
    for b in bodies:
        for it in x.walk_response(b):
            parsed[it["source_id"]] = it
    root = parsed.get(root_id)
    if root is None:
        return None, []
    author = (root["author"] or "").lower()
    conv = root["meta"].get("conversation_id") or root_id
    replies = [
        it for sid, it in parsed.items()
        if sid != root_id and author and (it["author"] or "").lower() == author
        and (it["meta"].get("conversation_id") or sid) == conv
    ]
    replies.sort(key=lambda it: int(it["source_id"]) if it["source_id"].isdigit() else 0)
    return root, replies


def apply(conn: sqlite3.Connection, item_id: int, bodies: list, checked_at: str) -> int:
    """Store the thread found in `bodies` on the item. Returns the number of replies."""
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    root, replies = extract(row["source_id"], bodies)
    meta = json.loads(row["meta"] or "{}")

    if root is not None and meta.get("imported_from"):
        # The conversation includes the full tweet: replace a reduced (Siftly) copy
        db.upsert_item(conn, {k: v for k, v in root.items() if k not in ("list", "list_rank")}, touch=False)
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        meta = json.loads(row["meta"] or "{}")

    meta["thread_checked_at"] = checked_at
    meta.pop("thread_error", None)
    if root is None:
        meta["thread_error"] = "tweet unavailable"
    thread = [
        {k: v for k, v in {
            "id": r["source_id"], "text": r["text"], "url": r["url"], "created_at": r["created_at"],
            "links": r["links"] or None, "media": r["meta"].get("media"),
        }.items() if v}
        for r in replies[:50]
    ]
    if thread:
        meta["thread"] = thread
    else:
        meta.pop("thread", None)

    links = [u for t in thread for u in t.get("links") or []]
    for url in dict.fromkeys(links):
        conn.execute("INSERT OR IGNORE INTO links (url) VALUES (?)", (url,))
        link_id = conn.execute("SELECT id FROM links WHERE url = ?", (url,)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO item_links (item_id, link_id) VALUES (?, ?)", (item_id, link_id))
    conn.execute(
        "UPDATE items SET meta = ?, link_url = COALESCE(link_url, ?) WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False), links[0] if links else None, item_id),
    )
    db.refresh_fts(conn, item_id)
    conn.commit()
    return len(thread)


def reparse(conn: sqlite3.Connection, log: Callable[[str], None] = print) -> dict:
    """Re-apply every saved thread capture."""
    stats = {"files": 0, "threads": 0}
    for f in sorted(raw_dir().glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as e:
            log(f"{f.name}: skipped ({e})")
            continue
        row = conn.execute(
            "SELECT id FROM items WHERE source = 'x' AND source_id = ?", (str(data.get("tweet_id")),)
        ).fetchone()
        if not row:
            continue
        stats["files"] += 1
        stats["threads"] += apply(conn, row["id"], data.get("responses") or [],
                                  data.get("captured_at") or db.now()) > 0
    log(f"reparse threads: {stats['files']} files, {stats['threads']} with author replies")
    return stats


def capture(
    conn: sqlite3.Connection,
    all_: bool = False,
    recheck: bool = False,
    limit: int | None = None,
    headed: bool = False,
    max_minutes: float = 45,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> dict:
    """Open each candidate tweet and store the author's replies."""
    from playwright.sync_api import Error as PWError, sync_playwright

    todo = candidates(conn, all_, recheck)
    if limit is not None:
        todo = todo[:limit]
    stats = {"candidates": len(todo), "checked": 0, "with_replies": 0, "replies": 0}
    if dry_run or not todo:
        log(f"x threads: {len(todo)} tweets to check" + (" (dry run)" if dry_run else ""))
        return stats

    out = raw_dir()
    pending: list = []
    deadline = time.monotonic() + max_minutes * 60

    def on_response(resp):
        if TWEETDETAIL_RE.search(resp.url) and resp.request.resource_type in ("xhr", "fetch"):
            pending.append(resp)

    def drain(bodies: list) -> bool:
        """Move captured responses into `bodies`; False if X is rate limiting."""
        while pending:
            resp = pending.pop(0)
            if resp.status == 429:
                return False
            try:
                bodies.append(resp.json())
            except Exception:
                pass
        return True

    log(f"x threads: checking {len(todo)} tweets (a few seconds each)")
    x._check_profile_free()
    with sync_playwright() as p:
        ctx = x._launch(p, headless=not headed)
        try:
            if not x._has_session(ctx):
                raise x.XError("Not logged in to X. Run `bm x login` first.")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("response", on_response)
            no_data = 0
            for n, row in enumerate(todo, 1):
                if time.monotonic() > deadline:
                    stats["stopped"] = "time limit"
                    break
                sid = row["source_id"]
                pending.clear()
                bodies: list = []
                try:
                    page.goto(f"https://x.com/i/status/{sid}", wait_until="domcontentloaded")
                except PWError as e:
                    log(f"  {sid}: page failed ({e}); will retry next run")
                    continue
                for _ in range(30):
                    if pending or x._logged_out(page.url):
                        break
                    page.wait_for_timeout(500)
                if x._logged_out(page.url):
                    raise x.XError("Not logged in to X. Run `bm x login` first.")
                if not drain(bodies):
                    stats["stopped"] = "rate limited by X"
                    break
                if not bodies:
                    no_data += 1
                    log(f"  {sid}: no conversation data; will retry next run")
                    if no_data >= 5:
                        stats["stopped"] = "no data for 5 tweets in a row"
                        break
                    continue
                no_data = 0
                # Long threads load more as you scroll; stop when no new author replies appear
                seen = len(extract(sid, bodies)[1])
                for _ in range(3):
                    page.mouse.wheel(0, random.randint(2500, 4000))
                    page.wait_for_timeout(random.randint(1500, 2500))
                    if not drain(bodies):
                        break
                    now_seen = len(extract(sid, bodies)[1])
                    if now_seen == seen:
                        break
                    seen = now_seen

                captured_at = db.now()
                (out / f"{sid}.json").write_text(json.dumps(
                    {"tweet_id": sid, "captured_at": captured_at, "responses": bodies},
                    ensure_ascii=False), encoding="utf-8")
                k = apply(conn, row["id"], bodies, captured_at)
                stats["checked"] += 1
                stats["with_replies"] += k > 0
                stats["replies"] += k
                log(f"  {n}/{len(todo)} {sid}: {k} author repl{'y' if k == 1 else 'ies'}")
                page.wait_for_timeout(random.randint(4000, 8000))
        finally:
            ctx.close()

    if stats.get("stopped"):
        log(f"x threads: stopped ({stats['stopped']}); the rest are checked next run")
    log(
        f"x threads: {stats['checked']} checked, {stats['with_replies']} with author replies "
        f"({stats['replies']} replies). Run `bm links` to fetch their links."
    )
    return stats

