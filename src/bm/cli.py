"""`bm` command line."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap

from . import db

SOURCES = ("x", "hn")
LIST_NAMES = ("bookmark", "like", "favorite")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _fail(msg: str) -> int:
    print(f"bm: {msg}", file=sys.stderr)
    return 1


# --- commands ------------------------------------------------------------------

def cmd_config(a, conn):
    if a.key is None:
        for r in conn.execute("SELECT key, value FROM settings ORDER BY key"):
            print(f"{r['key']} = {r['value']}")
    elif a.value is None:
        v = db.get_setting(conn, a.key)
        if v is None:
            return _fail(f"{a.key} is not set")
        print(v)
    else:
        db.set_setting(conn, a.key, a.value)
    return 0


def _run_hn(conn, user=None, full=False):
    from . import hn

    hn.sync(conn, user=user, full=full, log=_log)
    db.set_setting(conn, "last_sync_hn", db.now())


def _run_x(conn, what="all", full=False, headed=False) -> list[str]:
    """Sync X bookmarks and/or likes; returns the lists that failed."""
    from . import x

    failed = []
    for w in (("bookmarks", "likes") if what == "all" else (what,)):
        try:
            x.sync(conn, what=w, full=full, headed=headed, log=_log)
            db.set_setting(conn, f"last_sync_x_{w}", db.now())
        except x.XError as e:
            if "Not logged in" in str(e):
                raise  # same for every list
            failed.append(f"x {w}")
            _log(f"x {w}: failed: {e}")
    return failed


def cmd_sync(a, conn):
    """hn -> x -> links -> embed; one failing step doesn't stop the others."""
    from . import x

    failed = []
    if a.source in ("hn", "all"):
        if not db.get_setting(conn, "hn_user"):
            _log("hn: skipped (set your username: bm config hn_user <name>)")
        else:
            try:
                _run_hn(conn, full=a.full)
            except Exception as e:
                failed.append("hn")
                _log(f"hn: failed: {e}")
    if a.source in ("x", "all"):
        if not x.profile_dir().exists():
            _log("x: skipped (no X session yet: run bm x login)")
        else:
            try:
                failed += _run_x(conn, full=a.full)
                from . import threads

                try:  # replies of newly saved tweets; the rest wait for the next run
                    threads.capture(conn, limit=50, log=_log)
                except Exception as e:
                    failed.append("x threads")
                    _log(f"x threads: failed: {e}")
            except Exception as e:
                failed.append("x")
                _log(f"x: failed: {e}")
    if not a.no_links:
        from . import links

        try:
            links.fetch_links(conn, log=_log)
        except Exception as e:
            failed.append("links")
            _log(f"links: failed: {e}")
    if not a.no_embed:
        from . import embed

        try:
            embed.embed_all(conn, log=_log)
        except Exception as e:
            failed.append("embed")
            _log(f"embed: failed: {e}")
    if failed:
        return _fail("sync finished with errors in: " + ", ".join(failed))
    return 0


def cmd_hn(a, conn):
    _run_hn(conn, user=a.user, full=a.full)
    return 0


def cmd_x(a, conn):
    from . import x

    if a.x_cmd == "login":
        ok = x.login(log=_log)
        return 0 if ok else _fail("window closed before login finished")
    if a.x_cmd == "threads":
        from . import threads

        threads.capture(conn, all_=a.all, recheck=a.recheck, limit=a.limit,
                        headed=a.headed, dry_run=a.dry_run, log=_log)
        return 0
    failed = _run_x(conn, what=a.what, full=a.full, headed=a.headed)
    return _fail("failed: " + ", ".join(failed)) if failed else 0


def cmd_reparse(a, conn):
    from . import threads, x

    x.reparse(conn, log=_log)
    threads.reparse(conn, log=_log)
    return 0


def cmd_import(a, conn):
    from . import x

    x.import_files(conn, a.files, what=a.as_list, log=_log)
    return 0


def cmd_import_siftly(a, conn):
    from . import siftly

    siftly.import_db(conn, a.path, what=a.what, dry_run=a.dry_run, log=_log)
    if not a.dry_run:
        _log("next: bm links && bm embed (fetch their linked pages, index them)")
    return 0


def cmd_links(a, conn):
    from . import links

    links.fetch_links(conn, retry=a.retry, snapshots=a.snapshot, log=_log)
    return 0


def cmd_embed(a, conn):
    from . import embed

    embed.embed_all(conn, log=_log)
    return 0


def _print_results(results: list[dict]) -> None:
    for n, r in enumerate(results, 1):
        title = r.get("title") or (r.get("snippet") or r.get("text") or "")[:80]
        when = (r.get("created_at") or "")[:10]
        lists = ", ".join(r.get("lists") or [])
        print(f"{n}. [{r['source']}{': ' + lists if lists else ''}] {title}")
        print(f"   {r.get('author') or '?'} · {when} · {r['id']}")
        body = r.get("snippet") or r.get("text")
        if body and body[:80] != title:
            print(textwrap.fill(body, 100, initial_indent="   ", subsequent_indent="   "))
        print(f"   {r['url']}" + (f"\n   -> {r['link_url']}" if r.get("link_url") else ""))
        print()


def cmd_search(a, conn):
    from . import search

    try:
        out = search.search(
            conn, " ".join(a.query), mode=a.mode, limit=a.limit, raw=a.raw,
            source=a.source, since=a.since, until=a.until, author=a.author,
            in_list=a.in_list,
        )
    except ValueError as e:
        return _fail(str(e))
    if a.json:
        _json(out)
    elif not out["results"]:
        print("No matches.")
    else:
        _print_results(out["results"])
    return 0


def cmd_get(a, conn):
    from . import api

    item = api.get_item(conn, a.id, max_chars=a.max_chars)
    if item is None:
        return _fail(f"no bookmark {a.id!r} (ids look like x:123 or hn:456)")
    if a.json:
        _json(item)
    else:
        print(api.to_markdown(item))
    return 0


def cmd_recent(a, conn):
    from . import api

    items = api.recent(conn, a.n, a.source, a.in_list)
    if a.json:
        _json(items)
    else:
        _print_results(items)
    return 0


def cmd_stats(a, conn):
    from . import api

    _json(api.stats(conn))
    return 0


def cmd_translate_setup(a, conn):
    from . import translate

    try:
        translate.setup()
    except Exception as e:
        return _fail(f"could not download the translation model: {e}")
    _log("English translation model is ready")
    return 0


def cmd_export(a, conn):
    from . import api

    n = api.export_md(conn, a.outdir)
    _log(f"exported {n} bookmarks to {a.outdir}")
    return 0


def _skill_source():
    """SKILL.md from the installed package, or from the source checkout."""
    from importlib import resources
    from pathlib import Path

    packaged = resources.files("bm") / "skill" / "SKILL.md"
    if packaged.is_file():
        return packaged
    checkout = Path(__file__).resolve().parents[2] / "skill" / "bookmarks" / "SKILL.md"
    return checkout if checkout.is_file() else None


def cmd_skill(a, conn):
    from pathlib import Path

    src = _skill_source()
    if src is None:
        return _fail("SKILL.md not found in this installation")
    if a.skill_cmd == "show":
        print(src.read_text(encoding="utf-8"))
        return 0
    dest = Path(a.dir).expanduser() / "bookmarks"
    target = dest / "SKILL.md"
    if dest.is_symlink() or (target.exists() and not a.force):
        return _fail(f"{dest} already exists (use --force to overwrite a copied skill)")
    dest.mkdir(parents=True, exist_ok=True)
    target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    _log(f"installed the bookmarks skill to {target}")
    return 0


def cmd_mcp(a, conn):
    conn.close()
    from . import mcp_server

    mcp_server.main()
    return 0


# --- parser --------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bm", description="Local mirror of X bookmarks and likes, and HN favorites")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("config", help="show or set settings (e.g. hn_user)")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("sync", help="sync sources, fetch links, embed")
    s.add_argument("--source", choices=("hn", "x", "all"), default="all")
    s.add_argument("--full", action="store_true", help="walk the whole list, not just new items")
    s.add_argument("--no-links", action="store_true")
    s.add_argument("--no-embed", action="store_true")
    s.set_defaults(fn=cmd_sync)

    hn = sub.add_parser("hn", help="Hacker News favorites").add_subparsers(dest="hn_cmd", required=True)
    s = hn.add_parser("sync")
    s.add_argument("--user")
    s.add_argument("--full", action="store_true")
    s.set_defaults(fn=cmd_hn)

    xp = sub.add_parser("x", help="X bookmarks and likes (browser capture)").add_subparsers(dest="x_cmd", required=True)
    s = xp.add_parser("login", help="open Chrome to sign in to X yourself")
    s.set_defaults(fn=cmd_x)
    s = xp.add_parser("sync")
    s.add_argument("--what", choices=("all", "bookmarks", "likes"), default="all")
    s.add_argument("--full", action="store_true")
    s.add_argument("--headed", action="store_true", help="show the browser window")
    s.set_defaults(fn=cmd_x)
    s = xp.add_parser("threads", help="capture the author's replies of saved tweets (threads, 'link below')")
    s.add_argument("--all", action="store_true", help="check every saved tweet, not just likely threads")
    s.add_argument("--recheck", action="store_true", help="check tweets already checked again")
    s.add_argument("--limit", type=int, help="at most this many tweets this run")
    s.add_argument("--headed", action="store_true", help="show the browser window")
    s.add_argument("--dry-run", action="store_true", help="only count the tweets to check")
    s.set_defaults(fn=cmd_x)

    rp = sub.add_parser("reparse", help="re-run a parser over saved raw data")
    rp.add_argument("what", choices=("x",))
    rp.set_defaults(fn=cmd_reparse)

    ip = sub.add_parser("import", help="import files").add_subparsers(dest="import_cmd", required=True)
    s = ip.add_parser("x", help="twitter-web-exporter JSON or raw GraphQL responses")
    s.add_argument("files", nargs="+")
    s.add_argument("--as", dest="as_list", choices=("bookmarks", "likes"), default="bookmarks",
                   help="which list these tweets belong to")
    s.set_defaults(fn=cmd_import)
    s = ip.add_parser("siftly", help="add tweets bm lacks from a Siftly prisma/dev.db (insert-only)")
    s.add_argument("path", help="path to Siftly's prisma/dev.db")
    s.add_argument("--what", choices=("bookmarks", "likes", "all"), default="bookmarks")
    s.add_argument("--dry-run", action="store_true", help="only count what would be added")
    s.set_defaults(fn=cmd_import_siftly)

    s = sub.add_parser("links", help="fetch linked pages")
    s.add_argument("--retry", action="store_true", help="retry all failed links now")
    s.add_argument("--snapshot", action="store_true", help="save HTML snapshots with monolith")
    s.set_defaults(fn=cmd_links)

    sub.add_parser("embed", help="compute embeddings for semantic search").set_defaults(fn=cmd_embed)
    sub.add_parser("translate-setup", help="download the local English translation model").set_defaults(
        fn=cmd_translate_setup,
    )

    s = sub.add_parser("search", help="search bookmarks")
    s.add_argument("query", nargs="+")
    s.add_argument("--source", choices=SOURCES)
    s.add_argument("--since", help="YYYY-MM-DD (by post date)")
    s.add_argument("--until", help="YYYY-MM-DD, inclusive")
    s.add_argument("--author")
    s.add_argument("--list", dest="in_list", choices=LIST_NAMES,
                   help="only bookmarks, likes (X) or favorites (HN)")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--mode", choices=("hybrid", "keyword", "semantic"), default="hybrid")
    s.add_argument("--raw", action="store_true", help="pass the query to FTS5 as is")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("get", help="show one bookmark with its linked page text")
    s.add_argument("id")
    s.add_argument("--max-chars", type=int, default=4000)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_get)

    s = sub.add_parser("recent", help="most recently saved bookmarks")
    s.add_argument("-n", type=int, default=20)
    s.add_argument("--source", choices=SOURCES)
    s.add_argument("--list", dest="in_list", choices=LIST_NAMES)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_recent)

    sub.add_parser("stats", help="counts and sync status (JSON)").set_defaults(fn=cmd_stats)

    ep = sub.add_parser("export", help="export bookmarks").add_subparsers(dest="export_cmd", required=True)
    s = ep.add_parser("md", help="one Markdown file per bookmark")
    s.add_argument("outdir")
    s.set_defaults(fn=cmd_export)

    sub.add_parser("mcp", help="run the MCP server on stdio").set_defaults(fn=cmd_mcp)

    sk = sub.add_parser("skill", help="the Agent Skill (SKILL.md) for LLM agents").add_subparsers(dest="skill_cmd", required=True)
    s = sk.add_parser("install", help="copy SKILL.md into an agent's skills folder")
    s.add_argument("--dir", default="~/.claude/skills", help="skills folder (default ~/.claude/skills)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_skill)
    sk.add_parser("show", help="print SKILL.md").set_defaults(fn=cmd_skill)
    return p


def main(argv: list[str] | None = None) -> None:
    import warnings

    warnings.filterwarnings("ignore", message=".*SOCKS support in urllib3.*")
    args = parser().parse_args(argv)
    conn = db.connect()
    try:
        code = args.fn(args, conn)
    except KeyboardInterrupt:
        code = 130
    except Exception as e:
        from .hn import HNError
        from .siftly import SiftlyError
        from .x import XError

        if isinstance(e, (HNError, XError, SiftlyError)):
            code = _fail(str(e))
        else:
            raise
    finally:
        try:
            conn.close()
        except Exception:
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
