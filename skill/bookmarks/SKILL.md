---
name: bookmarks
description: Search the owner's saved X (Twitter) bookmarks and likes and Hacker News favorites, including the full text of the pages they link to. Use whenever the owner asks about something they saved, bookmarked, favorited, starred, liked or "read somewhere" on X/Twitter or Hacker News, e.g. "that thread I bookmarked about Postgres indexing", "what did I save about RAG?", "find the HN post on self-hosting I favorited", "what have I bookmarked recently?".
---

# Bookmarks

The owner's X bookmarks and likes and HN favorites are mirrored locally and searchable with the
`bm` command (keyword + semantic search over the posts *and* the linked articles/PDFs).
If the `bookmarks` MCP server is connected, its tools (`search_bookmarks`,
`get_bookmark`, `recent_bookmarks`, `bookmark_stats`) do the same thing; prefer them.

## How to search

1. Run 2 or 3 searches with different phrasings: the owner's words, a synonym-heavy
   version, and the likely title or key term. Semantic search catches paraphrases;
   keyword search catches exact names (libraries, people, products).
   ```bash
   bm search "postgres index bloat" --json --limit 8
   bm search "vacuum autovacuum dead tuples" --json --limit 8
   ```
2. Narrow when the owner gives hints:
   `--source x|hn`, `--list bookmark|like|favorite` (when the owner says "I liked" or
   "I bookmarked"; otherwise search everything), `--author <handle>`,
   `--since YYYY-MM-DD`, `--until YYYY-MM-DD`
   (dates are when the post was written, not when it was saved),
   `--mode keyword` for exact terms, `--raw` for FTS5 syntax (`"exact phrase"`, `NEAR(a b, 5)`, `term*`).
3. Open the promising hits for detail (post text, quoted tweet, the author's thread
   replies in `meta.thread`, linked page text):
   ```bash
   bm get x:1812345678901234567 --json --max-chars 6000
   ```
4. Answer from what you read and **cite the URLs**: the post's `url` and the
   article's `link_url` when the content came from the linked page.

For "what did I save recently?" use `bm recent -n 20 --json` (newest first); for
"what did I like recently?" add `--list like`. Each result's `lists` says whether it
was bookmarked, liked or both.
`bm stats` shows counts and when each source was last synced; mention it if the owner
expects something that isn't there (it may not be synced yet).

## Notes

- Ids are `x:<tweet id>` or `hn:<item id>`.
- Items with `meta.imported_from: "siftly"` came from an older tool: no author, and
  their `t.co` links may be unexpanded. Say so if the author matters; the `url` still
  opens the tweet.
- A result's `snippet` is only an excerpt. Use `bm get` before quoting or summarizing.
- If search reports "keyword only", embeddings are missing. `bm embed` builds them,
  but ask before running a sync or embed: they change the owner's data and can take a while.
- Don't run `bm x login`. The owner signs in to X themselves.
