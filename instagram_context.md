# Instagram — Apify Capability Notes

Reference doc for what's actually possible (and what isn't) scraping Instagram
via Apify for this project. Covers what's been tested, what's live, and what's
been ruled out — so we don't re-discover the same actor-schema quirks or
re-litigate the same dead ends later.

Related files:
- `ig_hashtag_scraper_prompt.md` — original Phase 0 discovery task spec
- `IG_HASHTAG_SCRAPER_PHASE0_REPORT.md` — Phase 0 findings (geo-locus %, cost, recommendation)
- `ig_hashtag_scraper_test.py` — Phase 0 scratch/test script (discovery only, not for reuse)
- `instagram_scraper.py` — live standalone extraction module (5 brands/HK via hashtag; Acuvue also via profile mode)
- `instagram_signals.py` — dashboard data layer, mirrors `youtube_signals.py`

---

## 1. What's live today

`instagram_scraper.py` — standalone module, own db (`output/instagram_data.db`),
not wired into `pipeline_v2.py`/`config.yaml`. Same pattern as `youtube_scraper.py`
and `xhs_scraper_v2.py`. `instagram_signals.py` (mirrors `youtube_signals.py`) reads
that db and renders the "Customer Signals (Instagram)" tab in `app.py`.

- **Discovery, two modes** (`--source hashtag|profile|both`):
  - **Hashtag mode** (`HASHTAGS` config): all 5 brands now configured, HK only —
    `acuvuehk`, `alconhk`, `bauschlombhk` + `博士倫隱形眼鏡`, `coopervisionhk`, `olenshk`.
    See §5 for how these were validated before adding.
  - **Profile mode** (`PROFILES` config): pulls a specific account's own post
    history directly — reaches much further back in time than hashtag volume
    allows (confirmed: 3 Acuvue-adjacent accounts at 50 posts/account reached
    back to 2024-01-19, vs. hashtag mode capping at ~17 total posts no matter
    how high `--max-posts` goes). Currently only 3 accounts configured, all
    under Acuvue: `acuvuehk`, `eyesmatehk`, `3optical_contactlens`.
- **Extracts:** captions (+ English translation), likes/comments count, owner, location tag if present, post type, `source_type`/`source_value` (which discovery mode found it)
- **Comments:** fetched per-post via `apidojo/instagram-comments-scraper`, translated, then classified for sentiment / purchase-barrier-signal / on-topic relevance (same shape as `youtube_scraper.py`'s comment fields, for downstream comparability)
- **Current totals (as of 2026-07-23):** 153 posts / 348 comments, Acuvue/HK only so far (17 hashtag + 136 profile). Real cost across all runs to date: ~$1.9.
- **Backfill:** `--classify-existing` re-runs sentiment/barrier/relevance classification on saved comments missing it (safe to re-run).
- **Resilience:** both the discovery loop and the comments-fetch call are wrapped so a transient Apify/network failure on one source (or on comments) doesn't discard posts already fetched (and paid for) from earlier sources in the same run — learned the hard way from a DNS blip mid-run that silently lost 150 already-paid-for posts before this fix.

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
- **Brand+locale hashtags run near-zero volume — for a NEW hashtag.** `#acuvuehk` returned 1/20 requested posts in Phase 0, 9/20 on the live run a few days later — volume fluctuates day to day and should never be assumed stable. This is expected, not a bug. (Once an account ecosystem is actively using a hashtag, volume can be much higher — see §5, all 4 other brands' `<brand>hk` tags returned a full 20/20 on first test.)
- **Hashtag search itself has a volume ceiling, independent of `--max-posts`.** Confirmed empirically: raising `--max-posts` from 20 to 200 for `#acuvuehk` returned the exact same 17 posts, 0 new — the hashtag's total indexed volume was already exhausted, not a pagination limit we could push past. Profile mode is the only way past this (see §1).

## 5. Hashtag validation methodology (per brand, before adding to config)

Same approach as Phase 0: pull ~20 posts per candidate hashtag, eyeball samples
for genuine HK/brand relevance before committing it to `HASHTAGS`. Mined
candidates from hashtags actually co-occurring in already-scraped posts (not
blind guesses) where possible.

Pattern confirmed across 4 more brands (2026-07-24): the `<brand>hk` suffix
convention that worked for Acuvue is reliable in general — all 4 returned a
full 20/20 posts, genuinely HK/brand-relevant. **Bare brand names are
consistently worse** — diluted by unrelated global usage:
- `alcon` (bare) — collides with Alcon the **brake caliper** brand (BMW/car-parts posts), plus a German optician and a medical-equipment supplier. Rejected.
- `coopervision` (bare) — mostly non-HK opticians (Croatia, UK). Rejected.
- `olensviviring` (product-line tag) — mostly Korea-market/dutyfree posts, or generic reseller hashtag-stuffing not really about that product line. Rejected.
- `博士倫隱形眼鏡` (Chinese brand name, mined not guessed) — clean, and surfaces a **different** account set than `bauschlombhk` does — worth keeping both for Bausch & Lomb rather than picking one.

Final `HASHTAGS` per brand: `acuvuehk` / `alconhk` / `bauschlombhk` + `博士倫隱形眼鏡` / `coopervisionhk` / `olenshk`.

## 6. Data-quality gap — profile-mode accounts are multi-brand resellers — FIXED 2026-07-24

`3optical_contactlens`/`eyesmatehk` (in `PROFILES` under Acuvue) sell many
brands across their own post history — profile mode pulls **everything** an
account posts and originally tagged it all with whatever brand key triggered
the fetch. Fixed by adding `check_brand_relevance()`/`_llm_brand_relevance_batch()`
to `instagram_scraper.py` (exact mirror of `youtube_scraper.py`'s), now run on
every new post and backfilled across history via `--classify-existing`.

**Backfill results (235 posts, 2026-07-24):** 41 posts (17%) flagged
`brand_relevant = 0` and excluded from per-brand metrics — almost entirely
`eyesmatehk`/`3optical_contactlens` profile-mode posts about **other real
brands** (Essilor Stellest, HOYA, Ray-Ban, SEED, Alcon PRECISION1) that had
been sitting mislabeled as `brand = "Acuvue"` purely because that account was
crawled under the Acuvue `PROFILES` entry.

**Known imprecision** (LLM check, not perfect — same tradeoff as YouTube's):
- One `conred_hk` post naming "Korean brand Clalen" explicitly was NOT flagged (should have been, for Bausch & Lomb) — a miss in the fail-open direction.
- One Alcon post about "Freshlook" WAS flagged not-relevant, even though Freshlook is Alcon-owned — an over-flag (the worse failure mode, though rare: 1 of 235).
- Net effect is still strongly positive (real, substantial noise removed) — just not perfect precision. Don't treat `brand_relevant` as ground truth without spot-checking if precision matters for a specific decision.

Hashtag-mode posts have less of this problem structurally (a post found via
`#alconhk` is usually genuinely about Alcon) but can still be hashtag-stuffed
by multi-brand resellers — the same check now covers both discovery modes.

Also cleaned in the same pass: Instagram returns `-1` for like/comment counts
a poster has hidden — `_clean_count()` now converts these to NULL (was
silently skewing `SUM()` aggregations before; 2 rows affected in current data).

## 7. Open / not yet done

- `PROFILES` (profile-mode accounts) only configured for Acuvue — the other 4 brands are hashtag-mode only so far.
- The `is_lens_relevant` regex whitelist misses some marketing phrasing that doesn't literally contain a whitelisted term (e.g. Alcon "Water Lens" campaign copy) — costs a handful of genuinely-relevant posts per brand. Not expanded yet; would need real examples reviewed first rather than guessing more terms.
- Instagram is NOT wired into the deeper cross-source integrations YouTube has (Brand Health composite score, Trends & Demand charts, blended sentiment views) — deliberate, pending more volume across brands.
- OCR-based Highlights/testimonial extraction — evaluated as feasible, not built.
