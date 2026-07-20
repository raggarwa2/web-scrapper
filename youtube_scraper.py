# ==================================================================
# YouTube Reputation Module — standalone (NOT part of pipeline_v2.py)
# ==================================================================
# Purpose:
#   Add YouTube as a reputation-pillar source for the contact lens
#   intelligence pipeline (HK + TH). Discovers videos per brand keyword
#   via the official YouTube Data API v3, then pulls comments (top-level
#   + replies) for reputation/sentiment analysis. No login, no Playwright.
#
# Status: STANDALONE MODULE — own SQLite db (youtube_data.db).
#   Per project convention, this is NOT wired into pipeline_v2.py or
#   config.yaml. Review sample output first; merge is a separate,
#   explicit step touching those files directly.
#
# Cost model (YouTube Data API v3, free tier = 10,000 units/day):
#   - search.list        : 100 units/call  (expensive — this is the
#                           only way to discover videos by keyword)
#   - videos.list         : 1 unit/call    (batch up to 50 IDs)
#   - commentThreads.list : 1 unit/call    (up to 100 threads/page)
#   - comments.list       : 1 unit/call    (up to 100 replies/page —
#                           only called for threads that have replies)
#
#   A full run across 5 brands x 2 markets x ~1-2 keyword variants
#   = ~15-20 search.list calls = ~1,500-2,000 units. Well inside the
#   free daily quota, but this is NOT a "run anytime" API like
#   HKTVmall's Algolia endpoint — budget accordingly if run daily.
#
# Setup required (one-time, by the user):
#   1. Google Cloud Console -> new/existing project -> enable
#      "YouTube Data API v3" -> create an API key.
#   2. Add to .env:  YOUTUBE_API_KEY=xxxxx
#
# Usage:
#   python youtube_scraper.py --brand Acuvue --market HK
#   python youtube_scraper.py --market HK TH          # all configured brands
#   python youtube_scraper.py --dry-run               # print instead of save
# ==================================================================

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── CONFIG: brand keywords per market ────────────────────────────────
# Kept inline (not config.yaml) while this is a standalone module.
# Mirrors the brand list already in pipeline_v2.py's config.yaml —
# copy this block into config.yaml under a `youtube:` key if/when merged.

BRAND_KEYWORDS = {
    "HK": {
        "Acuvue":          ["Acuvue 香港", "Acuvue review HK", "Acuvue 分享", "Acuvue 開箱", "ACUVUE OASYS 香港", "強生 隱形眼鏡"],
        "Alcon":           ["Alcon 隱形眼鏡 香港", "Air Optix HK review", "Dailies Total1 香港", "Alcon 試戴", "愛爾康 隱形眼鏡", "Dailies 香港"],
        "Bausch & Lomb":   ["博士倫 隱形眼鏡", "Bausch Lomb HK review", "博士倫 分享", "博士倫 試戴"],
        "CooperVision":    ["Biofinity 香港", "CooperVision HK review", "Biofinity 分享", "MyDay 香港", "clariti 香港", "MyDay 分享"],
        "Olens":           ["Olens 香港", "Olens HK 隱形眼鏡", "Olens 開箱", "Olens 試戴"],
    },
    "TH": {
        "Acuvue":          ["Acuvue รีวิว", "Acuvue Thailand"],
        "Alcon":           ["Alcon คอนแทคเลนส์ รีวิว", "Air Optix Thailand"],
        "Bausch & Lomb":   ["Bausch Lomb รีวิว", "Biotrue Thailand"],
        "CooperVision":    ["Biofinity รีวิว", "CooperVision Thailand"],
        "Olens":           ["Olens Thailand รีวิว"],
        "Acne-Aid":        ["Acne-Aid รีวิว", "Acne-Aid Thailand"],
    },
}

# Literal brand-name aliases — used only as a deterministic pre-check
# in check_brand_relevance() so the unambiguous case (title/channel
# literally names the brand) never depends on an LLM call-to-call judgment
# and can't flip between runs. Product lines that DON'T spell out the
# parent brand (Air Optix, Biotrue, Oasys, etc.) are deliberately left
# out — those still need the LLM's judgment, handled separately.
BRAND_ALIASES = {
    "Acuvue": ["acuvue"],
    "Alcon": ["alcon"],
    "Bausch & Lomb": ["bausch", "博士倫"],
    "CooperVision": ["coopervision"],
    "Olens": ["olens"],
    "Acne-Aid": ["acne-aid", "acne aid"],
}

REGION_CODE = {"HK": "HK", "TH": "TH"}
RELEVANCE_LANGUAGE = {"HK": "zh-Hant", "TH": "th"}

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

TRANSLATE_BATCH_SIZE = 20

# ── RELEVANCE FILTER ──────────────────────────────────────────────────
# Root cause of noise: keywords like "Air Optix HK review" or "博士倫" match
# YouTube's search on the word "optix"/"optics" alone — pulling in AR
# glasses, airsoft/gun-optics, and other unrelated hardware review videos.
# Fix: whitelist-based, not blacklist-based. A video must contain at least
# one explicit contact-lens term in its title or description to be kept.
# Blacklisting ("exclude airsoft/AR/scar") is a losing game — whitelisting
# what a contact lens video actually looks like is far more robust.

LENS_RELEVANCE_TERMS_EN = [
    "contact lens", "contact lenses", "contactlens", "coloured contact",
    "colored contact", "daily disposable", "monthly disposable",
    "toric lens", "1-day", "1 day",
]
LENS_RELEVANCE_TERMS_NONASCII = [
    # Traditional Chinese (HK)
    "隱形眼鏡", "月拋", "日拋", "散光", "老花", "彩色隱形", "美瞳",
    # Thai
    "คอนแทคเลนส์", "เลนส์สายตา", "เลนส์นิ่ม",
]

_LENS_TERM_PATTERN_NONASCII = re.compile("|".join(LENS_RELEVANCE_TERMS_NONASCII))
_LENS_TERM_PATTERN_EN = re.compile(
    "|".join(t.replace(" ", r"\s*") for t in LENS_RELEVANCE_TERMS_EN),
    re.IGNORECASE,
)


def _is_lens_relevant(title: str, description: str = "") -> bool:
    """True if the TITLE contains an explicit contact-lens term.
    Whitelist match — absence of a lens term means we drop the video,
    regardless of how the keyword search matched it.

    Deliberately title-only, not title+description: sponsored makeup/
    skincare channels (e.g. TA-TO Contacts-sponsored foundation reviews)
    carry lens-retailer sponsor boilerplate in the description on every
    video regardless of actual content, which caused false positives
    when description text was included in the check."""
    return bool(_LENS_TERM_PATTERN_NONASCII.search(title) or _LENS_TERM_PATTERN_EN.search(title))


# ── DB SCHEMA (separate db — youtube_data.db) ────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS youtube_videos (
    video_id       TEXT PRIMARY KEY,
    brand          TEXT NOT NULL,
    market         TEXT NOT NULL,
    keyword        TEXT,
    title          TEXT,
    title_en       TEXT,
    channel_title  TEXT,
    published_at   TEXT,
    view_count     INTEGER,
    like_count     INTEGER,
    comment_count  INTEGER,
    url            TEXT,
    discovered_at  TEXT,
    brand_relevant INTEGER
);

CREATE TABLE IF NOT EXISTS youtube_comments (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id                  TEXT NOT NULL,
    brand                     TEXT NOT NULL,
    market                    TEXT NOT NULL,
    author                    TEXT,
    comment_text              TEXT,
    comment_text_en           TEXT,
    like_count                INTEGER,
    published_at              TEXT,
    content_hash              TEXT UNIQUE,
    scraped_at                TEXT,
    sentiment                 TEXT,
    is_purchase_barrier_signal INTEGER,
    is_lens_relevant          INTEGER,
    FOREIGN KEY(video_id) REFERENCES youtube_videos(video_id)
);

CREATE INDEX IF NOT EXISTS idx_yt_videos_brand   ON youtube_videos(brand);
CREATE INDEX IF NOT EXISTS idx_yt_comments_brand ON youtube_comments(brand);
"""

# Columns added after the initial release — migrated in on open for dbs
# created before this field existed (SQLite has no ADD COLUMN IF NOT EXISTS).
_MIGRATION_COLUMNS = {
    "youtube_comments": {
        "sentiment": "TEXT",
        "is_purchase_barrier_signal": "INTEGER",
        "is_lens_relevant": "INTEGER",
    },
    "youtube_videos": {
        "title_en": "TEXT",
        "brand_relevant": "INTEGER",
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
    conn.commit()
    return conn


# ── LANGUAGE DETECTION + TRANSLATION (mirrors pipeline_v2.py FIX 4) ──

def _is_non_english(text: str) -> bool:
    """True if text has meaningful non-Latin script content (CJK or Thai)."""
    if not text or len(text) < 3:
        return False
    cjk   = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    thai  = sum(1 for c in text if '\u0e00' <= c <= '\u0e7f')
    return (cjk + thai) / len(text) > 0.15


def _llm_translate_batch(texts: List[str], client: OpenAI) -> List[str]:
    """Batch-translate non-English comments/titles to English.

    Each item is tagged with its 1-based input index ("i") in the response
    and mapped back by that index rather than by array position — source
    text can itself contain newlines that make the model split or merge
    items, silently misaligning a plain ordered array (observed in
    production: a 20-item batch came back with 21 or 22 elements).
    Falls back to the original text per-item for anything the model drops
    — better to show untranslated text than a shifted-by-one translation."""
    if not texts:
        return []
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    prompt = f"""Translate these {len(texts)} contact lens-related YouTube comments to English.
For each one, return an object with:
- "i": the comment's number as shown below (integer)
- "t": the English translation (natural, concise)

Return ONLY a JSON array of objects, one per comment, no other text.

Comments:
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


CLASSIFY_BATCH_SIZE = 20


def _llm_classify_batch(texts_en: List[str], client: OpenAI) -> List[dict]:
    """Batch sentiment + purchase-barrier + on-topic classification, same
    sentiment/barrier fields as LIHKGPost in lihkg_scraper.py so the two
    sources score comparably.

    YouTube comment sections drift off-topic easily (e.g. a brand
    sponsorship video with a K-pop group draws comments about the group,
    not the product) — is_lens_relevant flags those so sentiment/barrier
    aggregates in youtube_signals.py can exclude them while still showing
    them in the raw comment browser.

    Each item is tagged with its 1-based input index ("i") in the response
    and mapped back by that index rather than by array position — comment
    text can itself contain newlines/numbering that make the model split
    or merge items, so position-based mapping silently misaligned results.
    Falls back to neutral/False/relevant=True per-item for anything the
    model drops (fail open on relevance so a dropped item still surfaces
    for manual review rather than being silently hidden)."""
    if not texts_en:
        return []
    fallback = {"sentiment": "neutral", "is_purchase_barrier_signal": False, "is_lens_relevant": True}
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts_en))
    prompt = f"""Classify these {len(texts_en)} YouTube comments left on contact lens brand videos.
For each comment, return an object with:
- "i": the comment's number as shown below (integer)
- "sentiment": one of "positive", "negative", "neutral", "mixed"
- "is_purchase_barrier_signal": true if the comment expresses a reason for not buying/switching (price, availability, comfort, trust, etc.), else false
- "is_lens_relevant": true if the comment is actually about the contact lenses/product — feedback, a question about the lenses, praise/complaints about wearing them, purchase intent, or naming a specific product/color/style (even just a shade nickname, without the word "lens" — shoppers often refer to colored contacts by their style name alone). false if it's off-topic chatter unrelated to the product itself — e.g. commenting on a featured model/celebrity's appearance or identity, discussing an unrelated topic (a K-pop group's name, someone else's comment/argument), or a generic reaction with no product context.

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


BRAND_RELEVANCE_BATCH_SIZE = 20


def _llm_brand_relevance_batch(items: List[dict], client: OpenAI) -> List[bool]:
    """True if a video is actually about the brand it was tagged with at
    discovery (from the keyword search that found it), not just generic
    contact-lens content. _is_lens_relevant() already whitelist-filters
    for "is this about contact lenses at all" (catches airsoft/AR-glasses
    noise) — this catches the next layer: a video can pass that check and
    still be about a different brand entirely (a business case-study of
    Nike's failed lens venture surfaced under a "博士倫/Bausch & Lomb"
    keyword search) or generic multi-brand retailer content with no tie
    to the specific brand.

    Each `item` is {"title": str, "channel_title": str, "brand": str}.
    Deliberately not done with a substring/alias match — brand product
    lines (Air Optix/Opti-Free/Dailies/Freshlook = Alcon; Biotrue/ULTRA/
    PureVision = Bausch & Lomb; Biofinity/MyDay/clariti = CooperVision;
    Oasys = Acuvue) rarely spell out the parent brand name in a title,
    which a plain substring check would have flagged as false negatives.
    Fails open (True) on parse errors/drops — under-flagging just leaves
    the existing behavior, over-flagging silently deletes real data."""
    if not items:
        return []
    numbered = "\n".join(
        f"{i + 1}. Brand: {it['brand']} | Title: {it['title']} | Channel: {it['channel_title']}"
        for i, it in enumerate(items)
    )
    prompt = f"""Each of these {len(items)} YouTube videos was surfaced by a keyword search for the stated brand's contact lens products.

Mark "is_brand_relevant": true if EITHER:
(a) the channel is an official or reseller channel for that brand — its name contains the brand (e.g. channels named "OLENSHK"/"OLensglobal" belong to Olens; "博士倫香港"/"Bausch + Lomb HK" belong to Bausch & Lomb; "ACUVUE HK" belongs to Acuvue) — a video from the brand's own channel is relevant even if the title alone doesn't restate the brand name, OR
(b) the brand name, its hashtag, or one of its known product lines (Air Optix/Opti-Free/Dailies/Freshlook = Alcon; Biotrue/ULTRA/PureVision = Bausch & Lomb; Biofinity/MyDay/clariti/Avaira = CooperVision; Oasys = Acuvue) is named anywhere in the title.

Mark false ONLY if NEITHER (a) nor (b) holds — e.g. the video is clearly about a different company/brand/topic entirely (a Nike product story, an engineering/manufacturing process video, generic pharmacist advice naming no brand), not merely because the title is short or stylized.

When genuinely unsure, default to true — this check exists to catch clear mismatches, not to second-guess borderline cases.

For each video, return an object with:
- "i": the video's number as shown below (integer)
- "is_brand_relevant": true/false

Return ONLY a JSON array of objects, one per video, no other text.

Videos:
{numbered}"""
    by_index: Dict[int, bool] = {}
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            for p in parsed:
                idx = p.get("i")
                if isinstance(idx, int) and 1 <= idx <= len(items):
                    by_index[idx] = bool(p.get("is_brand_relevant", True))
        if len(by_index) != len(items):
            log.warning(f"[LLM] Brand relevance batch: expected {len(items)}, got {len(by_index)} indexed")
    except Exception as e:
        log.warning(f"[LLM] Brand relevance batch failed: {e}")

    return [by_index.get(i + 1, True) for i in range(len(items))]


def check_brand_relevance(items: List[dict], client: OpenAI) -> List[bool]:
    """Wraps _llm_brand_relevance_batch with a deterministic pre-check:
    if the brand's literal name already appears in the title or channel
    (BRAND_ALIASES), mark relevant without asking the LLM at all — this
    is the unambiguous case, and an LLM call-to-call judgment on it can
    flip between runs on identical input (observed empirically: batch
    context affects the model's answer even for a title that literally
    contains "OLENS"). Only the items where the brand name ISN'T literally
    present (product-line names, official-channel-without-brand-in-title
    cases) go to the LLM, which still needs to reason about those."""
    if not items:
        return []
    results: List[Optional[bool]] = [None] * len(items)
    needs_llm_idxs = []
    for i, it in enumerate(items):
        aliases = BRAND_ALIASES.get(it["brand"], [it["brand"].lower()])
        haystack = f"{it['title']} {it['channel_title']}".lower()
        if any(alias.lower() in haystack for alias in aliases):
            results[i] = True
        else:
            needs_llm_idxs.append(i)

    if needs_llm_idxs:
        llm_results = _llm_brand_relevance_batch([items[i] for i in needs_llm_idxs], client)
        for i, r in zip(needs_llm_idxs, llm_results):
            results[i] = r

    return results


# ── YOUTUBE API CALLS ─────────────────────────────────────────────────

def _api_get(endpoint: str, params: dict) -> dict:
    url = f"{YOUTUBE_API_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def search_videos(
    keyword: str,
    market: str,
    api_key: str,
    cutoff: datetime,
    max_results: int = 50,  # YouTube's per-request cap; same 100-unit cost as fewer results
) -> List[dict]:
    """search.list — 100 quota units. Returns raw video search items."""
    params = {
        "part": "snippet",
        "q": keyword,
        "type": "video",
        "regionCode": REGION_CODE.get(market, ""),
        "relevanceLanguage": RELEVANCE_LANGUAGE.get(market, ""),
        "order": "relevance",
        "maxResults": max_results,
        "publishedAfter": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "key": api_key,
    }
    try:
        data = _api_get("search", params)
        return data.get("items", [])
    except Exception as e:
        log.warning(f"[DISCOVER] search.list failed for '{keyword}': {e}")
        return []


def fetch_video_stats(video_ids: List[str], api_key: str) -> Dict[str, dict]:
    """videos.list — 1 unit/call, batch up to 50 IDs. Returns {video_id: stats}."""
    stats: Dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        params = {"part": "statistics", "id": ",".join(batch), "key": api_key}
        try:
            data = _api_get("videos", params)
            for item in data.get("items", []):
                stats[item["id"]] = item.get("statistics", {})
        except Exception as e:
            log.warning(f"[DISCOVER] videos.list stats failed: {e}")
    return stats


def fetch_comments(
    video_id: str,
    api_key: str,
    max_pages: int = 5,
) -> List[dict]:
    """commentThreads.list — 1 unit/page, up to 100 threads/page.
    Also pulls replies via comments.list for any thread with replies —
    videos.list's statistics.commentCount includes replies, so skipping
    them under-counts scraped comments relative to the reported total."""
    comments = []
    page_token = None
    for _ in range(max_pages):
        params = {
            "part": "snippet",
            "videoId": video_id,
            "maxResults": 100,
            "order": "relevance",
            "textFormat": "plainText",
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            data = _api_get("commentThreads", params)
        except Exception as e:
            # Comments disabled on video, or quota/network error — stop, not fatal.
            log.debug(f"[EXTRACT] commentThreads failed for {video_id}: {e}")
            break

        for item in data.get("items", []):
            top = item["snippet"]["topLevelComment"]["snippet"]
            comments.append({
                "author":       top.get("authorDisplayName", ""),
                "comment_text": top.get("textDisplay", ""),
                "like_count":   top.get("likeCount", 0),
                "published_at": top.get("publishedAt", ""),
            })
            if item["snippet"].get("totalReplyCount", 0) > 0:
                comments.extend(fetch_replies(item["id"], api_key))

        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(0.3)

    return comments


def fetch_replies(parent_id: str, api_key: str) -> List[dict]:
    """comments.list — 1 unit/page. Fetches all replies under a top-level comment."""
    replies = []
    page_token = None
    while True:
        params = {
            "part": "snippet",
            "parentId": parent_id,
            "maxResults": 100,
            "textFormat": "plainText",
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            data = _api_get("comments", params)
        except Exception as e:
            log.debug(f"[EXTRACT] comments.list (replies) failed for {parent_id}: {e}")
            break

        for item in data.get("items", []):
            r = item["snippet"]
            replies.append({
                "author":       r.get("authorDisplayName", ""),
                "comment_text": r.get("textDisplay", ""),
                "like_count":   r.get("likeCount", 0),
                "published_at": r.get("publishedAt", ""),
            })

        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(0.3)

    return replies


# ── DISCOVERY + EXTRACTION ORCHESTRATION ─────────────────────────────

def discover_and_extract(
    market: str,
    brand: str,
    keywords: List[str],
    api_key: str,
    client: OpenAI,
    cutoff: datetime,
    existing_video_ids: set,
) -> tuple[List[dict], List[dict]]:
    """Returns (videos, comments) ready to insert."""
    now = datetime.now(timezone.utc).isoformat()
    videos: List[dict] = []
    all_comments: List[dict] = []
    seen_ids: set = set()

    for keyword in keywords:
        log.info(f"[DISCOVER] YouTube | {brand} | {market} | keyword: {keyword}")
        items = search_videos(keyword, market, api_key, cutoff)
        video_ids = []

        skipped_irrelevant = 0
        for item in items:
            vid = item.get("id", {}).get("videoId")
            if not vid or vid in seen_ids or vid in existing_video_ids:
                continue
            snippet = item.get("snippet", {})
            title = snippet.get("title", "")
            description = snippet.get("description", "")
            if not _is_lens_relevant(title, description):
                skipped_irrelevant += 1
                log.debug(f"[DISCOVER] Skipped irrelevant: {title[:60]}")
                continue
            seen_ids.add(vid)
            video_ids.append(vid)
            videos.append({
                "video_id":      vid,
                "brand":         brand,
                "market":        market,
                "keyword":       keyword,
                "title":         snippet.get("title", ""),
                "title_en":      snippet.get("title", ""),  # placeholder, overwritten below
                "channel_title": snippet.get("channelTitle", ""),
                "published_at":  snippet.get("publishedAt", ""),
                "view_count":    None,   # filled in below
                "like_count":    None,
                "comment_count": None,
                "url":           f"https://www.youtube.com/watch?v={vid}",
                "discovered_at": now,
                "brand_relevant": None,  # filled in below
            })

        log.info(
            f"[DISCOVER] YouTube | {brand} | {keyword}: {len(video_ids)} new videos kept, "
            f"{skipped_irrelevant} skipped as irrelevant"
        )
        time.sleep(0.5)   # be a reasonable API citizen

    # Batch-fetch stats (cheap — 1 unit per 50 videos)
    if videos:
        stats = fetch_video_stats([v["video_id"] for v in videos], api_key)
        for v in videos:
            s = stats.get(v["video_id"], {})
            v["view_count"]    = int(s.get("viewCount", 0)) if s.get("viewCount") else None
            v["like_count"]    = int(s.get("likeCount", 0)) if s.get("likeCount") else None
            v["comment_count"] = int(s.get("commentCount", 0)) if s.get("commentCount") else None

    # Batch-translate non-English video titles (same pattern as comments below)
    non_en_title_idxs = [i for i, v in enumerate(videos) if _is_non_english(v["title"])]
    for start in range(0, len(non_en_title_idxs), TRANSLATE_BATCH_SIZE):
        batch_idxs = non_en_title_idxs[start:start + TRANSLATE_BATCH_SIZE]
        source_titles = [videos[i]["title"] for i in batch_idxs]
        translations = _llm_translate_batch(source_titles, client)
        for idx, translation in zip(batch_idxs, translations):
            videos[idx]["title_en"] = translation

    # Batch brand-relevance check — is each video actually about the brand
    # it was tagged with, not just generic lens content that surfaced under
    # its keyword search (see check_brand_relevance docstring)
    for start in range(0, len(videos), BRAND_RELEVANCE_BATCH_SIZE):
        batch = videos[start:start + BRAND_RELEVANCE_BATCH_SIZE]
        items = [{"title": v["title_en"], "channel_title": v["channel_title"], "brand": v["brand"]} for v in batch]
        results = check_brand_relevance(items, client)
        for v, is_relevant in zip(batch, results):
            v["brand_relevant"] = is_relevant

    # Fetch comments per video
    for v in videos:
        raw_comments = fetch_comments(v["video_id"], api_key)
        log.info(f"[EXTRACT] {v['video_id']} ({v['title'][:40]}): {len(raw_comments)} comments")
        for c in raw_comments:
            raw_key = f"{v['video_id']}|{c['published_at']}|{c['comment_text'][:50]}"
            c_hash = hashlib.md5(raw_key.encode()).hexdigest()
            all_comments.append({
                "video_id":        v["video_id"],
                "brand":           brand,
                "market":          market,
                "author":          c["author"],
                "comment_text":    c["comment_text"],
                "comment_text_en": c["comment_text"],  # placeholder, overwritten below
                "like_count":      c["like_count"],
                "published_at":    c["published_at"],
                "content_hash":    c_hash,
                "scraped_at":      now,
                "sentiment":       None,   # filled in below
                "is_purchase_barrier_signal": None,
                "is_lens_relevant": None,
            })
        time.sleep(0.3)

    # Batch-translate non-English comments (mirrors pipeline_v2.py pattern)
    non_en_idxs = [i for i, c in enumerate(all_comments) if _is_non_english(c["comment_text"])]
    log.info(
        f"[EXTRACT] {brand}/{market}: {len(all_comments)} comments total — "
        f"{len(non_en_idxs)} non-English (-> LLM), {len(all_comments) - len(non_en_idxs)} English (skip LLM)"
    )
    for start in range(0, len(non_en_idxs), TRANSLATE_BATCH_SIZE):
        batch_idxs = non_en_idxs[start:start + TRANSLATE_BATCH_SIZE]
        source_texts = [all_comments[i]["comment_text"] for i in batch_idxs]
        translations = _llm_translate_batch(source_texts, client)
        for idx, translation in zip(batch_idxs, translations):
            all_comments[idx]["comment_text_en"] = translation

    # Batch sentiment + purchase-barrier classification (always runs, on the
    # English text, same fields as lihkg_scraper.py's LIHKGPost)
    for start in range(0, len(all_comments), CLASSIFY_BATCH_SIZE):
        batch = all_comments[start:start + CLASSIFY_BATCH_SIZE]
        labels = _llm_classify_batch([c["comment_text_en"] for c in batch], client)
        for c, label in zip(batch, labels):
            c["sentiment"] = label["sentiment"]
            c["is_purchase_barrier_signal"] = label["is_purchase_barrier_signal"]
            c["is_lens_relevant"] = label["is_lens_relevant"]

    return videos, all_comments


def save_videos(conn: sqlite3.Connection, videos: List[dict]) -> int:
    inserted = 0
    for v in videos:
        cur = conn.execute(
            """INSERT OR IGNORE INTO youtube_videos
               (video_id, brand, market, keyword, title, title_en, channel_title,
                published_at, view_count, like_count, comment_count, url, discovered_at,
                brand_relevant)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                v["video_id"], v["brand"], v["market"], v["keyword"], v["title"],
                v["title_en"], v["channel_title"], v["published_at"], v["view_count"],
                v["like_count"], v["comment_count"], v["url"], v["discovered_at"],
                v["brand_relevant"],
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
                """INSERT OR IGNORE INTO youtube_comments
                   (video_id, brand, market, author, comment_text, comment_text_en,
                    like_count, published_at, content_hash, scraped_at,
                    sentiment, is_purchase_barrier_signal, is_lens_relevant)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    c["video_id"], c["brand"], c["market"], c["author"],
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


def retranslate_stale_comments(db_path: str = "output/youtube_data.db") -> int:
    """Backfill comment_text_en for comments left untranslated by the
    array-position translation bug fixed in _llm_translate_batch (a batch
    count-mismatch silently fell back to the original, non-English text).
    Detected as comment_text_en == comment_text where comment_text is
    itself non-English — there's no dedicated "not yet translated" flag,
    so this is the same signal _is_non_english() uses at scrape time.
    Safe to re-run — only touches rows still matching that signal."""
    client = OpenAI()
    conn = open_db(db_path)
    rows = conn.execute(
        "SELECT id, comment_text FROM youtube_comments WHERE comment_text_en = comment_text"
    ).fetchall()
    stale = [r for r in rows if _is_non_english(r["comment_text"])]
    log.info(f"[RETRANSLATE] {len(stale)} comments still untranslated (of {len(rows)} text==text_en rows)")

    retranslated = 0
    for start in range(0, len(stale), TRANSLATE_BATCH_SIZE):
        batch = stale[start:start + TRANSLATE_BATCH_SIZE]
        translations = _llm_translate_batch([r["comment_text"] for r in batch], client)
        for row, translation in zip(batch, translations):
            conn.execute(
                "UPDATE youtube_comments SET comment_text_en = ? WHERE id = ?",
                (translation, row["id"]),
            )
            retranslated += 1
        conn.commit()
        log.info(f"[RETRANSLATE] {retranslated}/{len(stale)} done")

    conn.close()
    log.info(f"[RETRANSLATE] Done — {retranslated} comments retranslated")
    return retranslated


def classify_existing(db_path: str = "output/youtube_data.db") -> int:
    """Backfill sentiment/is_purchase_barrier_signal/is_lens_relevant for
    comments missing is_lens_relevant — covers both never-classified rows
    and rows classified before is_lens_relevant existed. Safe to re-run."""
    client = OpenAI()
    conn = open_db(db_path)
    rows = conn.execute(
        "SELECT id, comment_text_en FROM youtube_comments WHERE is_lens_relevant IS NULL"
    ).fetchall()
    log.info(f"[CLASSIFY] {len(rows)} comments missing classification")

    classified = 0
    for start in range(0, len(rows), CLASSIFY_BATCH_SIZE):
        batch = rows[start:start + CLASSIFY_BATCH_SIZE]
        labels = _llm_classify_batch([r["comment_text_en"] for r in batch], client)
        for row, label in zip(batch, labels):
            conn.execute(
                """UPDATE youtube_comments
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


def translate_existing_titles(db_path: str = "output/youtube_data.db") -> int:
    """Backfill title_en for videos saved before title translation existed
    (title_en IS NULL). Safe to re-run — only touches untranslated rows."""
    client = OpenAI()
    conn = open_db(db_path)
    rows = conn.execute(
        "SELECT video_id, title FROM youtube_videos WHERE title_en IS NULL"
    ).fetchall()
    log.info(f"[TRANSLATE] {len(rows)} video titles missing translation")

    translated = 0
    for row in rows:
        if not _is_non_english(row["title"]):
            conn.execute(
                "UPDATE youtube_videos SET title_en = ? WHERE video_id = ?",
                (row["title"], row["video_id"]),
            )
            translated += 1
    conn.commit()

    non_en_rows = [r for r in rows if _is_non_english(r["title"])]
    for start in range(0, len(non_en_rows), TRANSLATE_BATCH_SIZE):
        batch = non_en_rows[start:start + TRANSLATE_BATCH_SIZE]
        translations = _llm_translate_batch([r["title"] for r in batch], client)
        for row, translation in zip(batch, translations):
            conn.execute(
                "UPDATE youtube_videos SET title_en = ? WHERE video_id = ?",
                (translation, row["video_id"]),
            )
            translated += 1
        conn.commit()
        log.info(f"[TRANSLATE] {translated}/{len(rows)} done")

    conn.close()
    log.info(f"[TRANSLATE] Done — {translated} titles translated")
    return translated


def backfill_video_relevance(db_path: str = "output/youtube_data.db") -> int:
    """Backfill brand_relevant for videos saved before that check existed
    (brand_relevant IS NULL). Run translate_existing_titles() first — this
    classifies against title_en, not the raw (possibly non-English) title.
    Safe to re-run — only touches unclassified rows."""
    client = OpenAI()
    conn = open_db(db_path)
    rows = conn.execute(
        "SELECT video_id, title, title_en, channel_title, brand FROM youtube_videos WHERE brand_relevant IS NULL"
    ).fetchall()
    log.info(f"[RELEVANCE] {len(rows)} videos missing brand-relevance check")

    checked = 0
    for start in range(0, len(rows), BRAND_RELEVANCE_BATCH_SIZE):
        batch = rows[start:start + BRAND_RELEVANCE_BATCH_SIZE]
        items = [
            {"title": r["title_en"] or r["title"], "channel_title": r["channel_title"], "brand": r["brand"]}
            for r in batch
        ]
        results = check_brand_relevance(items, client)
        for row, is_relevant in zip(batch, results):
            conn.execute(
                "UPDATE youtube_videos SET brand_relevant = ? WHERE video_id = ?",
                (int(is_relevant), row["video_id"]),
            )
            checked += 1
        conn.commit()
        log.info(f"[RELEVANCE] {checked}/{len(rows)} done")

    conn.close()
    log.info(f"[RELEVANCE] Done — {checked} videos checked")
    return checked


# ── MAIN ──────────────────────────────────────────────────────────────

def run(
    markets: List[str],
    brand_filter: Optional[List[str]],
    time_window_months: int,
    dry_run: bool,
    db_path: str = "output/youtube_data.db",
) -> None:
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        log.error("[SETUP] YOUTUBE_API_KEY not found in .env — see file header for setup steps")
        raise SystemExit(1)

    client = OpenAI()  # uses OPENAI_API_KEY from .env, same as pipeline_v2.py
    cutoff = datetime.now(timezone.utc) - timedelta(days=30 * time_window_months)

    conn = None if dry_run else open_db(db_path)
    existing_ids: set = set()
    if conn:
        existing_ids = {row[0] for row in conn.execute("SELECT video_id FROM youtube_videos")}
        if existing_ids:
            log.info(f"[DISCOVER] {len(existing_ids)} videos already in DB — skipping")

    total_videos = 0
    total_comments = 0

    for market in markets:
        brand_map = BRAND_KEYWORDS.get(market, {})
        for brand, keywords in brand_map.items():
            if brand_filter and brand.lower() not in [b.lower() for b in brand_filter]:
                continue

            videos, comments = discover_and_extract(
                market, brand, keywords, api_key, client, cutoff, existing_ids
            )

            if dry_run:
                print(f"\n=== {brand} / {market} — {len(videos)} videos, {len(comments)} comments (DRY RUN) ===")
                for v in videos[:3]:
                    print(f"  {v['title'][:60]} | views={v['view_count']} | {v['url']}")
                for c in comments[:3]:
                    print(f"    comment: {c['comment_text_en'][:80]}")
                continue

            n_v = save_videos(conn, videos)
            n_c = save_comments(conn, comments)
            total_videos += n_v
            total_comments += n_c
            log.info(f"[SAVE] {brand}/{market}: +{n_v} videos, +{n_c} comments")

    if conn:
        conn.close()

    log.info(f"[DONE] Total new: {total_videos} videos, {total_comments} comments")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="YouTube reputation module (standalone)")
    parser.add_argument("--market", nargs="+", default=["HK", "TH"], help="e.g. --market HK or --market HK TH")
    parser.add_argument("--brand", nargs="+", help='e.g. --brand Acuvue or --brand "Bausch & Lomb"')
    parser.add_argument("--time-window-months", type=int, default=6, help="Video recency cutoff (default 6mo)")
    parser.add_argument("--dry-run", action="store_true", help="Print results instead of saving to DB")
    parser.add_argument(
        "--classify-existing", action="store_true",
        help="Backfill sentiment/purchase-barrier labels for already-saved comments, then exit",
    )
    args = parser.parse_args()

    if args.classify_existing:
        translate_existing_titles()
        retranslate_stale_comments()
        backfill_video_relevance()
        classify_existing()
    else:
        run(
            markets=args.market,
            brand_filter=args.brand,
            time_window_months=args.time_window_months,
            dry_run=args.dry_run,
        )
