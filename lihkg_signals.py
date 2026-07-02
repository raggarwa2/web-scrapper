"""
Customer Signals data layer — LIHKG forum discussion sentiment and
purchase-barrier signals, for the "Customer Signals (LIHKG)" tab in app.py.

Read-only: only ever SELECTs from lensdata.db. Never writes to the db.

LIHKG posts are unsolicited forum chatter, not product reviews — no
star rating, no absolute date (age_raw is relative text like "11 個月前",
so no time-series charting is possible from this source yet). That's
why this is framed as "signals" rather than folded into the
review/XHS "feedback" tabs, which are both dated and product-scoped.
"""

import sqlite3

import pandas as pd
import streamlit as st

# Bare brand-keyword search on LIHKG can pick up unrelated threads that
# happen to share a word — same root problem demand_signals.py already
# documents for Google Trends' "Alcon" contamination. Confirmed live
# 2026-07-02:
#   - "Alcon" also matches Alcon Components, a UK brake-caliper maker
#     (thread 2362299, "Alcon 定AP racing?", category 汽車台/cars)
#   - "歐彩" (Olens) substring-matched into an unrelated football thread
#     (歐聯/光彩, category 體育台/sports)
# Both confirmed threads carried zero genuine brand-post mentions once
# extracted, so this set flags (not silently drops) posts from
# categories where a contact-lens discussion is very unlikely.
KNOWN_COLLISION_CATEGORIES = {"汽車台", "體育台"}


@st.cache_data(show_spinner=False)
def load_lihkg_posts(db_path: str, mtime: float) -> pd.DataFrame:
    """All lihkg_posts, one row per post, joined with the search keyword
    and LIHKG category of their thread. `mentioned_brands` is stored
    comma-joined — this adds mentioned_brands_list so callers can
    .explode() it the same way app.py already explodes XHS's
    themes_list. Returns an empty DataFrame if the tables don't exist
    yet (e.g. lihkg_scraper.py hasn't been run)."""
    conn = sqlite3.connect(db_path)
    try:
        posts = pd.read_sql_query("SELECT * FROM lihkg_posts", conn)
        results = pd.read_sql_query(
            "SELECT thread_url, keyword, category, thread_title FROM lihkg_search_results", conn
        )
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()

    if posts.empty:
        return posts

    # A thread can be found by more than one keyword search; keep the
    # first keyword/category label so each post appears once.
    results = results.drop_duplicates(subset="thread_url", keep="first")
    merged = posts.merge(results, on="thread_url", how="left")
    merged["mentioned_brands_list"] = merged["mentioned_brands"].apply(
        lambda s: [b for b in s.split(",") if b] if s else []
    )
    merged["likely_collision"] = merged["category"].isin(KNOWN_COLLISION_CATEGORIES)
    return merged


def brand_exploded(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (post, brand) — a post mentioning two brands counts
    toward both. Drops posts with no recognized brand mention."""
    if df.empty:
        return df
    exploded = df.explode("mentioned_brands_list")
    return exploded[exploded["mentioned_brands_list"].notna() & (exploded["mentioned_brands_list"] != "")]


def purchase_barrier_rate(exploded_df: pd.DataFrame) -> pd.DataFrame:
    """% of a brand's posts flagged as a purchase-barrier signal
    (a stated reason for not buying/switching — price, comfort, trust,
    availability, etc). Expects the output of brand_exploded()."""
    if exploded_df.empty:
        return pd.DataFrame(columns=["mentioned_brands_list", "post_count", "barrier_count", "barrier_rate"])
    g = exploded_df.groupby("mentioned_brands_list").agg(
        post_count=("is_purchase_barrier_signal", "count"),
        barrier_count=("is_purchase_barrier_signal", "sum"),
    )
    g["barrier_rate"] = (g["barrier_count"] / g["post_count"] * 100).round(1)
    return g.reset_index().rename(columns={"mentioned_brands_list": "brand"})
