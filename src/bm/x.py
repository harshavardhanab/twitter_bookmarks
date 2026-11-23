"""X (Twitter) bookmarks: GraphQL parsing, browser capture and file import.

The owner logs in once in a headed Chrome window (`bm x login`); the session lives
in `data/x-profile`. `bm x sync` reopens that profile, scrolls /i/bookmarks and
records every Bookmarks GraphQL response. Raw responses are kept in `data/raw/x/`
so the parser can be re-run over them (`bm reparse x`).
"""

from __future__ import annotations

import html
import json
import random
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from . import db

INTERNAL_HOSTS = ("x.com", "twitter.com", "t.co")
URL_RE = re.compile(r"https?://[^\s<>\"']+")


class XError(RuntimeError):
    pass


# --- parsing -----------------------------------------------------------------

def _is_internal(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in INTERNAL_HOSTS)


def parse_date(s: str | None) -> str | None:
    """X's 'Wed Oct 10 20:19:24 +0000 2018' (or an ISO-ish string) to ISO UTC."""
    if not s:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y",):
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            pass
    else:
        try:
            dt = datetime.fromisoformat(re.sub(r"\s+([+-]\d\d:?\d\d)$", r"\1", s.strip()))
        except ValueError:
            return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _unwrap(result: Any) -> dict | None:
    """Return the actual Tweet object, or None for tombstones / unavailable tweets."""
    for _ in range(3):
        if not isinstance(result, dict):
            return None
        if result.get("__typename") == "TweetWithVisibilityResults" or (
            "tweet" in result and "legacy" not in result
        ):
            result = result.get("tweet")
            continue
        break
    if not isinstance(result, dict):
        return None
    if result.get("__typename") in ("TweetTombstone", "TweetUnavailable"):
        return None
    if "legacy" not in result or not result.get("rest_id"):
        return None
    return result


def _author(tweet: dict) -> tuple[str | None, str | None]:
    user = ((tweet.get("core") or {}).get("user_results") or {}).get("result") or {}
    core, legacy = user.get("core") or {}, user.get("legacy") or {}
    return (
        core.get("screen_name") or legacy.get("screen_name"),
        core.get("name") or legacy.get("name"),
    )


def _media(legacy: dict) -> list[dict]:
    out = []
    for m in (legacy.get("extended_entities") or {}).get("media") or []:
        entry = {"type": m.get("type"), "url": m.get("media_url_https")}
        variants = [
            v for v in (m.get("video_info") or {}).get("variants") or []
            if v.get("content_type") == "video/mp4"
        ]
        if variants:
            best = max(variants, key=lambda v: v.get("bitrate") or 0)
            entry = {"type": m.get("type"), "url": best.get("url"), "poster": m.get("media_url_https")}
        out.append(entry)
    return out


def _text_and_links(tweet: dict) -> tuple[str, list[str]]:
    legacy = tweet["legacy"]
    note = (((tweet.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result")) or {}
    text = note.get("text") or legacy.get("full_text") or ""

    url_entities = list((legacy.get("entities") or {}).get("urls") or [])
    url_entities += list(((note.get("entity_set") or {}).get("urls")) or [])
    media_tco = {
        m.get("url")
        for m in ((legacy.get("entities") or {}).get("media") or [])
        + ((legacy.get("extended_entities") or {}).get("media") or [])
        if m.get("url")
    }

    links: list[str] = []
    for u in url_entities:
        short, full = u.get("url"), u.get("expanded_url")
        if short and full:
            text = text.replace(short, full)
            if not _is_internal(full) and full not in links:
                links.append(full)
    for short in media_tco:
        text = text.replace(short, "")
    return html.unescape(text).strip(), links


def parse_tweet(result: Any, rank: str | None = None, depth: int = 0) -> dict | None:
    """GraphQL `tweet_results.result` to an `upsert_item` dict."""
    tweet = _unwrap(result)
    if tweet is None:
        return None
    legacy = tweet["legacy"]
    tid = str(tweet["rest_id"])
    screen_name, name = _author(tweet)
    text, links = _text_and_links(tweet)

    meta: dict[str, Any] = {
        k.removesuffix("_count"): legacy[k]
        for k in ("favorite_count", "retweet_count", "reply_count", "quote_count", "bookmark_count")
        if k in legacy
    }
    views = (tweet.get("views") or {}).get("count")
    if views is not None:
        meta["views"] = int(views) if str(views).isdigit() else views
    if legacy.get("conversation_id_str"):
        meta["conversation_id"] = legacy["conversation_id_str"]
    if legacy.get("in_reply_to_status_id_str"):
        meta["in_reply_to"] = legacy["in_reply_to_status_id_str"]
    media = _media(legacy)
    if media:
        meta["media"] = media

    title = None
    article = ((tweet.get("article") or {}).get("article_results") or {}).get("result")
    if isinstance(article, dict) and (article.get("title") or article.get("preview_text")):
        meta["article"] = {"title": article.get("title"), "preview_text": article.get("preview_text")}
        title = article.get("title")
        text = re.sub(r"https?://(?:www\.)?(?:x|twitter)\.com/i/article/\S+", "", text).strip()
        if not text and article.get("preview_text"):
            text = article["preview_text"]

    if depth == 0:
        quoted = parse_tweet((tweet.get("quoted_status_result") or {}).get("result"), depth=1)
        if quoted:
            meta["quoted"] = {
                k: quoted.get(k) for k in ("source_id", "author", "author_name", "text", "url", "created_at")
            }
            meta["quoted"]["id"] = meta["quoted"].pop("source_id")
            links += [u for u in quoted["links"] if u not in links]

    return {
        "source": "x",
        "source_id": tid,
        "kind": "tweet",
        "url": f"https://x.com/{screen_name or 'i'}/status/{tid}",
        "link_url": links[0] if links else None,
        "title": title,
        "text": text,
        "author": screen_name,
        "author_name": name,
        "created_at": parse_date(legacy.get("created_at")),
        "bookmark_rank": None,
        "list_rank": rank,
        "meta": meta,
        "raw": tweet if depth == 0 else None,
        "links": links,
    }


def walk_response(obj: Any) -> Iterator[dict]:
    """Yield parsed tweets from any GraphQL response, with `sortIndex` as rank.

    Walks the whole JSON instead of a fixed path so it survives schema shifts.
    """
    def visit(node: Any, rank: str | None) -> Iterator[dict]:
        if isinstance(node, dict):
            entry_id = node.get("entryId")
            if isinstance(entry_id, str):
                if entry_id.startswith("cursor-"):
                    return
                if node.get("sortIndex") is not None:
                    rank = str(node["sortIndex"])
            tr = node.get("tweet_results")
            if isinstance(tr, dict) and "result" in tr:
                item = parse_tweet(tr["result"], rank)
                if item:
                    yield item
                return
            for v in node.values():
                yield from visit(v, rank)
        elif isinstance(node, list):
            for v in node:
                yield from visit(v, rank)

    seen: set[str] = set()
    for item in visit(obj, None):
        if item["source_id"] not in seen:
            seen.add(item["source_id"])
            yield item


def parse_exporter_record(rec: dict) -> dict | None:
    """One record of a twitter-web-exporter JSON export."""
    raw = rec.get("metadata")
    if isinstance(raw, dict) and (raw.get("rest_id") or raw.get("legacy")):
        return parse_tweet(raw)
    tid = rec.get("id") or rec.get("rest_id")
    if not tid:
        return None
    text = html.unescape(rec.get("full_text") or rec.get("text") or "")
    links = [u for u in URL_RE.findall(text) if not _is_internal(u)]
    meta: dict[str, Any] = {}
    for k in ("favorite_count", "retweet_count", "reply_count", "quote_count", "bookmark_count", "views_count"):
        if rec.get(k) is not None:
            meta[k.removesuffix("_count")] = rec[k]
    if rec.get("media"):
        meta["media"] = [
            {"type": m.get("type"), "url": m.get("original") or m.get("url"), "poster": m.get("thumbnail")}
            for m in rec["media"] if isinstance(m, dict)
        ]
    q = rec.get("quoted_status")
    if q:
        meta["quoted"] = {"id": str(q)} if not isinstance(q, dict) else q
    screen_name = rec.get("screen_name")
    return {
        "source": "x",
        "source_id": str(tid),
        "kind": "tweet",
        "url": rec.get("url") or f"https://x.com/{screen_name or 'i'}/status/{tid}",
        "link_url": links[0] if links else None,
        "title": None,
        "text": text,
        "author": screen_name,
        "author_name": rec.get("name"),
        "created_at": parse_date(rec.get("created_at")),
        "bookmark_rank": None,
        "list_rank": None,
        "meta": meta,
        "raw": rec,
        "links": links,
    }


def parse_any(data: Any) -> list[dict]:
    """A raw GraphQL response, a list of them, or a twitter-web-exporter export."""
    if isinstance(data, dict):
        if "data" in data:
            return list(walk_response(data))
        rec = parse_exporter_record(data)
        return [rec] if rec else []
    items: list[dict] = []
    for el in data if isinstance(data, list) else []:
        if isinstance(el, dict) and "data" in el:
            items += walk_response(el)
        elif isinstance(el, dict):
            rec = parse_exporter_record(el)
            if rec:
                items.append(rec)
    return items


# X lists bm mirrors: GraphQL operation, raw capture folder, stored list name
LISTS = {
    "bookmarks": {"list": "bookmark", "op": "Bookmarks", "raw": "x"},
    "likes": {"list": "like", "op": "Likes", "raw": "x-likes"},
}


def past_cutoff(items: list[dict], since: str | None) -> bool:
    """True when a response is mostly older than `since` (by tweet post date).

    Like/bookmark order carries no date, so the post date is the proxy; the median
    keeps one old tweet you liked recently from ending the walk early.
    """
    if not since or not items:
        return False
    dates = sorted(it.get("created_at") or "" for it in items)
    return dates[len(dates) // 2] < since


def cut_since(batches: list[list[dict]], since: str | None) -> tuple[list[dict], bool]:
    """Items of consecutive responses (newest first) up to the `since` cutoff.

    Keeps whole responses until the first one that is past the cutoff, keeps that
    response's tweets posted on/after `since`, and stops. Returns (items, reached).
    """
    out: list[dict] = []
    for batch in batches:
        if past_cutoff(batch, since):
            out += [it for it in batch if (it.get("created_at") or "") >= since]
            return out, True
        out += batch
    return out, False


def likes_since(conn: sqlite3.Connection) -> str | None:
    """`x_likes_since` setting (YYYY-MM-DD): likes older than this are not captured."""
    return db.get_setting(conn, "x_likes_since") or None


def store(
    conn: sqlite3.Connection, items: list[dict], touch: bool = True, list_name: str = "bookmark"
) -> int:
    """Upsert parsed tweets as members of one list, oldest first.

    Returns how many were not on that list before.
    """
    def key(it):
        r = it.get("list_rank")
        return int(r) if r and str(r).isdigit() else 0

    before = {r[0] for r in conn.execute(
        "SELECT i.source_id FROM items i JOIN item_lists l ON l.item_id = i.id "
        "WHERE i.source = 'x' AND l.list = ?", (list_name,))}
    new = 0
    for item in sorted(items, key=key):
        item = dict(item, list=list_name)
        if list_name == "bookmark":
            item["bookmark_rank"] = item.get("list_rank")
        db.upsert_item(conn, item, touch=touch)
        new += item["source_id"] not in before
        before.add(item["source_id"])
    conn.commit()
    return new


# --- import / reparse ----------------------------------------------------------

def import_files(
    conn: sqlite3.Connection, paths: list[str], what: str = "bookmarks",
    log: Callable[[str], None] = print,
) -> dict:
    """Import exporter JSON or raw GraphQL files as `what` ('bookmarks' | 'likes')."""
    list_name = LISTS[what]["list"]
    stats = {"files": 0, "tweets": 0, "new": 0}
    for p in paths:
        try:
            data = json.loads(Path(p).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise XError(f"{p}: cannot read JSON ({e})") from e
        items = parse_any(data)
        new = store(conn, items, list_name=list_name)
        log(f"{p}: {len(items)} tweets, {new} new {what}")
        stats["files"] += 1
        stats["tweets"] += len(items)
        stats["new"] += new
    return stats


def raw_dir(what: str = "bookmarks") -> Path:
    d = db.home() / "raw" / LISTS[what]["raw"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def reparse(conn: sqlite3.Connection, log: Callable[[str], None] = print) -> dict:
    """Re-run the parser over every saved raw response, oldest capture first."""
    stats = {"files": 0, "tweets": 0, "new": 0}
    for what, cfg in LISTS.items():
        since = likes_since(conn) if what == "likes" else None
        sessions: dict[str, list[Path]] = {}
        for f in sorted(raw_dir(what).glob("*.json")):
            sessions.setdefault(f.name.split("-")[0], []).append(f)  # <stamp>-NNN.json
        for files in sessions.values():
            batches = []
            for f in files:
                try:
                    batches.append(parse_any(json.loads(f.read_text(encoding="utf-8"))))
                except ValueError as e:
                    log(f"{f.name}: skipped ({e})")
                    continue
                stats["files"] += 1
            items, reached = cut_since(batches, since)
            if reached:
                log(f"reparse x {what}: kept likes newer than {since} ({len(items)} tweets)")
            stats["tweets"] += len(items)
            stats["new"] += store(conn, items, touch=False, list_name=cfg["list"])
    log(f"reparse x: {stats['files']} files, {stats['tweets']} tweets, {stats['new']} new")
    return stats


# --- browser capture ---------------------------------------------------------

def profile_dir() -> Path:
    return db.home() / "x-profile"


def _chrome_ua() -> str | None:
    """User agent of the installed Chrome, as a headed window would send it."""
    import plistlib

    try:
        with open("/Applications/Google Chrome.app/Contents/Info.plist", "rb") as f:
            major = plistlib.load(f)["CFBundleShortVersionString"].split(".")[0]
    except (OSError, KeyError, ValueError):
        return None
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def _launch(p, headless: bool):
    extra = {}
    if headless:
        # Headless Chrome announces itself as "HeadlessChrome"; look like the headed window
        extra = {"viewport": {"width": 1280, "height": 1000}}
        ua = _chrome_ua()
        if ua:
            extra["user_agent"] = ua
    else:
        extra = {"no_viewport": True}
    return p.chromium.launch_persistent_context(
        str(profile_dir()),
        headless=headless,
        channel="chrome",
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
        **extra,
    )


def _has_session(ctx) -> bool:
    """True if the profile holds an X login cookie (checked without loading any page)."""
    return any(
        c.get("name") == "auth_token" and c.get("value")
        for c in ctx.cookies(["https://x.com", "https://twitter.com"])
    )


def _logged_out(url: str) -> bool:
    path = urlparse(url).path
    return path.startswith(("/login", "/i/flow/login", "/i/flow/signup")) or path in ("", "/")


def _profile_in_use() -> int | None:
    """PID of a Chrome still running with the X profile, else None."""
    import os

    lock = profile_dir() / "SingletonLock"
    try:
        target = os.readlink(lock)  # "<hostname>-<pid>"
        pid = int(target.rsplit("-", 1)[1])
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError, IndexError):
        return None


def _check_profile_free() -> None:
    pid = _profile_in_use()
    if pid:
        raise XError(
            f"Chrome is still running with the X profile (pid {pid}). Close that window "
            "and quit Chrome there (Cmd+Q), then try again."
        )


def login(
    log: Callable[[str], None] = print,
    url: str = "https://x.com/login",
    headless: bool = False,
    timeout: float = 900,
) -> bool:
    """Open a Chrome window on x.com/login; the owner signs in themselves.

    Succeeds as soon as X's `auth_token` session cookie exists, whatever page the
    sign-in ends on (Google/Apple popups and redirects are fine). Returns False if
    the window is closed or `timeout` seconds pass first; other browser failures
    raise XError with the real error.
    """
    import time

    from playwright.sync_api import Error as PWError, sync_playwright

    _check_profile_free()
    with sync_playwright() as p:
        ctx = _launch(p, headless=headless)
        closed: list[bool] = []
        ctx.on("close", lambda *_: closed.append(True))
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url)
            log("Sign in to X in the Chrome window (any method). It closes by itself once you're in.")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    if _has_session(ctx):  # also pumps Playwright events (pages, close)
                        time.sleep(2)  # let X finish setting its cookies
                        log("Logged in; session saved in data/x-profile.")
                        return True
                    if closed or not ctx.pages:
                        return False
                except PWError as e:
                    if closed or "closed" in str(e).lower():
                        return False
                    raise XError(f"browser error during login: {e}") from e
                time.sleep(1)
            log(f"Gave up after {int(timeout)} s without a login.")
            return False
        finally:
            try:
                ctx.close()
            except PWError:
                pass


def _screen_name(conn: sqlite3.Connection, page, log: Callable[[str], None]) -> str:
    """The logged-in user's handle: `x_user` setting, else read from the X sidebar."""
    from playwright.sync_api import Error as PWError

    handle = db.get_setting(conn, "x_user")
    if handle:
        return handle.lstrip("@")
    page.goto("https://x.com/home", wait_until="domcontentloaded")
    href = None
    try:
        el = page.wait_for_selector('a[data-testid="AppTabBar_Profile_Link"]', timeout=20000)
        href = el.get_attribute("href") if el else None
    except PWError:
        pass
    if _logged_out(page.url):
        raise XError("Not logged in to X. Run `bm x login` first.")
    handle = (href or "").strip("/").split("/")[0]
    if not re.fullmatch(r"\w{1,15}", handle):
        raise XError("Couldn't find your X handle. Set it with: bm config x_user <handle>")
    db.set_setting(conn, "x_user", handle)
    log(f"x: your handle is @{handle} (saved as x_user)")
    return handle


def sync(
    conn: sqlite3.Connection,
    what: str = "bookmarks",
    full: bool = False,
    headed: bool = False,
    max_scrolls: int | None = None,
    max_minutes: float = 60,
    log: Callable[[str], None] = print,
) -> dict:
    """Scroll your bookmarks or likes in the saved session and store every captured tweet.

    `what` is 'bookmarks' (/i/bookmarks) or 'likes' (/<handle>/likes). X loads ~20
    tweets per response and needs a few scrolls per response, so a full walk of a
    few thousand takes a few thousand scrolls. Limits: `max_scrolls` (default 5000
    with `full`, 300 otherwise) and `max_minutes`.
    """
    import time

    from playwright.sync_api import sync_playwright

    cfg = LISTS[what]
    if max_scrolls is None:
        max_scrolls = 5000 if full else 300
    op_re = re.compile(rf"/graphql/[^/]+/{cfg['op']}(?:\?|$)")

    out = raw_dir(what)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pending: list = []
    stats = {"responses": 0, "tweets": 0, "new": 0, "scrolls": 0}
    since = likes_since(conn) if what == "likes" else None
    reached_cutoff: list[bool] = []
    known = {r[0] for r in conn.execute(
        "SELECT i.source_id FROM items i JOIN item_lists l ON l.item_id = i.id "
        "WHERE i.source = 'x' AND l.list = ?", (cfg["list"],))}
    collected: dict[str, dict] = {}  # stored together at the end, oldest first

    def on_response(resp):
        if op_re.search(resp.url) and resp.request.resource_type in ("xhr", "fetch"):
            pending.append(resp)

    def drain() -> list[int]:
        """Process captured responses; return the new-tweet count of each one with tweets."""
        counts = []
        while pending:
            resp = pending.pop(0)
            try:
                body = resp.json()
            except Exception:  # body gone or not JSON
                continue
            stats["responses"] += 1
            (out / f"{stamp}-{stats['responses']:03d}.json").write_text(
                json.dumps(body, ensure_ascii=False), encoding="utf-8"
            )
            items = list(walk_response(body))
            if not items:
                continue  # end of list: only cursors
            if reached_cutoff:
                continue  # already past the cutoff
            if past_cutoff(items, since):
                items = [it for it in items if (it.get("created_at") or "") >= since]
                reached_cutoff.append(True)
            new = sum(1 for it in items if it["source_id"] not in known and it["source_id"] not in collected)
            for it in items:
                prev = collected.get(it["source_id"])
                if prev and prev.get("list_rank") and not it.get("list_rank"):
                    it["list_rank"] = prev["list_rank"]
                collected[it["source_id"]] = it
            stats["tweets"] += len(items)
            counts.append(new)
            log(f"x {what} response {stats['responses']}: {len(items)} tweets, {new} new")
        return counts

    _check_profile_free()
    with sync_playwright() as p:
        ctx = _launch(p, headless=not headed)
        try:
            if not _has_session(ctx):
                raise XError("Not logged in to X. Run `bm x login` first.")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            if what == "likes":
                url = f"https://x.com/{_screen_name(conn, page, log)}/likes"
            else:
                url = "https://x.com/i/bookmarks"
            page.on("response", on_response)
            page.goto(url, wait_until="domcontentloaded")
            for _ in range(20):
                if pending or _logged_out(page.url):
                    break
                page.wait_for_timeout(750)
            if _logged_out(page.url):
                raise XError("Not logged in to X. Run `bm x login` first.")

            no_new = idle = 0
            deadline = time.monotonic() + max_minutes * 60
            for _ in range(max_scrolls):
                if time.monotonic() > deadline:
                    stats["stopped"] = "time limit"
                    break
                counts = drain()
                if reached_cutoff:
                    log(f"x: reached {what} older than {since} (x_likes_since); stopping")
                    break
                if counts:
                    idle = 0
                    for c in counts:
                        no_new = no_new + 1 if c == 0 else 0
                    if not full and no_new >= 2:
                        log(f"x: caught up with known {what}")
                        break
                else:
                    idle += 1
                    if idle >= 6:
                        if not stats["responses"]:
                            raise XError(
                                f"No {what} data came back from X. The session may have "
                                "expired (run `bm x login`), or try `bm x sync --headed` to watch."
                            )
                        log(f"x: reached the end of the {what} list")
                        break
                page.mouse.wheel(0, random.randint(2500, 4500))
                stats["scrolls"] += 1
                page.wait_for_timeout(random.randint(1500, 3000))
            else:
                stats["stopped"] = "scroll limit"
            drain()
            if stats.get("stopped"):
                log(
                    f"x: stopped at the {stats['stopped']} ({stats['scrolls']} scrolls, "
                    f"{max_minutes:g} min max) before the end of the {what} list. Everything "
                    "captured is saved; run `bm x sync --full` again to go further."
                )
        finally:
            ctx.close()
            # Store even if the capture stopped early; the raw files are kept too
            if collected:
                stats["new"] = store(conn, list(collected.values()), list_name=cfg["list"])

    log(
        f"x {what}: {stats['responses']} responses, {stats['tweets']} tweets, "
        f"{stats['new']} new ({stats['scrolls']} scrolls)"
    )
    return stats
