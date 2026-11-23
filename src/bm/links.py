"""Fetch the pages that bookmarks link to and index their text."""

from __future__ import annotations

import io
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import warnings
from collections import defaultdict, deque
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

warnings.filterwarnings("ignore", message=".*SOCKS support in urllib3.*")
import trafilatura  # noqa: E402

from . import db  # noqa: E402

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 20
MAX_BYTES = 15 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_CHARS = 500_000
THREADS = 8
PER_HOST = 2          # concurrent requests to one site
RATE_LIMIT_TRIES = 3  # attempts when a site answers 429
_host_locks: dict[str, threading.Semaphore] = {}
_host_locks_guard = threading.Lock()


def _host(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _host_slot(url: str) -> threading.Semaphore:
    with _host_locks_guard:
        return _host_locks.setdefault(_host(url), threading.Semaphore(PER_HOST))
RETRY_AFTER = timedelta(days=7)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _kind(content_type: str, body: bytes, url: str) -> str | None:
    ct = content_type.lower()
    if "pdf" in ct or body[:5] == b"%PDF-":
        return "pdf"
    if "html" in ct:  # text/html, application/xhtml+xml
        return "html"
    if not ct or ct.startswith("text/plain"):
        head = body[:1024].lstrip().lower()
        if head.startswith((b"<!doctype html", b"<html")):
            return "html"
    return None


def _html(body: bytes, url: str) -> tuple[str | None, str | None]:
    tree = trafilatura.load_html(body)
    if tree is None:
        return None, None
    title = None
    try:
        md = trafilatura.extract_metadata(tree, default_url=url)
        title = md.title if md else None
    except Exception:
        pass
    if not title:
        m = TITLE_RE.search(body[:200_000].decode("utf-8", "replace"))
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else None
    text = trafilatura.extract(trafilatura.load_html(body), url=url, include_comments=False)
    return title, text


def _pdf(body: bytes) -> tuple[str | None, str | None]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body))
    title = None
    try:
        title = (reader.metadata or {}).get("/Title") or None
    except Exception:
        pass
    pages = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    return (str(title).strip() or None) if title else None, text or None


def fetch_one(http: httpx.Client, url: str) -> dict:
    """Download and extract one URL. Never raises; failures go in `error`.

    At most PER_HOST requests per site run at once; a 429 is retried after the
    site's Retry-After (capped at 60 s) or 5 s / 15 s.
    """
    for attempt in range(RATE_LIMIT_TRIES):
        with _host_slot(url):
            res = _fetch_once(http, url)
        if res["status"] != 429 or attempt == RATE_LIMIT_TRIES - 1:
            break
        wait = res.pop("retry_after", None)
        time.sleep(min(wait, 60) if wait else (5, 15)[min(attempt, 1)])
    res.pop("retry_after", None)
    return res


def _fetch_once(http: httpx.Client, url: str) -> dict:
    res = {"final_url": None, "status": None, "content_type": None,
           "title": None, "content": None, "error": None}
    try:
        with http.stream("GET", url) as resp:
            res["final_url"] = str(resp.url)
            res["status"] = resp.status_code
            res["content_type"] = resp.headers.get("content-type", "").split(";")[0].strip() or None
            if resp.status_code == 429:
                ra = resp.headers.get("retry-after", "")
                res["retry_after"] = int(ra) if ra.isdigit() else None
            if resp.status_code >= 400:
                res["error"] = f"HTTP {resp.status_code}"
                return res
            declared = int(resp.headers.get("content-length") or 0)
            if declared > MAX_BYTES:
                res["error"] = f"larger than {MAX_BYTES // 2**20} MB"
                return res
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    res["error"] = f"larger than {MAX_BYTES // 2**20} MB"
                    return res
        body = bytes(buf)
        kind = _kind(res["content_type"] or "", body, res["final_url"])
        if kind == "html":
            title, text = _html(body, res["final_url"])
        elif kind == "pdf":
            title, text = _pdf(body)
        else:
            return res  # other types: recorded, not extracted
        res["title"] = title
        res["content"] = text[:MAX_CHARS] if text else None
    except httpx.HTTPError as e:
        res["error"] = f"{type(e).__name__}: {e}"[:300] or type(e).__name__
    except Exception as e:  # parser failures
        res["error"] = f"extract failed: {type(e).__name__}: {e}"[:300]
    return res


def snapshot(link_id: int, url: str) -> str | None:
    """Save a self-contained HTML copy with monolith. Returns the path or None."""
    if not shutil.which("monolith"):
        return None
    out = db.home() / "snapshots"
    out.mkdir(exist_ok=True)
    path = out / f"{link_id}.html"
    try:
        subprocess.run(
            ["monolith", url, "-o", str(path)],
            check=True, timeout=120, capture_output=True,
        )
    except (subprocess.SubprocessError, OSError):
        path.unlink(missing_ok=True)
        return None
    return str(path) if path.exists() and path.stat().st_size else None


def pending(conn: sqlite3.Connection, retry: bool = False) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - RETRY_AFTER).replace(microsecond=0).isoformat()
    return conn.execute(
        "SELECT id, url FROM links WHERE fetched_at IS NULL OR error = 'HTTP 429' "
        "OR (error IS NOT NULL AND (? OR fetched_at < ?)) ORDER BY id",
        (int(retry), cutoff),
    ).fetchall()


def fetch_links(
    conn: sqlite3.Connection,
    retry: bool = False,
    snapshots: bool = False,
    log: Callable[[str], None] = print,
) -> dict:
    """Fetch new links (and old failures); refresh the search index of their items."""
    todo = [r for r in pending(conn, retry) if r["url"].startswith(("http://", "https://"))]
    stats = {"fetched": 0, "ok": 0, "failed": 0, "skipped_type": 0, "snapshots": 0}
    changed: list[int] = []

    if todo:
        log(f"links: fetching {len(todo)}")
        with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as http, \
                ThreadPoolExecutor(THREADS) as pool:
            # Round-robin across sites so one site's links aren't fetched back to back
            by_host: dict[str, deque] = defaultdict(deque)
            for r in todo:
                by_host[_host(r["url"])].append(r)
            ordered = []
            while by_host:
                for h in list(by_host):
                    ordered.append(by_host[h].popleft())
                    if not by_host[h]:
                        del by_host[h]
            futures = {pool.submit(fetch_one, http, r["url"]): r for r in ordered}
            for fut in as_completed(futures):
                row, res = futures[fut], fut.result()
                conn.execute(
                    "UPDATE links SET final_url = ?, status = ?, content_type = ?, title = ?, "
                    "content = ?, error = ?, fetched_at = ? WHERE id = ?",
                    (res["final_url"], res["status"], res["content_type"], res["title"],
                     res["content"], res["error"], db.now(), row["id"]),
                )
                changed.append(row["id"])
                stats["fetched"] += 1
                if res["error"]:
                    stats["failed"] += 1
                    log(f"  fail {row['url']}: {res['error']}")
                elif res["content"] is None and res["title"] is None:
                    stats["skipped_type"] += 1
                    log(f"  skip {row['url']} ({res['content_type']})")
                else:
                    stats["ok"] += 1
                    log(f"  ok   {row['url']} ({len(res['content'] or '')} chars)")
                conn.commit()  # short write transactions: embed may run at the same time

    if snapshots:
        if not shutil.which("monolith"):
            log("links: --snapshot needs monolith (brew install monolith); skipped")
        else:
            rows = conn.execute(
                "SELECT id, COALESCE(final_url, url) AS u FROM links WHERE error IS NULL "
                "AND fetched_at IS NOT NULL AND snapshot_path IS NULL "
                "AND (content_type LIKE '%html%')"
            ).fetchall()
            for r in rows:
                path = snapshot(r["id"], r["u"])
                if path:
                    conn.execute("UPDATE links SET snapshot_path = ? WHERE id = ?", (path, r["id"]))
                    stats["snapshots"] += 1
            log(f"links: {stats['snapshots']} snapshots saved")

    if changed:
        marks = ",".join("?" * len(changed))
        items = [r[0] for r in conn.execute(
            f"SELECT DISTINCT item_id FROM item_links WHERE link_id IN ({marks})", changed)]
        for item_id in items:
            db.refresh_fts(conn, item_id)
    conn.commit()
    log(
        f"links: {stats['fetched']} fetched, {stats['ok']} ok, {stats['failed']} failed, "
        f"{stats['skipped_type']} other types"
    )
    return stats
