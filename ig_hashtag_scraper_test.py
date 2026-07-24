#!/usr/bin/env python3
"""
ig_hashtag_scraper_test.py — Phase 0 discovery test: Instagram hashtag scraping via Apify.

STANDALONE / SCRATCH SCRIPT for ig_hashtag_scraper_prompt.md.
Does NOT touch pipeline_v2.py, pipeline_auto.py, lensdata.db, or config.yaml.
All output goes to output/ig_hashtag_test/ only. No production DB writes.

Usage:
    python ig_hashtag_scraper_test.py

Prerequisites:
    APIFY_TOKEN and OPENAI_API_KEY in .env (both already present in this project)
"""

import os
import sys
import json
import time
import logging
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

load_dotenv()

APIFY_BASE = "https://api.apify.com/v2"
OUT_DIR = Path("output/ig_hashtag_test")
OUT_DIR.mkdir(parents=True, exist_ok=True)

LOG_PATH = OUT_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

HASHTAGS = ["隱形眼鏡", "contactlensHK", "acuvuehk"]
POSTS_PER_HASHTAG = 20
SAMPLE_SIZE = 5

POSTS_ACTOR = "apify/instagram-scraper"
COMMENTS_ACTOR = "apidojo/instagram-comments-scraper"

# Documented rate cards (from ig_hashtag_scraper_prompt.md) — used as a fallback
# when Apify doesn't expose actual usageTotalUsd on the run object.
POSTS_RATE_PER_1K = 1.50
COMMENTS_RATE_PER_1K = 0.50

openai_client = OpenAI()


class LocusAssessment(BaseModel):
    language: str        # Traditional Chinese | Simplified Chinese | English | Mixed | Other
    hk_signals: list[str]  # subset of the 5 signal tags below
    verdict: str          # HK-confirmed | ambiguous | likely-not-HK


SYSTEM_PROMPT = """You assess whether a single Instagram post shows explicit Hong Kong
locus signals, for a research task validating hashtag search as an HK-only data source
for a contact-lens market intelligence project.

Rules:
- language: exactly one of "Traditional Chinese" | "Simplified Chinese" | "English" | "Mixed" | "Other"
  based on the caption text provided.
- hk_signals: 0 or more items, only from this exact list, only if actually present in the
  given text: ["location_tag", "bio_mentions_hk", "traditional_script", "hkd_price_mention", "hk_store_mention"]
- verdict: exactly one of "HK-confirmed" | "ambiguous" | "likely-not-HK"
  - "HK-confirmed": at least one STRONG signal present (location_tag, bio_mentions_hk,
    hkd_price_mention, or hk_store_mention).
  - "ambiguous": only traditional_script present (or Chinese text with no other signal) —
    Taiwan, Malaysia, and overseas Chinese communities also use traditional script, so this
    alone does not confirm HK.
  - "likely-not-HK": no HK signals at all, or the content reads as clearly non-HK
    (e.g. simplified script with no HK indicators, or a caption in a different market's context).

Respond ONLY with valid JSON matching the schema. No markdown, no preamble."""


def run_actor(token: str, actor_id: str, run_input: dict, label: str, max_wait_attempts: int = 60):
    """Start an Apify actor run, poll until terminal, return (items, cost_usd, run_detail)."""
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
            log.error(f"[{label}] ended with {final['status']}")
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

    cost = None
    if final:
        cost = final.get("usageTotalUsd") or final.get("usageUsd")

    log.info(f"[{label}] retrieved {len(items)} items | reported cost field: {cost}")
    return items, cost, final


def assess_locus(caption: str, bio: str, location: str) -> LocusAssessment | None:
    text = f"Caption: {caption}\nOwner bio: {bio}\nLocation tag: {location}".strip()
    if len(text) < 10:
        return None
    try:
        resp = openai_client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            response_format=LocusAssessment,
            max_tokens=200,
        )
        return resp.choices[0].message.parsed
    except Exception as e:
        log.warning(f"locus assess failed: {e}")
        return None


def owner_bio(post: dict) -> str:
    owner = post.get("owner")
    if isinstance(owner, dict):
        return owner.get("biography", "") or ""
    return post.get("ownerBio", "") or post.get("ownerFullName", "") or ""


def post_url(post: dict) -> str:
    return post.get("url") or post.get("postUrl") or ""


def main():
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        sys.exit("ERROR: APIFY_TOKEN environment variable is not set.")

    summary = {}
    sample_post_urls: list[str] = []

    for tag in HASHTAGS:
        clean_tag = tag.lstrip("#")
        log.info(f"{'=' * 60}")
        log.info(f"Hashtag: #{tag}")

        run_input = {
            "directUrls": [f"https://www.instagram.com/explore/tags/{clean_tag}/"],
            "resultsType": "posts",
            "resultsLimit": POSTS_PER_HASHTAG,
        }

        try:
            posts, cost, final = run_actor(token, POSTS_ACTOR, run_input, f"posts:{clean_tag}")
        except Exception as e:
            log.error(f"Hashtag #{tag} posts actor failed: {e}")
            summary[tag] = {"error": str(e)}
            continue

        (OUT_DIR / f"raw_{clean_tag}.json").write_text(
            json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        sample = posts[:SAMPLE_SIZE]
        (OUT_DIR / f"sample5_{clean_tag}.json").write_text(
            json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        fields_present = sorted(set(posts[0].keys())) if posts else []
        has_latest_comments = any(bool(p.get("latestComments")) for p in posts) if posts else False

        assessments = []
        for p in posts:
            a = assess_locus(p.get("caption", "") or "", owner_bio(p), p.get("locationName", "") or "")
            if a:
                assessments.append(a)

        confirmed = sum(1 for a in assessments if a.verdict == "HK-confirmed")
        ambiguous = sum(1 for a in assessments if a.verdict == "ambiguous")
        not_hk = sum(1 for a in assessments if a.verdict == "likely-not-HK")
        total = len(assessments)

        summary[tag] = {
            "retrieved": len(posts),
            "fields_present": fields_present,
            "has_latest_comments_bundled": has_latest_comments,
            "cost_usd_reported": cost,
            "cost_usd_estimated": round(len(posts) / 1000 * POSTS_RATE_PER_1K, 4),
            "locus": {
                "HK-confirmed": confirmed,
                "ambiguous": ambiguous,
                "likely-not-HK": not_hk,
                "total_assessed": total,
                "pct_confirmed_or_ambiguous": round((confirmed + ambiguous) / total * 100, 1) if total else None,
            },
        }

        for p in posts[:2]:
            u = post_url(p)
            if u:
                sample_post_urls.append(u)

        time.sleep(3)

    # ── Comments actor test — confirm empirically whether comments require a
    #    separate paid scrape, using up to 3 sample post URLs gathered above ──
    comments_test = {}
    test_urls = sample_post_urls[:3]
    if test_urls:
        log.info(f"{'=' * 60}")
        log.info(f"Testing comments actor on {len(test_urls)} sample post URLs")
        comments_input = {"startUrls": test_urls, "maxItems": 60}
        try:
            comments, cost, final = run_actor(token, COMMENTS_ACTOR, comments_input, "comments-test")
            comments_test = {
                "urls_tested": test_urls,
                "comments_retrieved": len(comments),
                "cost_usd_reported": cost,
                "cost_usd_estimated": round(len(comments) / 1000 * COMMENTS_RATE_PER_1K, 4),
                "sample": comments[:5],
            }
        except Exception as e:
            log.error(f"Comments actor test failed: {e}")
            comments_test = {"error": str(e), "urls_tested": test_urls}
    else:
        comments_test = {"error": "no post URLs retrieved from any hashtag to test comments on"}

    (OUT_DIR / "comments_test.json").write_text(
        json.dumps(comments_test, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"{'=' * 60}")
    log.info(f"Done. Output written to {OUT_DIR}/ (summary.json, comments_test.json, raw_*.json, sample5_*.json)")


if __name__ == "__main__":
    main()
