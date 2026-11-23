# bm: your X bookmarks, searchable by any LLM

`bm` keeps a **local, searchable mirror** of everything you save:

- **X (Twitter) bookmarks and likes**, captured from your own logged-in browser
  session. There's no paid API and no third-party service.
- **Hacker News favorites** (stories and comments).
- The **full text of the pages and PDFs they link to**.
- The **author's own replies** to a saved tweet. That's often where the actual link
  or the rest of a 🧵 lives.

Search is **hybrid**: SQLite FTS5 keyword ranking fused with local embeddings
(`BAAI/bge-small-en-v1.5` via [fastembed](https://github.com/qdrant/fastembed), stored
with [sqlite-vec](https://github.com/asg017/sqlite-vec)). Everything runs on your
machine. No API keys are needed, and nothing leaves your computer except requests to
X, HN and the linked sites.

Any LLM agent can use it through:
- the `bm` **CLI**,
- an **MCP server** (`bm mcp`) for Claude Code, Claude Desktop, Cursor, Codex and Gemini CLI,
- an **Agent Skill** (`SKILL.md`),
- or a **Markdown export** for chat-only tools.

> **Disclaimer.** This is an unofficial tool, not affiliated with X Corp. or Y Combinator.
> It reads X's web app in a real browser that **you** sign in to. Automated use of
> X's website may be against [X's Terms of Service](https://x.com/en/tos) and could
> get an account rate-limited or flagged. Use it only with your own account, keep
> syncs infrequent (daily is plenty), and use it at your own risk. `bm` never asks
> for or stores your password; you sign in yourself in a normal Chrome window.

## Requirements

- **macOS** (tested). Linux should work for everything except the launchd job, but
  it is untested.
- [**uv**](https://docs.astral.sh/uv/) (it installs Python 3.12 or 3.13 for you).
- **Google Chrome**, for X capture.
- About 70 MB for the embedding model, downloaded on first use.

## Install

**As a tool** (data goes in `~/.local/share/bm`, or wherever `$BM_HOME` points):

```bash
uv tool install git+https://github.com/harshavardhanab/twitter_bookmarks
```

**From a clone** (needed for the launchd scripts, or if you want to hack on it):

```bash
git clone https://github.com/harshavardhanab/twitter_bookmarks
cd twitter_bookmarks
uv sync
uv tool install -e .        # puts `bm` on your PATH; data goes in ./data
```

## Quick start

```bash
# Hacker News (public profile, no login)
bm config hn_user <your-hn-username>
bm hn sync

# X: sign in once in the Chrome window bm opens (any sign-in method)
bm x login
bm x sync --full --headed   # first run: walk all bookmarks, then likes, and watch it
bm x threads                # capture the author's replies of thread-like tweets

# Fetch linked pages, then build the semantic index
bm links
bm embed

bm search "running LLMs locally"
```

After the first run, `bm sync` does everything incrementally: HN, then X (bookmarks
and likes), then up to 50 thread checks, then links, then embeddings.

## Searching

```bash
bm search "postgres index bloat"                 # hybrid (default)
bm search "sqlite-vec" --mode keyword            # exact terms
bm search "agent memory" --mode semantic         # meaning only
bm search "rust async" --source x --list like --since 2025-01-01 --author someone
bm search 'NEAR(vector search, 3)' --raw         # FTS5 syntax: "phrase", NEAR, prefix*
bm get x:1812345678901234567                      # full item: thread, quoted tweet, page text
bm recent -n 20 --list bookmark                  # newest saved first
bm stats                                         # counts, link status, last syncs
```

Ids are `x:<tweet id>` and `hn:<item id>`. Every result carries `lists`: `bookmark`
and/or `like` for X, `favorite` for HN. Add `--json` for machine-readable output.
Until embeddings exist, search falls back to keyword mode and says so.

## Use it from LLM agents

### MCP server

`bm mcp` is a stdio MCP server with four tools: `search_bookmarks`, `get_bookmark`,
`recent_bookmarks` and `bookmark_stats`. GUI apps don't inherit your shell's PATH, so
use the absolute path of `bm` (`which bm`; usually `~/.local/bin/bm`). Replace
`/Users/YOU` below.

**Claude Code**
```bash
claude mcp add --scope user bookmarks -- ~/.local/bin/bm mcp
```

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`),
**Cursor** (`~/.cursor/mcp.json`) and **Gemini CLI** (`~/.gemini/settings.json`) use the
same shape. Merge it into the existing `mcpServers` object:
```json
{
  "mcpServers": {
    "bookmarks": { "command": "/Users/YOU/.local/bin/bm", "args": ["mcp"] }
  }
}
```

**Codex** (`~/.codex/config.toml`):
```toml
[mcp_servers.bookmarks]
command = "/Users/YOU/.local/bin/bm"
args = ["mcp"]
```

Restart the app, or start a new session, then just ask: *"What did I bookmark about
vector databases?"* or *"Find the thread I liked about Postgres indexing."*

### Agent Skill

For agents that read `SKILL.md` skills (such as Claude Code):

```bash
bm skill install                     # copies it to ~/.claude/skills/bookmarks/
bm skill install --dir <skills-dir>  # another agent's skills folder
```

The skill teaches the agent to try a few phrasings, open hits with `bm get`, and cite
URLs. It needs `bm` on the PATH.

### Chat-only LLMs

```bash
bm export md ~/bookmarks-md          # one Markdown file per item, ready to upload
```

## X: what gets captured

`bm x sync` opens `x.com/i/bookmarks` and `x.com/<you>/likes` with your saved session
and records the GraphQL responses the page receives. `bm` never calls X's API directly.
It stops when it reaches items it already has (or the end of the list with `--full`),
with limits of 5,000 scrolls and 60 minutes per list. Raw responses are kept under
`raw/`, so `bm reparse x` can re-run the parser at any time.

For each tweet it stores:
- the **full text**, including long tweets, with `t.co` links expanded;
- the **quoted tweet**;
- **media URLs** (the photo, and the best-quality mp4);
- **X Article** title and preview;
- engagement counts.

A tweet that is both bookmarked and liked is a single item on both lists.

- **Likes cutoff:** `bm config x_likes_since 2025-10-01` stops syncs at likes of tweets
  posted before that date. X doesn't expose when you liked something, so the post
  date is the proxy, and an old tweet you liked recently is still kept.
- **Threads:** `bm x threads` opens tweets whose text points to a thread or the replies
  (🧵, 👇, "1/", "link below", "in the replies"...) and keeps the **author's own**
  tweets in that conversation. `--all` checks every saved tweet. Each tweet is checked
  once, with a 4–8 s pause between tweets and a 45-minute cap per run.
- **Other imports:** `bm import x FILE... [--as likes]` reads
  [twitter-web-exporter](https://github.com/prinsss/twitter-web-exporter) JSON or raw
  GraphQL responses. `bm import siftly PATH/prisma/dev.db` adds items from a
  [Siftly](https://github.com/viperrcrypto/Siftly) database; it is insert-only and never
  overwrites data captured from X.

## Daily sync

**launchd (macOS, from a clone):**
```bash
scripts/install-launchd.sh --dry-run     # show the plist, change nothing
scripts/install-launchd.sh --at 09:30    # install and load the LaunchAgent
launchctl kickstart gui/$(id -u)/local.bm.sync   # run it once now
scripts/install-launchd.sh --uninstall
```
It runs `bm sync` through `scripts/bm-sync.sh`, writes `logs/sync-YYYY-MM-DD.log`
(30 days are kept), and skips a run if another sync is still going.

**cron (anywhere):**
```cron
30 9 * * * $HOME/.local/bin/bm sync >> $HOME/.local/share/bm/sync.log 2>&1
```

## Data and privacy

The data folder is `$BM_HOME` if set; otherwise `./data` in a clone; otherwise
`~/.local/share/bm`. It contains:

| Path | What |
|------|------|
| `bookmarks.db` | SQLite: items, lists, links, FTS index, chunks, vectors, settings |
| `x-profile/` | Chrome profile holding **your X session cookies**. Treat it like a password. |
| `raw/x/`, `raw/x-likes/`, `raw/x-threads/` | Raw X responses, for re-parsing |
| `models/` | Embedding model cache |
| `logs/`, `snapshots/` | Sync logs; optional HTML snapshots (`bm links --snapshot`, needs `monolith`) |

- Items are never deleted; `last_seen_at` records when a sync last saw each one.
- HN requests use the generic User-Agent `bm-bookmarks-mirror/0.1`. Linked pages are
  fetched with a browser User-Agent, at most 2 requests at a time per site, and
  rate-limit (429) responses are retried politely.

## How it works

```
X web app ──(your Chrome session, Playwright)──► raw GraphQL JSON ─┐
HN favorites pages + Firebase/Algolia APIs ────────────────────────┼─► SQLite items + lists
linked pages (httpx → trafilatura / pypdf) ────────────────────────┘        │
                                                                             ├─ FTS5 (bm25)
                                                                             └─ sqlite-vec (bge-small chunks)
                                       CLI / MCP / Skill ◄── hybrid search (reciprocal rank fusion)
```

| Module | Role |
|--------|------|
| `src/bm/db.py` | Schema, upserts, list membership, FTS documents |
| `src/bm/x.py` | X parsing, login, capture |
| `src/bm/threads.py` | Thread capture |
| `src/bm/hn.py` | HN favorites |
| `src/bm/links.py` | Linked-page fetching |
| `src/bm/embed.py`, `src/bm/search.py` | Embeddings and hybrid search |
| `src/bm/api.py`, `src/bm/cli.py`, `src/bm/mcp_server.py` | Read operations, CLI and MCP server |

## Development

```bash
uv sync
uv run python scripts/smoke_test.py            # ~70 offline end-to-end checks on tests/fixtures/
uv run python scripts/smoke_test.py --network  # also fetches the fixtures' linked pages
```

The smoke test uses a throwaway `$BM_HOME` and never contacts X. It covers parsing,
import and reparse, likes and lists, the likes cutoff, thread capture, the Siftly
import, every search mode and filter, the CLI, and all MCP tools over stdio.

## Troubleshooting

- **`Not logged in to X`:** run `bm x login`. Syncs check for the session cookie before
  loading any X page.
- **`Chrome is still running with the X profile`:** close the window that `bm`
  opened (Cmd+Q), then retry.
- **`No bookmarks data came back from X`:** the session expired or X changed its page.
  Try `bm x sync --headed` to watch, or use an exporter import.
- **Search says "keyword only":** run `bm embed`.

## License

[MIT](LICENSE)
