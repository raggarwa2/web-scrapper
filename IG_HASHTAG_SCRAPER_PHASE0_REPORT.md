# Instagram Hashtag Scraper — Phase 0 Discovery Report

**Date:** 2026-07-18
**Scope:** Per `ig_hashtag_scraper_prompt.md` — test-run only, no production code/schema/merge.
**Script used:** `ig_hashtag_scraper_test.py` (standalone, does not touch `pipeline_v2.py`, `pipeline_auto.py`, `lensdata.db`, or `config.yaml`)
**Raw output:** `output/ig_hashtag_test/` (`summary.json`, `comments_test.json`, `raw_*.json` full 20-post pulls, `sample5_*.json` 5-post samples — all unmodified Apify output)

---

## 1. Actor test setup (what actually worked)

| Actor | Purpose | Working input schema (found empirically) |
|---|---|---|
| `apify/instagram-scraper` | Hashtag → posts | `{"directUrls": ["https://www.instagram.com/explore/tags/<tag>/"], "resultsType": "posts", "resultsLimit": 20}` |
| `apidojo/instagram-comments-scraper` | Post URL → comments | `{"startUrls": ["<post url>"], "maxItems": <n>}` — **not** `directUrls`/`resultsLimit` as originally guessed; first attempt failed with `"Start URLs or Post IDs must be provided"` until corrected against the actor's actual input schema. |

Both actors ran as logged-out/public calls — no login or session automation used, per the scope constraint.

## 2. Sample JSON output (5 posts per hashtag, raw + unmodified)

Full raw samples are in `output/ig_hashtag_test/sample5_<tag>.json`. Condensed excerpts (image/CDN URLs stripped for readability — the linked files retain everything unmodified):

**#隱形眼鏡** (generic Chinese, 20/20 retrieved)
- Post 1 — `mogo_shop888` (Taiwan-flavored: "蛇院女孩", cosplay/cosplay-eye hashtags, no HK signal) → likely-not-HK
- Post 2 — `strawberry._.mochi_`, tags `choose+ 眼選美瞳專賣` (HK brand tag) → ambiguous/confirmed depending on tag strength
- Post 3 — `bqlens` (`bqlens 隱形眼鏡專賣店`): explicit HKD-style pricing (`$150/盒`), **+852 92336606** WhatsApp, 順豐 (SF Express, HK courier) → HK-confirmed
- Post 4 — same `bqlens` account, another SKU, same +852 number, `#hkig #macau #853shop` → HK-confirmed

**#contactlensHK** (20/20 retrieved)
- `contactlens88.store` — "隱形眼鏡-香港人的零售店Hongkonger", `#香港隱形眼鏡 #ContactLensHK #香港購物`, 順豐 delivery → HK-confirmed
- `kochun12002` — **location tag: "旺角 - Mong Kok"** and **"Dragon Centre 西九龍中心"** (native Apify `locationName` field, populated) → HK-confirmed
- Repeated Olens reseller posts tagged `#hkonlinestore #hkshop #hkigshop #hkig #hkdeal #hkdiscount` → HK-confirmed

**#acuvuehk** (1/20 retrieved — see §4)
- `eyesmatehk` ("愛視美眼鏡Eye's Mate Optical"): named HK malls **鑽石山荷里活廣場** (Hollywood Plaza, Diamond Hill) and **筲箕灣愛東商場** (Shau Kei Wan) in caption, official ACUVUE HK campaign copy → HK-confirmed

## 3. Fields actually present (from live `apify/instagram-scraper` output)

`caption, hashtags, mentions, likesCount, commentsCount, firstComment, latestComments, timestamp, url, shortCode, ownerUsername, ownerFullName, ownerId, locationName (when tagged), locationId, type, productType, images, displayUrl, dimensionsHeight/Width, musicInfo, childPosts (carousel items), taggedUsers (when present)`

No separate "poster bio" field came back on the post object — bio would require a second call to a profile-details actor/mode. `locationName`/`locationId` were present and populated on posts where the poster tagged a location (2 of 60 total posts in this sample had one).

## 4. Comments — confirmed empirically

- `latestComments` **is present as a field on every post** but was an **empty array on all 41 posts retrieved**, even where `commentsCount` was 1. **Comments do not come bundled with the post pull** — this confirms the billing docs empirically, not just by assumption.
- Separate `apidojo/instagram-comments-scraper` call on 3 sample post URLs returned exactly the number of comments each post actually had (1 comment on one post, 0 on the other two — `"noResults": true`), confirming per-post comment volume on this hashtag set is very low.
- Comment fields returned: `postId, message, createdAt, likeCount, replyCount, hasReply, user.{username, fullName, isVerified, isPrivate}`.

## 5. Language

All 41 captions retrieved were **Traditional Chinese** (or Traditional Chinese + English/brand-term code-mixing) — zero Simplified Chinese observed. This is consistent with HK/Taiwan/Macau seller convention; it does **not** by itself distinguish HK from Taiwan/Macau (see §6 caveat).

## 6. Geo-locus signal check (the actual go/no-go question)

Adapted for social posts: the original Domain/Language/Locus 3-check (LIHKG/393lens audit) doesn't map directly since IG posts have no domain. Substituted signals: `location_tag`, `bio_mentions_hk`, `traditional_script`, `hkd_price_mention`, `hk_store_mention` — assessed per-post via GPT-4o-mini against caption + owner bio + location tag, with the same domain expert consistently applying a strict rule: **traditional script alone = ambiguous, not confirmed** (Taiwan/Macau/overseas Chinese also use it).

| Hashtag | Retrieved | HK-confirmed | Ambiguous | Likely-not-HK | % pass (confirmed+ambiguous) |
|---|---|---|---|---|---|
| #隱形眼鏡 (generic) | 20 | 19 | 0 | 1 | **95%** |
| #contactlensHK | 20 | 20 | 0 | 0 | **100%** |
| #acuvuehk (brand+locale) | 1 | 1 | 0 | 0 | **100% (n=1)** |

**Caveat on #隱形眼鏡's 95%:** this is a single snapshot of the hashtag's most-recent/top posts on 2026-07-18, not a random sample across time — HK resellers (bqlens, etc.) happened to dominate the current feed. A generic Chinese hashtag has no structural guarantee of HK locus the way `#contactlensHK` or a location tag does; re-running on a different day could surface more Taiwan/Malaysia/mainland content. Treat the 95% as "current feed composition," not a stable rate.

## 7. Cost projection

Apify's run-detail API only surfaced `usageTotalUsd` reliably on the smallest run; documented rates from the prompt are shown alongside the one real measurement obtained:

| Item | Documented rate | Actual observed (this test) |
|---|---|---|
| Posts (official actor, "posts" content type) | $1.50 / 1k | **$2.30 / 1k** (measured on the 1-post `#acuvuehk` run: `usageTotalUsd: 0.0023`) |
| Comments (`apidojo` actor) | $0.50 / 1k | **$0.50 / 1k confirmed exactly** (`usageTotalUsd: 0.0015` for 3 comment-events, PAY_PER_EVENT pricing at `$0.0005`/comment) |

The official actor's real per-post cost is ~53% higher than the prompt's assumed $1.50/1k. Using the confirmed $2.30/1k rate for projection:

| Scenario | Posts/mo | Comments/mo (est., ~0.5 comments/post observed avg) | Posts cost | Comments cost | Total/mo |
|---|---|---|---|---|---|
| 5 hashtags × 100 posts | 500 | ~250 | $1.15 | $0.125 | **~$1.28/mo** |
| 5 hashtags × 100 posts, all comments pulled (worst case, 5/post) | 500 | 2,500 | $1.15 | $1.25 | **~$2.40/mo** |

For portfolio context: this is far cheaper than XHS's per-post + GPT-classification cost structure, largely because comment volume on these HK contact-lens hashtags is extremely low (most sample posts had 0–1 comments) — there's simply less to pay for, not because the per-unit rate is dramatically lower.

## 8. Recommendation

**Viable with caveats.**

- A locale-anchored hashtag (`#contactlensHK`) and even a generic Chinese hashtag (`#隱形眼鏡`) currently surface a high proportion of genuinely HK-locus-confirmed content (explicit HKD-style pricing, +852 numbers, 順豐 courier, HK mall/district location tags) — stronger signal availability than initially assumed, because HK resellers self-tag heavily (`#hkig`, `#hkshop`, `#hkdeal`, actual location tags).
- The brand+locale hashtag (`#acuvuehk`) returned **1 post for 20 requested** — confirms the prompt's own prediction that near-zero volume is a valid finding; not viable as a primary hashtag on its own, though the one hit was a clean official-campaign match.
- Main caveat: the 95–100% pass rates reflect one day's snapshot of currently-active resellers, not a structural guarantee — generic hashtags need periodic re-validation, and a hashtag mix (locale-suffixed + generic + monitoring for reseller account drift) is safer than relying on any single tag.
- Comment volume is low enough that the second paid comment-scrape phase is optional/cheap — could be run only for posts crossing an engagement threshold rather than universally.
- Real per-post cost is ~53% above the docs' quoted rate; budget accordingly, though absolute cost at realistic volumes remains trivial (~$1–2.50/month).

Do not proceed past this report — awaiting review before writing any extraction script, schema, or touching production files, per the original scope.
