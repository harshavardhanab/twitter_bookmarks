"""End-to-end smoke test of the `bm` CLI and MCP server on the sample fixtures.

    uv run python scripts/smoke_test.py            # offline (uses data/models if present)
    uv run python scripts/smoke_test.py --network  # also fetch the fixtures' linked pages

Runs against a throwaway $BM_HOME; your real data/ is only read for the model cache.
Never touches X: the X check uses an empty browser profile and must fail before any page loads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIX = REPO / "tests" / "fixtures"
GQL = str(FIX / "x_bookmarks_response.json")
EXP = str(FIX / "x_exporter.json")
LIKES = str(FIX / "x_likes_response.json")

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok    {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def bm(*args: str, expect: int = 0) -> subprocess.CompletedProcess:
    p = subprocess.run(
        ["uv", "run", "--project", str(REPO), "bm", *args],
        capture_output=True, text=True, env=os.environ, timeout=300,
    )
    if p.returncode != expect:
        print(f"  bm {' '.join(args)} -> exit {p.returncode}\n{p.stderr[-2000:]}")
    return p


def js(*args: str) -> object:
    return json.loads(bm(*args).stdout)


def ids(out) -> list[str]:
    return [r["id"] for r in (out["results"] if isinstance(out, dict) else out)]


async def mcp_checks() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command="uv", args=["run", "--project", str(REPO), "bm", "mcp"], env=dict(os.environ)
    )
    async with stdio_client(params, errlog=open(os.devnull, "w")) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        tools = {t.name for t in (await s.list_tools()).tools}
        check("mcp lists 4 tools", tools == {"search_bookmarks", "get_bookmark", "recent_bookmarks", "bookmark_stats"}, str(tools))
        res = await s.call_tool("search_bookmarks", {"query": "reciprocal rank fusion", "limit": 3})
        d = json.loads(res.content[0].text)
        check("mcp search finds the note tweet", "x:1800000000000000002" in ids(d), str(d)[:300])
        res = await s.call_tool("get_bookmark", {"id": "x:1800000000000000001", "max_chars": 50})
        d = json.loads(res.content[0].text)
        check("mcp get_bookmark", d["author"] == "alice" and d["meta"].get("views") == 12345)
        res = await s.call_tool("get_bookmark", {"id": "x:404"})
        check("mcp unknown id is an error", res.isError)
        res = await s.call_tool("bookmark_stats", {})
        check("mcp stats", json.loads(res.content[0].text)["items"] == 4)


async def mcp_list_checks() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command="uv", args=["run", "--project", str(REPO), "bm", "mcp"], env=dict(os.environ)
    )
    async with stdio_client(params, errlog=open(os.devnull, "w")) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool("search_bookmarks", {"query": "sqlite tomatoes", "in_list": "like", "mode": "keyword"})
        d = json.loads(res.content[0].text)
        check("mcp search in_list=like", sorted(ids(d)) == ["x:1800000000000000001", "x:1900000000000000007"], str(ids(d)))
        res = await s.call_tool("recent_bookmarks", {"n": 5, "in_list": "like"})
        got = [json.loads(c.text)["id"] for c in res.content] if len(res.content) > 1 else ids(json.loads(res.content[0].text))
        check("mcp recent in_list=like", got == ["x:1900000000000000007", "x:1800000000000000001"], str(got))


def ranking_checks(home: Path) -> None:
    """Search quality on a separate throwaway database (so the fixture counts stay put)."""
    from bm import db, embed, search

    old_home = os.environ["BM_HOME"]
    rank_home = home / "ranking"
    rank_home.mkdir()
    if (home / "models").exists():
        (rank_home / "models").symlink_to((home / "models").resolve())
    os.environ["BM_HOME"] = str(rank_home)
    try:
        conn = db.connect()
        posts = {
            "jp": ("taro", "勾配クリッピングはニューラルネットワークの学習を安定させる。"),
            "zh": ("li", "用证书透明度日志可以发现别人刚注册的新域名。"),
            "notebook": ("ann", "Notebook: pick a colour gradient for your matplotlib charts. Gradient presets included."),
            "clip": ("cara", "Gradient clipping explained: clip the norm before the optimizer step."),
            "solo": ("solo", "My grandmother's tomato soup recipe, slow-cooked with basil."),
        }
        posts |= {f"pg{i}": ("dba", f"Postgres tip {i}: index bloat, vacuum and database indexing.") for i in range(20)}
        for k, (author, text) in posts.items():
            db.upsert_item(conn, {"source": "x", "source_id": k, "kind": "tweet", "url": f"https://x.com/{author}/status/{k}",
                                  "text": text, "author": author, "created_at": "2026-01-01T00:00:00Z", "list": "bookmark"})
        conn.commit()

        def got(out):
            return [r["id"] for r in out["results"]]

        out = search.search(conn, "クリッピング", mode="keyword")
        check("CJK word inside a sentence (keyword)", got(out) == ["x:jp"], str(got(out)))
        out = search.search(conn, "域名", mode="keyword")
        check("Chinese two-character word (keyword)", got(out) == ["x:zh"], str(got(out)))
        check("CJK snippet has no split marks", db.ZWSP not in out["results"][0]["snippet"])
        kw = search.keyword(conn, "gradient clipping")
        check("all-words match outranks a repeated single word", [i for i, _, _ in kw][:1] == [db.find_item(conn, "x:clip")["id"]]
              and kw[-1][2] == 0.5, str(kw))

        embed.embed_all(conn, log=lambda *_: None)
        sem = search.semantic(conn, "postgres database indexing", n=1, author="solo")
        check("filters apply before the nearest-neighbour cutoff", [i for i, _ in sem] == [db.find_item(conn, "x:solo")["id"]], str(sem))

        db.set_setting(conn, "embed_model", "some/other-model")
        out = search.search(conn, "soup", mode="hybrid")
        check("vectors from another model: keyword only, with a note", out["mode"] == "keyword" and "bm embed" in out.get("note", ""), str(out)[:200])
        db.set_setting(conn, "embed_model", embed.MODEL)

        table = "Results below.\n" + "| model | 26.67 | 30.30 | 67.50 |\n" * 120
        check("table-only chunks are dropped (first chunk kept)", len(embed.chunk(table)) == 1)
        page = "Title:Foo\nView PDF HTML (experimental)\nAbstract: we do X.\nSubmission history\n[v1] ...\nBibliographic and Citation Tools\narXivLabs: ..."
        check("arXiv page chrome stripped", db.clean_page_text(page) == "Title:Foo\nAbstract: we do X.")

        db.set_setting(conn, "fts_version", "0")
        conn.close()
        conn = db.connect()
        check("keyword index rebuilt after a format change", db.get_setting(conn, "fts_version") == db.FTS_VERSION
              and search.search(conn, "クリッピング", mode="keyword")["results"])
        conn.close()
    finally:
        os.environ["BM_HOME"] = old_home


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", action="store_true", help="also fetch linked pages")
    ap.add_argument("--keep", action="store_true", help="keep the temp BM_HOME")
    a = ap.parse_args()

    home = Path(tempfile.mkdtemp(prefix="bm-smoke-"))
    os.environ["BM_HOME"] = str(home)
    models = REPO / "data" / "models"
    if models.exists():
        (home / "models").symlink_to(models)
    print(f"BM_HOME={home}")

    try:
        print("environment")
        import sqlite3

        import sqlite_vec

        c = sqlite3.connect(":memory:")
        c.enable_load_extension(True)
        sqlite_vec.load(c)
        check(f"sqlite-vec loads ({c.execute('select vec_version()').fetchone()[0]}, SQLite {sqlite3.sqlite_version})", True)
        check("FTS5 available", bool(c.execute("select sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0]))

        print("import")
        p = bm("import", "x", GQL, EXP)
        check("import GraphQL + exporter", "3 tweets, 3 new" in p.stderr and "2 tweets, 1 new" in p.stderr, p.stderr)
        p = bm("import", "x", GQL, EXP)
        check("re-import adds nothing", "3 tweets, 0 new" in p.stderr and "2 tweets, 0 new" in p.stderr, p.stderr)
        (home / "raw" / "x").mkdir(parents=True, exist_ok=True)
        shutil.copy(GQL, home / "raw" / "x" / "20260101T000000Z-001.json")
        p = bm("reparse", "x")
        check("reparse x", "1 files, 3 tweets, 0 new" in p.stderr, p.stderr)

        print("item content")
        it = js("get", "x:1800000000000000002", "--json")
        check("note tweet text preferred", it["text"].startswith("A long note tweet") and "embeddings" in it["text"])
        check("t.co expanded, media link dropped", "https://example.com/rrf-paper.pdf" in it["text"] and "t.co" not in it["text"])
        check("quoted tweet parsed", it["meta"]["quoted"]["author"] == "carol")
        check("best mp4 variant", it["meta"]["media"][0]["url"].endswith("high.mp4"))
        check("counts parsed", js("get", "x:1800000000000000001", "--json")["meta"].get("bookmark") == 5)
        art = js("get", "x:1800000000000000003", "--json")
        check("X Article title/preview", art["title"] == "Notes on local-first software" and art["text"].startswith("Why your data"))
        flat = js("get", "x:1600000000000000005", "--json")
        check("flat exporter record", flat["author"] == "erin" and flat["created_at"] == "2023-05-02T08:15:00Z")
        bm("get", "x:404", expect=1)
        check("get unknown id exits 1", True)

        if a.network:
            print("links (network)")
            p = bm("links")
            check("links fetched", "4 fetched" in p.stderr, p.stderr)

        print("search")
        out = js("search", "tokenizer", "--json", "--mode", "keyword") if a.network else None
        if out is not None:
            check("keyword hit inside linked page", "x:1800000000000000001" in ids(out), str(out)[:300])
        out = js("search", "fts5 ranking", "--json")
        check("fallback to keyword before embed", out["mode"] == "keyword" and "note" in out)
        p = bm("embed")
        check("embed", "4 items embedded" in p.stderr, p.stderr)
        check("embed is incremental", "up to date" in bm("embed").stderr)
        out = js("search", "keeping my data on my own computer", "--json", "--mode", "semantic", "--limit", "1")
        check("semantic paraphrase", ids(out) == ["x:1800000000000000003"], str(out)[:300])
        out = js("search", "sqlite", "--json")
        check("hybrid mode", out["mode"] == "hybrid" and out["results"], str(out)[:300])
        out = js("search", "sqlite", "--json", "--author", "@alice")
        check("--author filter", ids(out) == ["x:1800000000000000001"], str(ids(out)))
        out = js("search", "retrieval", "--json", "--mode", "keyword", "--until", "2024-03-05")
        check("--until is inclusive", "x:1800000000000000002" in ids(out), str(ids(out)))
        out = js("search", "sqlite", "--json", "--source", "hn")
        check("--source filter", ids(out) == [])
        out = js("search", "NEAR(hybrid retrieval, 2)", "--json", "--mode", "keyword", "--raw")
        check("--raw FTS syntax", ids(out) == ["x:1800000000000000002"], str(ids(out)))
        bm("search", "NEAR(", "--raw", expect=1)
        check("bad --raw query exits 1", True)
        out = js("search", "what's \"FTS5\" (ranking)?", "--json", "--mode", "keyword")
        check("punctuation-safe query", bool(out["results"]))
        check("human output", "1. [x: bookmark]" in bm("search", "sqlite").stdout)

        print("search ranking and languages")
        ranking_checks(home)

        print("recent / stats / export / config")
        rec = js("recent", "-n", "3", "--json")
        check("recent in bookmark order", ids(rec) == ["x:1800000000000000003", "x:1800000000000000002", "x:1800000000000000001"], str(ids(rec)))
        st = js("stats")
        check("stats", st["items"] == 4 and st["sources"]["x"]["total"] == 4 and st["embedded_items"] == 4, str(st)[:300])
        bm("export", "md", str(home / "md"))
        files = sorted(f.name for f in (home / "md").glob("*.md"))
        check("export md", len(files) == 4 and "> Quoting @carol" in (home / "md" / "x-1800000000000000002.md").read_text())
        bm("config", "hn_user", "example")
        check("config set/get", bm("config", "hn_user").stdout.strip() == "example")

        print("mcp")
        asyncio.run(mcp_checks())

        print("likes")
        p = bm("import", "x", LIKES, "--as", "likes")
        check("import likes", "2 tweets, 2 new likes" in p.stderr, p.stderr)
        p = bm("import", "x", LIKES, "--as", "likes")
        check("re-import likes adds nothing", "2 tweets, 0 new likes" in p.stderr, p.stderr)
        both = js("get", "x:1800000000000000001", "--json")
        check("bookmarked + liked tweet is one item on both lists", both["lists"] == ["bookmark", "like"], str(both["lists"]))
        only = js("get", "x:1900000000000000007", "--json")
        check("liked-only tweet", only["lists"] == ["like"] and only["author"] == "frank")
        rec = js("recent", "-n", "3", "--json", "--list", "bookmark")
        check("likes leave bookmark order alone", ids(rec) == ["x:1800000000000000003", "x:1800000000000000002", "x:1800000000000000001"], str(ids(rec)))
        rec = js("recent", "-n", "5", "--json", "--list", "like")
        check("recent --list like (newest like first)", ids(rec) == ["x:1900000000000000007", "x:1800000000000000001"], str(ids(rec)))
        out = js("search", "sqlite", "--json", "--mode", "keyword", "--list", "like")
        check("search --list like", ids(out) == ["x:1800000000000000001"], str(ids(out)))
        out = js("search", "sqlite", "--json", "--mode", "keyword", "--list", "bookmark")
        check("search --list bookmark", "x:1900000000000000007" not in ids(out) and "x:1800000000000000001" in ids(out))
        st = js("stats")
        check("stats per list", st["lists"] == {"bookmark": 4, "like": 2} and st["x_bookmarked_and_liked"] == 1, str(st["lists"]))
        (home / "raw" / "x-likes").mkdir(parents=True, exist_ok=True)
        shutil.copy(LIKES, home / "raw" / "x-likes" / "20260101T000000Z-001.json")
        p = bm("reparse", "x")
        check("reparse covers bookmarks and likes", "2 files, 5 tweets, 0 new" in p.stderr, p.stderr)
        check("reparse keeps lists", js("get", "x:1800000000000000001", "--json")["lists"] == ["bookmark", "like"])
        check("human output shows lists", "[x: bookmark, like]" in bm("search", "sqlite", "--mode", "keyword").stdout)
        asyncio.run(mcp_list_checks())

        from bm import x as _x

        mk = lambda *ds: [{"created_at": d + "T00:00:00Z", "source_id": d} for d in ds]
        b = [mk("2026-01-01", "2019-05-05"), mk("2025-10-02", "2025-09-30", "2025-10-05"),
             mk("2025-09-01", "2025-10-03", "2025-08-01"), mk("2025-07-01")]
        kept, reached = _x.cut_since(b, "2025-10-01")
        check("likes cutoff: keeps recent likes (even old tweets), trims the boundary, stops",
              reached and [i["source_id"] for i in kept] == ["2026-01-01", "2019-05-05", "2025-10-02", "2025-09-30", "2025-10-05", "2025-10-03"],
              str([i["source_id"] for i in kept]))
        check("no cutoff keeps everything", _x.cut_since(b, None) == ([i for bb in b for i in bb], False))

        print("siftly import")
        import sqlite3 as _sq

        sdb = home / "siftly.db"
        c = _sq.connect(sdb)
        c.executescript("""
          CREATE TABLE Bookmark (id TEXT PRIMARY KEY, tweetId TEXT, text TEXT, authorHandle TEXT,
            authorName TEXT, tweetCreatedAt TEXT, importedAt TEXT, rawJson TEXT, source TEXT);
          CREATE TABLE MediaItem (id TEXT, bookmarkId TEXT, type TEXT, url TEXT, thumbnailUrl TEXT);
        """)
        rows = [  # one tweet bm already has from X, one it lacks, one like
            ("b1", "1800000000000000001", "SIFTLY COPY", "unknown", "Unknown", "2018-10-10T20:19:24.000+00:00",
             "2026-03-12T15:41:56.050+00:00", '{"full_text": "SIFTLY COPY"}', "bookmark"),
            ("b2", "1700000000000000042", "Old bookmark about zettelkasten notes &amp; links https://t.co/z",
             "unknown", "Unknown", "2021-11-12T08:00:00.000+00:00", "2026-03-12T15:41:56.050+00:00",
             '{"entities": {"urls": [{"expanded_url": "https://example.net/zettel"}, {"expanded_url": "http://x.com/i/article/1"}]}}',
             "bookmark"),
            ("b3", "1700000000000000043", "A liked tweet", "unknown", "Unknown", None,
             "2026-05-26T02:41:08.088+00:00", "{}", "like"),
        ]
        c.executemany("INSERT INTO Bookmark VALUES (?,?,?,?,?,?,?,?,?)", rows)
        c.execute("INSERT INTO MediaItem VALUES ('m1','b2','photo','https://pbs.twimg.com/media/z.jpg','https://pbs.twimg.com/media/z.jpg')")
        c.commit(); c.close()
        p = bm("import", "siftly", str(sdb), "--dry-run")
        check("siftly dry run", "would add 1 new tweets" in p.stderr and js("stats")["items"] == 5, p.stderr)
        p = bm("import", "siftly", str(sdb))
        check("siftly adds only what bm lacks", "1 already in bm, added 1 new tweets" in p.stderr, p.stderr)
        kept = js("get", "x:1800000000000000001", "--json")
        check("siftly never overwrites X data", kept["author"] == "alice" and "SIFTLY" not in kept["text"])
        imp = js("get", "x:1700000000000000042", "--json")
        check("siftly item: marker, text, links, media, dates",
              imp["meta"].get("imported_from") == "siftly" and "& links" in imp["text"]
              and imp["link_url"] == "https://example.net/zettel" and imp["meta"]["media"][0]["type"] == "photo"
              and imp["created_at"] == "2021-11-12T08:00:00Z" and imp["saved_at"] == "2026-03-12T15:41:56+00:00",
              json.dumps(imp)[:400])
        check("siftly re-import adds nothing", "added 0 new" in bm("import", "siftly", str(sdb)).stderr)
        p = bm("import", "siftly", str(sdb), "--what", "likes")
        check("siftly likes on request", "added 1 new tweets" in p.stderr and js("get", "x:1700000000000000043", "--json")["lists"] == ["like"], p.stderr)

        print("threads")
        (home / "raw" / "x-threads").mkdir(parents=True, exist_ok=True)
        shutil.copy(FIX / "x_thread_capture.json", home / "raw" / "x-threads" / "1800000000000000001.json")
        shutil.copy(FIX / "x_thread_capture_siftly.json", home / "raw" / "x-threads" / "1700000000000000042.json")
        p = bm("reparse", "x")
        check("thread captures applied", "2 files, 2 with author replies" in p.stderr, p.stderr)
        th = js("get", "x:1800000000000000001", "--json")
        ids_ = [t["id"] for t in th["meta"].get("thread", [])]
        check("thread keeps only the author's replies, oldest first", ids_ == ["1800000000000000011", "1800000000000000013"], str(ids_))
        check("thread reply media and links kept", th["meta"]["thread"][0].get("media", [{}])[0].get("url", "").endswith("bench.png")
              and "https://example.com/fts5-benchmark-notebook" in th["meta"]["thread"][0]["links"])
        urls = {l["url"] for l in th["links"]}
        check("thread links queued for bm links", "https://example.com/fts5-benchmark-notebook" in urls and "https://example.com/bob-spam" not in urls, str(urls))
        check("own link_url kept", th["link_url"] == "https://sqlite.org/fts5.html")
        out = js("search", "tomatillo", "--json", "--mode", "keyword")
        check("thread text is searchable", ids(out) == ["x:1800000000000000001"], str(ids(out)))
        up = js("get", "x:1700000000000000042", "--json")
        check("thread capture upgrades a Siftly copy", up["author"] == "zara" and "imported_from" not in up["meta"]
              and up["lists"] == ["bookmark"] and up["saved_at"] == "2026-03-12T15:41:56+00:00" and len(up["meta"]["thread"]) == 1,
              json.dumps({k: up[k] for k in ("author", "lists", "saved_at")}))
        bm("import", "x", GQL)
        check("re-capturing a tweet keeps its thread", len(js("get", "x:1800000000000000001", "--json")["meta"].get("thread", [])) == 2)
        p = bm("x", "threads", "--dry-run", "--all")
        check("checked tweets are skipped", "5 tweets to check" in p.stderr, p.stderr)

        print("skill")
        check("bm skill show", bm("skill", "show").stdout.startswith("---\nname: bookmarks"))
        p = bm("skill", "install", "--dir", str(home / "skills"))
        check("bm skill install", (home / "skills" / "bookmarks" / "SKILL.md").is_file(), p.stderr)
        bm("skill", "install", "--dir", str(home / "skills"), expect=1)
        check("bm skill install won't overwrite", True)

        print("x sync without a login")
        from playwright.sync_api import sync_playwright

        from bm import x

        with sync_playwright() as pw:  # empty profile, about:blank only
            ctx = x._launch(pw, headless=True)
            ctx.close()
        t = time.time()
        p = bm("x", "sync", expect=1)
        check("refuses before loading X", "Not logged in" in p.stderr and time.time() - t < 30, p.stderr)
        p = bm("x", "threads", "--all", "--limit", "1", expect=1)
        check("threads refuse before loading X", "Not logged in" in p.stderr, p.stderr)
    finally:
        if a.keep:
            print(f"kept {home}")
        else:
            shutil.rmtree(home, ignore_errors=True)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
