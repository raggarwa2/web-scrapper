"""
lihkg_scraper.py
=================
LIHKG (討論區) discovery + extraction module for the contact lens
intelligence pipeline. Standalone script, same pattern as
xhs_scraper_v2.py — reads brands/keywords from config.yaml, writes to
its own tables in the shared lensdata.db (WAL mode, so it can run
alongside pipeline_v2.py safely), and is invoked separately rather than
through pipeline_v2.py's discover/extract dispatch. Sustained LIHKG
scraping is more Cloudflare-sensitive than the mostly-API-driven sites
in pipeline_v2.py, so keeping it out of that file's parallel N-worker
extraction pool is deliberate, not a placeholder.

DISCOVERY PASS FINDINGS (2026-07-02) — read before modifying:
  - LIHKG threads are a client-side rendered SPA. Raw requests/curl
    return an empty shell ("請啟用 JavaScript 繼續瀏覧"). Playwright
    is required — there is no shortcut.
  - No login is required to READ threads or search results. Only
    posting/reacting needs an account.
  - The site is behind Cloudflare bot-detection. Direct HTTP calls to
    the underlying API (https://lihkg.com/api_v2/thread/search...)
    are blocked with a Cloudflare challenge page even with a browser
    UA string. Only a real rendered browser (Playwright) that runs
    the challenge JS gets through. Do not attempt to hit api_v2
    directly with requests/httpx — it will not work.
  - LIHKG's own search (https://lihkg.com/search?q=KEYWORD) works
    without login and returns cross-forum results — not just one
    megathread — including complaint threads, comparison threads,
    and "where to buy" threads. This is a better reputation-mining
    entry point than trying to find one canonical brand thread.
  - Search-result thread URLs: the <a href="/thread/..."> tags render
    with EMPTY innerText (confirmed live) — the visible title lives in
    a sibling text node two DOM levels above the anchor. A flat
    page.inner_text("body") dump loses the href/title association
    entirely, so thread_url_guess came back null almost always. Fixed
    by pairing each href with its own card's text via
    page.eval_on_selector_all, walking 2 parentElement levels up from
    the anchor (see _extract_search_result_cards).
  - That same per-card text turned out fully regular (author, age_raw,
    score, then pagination chrome, then title, category, always in
    that order), so search-result fields are now parsed deterministically
    via regex/positional split instead of an LLM call — cheaper and
    fixes a second bug where the LLM, given a prompt describing the
    reverse field order, was shifting score/author/age_raw onto the
    next result's title.
  - Thread page posts remain LLM-parsed (extract_lihkg_thread) — that
    prompt's field order matches the real per-post layout and was
    already producing correct results in discovery testing.
  - Cloudflare retry/backoff in _goto_and_settle() was stress-tested
    2026-07-02 across 3 separate process runs and one sustained
    11-navigation single-browser-session run (5 keywords, discovery +
    thread follow-through) — zero challenges triggered. Note: this
    confirms sustained use at this volume doesn't trigger a block: it
    does NOT confirm the retry path successfully recovers from a real
    challenge, since one never occurred. Monitor in production.

USAGE:
  python lihkg_scraper.py                      # all brands in config.yaml
  python lihkg_scraper.py --brand Acuvue
  python lihkg_scraper.py --brand Acuvue --discover-only
"""

import argparse
import json
import re
import sys
import time
import sqlite3
from typing import List, Literal, Optional
from urllib.parse import quote

import yaml
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeoutError
from pydantic import BaseModel, Field
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = "config.yaml"
DB_PATH     = "output/lensdata.db"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
MODEL = "gpt-4o-mini"

KnownBrand = Literal["Acuvue", "Alcon", "Bausch & Lomb", "CooperVision", "Olens"]

# Copied verbatim from xhs_scraper_v2.py's VALID_THEMES — same product
# domain, shared vocabulary keeps themes comparable across every source
# (XHS, LIHKG, YouTube, Instagram, Facebook) rather than each inventing
# its own taxonomy that means the same thing with different words.
VALID_THEMES = [
    "comfort", "dryness", "colour", "price", "value",
    "packaging", "delivery", "authenticity", "brand_comparison",
    "recommendation", "warning", "vision_clarity",
]
KnownTheme = Literal[
    "comfort", "dryness", "colour", "price", "value",
    "packaging", "delivery", "authenticity", "brand_comparison",
    "recommendation", "warning", "vision_clarity",
]


# ---------------------------------------------------------------------------
# Pydantic schemas for LLM-structured extraction
# ---------------------------------------------------------------------------

class LIHKGSearchResult(BaseModel):
    thread_title: str
    category: str = Field(description="LIHKG sub-forum name, e.g. 吹水台, 美容台")
    author: str
    age_raw: str = Field(description="Relative age as shown, e.g. '11 個月前'")
    score: int = Field(description="Net upvote score shown next to the thread, can be negative")
    thread_url_guess: Optional[str] = Field(
        default=None,
        description="Canonical lihkg.com/thread/{id}/page/{n} URL for this result",
    )


class LIHKGPost(BaseModel):
    post_number: int = Field(description="The #N post number shown, e.g. 1 for #1")
    author: str
    age_raw: str
    text_original: str = Field(description="Original post text, Cantonese/Chinese/English as written, verbatim")
    text_english: str = Field(description="English translation of text_original")
    upvotes: int
    downvotes: int
    mentioned_brands: List[KnownBrand] = Field(
        default_factory=list,
        description="Which of these five brands are mentioned in this post — leave empty if none of them are (do not invent other brand names, e.g. unrelated products/companies that happen to share a keyword like 'Alcon' the brake-caliper maker)",
    )
    sentiment: str = Field(description="One of: positive, negative, neutral, mixed")
    is_purchase_barrier_signal: bool = Field(
        description="True if the post expresses a reason for not buying/switching (price, availability, comfort, trust, etc.)"
    )
    themes: List[KnownTheme] = Field(
        default_factory=list,
        description="1-4 themes from the fixed list that the post's content clearly touches on — leave empty if none clearly apply",
    )


class LIHKGThreadPosts(BaseModel):
    posts: List[LIHKGPost]


# ---------------------------------------------------------------------------
# Playwright helpers
# ---------------------------------------------------------------------------

def _new_page(browser) -> Page:
    page = browser.new_page(user_agent=USER_AGENT)
    return page


def _looks_like_cloudflare_challenge(text: str) -> bool:
    markers = ["Just a moment", "Attention Required", "Checking your browser", "cf-error-details"]
    return any(m in text for m in markers)


def _goto_and_settle(page: Page, url: str, settle_seconds: float = 3.0, retries: int = 2) -> str:
    """
    Navigate and wait for the SPA to hydrate. Returns the rendered visible
    body text. Retries once if a Cloudflare interstitial is detected —
    the challenge usually clears itself within a few seconds on a real
    browser context; if it doesn't clear after retries, raise so the
    caller can log/skip rather than silently returning junk.
    """
    last_text = ""
    for attempt in range(retries + 1):
        page.goto(url, wait_until="commit", timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeoutError:
            pass
        time.sleep(settle_seconds)
        last_text = page.inner_text("body")
        if not _looks_like_cloudflare_challenge(last_text):
            return last_text
        time.sleep(5 * (attempt + 1))  # back off before retry
    raise RuntimeError(f"Cloudflare challenge did not clear for {url} after {retries + 1} attempts")


# ---------------------------------------------------------------------------
# Discovery: LIHKG search -> list of threads mentioning a keyword
# ---------------------------------------------------------------------------

def _extract_search_result_cards(page: Page) -> List[dict]:
    """
    Pair each search-result href with its own card's text, read straight
    from the DOM. The <a href="/thread/..."> tags render with EMPTY
    innerText — the visible title lives in a sibling text node two DOM
    levels above the anchor — so a flat page.inner_text("body") dump
    loses the href/title association entirely. Walking up 2
    parentElement levels from each thread anchor gives back
    (href, card_text) pairs with no ambiguity.
    """
    return page.eval_on_selector_all(
        "a[href*='/thread/']",
        """els => els.map(e => {
            let card = e;
            for (let i = 0; i < 2 && card.parentElement; i++) card = card.parentElement;
            return { href: e.href, card_text: card.innerText };
        })""",
    )


_AGE_RE = re.compile(r"^\d+\s*(?:年|個月|星期|日|小時|分鐘)前$")


def _parse_card_fields(card_text: str) -> Optional[dict]:
    """
    Deterministically parse a card's fields from its raw innerText.

    Every card follows this exact line order, with a variable amount of
    pagination chrome ("1 頁", "選擇頁數" etc.) sandwiched in between —
    so this anchors on the first 3 lines (author, age_raw, score) and
    last 2 lines (title, category) rather than trying to match the
    chrome itself:
        author
        age_raw
        score
        ...pagination chrome (0+ lines)...
        title
        category
    """
    lines = [l for l in card_text.split("\n") if l.strip()]
    if len(lines) < 5:
        return None
    author, age_raw, score_raw = lines[0].strip(), lines[1].strip(), lines[2].strip()
    title, category = lines[-2].strip(), lines[-1].strip()
    if not _AGE_RE.match(age_raw):
        return None
    try:
        score = int(score_raw)
    except ValueError:
        return None
    return {
        "author": author,
        "age_raw": age_raw,
        "score": score,
        "title": title,
        "category": category,
    }


def discover_lihkg_threads(
    page: Page,
    client: OpenAI,
    keyword: str,
    sort: str = "score",
) -> List[LIHKGSearchResult]:
    """
    Search LIHKG for `keyword` and return structured results parsed
    deterministically from the DOM (see _extract_search_result_cards /
    _parse_card_fields) — no LLM call needed for this step; the card
    structure is fully regular once paired with its own href.

    sort options observed on-site: score (最相關 / most relevant),
    new (新至舊), reply (回覆新至舊). We default to score/relevance.

    `client` is accepted for CLI/interface consistency with
    extract_lihkg_thread (which does need the LLM) but is unused here.
    """
    url = f"https://lihkg.com/search?q={quote(keyword)}&sort={sort}"
    _goto_and_settle(page, url)  # also raises if a Cloudflare challenge won't clear
    cards = _extract_search_result_cards(page)

    results: List[LIHKGSearchResult] = []
    for card in cards:
        fields = _parse_card_fields(card["card_text"])
        if fields is None:
            continue
        results.append(
            LIHKGSearchResult(
                thread_title=fields["title"],
                category=fields["category"],
                author=fields["author"],
                age_raw=fields["age_raw"],
                score=fields["score"],
                thread_url_guess=card["href"],
            )
        )
    return results


# ---------------------------------------------------------------------------
# Extraction: thread page -> list of posts
# ---------------------------------------------------------------------------

def extract_lihkg_thread(
    page: Page,
    client: OpenAI,
    thread_url: str,
    max_pages: int = 1,
    max_chars_per_page: int = 8000,
) -> List[LIHKGPost]:
    """
    Visit a LIHKG thread (optionally across multiple pages) and return
    structured posts via LLM parsing.

    thread_url should be the canonical https://lihkg.com/thread/{id}/page/{n}
    form. If a short lih.kg/{id} link is all you have, resolve it first
    (Playwright will follow the redirect on goto()).
    """
    all_posts: List[LIHKGPost] = []
    base_url = re.sub(r"/page/\d+$", "", thread_url.rstrip("/"))

    for page_num in range(1, max_pages + 1):
        url = f"{base_url}/page/{page_num}"
        try:
            body_text = _goto_and_settle(page, url)
        except RuntimeError:
            break  # Cloudflare didn't clear — stop rather than feed junk to the LLM

        if "請啟用 JavaScript" in body_text or len(body_text) < 200:
            break  # empty shell — likely last page or thread removed

        prompt = f"""You are parsing rendered plain-text output of a single page of a
LIHKG (Hong Kong forum) discussion thread. The text contains site
navigation chrome and repeated pagination boilerplate ("選擇頁數", "1
頁" etc.) — ignore that.

Each real post follows this pattern: a "#N" post number marker, then
the author handle, then a "•" bullet, then a relative timestamp (e.g.
"2 年前"), then the post's text content (can be multiple lines), then
two numbers in sequence representing upvotes and downvotes.

For each post, translate the original Cantonese/Chinese text to English,
flag any of these five brands if mentioned: Acuvue, Alcon, Bausch & Lomb,
CooperVision, Olens, note whether the post expresses a reason for
not buying/switching brands (price, comfort, trust, availability, habit,
etc — a "purchase barrier" signal), and pick 1-4 themes from the fixed
list ({', '.join(VALID_THEMES)}) that the post's content clearly touches
on — leave empty if none clearly apply.

RENDERED TEXT (thread page {page_num}):
{body_text[:max_chars_per_page]}
"""
        completion = client.beta.chat.completions.parse(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format=LIHKGThreadPosts,
        )
        parsed = completion.choices[0].message.parsed
        if parsed and parsed.posts:
            all_posts.extend(parsed.posts)
        else:
            break  # nothing extracted — likely ran past the last real page

    return all_posts


# ---------------------------------------------------------------------------
# SQLite schema — own tables, own connection (WAL), same pattern as
# xhs_scraper_v2.py's xhs_posts/xhs_comments. Not part of pipeline_v2.py's
# central SCHEMA or single-writer queue.
# ---------------------------------------------------------------------------

LIHKG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lihkg_search_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    thread_title TEXT NOT NULL,
    category TEXT,
    author TEXT,
    age_raw TEXT,
    score INTEGER,
    thread_url TEXT,
    discovered_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lihkg_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_url TEXT NOT NULL,
    post_number INTEGER,
    author TEXT,
    age_raw TEXT,
    text_original TEXT,
    text_english TEXT,
    upvotes INTEGER,
    downvotes INTEGER,
    mentioned_brands TEXT,      -- comma-joined
    sentiment TEXT,
    is_purchase_barrier_signal INTEGER,  -- 0/1
    themes TEXT,                -- JSON list, same convention as xhs_posts.themes
    extracted_at TEXT DEFAULT (datetime('now')),
    UNIQUE(thread_url, post_number)
);
"""

# Columns added after the initial release — migrated in on open for dbs
# created before this field existed (SQLite has no ADD COLUMN IF NOT EXISTS),
# same pattern as youtube_scraper.py/instagram_scraper.py's _MIGRATION_COLUMNS.
_MIGRATION_COLUMNS = {
    "lihkg_posts": {
        "themes": "TEXT",
    },
}


def init_lihkg_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(LIHKG_SCHEMA_SQL)
    for table, columns in _MIGRATION_COLUMNS.items():
        existing_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col, col_type in columns.items():
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
    conn.commit()


def write_search_results(conn: sqlite3.Connection, keyword: str, results: List[LIHKGSearchResult]) -> None:
    conn.executemany(
        """INSERT INTO lihkg_search_results
           (keyword, thread_title, category, author, age_raw, score, thread_url)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(keyword, r.thread_title, r.category, r.author, r.age_raw, r.score, r.thread_url_guess) for r in results],
    )
    conn.commit()


def write_thread_posts(conn: sqlite3.Connection, thread_url: str, posts: List[LIHKGPost]) -> None:
    conn.executemany(
        """INSERT OR IGNORE INTO lihkg_posts
           (thread_url, post_number, author, age_raw, text_original, text_english,
            upvotes, downvotes, mentioned_brands, sentiment, is_purchase_barrier_signal, themes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                thread_url, p.post_number, p.author, p.age_raw, p.text_original, p.text_english,
                p.upvotes, p.downvotes, ",".join(p.mentioned_brands), p.sentiment,
                int(p.is_purchase_barrier_signal), json.dumps(p.themes, ensure_ascii=False),
            )
            for p in posts
        ],
    )
    conn.commit()


def _llm_themes_batch(texts_en: List[str], client: OpenAI) -> List[List[str]]:
    """Standalone batch themes-only classifier for backfilling posts that
    predate the themes field — reclassifying via extract_lihkg_thread()
    would mean re-visiting every thread page with Playwright, which is
    unnecessary just to add themes to text already stored. Same
    index-tagged-response idiom as every other batch LLM helper in this
    codebase (see facebook_reviews_merge.py's docstring for why)."""
    if not texts_en:
        return []
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts_en))
    theme_list = ", ".join(VALID_THEMES)
    prompt = f"""Analyse these {len(texts_en)} LIHKG (Hong Kong forum) posts about contact lenses.
For each post, return an object with:
- "i": the post's number as shown below (integer)
- "themes": 1-4 items chosen from {theme_list} — leave empty if none clearly apply

Return ONLY a JSON array of objects, one per post, no other text.

Posts:
{numbered}"""
    by_index: dict = {}
    try:
        resp = client.chat.completions.create(
            model=MODEL,
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
                    by_index[idx] = [t for t in (p.get("themes") or []) if t in VALID_THEMES]
        if len(by_index) != len(texts_en):
            print(f"  [LLM] themes batch mismatch: expected {len(texts_en)}, got {len(by_index)} indexed")
    except Exception as e:
        print(f"  [LLM] themes batch failed: {e}")

    return [by_index.get(i + 1, []) for i in range(len(texts_en))]


def backfill_themes(db_path: str = DB_PATH) -> int:
    """Backfill themes for lihkg_posts saved before that field existed
    (themes IS NULL). Safe to re-run — only touches unclassified rows."""
    client = OpenAI()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_lihkg_schema(conn)  # ensures the themes column exists on dbs created before this field did
    rows = conn.execute("SELECT id, text_english FROM lihkg_posts WHERE themes IS NULL").fetchall()
    print(f"[THEMES] {len(rows)} posts missing themes")

    classified = 0
    for start in range(0, len(rows), 20):
        batch = rows[start:start + 20]
        themes_batch = _llm_themes_batch([r["text_english"] or "" for r in batch], client)
        for row, themes in zip(batch, themes_batch):
            conn.execute(
                "UPDATE lihkg_posts SET themes = ? WHERE id = ?",
                (json.dumps(themes, ensure_ascii=False), row["id"]),
            )
            classified += 1
        conn.commit()
        print(f"[THEMES] {classified}/{len(rows)} done")

    conn.close()
    print(f"[THEMES] Done — {classified} posts classified")
    return classified


# ---------------------------------------------------------------------------
# Main — config-driven, same shape as xhs_scraper_v2.py's run()
# ---------------------------------------------------------------------------

def run(
    brand_filter: Optional[str],
    discover_only: bool,
    sort_override: Optional[str],
    max_threads_override: Optional[int],
    max_pages_override: Optional[int],
) -> None:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        config = yaml.safe_load(fh)

    hk = config["markets"]["HK"]
    lihkg_cfg = hk.get("sites", {}).get("lihkg", {})
    if not lihkg_cfg.get("enabled", True):
        sys.exit("lihkg site is disabled in config.yaml (markets.HK.sites.lihkg.enabled)")

    sort        = sort_override or lihkg_cfg.get("sort", "score")
    max_threads = max_threads_override if max_threads_override is not None else lihkg_cfg.get("max_threads_per_keyword", 5)
    max_pages   = max_pages_override if max_pages_override is not None else lihkg_cfg.get("max_pages_per_thread", 2)

    brands = hk["brands"]
    if brand_filter:
        brands = [b for b in brands if b["name"].lower() == brand_filter.lower()]
        if not brands:
            sys.exit(f"Brand '{brand_filter}' not found in config.yaml")

    client = OpenAI()  # expects OPENAI_API_KEY in env
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA synchronous=NORMAL")
    init_lihkg_schema(conn)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = _new_page(browser)

        for brand in brands:
            name = brand["name"]
            keywords = brand.get("lihkg_keywords", [name])
            print(f"{'─'*60}")
            print(f"Brand: {name}  |  keywords: {keywords}")

            for keyword in keywords:
                print(f"Discovering threads for '{keyword}'...")
                try:
                    results = discover_lihkg_threads(page, client, keyword, sort=sort)
                except RuntimeError as e:
                    print(f"  SKIPPED — {e}")
                    continue

                write_search_results(conn, keyword, results)
                print(f"  Found {len(results)} search results")
                for r in results:
                    print(f"    [{r.score:>4}] {r.category:<8} {r.thread_title}")

                if discover_only:
                    continue

                targets = [r for r in results if r.thread_url_guess][:max_threads]
                for r in targets:
                    print(f"  Extracting: {r.thread_title} ({r.thread_url_guess})")
                    try:
                        posts = extract_lihkg_thread(page, client, r.thread_url_guess, max_pages=max_pages)
                    except RuntimeError as e:
                        print(f"    SKIPPED — {e}")
                        continue
                    print(f"    -> {len(posts)} posts extracted")
                    write_thread_posts(conn, r.thread_url_guess, posts)

        browser.close()

    conn.close()
    print(f"{'─'*60}")
    print(f"All done  |  DB: {DB_PATH}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Scrape LIHKG search results + threads and store in lensdata.db"
    )
    ap.add_argument(
        "--brand",
        type=str,
        default=None,
        help="Single brand name to scrape (default: all brands with lihkg_keywords in config.yaml)",
    )
    ap.add_argument("--sort", default=None, choices=["score", "new", "reply"], help="Overrides config.yaml")
    ap.add_argument("--max-threads", type=int, default=None, help="Threads to extract per keyword (overrides config.yaml)")
    ap.add_argument("--max-pages", type=int, default=None, help="Pages per thread to extract (overrides config.yaml)")
    ap.add_argument("--discover-only", action="store_true")
    ap.add_argument(
        "--backfill-themes", action="store_true",
        help="Backfill themes for already-saved posts missing them, then exit",
    )
    args = ap.parse_args()

    if args.backfill_themes:
        backfill_themes()
        sys.exit(0)

    run(
        brand_filter=args.brand,
        discover_only=args.discover_only,
        sort_override=args.sort,
        max_threads_override=args.max_threads,
        max_pages_override=args.max_pages,
    )
