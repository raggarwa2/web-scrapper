# Instagram — Apify Capability Notes

Reference doc for what's actually possible (and what isn't) scraping Instagram
via Apify for this project. Covers what's been tested, what's live, and what's
been ruled out — so we don't re-discover the same actor-schema quirks or
re-litigate the same dead ends later.

Related files:
- `ig_hashtag_scraper_prompt.md` — original Phase 0 discovery task spec
- `IG_HASHTAG_SCRAPER_PHASE0_REPORT.md` — Phase 0 findings (geo-locus %, cost, recommendation)
- `ig_hashtag_scraper_test.py` — Phase 0 scratch/test script (discovery only, not for reuse)
- `instagram_scraper.py` — live standalone extraction module (Acuvue/HK only so far)

---

## 1. What's live today

`instagram_scraper.py` — standalone module, own db (`output/instagram_data.db`),
not wired into `pipeline_v2.py`/`config.yaml`. Same pattern as `youtube_scraper.py`
and `xhs_scraper_v2.py`.

- **Scope:** brand hashtag only, Acuvue/HK only (`HASHTAGS = {"HK": {"Acuvue": ["acuvuehk"]}}`)
- **Discovers:** posts tagged `#acuvuehk` via `apify/instagram-scraper`
- **Extracts:** captions (+ English translation), likes/comments count, owner, location tag if present, post type
- **Comments:** fetched per-post via `apidojo/instagram-comments-scraper`, translated, then classified for sentiment / purchase-barrier-signal / on-topic relevance (same shape as `youtube_scraper.py`'s comment fields, for downstream comparability)
- **First real run (2026-07-20):** 9 new posts, 0 comments (all 9 posts genuinely had 0 comments) — cost ~$0.02. All 9 posts were HK-context Acuvue reseller/official content (`eyesmatehk`, `3optical_contactlens`, `brighteroptical_boc`, `eyenihk`, and the official `acuvuehk` handle).
- **Backfill:** `--classify-existing` re-runs sentiment/barrier/relevance classification on saved comments missing it (safe to re-run).

## 2. Actors used, and their real (not documented) input schemas

Apify Store descriptions/example inputs are frequently wrong or incomplete —
every schema below was confirmed either by a failed run's error message or by
pulling the actor's actual `inputSchema` from its build. Don't trust the store
page's "example input" without checking this first.

### `apify/instagram-scraper` (posts/hashtag discovery)
```json
{
  "directUrls": ["https://www.instagram.com/explore/tags/<hashtag>/"],
  "resultsType": "posts",
  "resultsLimit": 20
}
```
- `resultsType` enum (from actual schema): `posts | details | comments | reels | mentions | stories`
  - `"stories"` = current ephemeral (24h) stories only — **not** permanent Highlights (see §4)
  - `"details"` = profile/hashtag/place metadata (follower count, bio, post count, pic)
  - `"comments"` mode exists on this same actor too, but we use the dedicated comments actor below instead (cheaper, purpose-built)
- `searchType` enum: `hashtag | profile | place | user`

### `apidojo/instagram-comments-scraper` (comments on known post URLs)
```json
{
  "startUrls": ["https://www.instagram.com/p/<shortcode>/"],
  "maxItems": 60
}
```
- **NOT** `directUrls`/`resultsLimit` — that was our first guess (from the posts actor's convention) and it failed with `"Start URLs or Post IDs must be provided"`. Confirmed correct fields by pulling the actor's build `inputSchema` directly.
- Also accepts `postIds` (Instagram post ID strings) as an alternative to `startUrls`.
- Posts with no comments return `{"noResults": true}` placeholder items, not an empty array slot — filter these out (`instagram_scraper.py`'s `extract_comment_fields` does this).
- Response fields per comment: `postId, message, createdAt, likeCount, replyCount, hasReply, user.{username, fullName, isVerified, isPrivate}`

### Instagram Highlights scrapers (evaluated, not adopted — see §4)
`datavoyantlab/instagram-highlights-scraper-api-by-url` (no-login):
```json
{ "links": ["https://www.instagram.com/stories/highlights/<id>/"] }
```
Returns highlight **metadata, cover media, mentions, and timestamps only** — confirmed no comment/text field exists in its output, because Instagram doesn't support public comments on Stories/Highlights at the platform level.

## 3. Pricing — measured vs. advertised

Apify Store rate cards are optimistic; always budget off measured cost, not the listed rate.

| Actor | Advertised | **Measured (this project)** |
|---|---|---|
| `apify/instagram-scraper` (posts) | $1.50/1k | **$2.30/1k** (Phase 0), **~$2.30/1k** confirmed again on the real Acuvue run (9 posts = $0.0207 → $2.30/1k) |
| `apidojo/instagram-comments-scraper` | $0.50/1k | **$0.50/1k confirmed exactly** ($0.0005/comment, PAY_PER_EVENT) |
| `datavoyantlab` Highlights actor | — | $0.02 flat/run + $0.00169/highlight scraped (not currently used) |

Real-world cost is trivial at our volumes (single-digit cents per run) because:
(a) hashtag-scoped discovery keeps post counts low, and (b) comment volume on
these HK contact-lens hashtags is very low (0–1 comments/post typical).

## 4. What's confirmed NOT possible / not worth pursuing

- **Comments do not come bundled with post pulls.** The `latestComments` field exists on every post object from `apify/instagram-scraper` but was empty on all 50 posts sampled across Phase 0 + the live run, even when `commentsCount > 0`. A separate paid comments-actor call is always required.
- **Story Highlights ("Reviews" tabs on reseller profiles) have no comments, structurally.** Instagram doesn't support public comments on Stories/Highlights at all — full stop, not an Apify limitation. What looks like "reviews with comments" in a Highlight is almost always a **screenshot** of a DM/WhatsApp message/feed comment that the account owner posted as a story slide. Extracting that text would require OCR on the downloaded images, not a data field — a different pipeline (image scrape → vision/OCR), not a text-comment extraction. Not built; flagged as a possible future mini-project if testimonial mining is wanted, not treated as equivalent to real unfiltered comment data.
- **Highlights are profile-targeted, not hashtag-discoverable.** You need a specific account's highlight URL/ID up front — this doesn't fit the current hashtag-discovery pipeline shape at all; it would be a "track this specific reseller account" feature, a separate design.
- **Brand+locale hashtags run near-zero volume.** `#acuvuehk` returned 1/20 requested posts in Phase 0, 9/20 on the live run a few days later — volume fluctuates day to day and should never be assumed stable. This is expected, not a bug.

## 5. Open / not yet done

- Only Acuvue/HK is configured in `HASHTAGS`. Other 4 brands not yet added.
- No generic-hashtag + LLM brand-relevance layer (like `youtube_scraper.py`'s `check_brand_relevance`) — would be needed if we ever add generic hashtags (`#隱形眼鏡`, `#contactlensHK`) to increase volume, since those aren't brand-exclusive.
- No `instagram_signals.py` aggregator yet (mirroring `youtube_signals.py`) — worth building once post/comment volume accumulates across a few runs.
- OCR-based Highlights/testimonial extraction — evaluated as feasible, not built.
