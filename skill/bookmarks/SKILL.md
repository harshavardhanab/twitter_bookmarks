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

## When results are weak or empty

An empty or off-topic result does not prove the owner never saved it. Before saying so:

- **Check `mode` and `note`** in the JSON. `"mode": "keyword"` with a `note` means
  semantic search was unavailable (no embeddings, a model change awaiting `bm embed`, or
  a model load failure), so paraphrases and other-language posts were not searched.
  Say that in your answer.
- **Retry in semantic mode** with a plain-language description of the idea
  (`--mode semantic`), and in keyword mode with one or two distinctive words
  (a name, product, library, number).
- **Drop the filters** (`--author`, dates, `--list`) if a filtered search is empty: the
  owner may remember them wrong.
- **Try the original language** when there's a hint (a Chinese author, a Japanese
  product, "a thread in Spanish"): search the key term in that language too.
  Chinese, Japanese and Korean terms work in keyword mode.

## Posts in other languages

When an English query misses a post, try the key term in the post's likely language.
Present tweet text in English. Use the translations from `bm` or the MCP tool, and
translate any original-language text returned by an older version before showing it.
If a translation is unavailable, share the post link and say so.

For recent X bookmarks use `recent_bookmarks(in_list="bookmark")` or
`bm recent -n 20 --list bookmark --json`. Use `like` for X likes. Preserve the
returned order. `first_seen_at` (legacy alias `saved_at`) is the local discovery
time. Each result's `lists` shows its list membership.
`bm stats` shows counts and when each source was last synced; mention it if the owner
expects something that isn't there (it may not be synced yet).

## Notes

- Ids are `x:<tweet id>` or `hn:<item id>`.
- Items with `meta.imported_from: "siftly"` came from an older tool: no author, and
  their `t.co` links may be unexpanded. Say so if the author matters; the `url` still
  opens the tweet.
- A result's `snippet` is only an excerpt. Use `bm get` before quoting or summarizing.
- If search reports "keyword only", embeddings are missing or out of date. `bm embed`
  builds them, but ask before running a sync or embed: they change the owner's data and
  can take a while (a full rebuild of several thousand posts takes 10+ minutes).
- Don't run `bm x login`. The owner signs in to X themselves.
