# ==================================================================
# Instagram Reputation Module — standalone (NOT part of pipeline_v2.py)
# ==================================================================
# Purpose:
#   Add Instagram as a reputation-pillar source for the contact lens
#   intelligence pipeline. Discovers posts via brand hashtag search on
#   Apify's apify/instagram-scraper, then pulls comments via
#   apidojo/instagram-comments-scraper. No login, no Playwright.
#
# Status: STANDALONE MODULE — own SQLite db (instagram_data.db).
#   Per project convention, this is NOT wired into pipeline_v2.py or
#   config.yaml. Review sample output first; merge is a separate,
#   explicit step touching those files directly.
#
# Scope of this build (per Phase 0 discovery, see
# IG_HASHTAG_SCRAPER_PHASE0_REPORT.md): brand hashtag only, Acuvue/HK
# only. #acuvuehk returned near-zero volume in testing (1 of 20
# requested) — that is expected, not a bug. Generic hashtags
# (#隱形眼鏡, #contactlensHK) were NOT included here; adding them
# would need an LLM brand-relevance filter like youtube_scraper.py's
# check_brand_relevance() and is a deliberate later step, not this one.
#
# Cost model (measured empirically in Phase 0, NOT the Apify store's
# advertised rate):
#   - apify/instagram-scraper (posts)         : ~$2.30 / 1k posts
#     (store page advertises $1.50/1k — actual measured PAY_PER_EVENT
#      cost was higher; budget off the measured rate)
#   - apidojo/instagram-comments-scraper       : $0.50 / 1k comments
#     (confirmed exactly: $0.0005/comment PAY_PER_EVENT)
#   Comment volume on these hashtags is very low (0-1 comments/post
#   typical in Phase 0 sample) — total run cost is trivial (cents).
#
# Setup required: APIFY_TOKEN and OPENAI_API_KEY already in .env
# (shared with xhs_scraper_v2.py / youtube_scraper.py).
#
# Usage:
#   python instagram_scraper.py --dry-run
#   python instagram_scraper.py --brand Acuvue --market HK
#   python instagram_scraper.py --max-posts 50
#   python instagram_scraper.py --skip-comments
#   python instagram_scraper.py --classify-existing
#
# Discovery source (added after Phase 0): hashtag search only reaches
# whatever volume currently carries that hashtag — for #acuvuehk that
# was ~17 posts total, all from the last few weeks, regardless of how
# high --max-posts is set (confirmed empirically: raising it to 200
# returned the same 17 posts, 0 new). To reach further back in time,
# scrape known accounts' own post histories directly instead:
#   python instagram_scraper.py --source profile
#   python instagram_scraper.py --source both --max-posts 200
# Profile mode pulls an account's own timeline (reverse-chronological),
# independent of hashtag volume — but older posts on that account may
# predate their use of #acuvuehk entirely, so this is "everything this
# account posted," not "everything about Acuvue."
# ==================================================================

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import os
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── CONFIG: brand hashtags per market ────────────────────────────────
# Deliberately brand-hashtag-only for this first build (see header).
# Add generic hashtags + an LLM brand-relevance pass later, following
# youtube_scraper.py's BRAND_KEYWORDS + check_brand_relevance() pattern.

HASHTAGS = {
    "HK": {
        "Acuvue": ["acuvuehk"],
    },
}

# ── CONFIG: known accounts to scrape directly (profile mode) ─────────
# Reseller/official accounts identified from hashtag-mode results so far.
# Profile mode pulls each account's own post history — use this to reach
# further back in time than hashtag volume allows (see header note).

PROFILES = {
    "HK": {
        "Acuvue": ["acuvuehk", "eyesmatehk", "3optical_contactlens"],
    },
}

APIFY_BASE = "https://api.apify.com/v2"
POSTS_ACTOR = "apify/instagram-scraper"
COMMENTS_ACTOR = "apidojo/instagram-comments-scraper"

TRANSLATE_BATCH_SIZE = 20
CLASSIFY_BATCH_SIZE = 20

# ── DB SCHEMA (separate db — instagram_data.db) ──────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS ig_posts (
    post_id         TEXT PRIMARY KEY,
    brand           TEXT NOT NULL,
    market          TEXT NOT NULL,
    hashtag         TEXT,
    caption         TEXT,
    caption_en      TEXT,
    hashtags        TEXT,           -- JSON list
    owner_username  TEXT,
    owner_full_name TEXT,
    location_name   TEXT,
    location_id     TEXT,
    likes_count     INTEGER,
    comments_count  INTEGER,
    post_type       TEXT,
    published_at    TEXT,
    url             TEXT,
    discovered_at   TEXT,
    is_lens_relevant INTEGER
);

CREATE TABLE IF NOT EXISTS ig_comments (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id                    TEXT NOT NULL,
    brand                      TEXT NOT NULL,
    market                     TEXT NOT NULL,
    author                     TEXT,
    comment_text               TEXT,
    comment_text_en            TEXT,
    like_count                 INTEGER,
    published_at               TEXT,
    content_hash               TEXT UNIQUE,
    scraped_at                 TEXT,
    sentiment                  TEXT,
    is_purchase_barrier_signal INTEGER,
    is_lens_relevant           INTEGER,
    FOREIGN KEY(post_id) REFERENCES ig_posts(post_id)
);

CREATE INDEX IF NOT EXISTS idx_ig_posts_brand    ON ig_posts(brand);
CREATE INDEX IF NOT EXISTS idx_ig_comments_brand ON ig_comments(brand);
CREATE INDEX IF NOT EXISTS idx_ig_comments_post  ON ig_comments(post_id);
"""

# Columns added after the initial (hashtag-only) release, for profile-mode
# discovery — same ALTER-TABLE-on-open migration pattern as youtube_scraper.py.
_MIGRATION_COLUMNS = {
    "ig_posts": {
        "source_type": "TEXT",      # 'hashtag' | 'profile'
        "source_value": "TEXT",     # the hashtag word, or the queried username
    },
}


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for table, columns in _MIGRATION_COLUMNS.items():
        existing_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col, col_type in columns.items():
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
    conn.execute(
        "UPDATE ig_posts SET source_type = 'hashtag', source_value = hashtag "
        "WHERE source_type IS NULL AND hashtag IS NOT NULL"
    )
    conn.commit()
    return conn


# ── LENS-RELEVANCE WHITELIST (regex, no LLM cost) ────────────────────
# Same whitelist-not-blacklist philosophy as youtube_scraper.py's
# _is_lens_relevant. A hashtag can be stuffed onto unrelated content —
# this flags (does NOT drop) posts whose caption carries no explicit
# lens term, so low-volume data isn't silently thrown away, but noise
# is still visible to downstream analysis.

LENS_RELEVANCE_TERMS_EN = [
    "contact lens", "contact lenses", "contactlens", "coloured contact",
    "colored contact", "daily disposable", "monthly disposable",
    "toric lens", "1-day", "1 day", "acuvue",
]
LENS_RELEVANCE_TERMS_NONASCII = [
    "隱形眼鏡", "月拋", "日拋", "散光", "老花", "彩色隱形", "美瞳",
]
_LENS_TERM_PATTERN_NONASCII = re.compile("|".join(LENS_RELEVANCE_TERMS_NONASCII))
_LENS_TERM_PATTERN_EN = re.compile(
    "|".join(t.replace(" ", r"\s*") for t in LENS_RELEVANCE_TERMS_EN),
    re.IGNORECASE,
)


def _is_lens_relevant(caption: str) -> bool:
    return bool(_LENS_TERM_PATTERN_NONASCII.search(caption) or _LENS_TERM_PATTERN_EN.search(caption))


# ── LANGUAGE DETECTION + TRANSLATION (mirrors youtube_scraper.py) ────

def _is_non_english(text: str) -> bool:
    if not text or len(text) < 3:
        return False
    cjk = sum(1 for c in text if '一' <= c <= '鿿')
    return cjk / len(text) > 0.15


def _llm_translate_batch(texts: List[str], client: OpenAI) -> List[str]:
    """Batch-translate non-English captions/comments to English.
    Index-tagged response mapping (not array position) — same fix as
    youtube_scraper.py's _llm_translate_batch, for the same reason:
    source text with embedded newlines can make the model split/merge
    items, silently misaligning a plain ordered array."""
    if not texts:
        return []
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    prompt = f"""Translate these {len(texts)} contact lens-related Instagram posts/comments to English.
For each one, return an object with:
- "i": the item's number as shown below (integer)
- "t": the English translation (natural, concise)

Return ONLY a JSON array of objects, one per item, no other text.

Items:
{numbered}"""
    by_index: Dict[int, str] = {}
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3000,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            for p in parsed:
                idx = p.get("i")
                if isinstance(idx, int) and 1 <= idx <= len(texts):
                    by_index[idx] = str(p.get("t", texts[idx - 1]))
        if len(by_index) != len(texts):
            log.warning(f"[LLM] Translate batch: expected {len(texts)}, got {len(by_index)} indexed")
    except Exception as e:
        log.warning(f"[LLM] Translate batch failed: {e}")

    return [by_index.get(i + 1, texts[i]) for i in range(len(texts))]


def _llm_classify_batch(texts_en: List[str], client: OpenAI) -> List[dict]:
    """Batch sentiment + purchase-barrier + on-topic classification —
    same fields/shape as youtube_scraper.py's _llm_classify_batch so
    the two sources score comparably downstream."""
    if not texts_en:
        return []
    fallback = {"sentiment": "neutral", "is_purchase_barrier_signal": False, "is_lens_relevant": True}
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts_en))
    prompt = f"""Classify these {len(texts_en)} Instagram comments left on contact lens brand posts.
For each comment, return an object with:
- "i": the comment's number as shown below (integer)
- "sentiment": one of "positive", "negative", "neutral", "mixed"
- "is_purchase_barrier_signal": true if the comment expresses a reason for not buying/switching (price, availability, comfort, trust, etc.), else false
- "is_lens_relevant": true if the comment is actually about the contact lenses/product, false if it's off-topic chatter (a generic emoji reaction, praise for an unrelated model/celebrity, spam)

Return ONLY a JSON array of objects, one per comment, no other text.

Comments:
{numbered}"""
    by_index: Dict[int, dict] = {}
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3000,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            for p in parsed:
                idx = p.get("i")
                if isinstance(idx, int) and 1 <= idx <= len(texts_en):
                    by_index[idx] = {
                        "sentiment": str(p.get("sentiment", "neutral")),
                        "is_purchase_barrier_signal": bool(p.get("is_purchase_barrier_signal", False)),
                        "is_lens_relevant": bool(p.get("is_lens_relevant", True)),
                    }
        if len(by_index) != len(texts_en):
            log.warning(f"[LLM] Classify batch: expected {len(texts_en)}, got {len(by_index)} indexed")
    except Exception as e:
        log.warning(f"[LLM] Classify batch failed: {e}")

    return [by_index.get(i + 1, fallback) for i in range(len(texts_en))]


# ── APIFY CALLS ────────────────────────────────────────────────────────

def run_actor(token: str, actor_id: str, run_input: dict, label: str, max_wait_attempts: int = 60) -> List[dict]:
    """Start an Apify actor run, poll until terminal, return dataset items.
    Same start/poll/fetch pattern as xhs_scraper_v2.py and the Phase 0
    ig_hashtag_scraper_test.py — proven against these exact two actors."""
    actor_slug = actor_id.replace("/", "~")
    r = requests.post(
        f"{APIFY_BASE}/acts/{actor_slug}/runs",
        json=run_input,
        params={"token": token},
        timeout=30,
    )
    if not r.ok:
        log.error(f"[{label}] start failed {r.status_code}: {r.text[:800]}")
    r.raise_for_status()
    data = r.json()["data"]
    run_id, dataset_id = data["id"], data["defaultDatasetId"]
    log.info(f"[{label}] run started run_id={run_id}")

    status_url = f"{APIFY_BASE}/actor-runs/{run_id}"
    final = None
    for attempt in range(max_wait_attempts):
        time.sleep(10)
        final = requests.get(status_url, params={"token": token}, timeout=15).json()["data"]
        log.info(f"[{label}] status={final['status']}  (attempt {attempt + 1})")
        if final["status"] == "SUCCEEDED":
            break
        if final["status"] in ("FAILED", "ABORTED", "TIMED-OUT"):
            log.error(f"[{label}] ended with {final['status']}: {final.get('statusMessage', '')}")
            break
    else:
        raise TimeoutError(f"[{label}] run {run_id} did not finish in time")

    items_r = requests.get(
        f"{APIFY_BASE}/datasets/{dataset_id}/items",
        params={"token": token, "format": "json"},
        timeout=60,
    )
    items_r.raise_for_status()
    items = items_r.json()

    cost = final.get("usageTotalUsd") if final else None
    log.info(f"[{label}] retrieved {len(items)} items | cost: {cost}")
    return items


def discover_posts(hashtag: str, token: str, max_posts: int) -> List[dict]:
    run_input = {
        "directUrls": [f"https://www.instagram.com/explore/tags/{hashtag}/"],
        "resultsType": "posts",
        "resultsLimit": max_posts,
    }
    return run_actor(token, POSTS_ACTOR, run_input, f"posts:{hashtag}")


def discover_profile_posts(username: str, token: str, max_posts: int) -> List[dict]:
    """Pull an account's own post history (reverse-chronological), independent
    of hashtag volume. Confirmed empirically that #acuvuehk tops out at ~17
    posts total regardless of --max-posts — this is the way to reach further
    back in time, at the cost of pulling everything the account posted, not
    just Acuvue-tagged content."""
    run_input = {
        "directUrls": [f"https://www.instagram.com/{username}/"],
        "resultsType": "posts",
        "resultsLimit": max_posts,
    }
    return run_actor(token, POSTS_ACTOR, run_input, f"profile:{username}")


def fetch_comments_for_posts(post_urls: List[str], token: str, max_items: int) -> List[dict]:
    if not post_urls:
        return []
    run_input = {"startUrls": post_urls, "maxItems": max_items}
    return run_actor(token, COMMENTS_ACTOR, run_input, "comments")


def _owner_bio(post: dict) -> str:
    owner = post.get("owner")
    if isinstance(owner, dict):
        return owner.get("biography", "") or ""
    return ""


def extract_post_fields(post: dict, brand: str, market: str, source_type: str, source_value: str, now: str) -> Optional[dict]:
    post_id = post.get("shortCode") or post.get("id")
    if not post_id:
        return None
    caption = post.get("caption", "") or ""
    return {
        "post_id":         post_id,
        "brand":           brand,
        "market":          market,
        "hashtag":         source_value if source_type == "hashtag" else None,
        "source_type":     source_type,
        "source_value":    source_value,
        "caption":         caption,
        "caption_en":      caption,  # placeholder, overwritten below if non-English
        "hashtags":        json.dumps(post.get("hashtags", []), ensure_ascii=False),
        "owner_username":  post.get("ownerUsername", ""),
        "owner_full_name": post.get("ownerFullName", ""),
        "location_name":   post.get("locationName"),
        "location_id":     post.get("locationId"),
        "likes_count":     post.get("likesCount"),
        "comments_count":  post.get("commentsCount"),
        "post_type":       post.get("type"),
        "published_at":    post.get("timestamp", ""),
        "url":             post.get("url", ""),
        "discovered_at":   now,
        "is_lens_relevant": int(_is_lens_relevant(caption)),
    }


def extract_comment_fields(item: dict, post_id_by_url: Dict[str, str], brand: str, market: str, now: str) -> Optional[dict]:
    if item.get("noResults"):
        return None
    input_source = item.get("inputSource", "")
    post_id = post_id_by_url.get(input_source) or item.get("postId", "")
    text = item.get("message", "") or ""
    if not text or not post_id:
        return None
    raw_key = f"{post_id}|{item.get('createdAt', '')}|{text[:50]}"
    content_hash = hashlib.md5(raw_key.encode()).hexdigest()
    return {
        "post_id":         post_id,
        "brand":           brand,
        "market":          market,
        "author":          (item.get("user") or {}).get("username", ""),
        "comment_text":    text,
        "comment_text_en": text,  # placeholder, overwritten below if non-English
        "like_count":      item.get("likeCount", 0),
        "published_at":    item.get("createdAt", ""),
        "content_hash":    content_hash,
        "scraped_at":      now,
        "sentiment":       None,
        "is_purchase_barrier_signal": None,
        "is_lens_relevant": None,
    }


# ── DISCOVERY + EXTRACTION ORCHESTRATION ─────────────────────────────

def discover_and_extract(
    market: str,
    brand: str,
    sources: List[tuple[str, str]],
    token: str,
    client: OpenAI,
    max_posts: int,
    existing_post_ids: set,
    skip_comments: bool,
) -> tuple[List[dict], List[dict]]:
    """`sources` is a list of (source_type, source_value) pairs:
    ("hashtag", "acuvuehk") or ("profile", "eyesmatehk")."""
    now = datetime.now(timezone.utc).isoformat()
    posts: List[dict] = []

    for source_type, source_value in sources:
        try:
            if source_type == "hashtag":
                log.info(f"[DISCOVER] Instagram | {brand} | {market} | #{source_value}")
                raw_items = discover_posts(source_value, token, max_posts)
            else:
                log.info(f"[DISCOVER] Instagram | {brand} | {market} | profile:@{source_value}")
                raw_items = discover_profile_posts(source_value, token, max_posts)
        except Exception as e:
            # One source failing (transient network/Apify error) must not
            # discard posts already collected from earlier sources in this
            # same loop — log and move to the next source instead.
            log.error(f"[DISCOVER] {source_type}:{source_value} failed, skipping: {e}")
            continue

        for item in raw_items:
            f = extract_post_fields(item, brand, market, source_type, source_value, now)
            if not f or f["post_id"] in existing_post_ids:
                continue
            posts.append(f)
            existing_post_ids.add(f["post_id"])
        time.sleep(1)

    if not posts:
        return [], []

    # Translate non-English captions
    non_en_idxs = [i for i, p in enumerate(posts) if _is_non_english(p["caption"])]
    for start in range(0, len(non_en_idxs), TRANSLATE_BATCH_SIZE):
        batch_idxs = non_en_idxs[start:start + TRANSLATE_BATCH_SIZE]
        translations = _llm_translate_batch([posts[i]["caption"] for i in batch_idxs], client)
        for idx, translation in zip(batch_idxs, translations):
            posts[idx]["caption_en"] = translation

    all_comments: List[dict] = []
    if not skip_comments:
        post_urls = [p["url"] for p in posts if p["url"]]
        post_id_by_url = {p["url"]: p["post_id"] for p in posts if p["url"]}
        max_items = max(len(post_urls) * 20, 20)
        try:
            raw_comments = fetch_comments_for_posts(post_urls, token, max_items)
        except Exception as e:
            # A transient Apify/network failure here must not lose the posts
            # already fetched (and already paid for) above — return them with
            # no comments rather than letting the exception propagate and
            # discard everything (observed in practice: a DNS blip mid-run
            # killed an entire 150-post/3-account profile scrape).
            log.error(f"[EXTRACT] Comments fetch failed, continuing with posts only: {e}")
            raw_comments = []
        for item in raw_comments:
            c = extract_comment_fields(item, post_id_by_url, brand, market, now)
            if c:
                all_comments.append(c)
        log.info(f"[EXTRACT] {brand}/{market}: {len(all_comments)} comments across {len(post_urls)} posts")

        # Translate non-English comments
        non_en_c_idxs = [i for i, c in enumerate(all_comments) if _is_non_english(c["comment_text"])]
        for start in range(0, len(non_en_c_idxs), TRANSLATE_BATCH_SIZE):
            batch_idxs = non_en_c_idxs[start:start + TRANSLATE_BATCH_SIZE]
            translations = _llm_translate_batch([all_comments[i]["comment_text"] for i in batch_idxs], client)
            for idx, translation in zip(batch_idxs, translations):
                all_comments[idx]["comment_text_en"] = translation

        # Sentiment + purchase-barrier classification
        for start in range(0, len(all_comments), CLASSIFY_BATCH_SIZE):
            batch = all_comments[start:start + CLASSIFY_BATCH_SIZE]
            labels = _llm_classify_batch([c["comment_text_en"] for c in batch], client)
            for c, label in zip(batch, labels):
                c["sentiment"] = label["sentiment"]
                c["is_purchase_barrier_signal"] = label["is_purchase_barrier_signal"]
                c["is_lens_relevant"] = label["is_lens_relevant"]

    return posts, all_comments


def save_posts(conn: sqlite3.Connection, posts: List[dict]) -> int:
    inserted = 0
    for p in posts:
        cur = conn.execute(
            """INSERT OR IGNORE INTO ig_posts
               (post_id, brand, market, hashtag, caption, caption_en, hashtags,
                owner_username, owner_full_name, location_name, location_id,
                likes_count, comments_count, post_type, published_at, url,
                discovered_at, is_lens_relevant, source_type, source_value)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p["post_id"], p["brand"], p["market"], p["hashtag"], p["caption"],
                p["caption_en"], p["hashtags"], p["owner_username"], p["owner_full_name"],
                p["location_name"], p["location_id"], p["likes_count"], p["comments_count"],
                p["post_type"], p["published_at"], p["url"], p["discovered_at"],
                p["is_lens_relevant"], p["source_type"], p["source_value"],
            ),
        )
        if cur.rowcount == 1:
            inserted += 1
    conn.commit()
    return inserted


def save_comments(conn: sqlite3.Connection, comments: List[dict]) -> int:
    inserted = 0
    for c in comments:
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO ig_comments
                   (post_id, brand, market, author, comment_text, comment_text_en,
                    like_count, published_at, content_hash, scraped_at,
                    sentiment, is_purchase_barrier_signal, is_lens_relevant)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    c["post_id"], c["brand"], c["market"], c["author"],
                    c["comment_text"], c["comment_text_en"], c["like_count"],
                    c["published_at"], c["content_hash"], c["scraped_at"],
                    c["sentiment"], c["is_purchase_barrier_signal"], c["is_lens_relevant"],
                ),
            )
            if cur.rowcount == 1:
                inserted += 1
        except Exception as e:
            log.warning(f"[DB] Comment insert error: {e}")
    conn.commit()
    return inserted


# ── MAIN ──────────────────────────────────────────────────────────────

def _sources_for_brand(market: str, brand: str, source_mode: str) -> List[tuple[str, str]]:
    """Build the (source_type, source_value) list for one brand/market,
    per --source: 'hashtag', 'profile', or 'both'."""
    sources: List[tuple[str, str]] = []
    if source_mode in ("hashtag", "both"):
        sources += [("hashtag", h) for h in HASHTAGS.get(market, {}).get(brand, [])]
    if source_mode in ("profile", "both"):
        sources += [("profile", u) for u in PROFILES.get(market, {}).get(brand, [])]
    return sources


def run(
    markets: List[str],
    brand_filter: Optional[List[str]],
    max_posts: int,
    dry_run: bool,
    skip_comments: bool,
    source_mode: str = "hashtag",
    db_path: str = "output/instagram_data.db",
) -> None:
    token = os.getenv("APIFY_TOKEN", "").strip()
    if not token:
        log.error("[SETUP] APIFY_TOKEN not found in .env")
        raise SystemExit(1)

    client = OpenAI()
    conn = None if dry_run else open_db(db_path)
    existing_ids: set = set()
    if conn:
        existing_ids = {row[0] for row in conn.execute("SELECT post_id FROM ig_posts")}
        if existing_ids:
            log.info(f"[DISCOVER] {len(existing_ids)} posts already in DB — skipping")

    total_posts = 0
    total_comments = 0

    for market in markets:
        brands = set(HASHTAGS.get(market, {})) | set(PROFILES.get(market, {}))
        for brand in brands:
            if brand_filter and brand.lower() not in [b.lower() for b in brand_filter]:
                continue

            sources = _sources_for_brand(market, brand, source_mode)
            if not sources:
                continue

            posts, comments = discover_and_extract(
                market, brand, sources, token, client, max_posts, existing_ids, skip_comments
            )

            if dry_run:
                print(f"\n=== {brand} / {market} — {len(posts)} posts, {len(comments)} comments (DRY RUN) ===")
                for p in posts[:5]:
                    print(f"  {p['caption_en'][:60]!r} | likes={p['likes_count']} | {p['url']}")
                for c in comments[:5]:
                    print(f"    comment: {c['comment_text_en'][:80]}")
                continue

            n_p = save_posts(conn, posts)
            n_c = save_comments(conn, comments)
            total_posts += n_p
            total_comments += n_c
            log.info(f"[SAVE] {brand}/{market}: +{n_p} posts, +{n_c} comments")

    if conn:
        conn.close()

    log.info(f"[DONE] Total new: {total_posts} posts, {total_comments} comments")


def classify_existing(db_path: str = "output/instagram_data.db") -> int:
    """Backfill sentiment/is_purchase_barrier_signal/is_lens_relevant for
    comments missing is_lens_relevant. Safe to re-run."""
    client = OpenAI()
    conn = open_db(db_path)
    rows = conn.execute(
        "SELECT id, comment_text_en FROM ig_comments WHERE is_lens_relevant IS NULL"
    ).fetchall()
    log.info(f"[CLASSIFY] {len(rows)} comments missing classification")

    classified = 0
    for start in range(0, len(rows), CLASSIFY_BATCH_SIZE):
        batch = rows[start:start + CLASSIFY_BATCH_SIZE]
        labels = _llm_classify_batch([r["comment_text_en"] for r in batch], client)
        for row, label in zip(batch, labels):
            conn.execute(
                """UPDATE ig_comments
                   SET sentiment = ?, is_purchase_barrier_signal = ?, is_lens_relevant = ?
                   WHERE id = ?""",
                (
                    label["sentiment"], int(label["is_purchase_barrier_signal"]),
                    int(label["is_lens_relevant"]), row["id"],
                ),
            )
            classified += 1
        conn.commit()
        log.info(f"[CLASSIFY] {classified}/{len(rows)} done")

    conn.close()
    log.info(f"[CLASSIFY] Done — {classified} comments classified")
    return classified


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Instagram reputation module (standalone) — Acuvue/HK only for now")
    parser.add_argument("--market", nargs="+", default=["HK"], help="e.g. --market HK")
    parser.add_argument("--brand", nargs="+", help='e.g. --brand Acuvue')
    parser.add_argument("--max-posts", type=int, default=20, help="Max posts per hashtag/profile (default 20)")
    parser.add_argument("--dry-run", action="store_true", help="Print results instead of saving to DB")
    parser.add_argument("--skip-comments", action="store_true", help="Posts only, skip the comments phase")
    parser.add_argument(
        "--source", choices=["hashtag", "profile", "both"], default="hashtag",
        help="'hashtag' (default, uses HASHTAGS config), 'profile' (uses PROFILES config, "
             "reaches further back in time per-account), or 'both'",
    )
    parser.add_argument(
        "--classify-existing", action="store_true",
        help="Backfill sentiment/purchase-barrier labels for already-saved comments, then exit",
    )
    args = parser.parse_args()

    if args.classify_existing:
        classify_existing()
    else:
        run(
            markets=args.market,
            brand_filter=args.brand,
            max_posts=args.max_posts,
            dry_run=args.dry_run,
            skip_comments=args.skip_comments,
            source_mode=args.source,
        )
