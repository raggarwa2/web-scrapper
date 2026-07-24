# Claude Code Prompt — Instagram HK Hashtag Scraper (Phase 0: Discovery Only)

## Protected files — DO NOT read, open, modify, or reference for editing
- `pipeline_v2.py`
- `pipeline_auto.py`
- `lensdata.db`
- `config.yaml`

This is a standalone discovery task. Nothing in this phase touches the
production pipeline. Build a new, separate scratch script and a new,
separate SQLite file if any storage is needed for sample output.

## Objective

Validate whether Apify's Instagram scraper actors can produce a usable,
HK-locus-confirmed dataset from hashtag search — before any extraction
code is written or merged into the pipeline. This is Phase 0 of a
new Reputation-pillar source, same status as WateryEyes.hk before it.

## Scope for this phase (read-only / test-run only)

1. **Actor selection test** — run a small test call against two candidate
   actors:
   - `apify/instagram-scraper` (official, $1.50/1k posts, $2.30/1k comments)
   - `apidojo/instagram-comments-scraper` ($0.50/1k comments)

   Use `Content to scrape: Posts` on the official actor with hashtag
   input, limited to **3 hashtags, 20 posts each** for this test —
   do not run a full pull.

2. **Hashtag set to test** (HK-relevant, mix of Chinese and English):
   - `#隱形眼鏡` (contact lens, generic Chinese)
   - `#contactlensHK`
   - `#acuvuehk` (brand + locale test — may return near-zero, that's a
     valid finding)

3. **Capture and report, per hashtag:**
   - Raw sample of 5 returned posts (full JSON, unmodified)
   - Fields actually present: caption, hashtags, likesCount, commentsCount,
     location tag (if any), poster username, poster bio/location (if
     accessible), timestamp
   - Whether `latestComments` (or equivalent) come bundled with the post
     pull, or require a second scrape (comments are billed separately —
     confirm this empirically, don't assume from docs)
   - Language of returned captions — Traditional Chinese vs Simplified vs
     English mix

4. **Geo-locus signal check (critical — this is the actual open question)**
   Instagram has no market filter like LIHKG or 393lens do implicitly.
   For each of the 20 posts per hashtag, assess and tabulate:
   - Does the post/profile carry any explicit HK signal? (location tag,
     bio mentioning HK, Traditional Chinese script, HKD price mention,
     HK store tag/mention)
   - Or is it ambiguous/global (could be Taiwan, Malaysia, mainland China,
     or anywhere Traditional/Simplified Chinese contact-lens content
     appears)?
   - Report a rough % of posts per hashtag that would plausibly PASS
     the existing three-check geo-validation audit (Domain, Language,
     Locus — pass two of three) vs. fail it outright.

   This % is the actual go/no-go signal for this source — if most
   results can't be locus-confirmed, hashtag scraping isn't viable
   as a Reputation-pillar source regardless of scrape cost.

5. **Cost projection** — based on the test run's actual per-post and
   per-comment consumption, project cost for a realistic monthly volume
   (e.g. 5 hashtags × 100 posts × comments-if-available). Compare against
   XHS/Apify cost already in use, for portfolio context.

## Explicitly out of scope for this phase

- No comment-level sentiment tagging or barrier-language matching
- No new SQLite schema or table design
- No merge into `pipeline_v2.py` or `config.yaml`
- No scraping of specific competitor brand accounts (that's a separate,
  later decision — this phase is hashtag-search only)
- No login-based or session-automation approaches — public/logged-out
  actor calls only

## Deliverable

A single markdown report (not code) containing:
1. Sample JSON output (raw, 5 posts per hashtag)
2. The geo-locus table described in step 4
3. Cost projection table
4. A one-line recommendation: **viable / not viable / viable with
   caveats** for hashtag-based IG data as a geo-fenced Reputation source

Do not proceed past this report. Wait for review before writing any
extraction script, schema, or touching production files.
