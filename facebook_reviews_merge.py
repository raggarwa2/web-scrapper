"""
facebook_reviews_merge.py
==========================
Reads output/facebook_test/all_reviews_raw.json (produced by
facebook_reviews_test_all.py's discovery-only scrape) and writes
output/facebook_data.db — a NEW, SEPARATE database, not lensdata.db.
Deliberately does not touch pipeline_v2.py, config.yaml, or lensdata.db,
per facebook_scraper_prompt.md's constraint. Mirrors the youtube_data.db
precedent: a source that lives in its own db but still gets pooled into
triangulation/reputation_crosscheck.py etc. via a --facebook-db arg.

Safe to re-run: reviews are INSERT OR IGNORE'd by legacy_id (a natural,
stable Facebook ID — verified unique across all 310 rows in the current
JSON, so no synthesized MD5 hash needed, unlike reviews.content_hash or
xhs_comments.comment_id). Rows already present are skipped before the LLM
call too, so a re-run never re-spends on rows already classified.

PII: the raw JSON's per-review `user` object (name, profileUrl,
profilePic, Facebook user id) is intentionally never read into any
persisted column or logged — only legacyId/id and the review content
fields are used. Do not add a user/reviewer column to the schema or
print a raw review dict.

Sentiment is rule-derived from Facebook's own first-party isRecommended
flag (True -> positive, False -> negative, missing -> neutral) rather
than LLM-classified — it's a ground-truth binary signal, unlike LIHKG/XHS
which have no equivalent structured flag and need the LLM for sentiment
too. The LLM is reserved for English translation, brand-mention
extraction (constrained to the 5 tracked brands, LIHKG's convention —
not XHS's free-text field, which has documented drift), and the
purchase-barrier signal (for dashboard parity with LIHKG/YouTube/
Instagram).

The nested `comments` array present on ~32% of reviews (replies to a
review) is intentionally ignored in this phase — that's closer to
facebook_scraper_prompt.md's parked Phase 2 (posts/comments, a different
actor and relevance-filtering profile) than to review ingestion.

Usage:
    python facebook_reviews_merge.py
"""

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

RAW_JSON = Path("output/facebook_test/all_reviews_raw.json")
DB_PATH = "output/facebook_data.db"

MODEL = "gpt-4o-mini"
BATCH_SIZE = 20
KNOWN_BRANDS = ["Acuvue", "Alcon", "Bausch & Lomb", "CooperVision", "Olens"]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fb_reviews (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_id                   TEXT UNIQUE NOT NULL,
    page_name                   TEXT NOT NULL,
    facebook_url                TEXT,
    review_url                  TEXT,
    review_date                 TEXT,
    is_recommended              INTEGER,
    sentiment                   TEXT,
    text_original                TEXT,
    text_english                 TEXT,
    mentioned_brands              TEXT,
    is_purchase_barrier_signal    INTEGER,
    likes_count                   INTEGER,
    comments_count                 INTEGER,
    scraped_at                     TEXT DEFAULT (datetime('now')),
    extracted_at                    TEXT
);
CREATE INDEX IF NOT EXISTS idx_fb_reviews_page ON fb_reviews(page_name);
CREATE INDEX IF NOT EXISTS idx_fb_reviews_sentiment ON fb_reviews(sentiment);
CREATE INDEX IF NOT EXISTS idx_fb_reviews_date ON fb_reviews(review_date);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def load_raw_reviews() -> List[dict]:
    """Flattens the {page_name: {facebook_url, reviews: [...]}} JSON into
    one list of reviews, each carrying its page_name/facebook_url down."""
    data = json.loads(RAW_JSON.read_text(encoding="utf-8"))
    flat = []
    for page_name, page in data.items():
        for review in page.get("reviews") or []:
            flat.append({
                "page_name": page_name,
                "facebook_url": page.get("facebook_url"),
                "legacy_id": review.get("legacyId") or review.get("id"),
                "review_url": review.get("url"),
                "review_date": review.get("date"),
                "is_recommended": review.get("isRecommended"),
                "text": (review.get("text") or "").strip(),
                "likes_count": review.get("likesCount"),
                "comments_count": review.get("commentsCount"),
            })
    return flat


def load_existing_legacy_ids(conn: sqlite3.Connection) -> set:
    return {row[0] for row in conn.execute("SELECT legacy_id FROM fb_reviews")}


def derive_sentiment(is_recommended: Optional[bool]) -> str:
    if is_recommended is True:
        return "positive"
    if is_recommended is False:
        return "negative"
    return "neutral"


def _llm_classify_batch(texts: List[str], client: OpenAI) -> List[dict]:
    """One combined call per batch: English translation + brand mentions
    (constrained to the 5 tracked brands) + purchase-barrier flag.
    Index-tagged response items, not plain array position — embedded
    newlines in review text can make the model split/merge items and
    silently misalign a plain ordered array (same fix as
    instagram_scraper.py's _llm_translate_batch/_llm_classify_batch)."""
    if not texts:
        return []
    fallback = {"text_english": None, "mentioned_brands": [], "is_purchase_barrier_signal": False}
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    brand_list = ", ".join(KNOWN_BRANDS)
    prompt = f"""Analyse these {len(texts)} Facebook page reviews for Hong Kong optical/contact lens retailers.
For each review, return an object with:
- "i": the review's number as shown below (integer)
- "text_english": English translation of the review text (natural, concise)
- "mentioned_brands": a list of which of these five brands are mentioned in the review text — {brand_list} — leave empty if none are mentioned (do not invent other brand names)
- "is_purchase_barrier_signal": true if the review expresses a reason for not buying/switching/recommending (price, availability, comfort, trust, service quality, etc.), else false

Return ONLY a JSON array of objects, one per review, no other text.

Reviews:
{numbered}"""
    by_index: Dict[int, dict] = {}
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            for p in parsed:
                idx = p.get("i")
                if isinstance(idx, int) and 1 <= idx <= len(texts):
                    brands = [b for b in (p.get("mentioned_brands") or []) if b in KNOWN_BRANDS]
                    by_index[idx] = {
                        "text_english": p.get("text_english") or texts[idx - 1],
                        "mentioned_brands": brands,
                        "is_purchase_barrier_signal": bool(p.get("is_purchase_barrier_signal", False)),
                    }
        if len(by_index) != len(texts):
            print(f"  [LLM] batch mismatch: expected {len(texts)}, got {len(by_index)} indexed")
    except Exception as e:
        print(f"  [LLM] batch failed: {e}")

    return [by_index.get(i + 1, {**fallback, "text_english": texts[i]}) for i in range(len(texts))]


def main():
    if not RAW_JSON.exists():
        sys.exit(f"{RAW_JSON} not found — run facebook_reviews_test_all.py first")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    init_schema(conn)

    reviews = load_raw_reviews()
    print(f"Loaded {len(reviews)} reviews from {RAW_JSON}")

    existing = load_existing_legacy_ids(conn)
    new_reviews = [r for r in reviews if r["legacy_id"] and r["legacy_id"] not in existing]
    skipped_existing = len(reviews) - len(new_reviews)
    print(f"{skipped_existing} already in {DB_PATH} — {len(new_reviews)} new to process")

    if not new_reviews:
        print("Nothing new to do.")
        conn.close()
        return

    client = OpenAI()
    now = datetime.now(timezone.utc).isoformat()

    to_classify_idx = [i for i, r in enumerate(new_reviews) if r["text"]]
    empty_text_count = len(new_reviews) - len(to_classify_idx)
    print(f"{len(to_classify_idx)} reviews have text (-> LLM), {empty_text_count} empty (rating-only, skip LLM)")

    classifications: Dict[int, dict] = {}
    batches_run = 0
    for batch_start in range(0, len(to_classify_idx), BATCH_SIZE):
        batch_idx = to_classify_idx[batch_start: batch_start + BATCH_SIZE]
        batch_texts = [new_reviews[i]["text"] for i in batch_idx]
        results = _llm_classify_batch(batch_texts, client)
        for i, result in zip(batch_idx, results):
            classifications[i] = result
        batches_run += 1
        print(f"  classified batch {batches_run} ({len(batch_idx)} reviews)")

        inserted_this_batch = 0
        for i in batch_idx:
            r = new_reviews[i]
            c = classifications[i]
            conn.execute(
                """INSERT OR IGNORE INTO fb_reviews
                   (legacy_id, page_name, facebook_url, review_url, review_date,
                    is_recommended, sentiment, text_original, text_english,
                    mentioned_brands, is_purchase_barrier_signal, likes_count,
                    comments_count, extracted_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["legacy_id"], r["page_name"], r["facebook_url"], r["review_url"], r["review_date"],
                 int(r["is_recommended"]) if r["is_recommended"] is not None else None,
                 derive_sentiment(r["is_recommended"]), r["text"], c["text_english"],
                 ",".join(c["mentioned_brands"]), int(c["is_purchase_barrier_signal"]),
                 r["likes_count"], r["comments_count"], now),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted_this_batch += 1
        conn.commit()
        print(f"  saved batch {batches_run} ({inserted_this_batch} rows inserted)")

    # Empty-text rows: no LLM needed, sentiment still comes from is_recommended.
    empty_inserted = 0
    for i, r in enumerate(new_reviews):
        if r["text"]:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO fb_reviews
               (legacy_id, page_name, facebook_url, review_url, review_date,
                is_recommended, sentiment, text_original, text_english,
                mentioned_brands, is_purchase_barrier_signal, likes_count,
                comments_count, extracted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["legacy_id"], r["page_name"], r["facebook_url"], r["review_url"], r["review_date"],
             int(r["is_recommended"]) if r["is_recommended"] is not None else None,
             derive_sentiment(r["is_recommended"]), r["text"], "",
             "", 0, r["likes_count"], r["comments_count"], now),
        )
        if conn.execute("SELECT changes()").fetchone()[0]:
            empty_inserted += 1
    conn.commit()

    total_inserted = conn.execute("SELECT COUNT(*) FROM fb_reviews").fetchone()[0] - len(existing)
    print(f"\nDone. {total_inserted} new rows inserted into {DB_PATH} "
          f"({batches_run} LLM batches, {empty_inserted} empty-text rows skipped LLM).")
    conn.close()


if __name__ == "__main__":
    main()
