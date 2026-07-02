"""
Demand Signals data layer — Google Trends search interest vs. scraped
review / XHS activity, for the "Demand Signals" tab in app.py.

Read-only: only ever SELECTs from lensdata.db, and only reads CSV/YAML
files under the Google Trends export folder. Never writes to the db.
"""

import csv
import os
import sqlite3
from glob import glob

import pandas as pd
import streamlit as st
import yaml

DEFAULT_TRENDS_DIR = os.path.join("Research", "Google Trend")
DEFAULT_CAMPAIGNS_PATH = "campaigns.yaml"

# Google Trends' "Alcon" search term is contaminated by Alcon Components, a
# UK automotive brake-caliper brand — its relatedEntities.csv top terms
# include Brembo, Piston, Brake, RC6, and its weekly timeline has many
# false-zero weeks. Do not chart Alcon's trend data as-is. The same
# generic-word risk likely applies to Olens and possibly Cooper/
# CooperVision. Treat every brand as unreliable by default; only add a
# brand to this set once its relatedEntities.csv (ideally a category-
# filtered pull — Shopping/Health — since "All categories" pulls are more
# prone to contamination) has been manually reviewed and shows >90% of
# TOP terms as lens/eye-care relevant. Acuvue was checked this way on
# 2026-07-02 (its "All categories" relatedEntities.csv is already clean).
RELIABLE_BRANDS = {"Acuvue"}

DIVERGENCE_SEARCH_SPIKE_PCT = 20.0
DIVERGENCE_REVIEW_DROP_PCT = -15.0


def classify_trend_reliability(brand: str) -> bool:
    """True if `brand`'s Google Trends search-index data has been manually
    verified clean enough to chart. See RELIABLE_BRANDS above for the
    contamination issue and how to add a brand once it's been checked."""
    return brand in RELIABLE_BRANDS


def _find_file(trends_dir: str, brand: str, suffix: str):
    """Case-insensitive exact-basename match for '{brand}{suffix}' inside
    trends_dir, so category-filtered variants (e.g. an
    '..._Health_english_name.csv' export) aren't picked up by accident."""
    target = f"{brand}{suffix}".lower()
    for path in glob(os.path.join(trends_dir, f"*{suffix}")):
        if os.path.basename(path).lower() == target:
            return path
    return None


@st.cache_data(show_spinner=False)
def load_trends_timeline(brand: str, trends_dir: str = DEFAULT_TRENDS_DIR) -> pd.DataFrame:
    """Weekly Google Trends search index for `brand`, from its own single-
    term 'All categories' export ({Brand}multiTimeline.csv). Self-scaled
    0-100 by Google Trends, so it's comparable week-to-week for this brand
    only — not directly comparable to another brand's index."""
    path = _find_file(trends_dir, brand, "multiTimeline.csv")
    if path is None:
        return pd.DataFrame(columns=["week", "search_index"])
    df = pd.read_csv(path, skiprows=2)
    df.columns = ["week", "search_index"]
    df["week"] = pd.to_datetime(df["week"], errors="coerce")
    df["search_index"] = pd.to_numeric(df["search_index"], errors="coerce")
    return df.dropna(subset=["week"]).reset_index(drop=True)


@st.cache_data(show_spinner=False)
def load_trends_entities(brand: str, trends_dir: str = DEFAULT_TRENDS_DIR) -> dict:
    """Parsed TOP / RISING related-search terms for `brand`. Used only for
    data-quality flagging (see classify_trend_reliability) — never charted."""
    path = _find_file(trends_dir, brand, "relatedEntities.csv")
    out = {"top": [], "rising": []}
    if path is None:
        return out

    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))

    section = None
    for row in rows:
        if not row:
            continue
        first = row[0].strip()
        if first == "TOP":
            section = "top"
            continue
        if first == "RISING":
            section = "rising"
            continue
        if section is None or len(row) < 2:
            continue
        out[section].append({"term": row[0].strip(), "value": row[-1].strip()})
    return out


@st.cache_data(show_spinner=False)
def aggregate_monthly_search_index(brand: str, trends_dir: str = DEFAULT_TRENDS_DIR) -> pd.DataFrame:
    """Monthly average of the weekly search index, keyed by calendar month
    of the week-start date. Drops the leading/trailing partial month (fewer
    than 3 weeks in that month) so a 1-week average doesn't distort the
    trend."""
    weekly = load_trends_timeline(brand, trends_dir)
    if weekly.empty:
        return pd.DataFrame(columns=["month", "search_index"])
    weekly = weekly.copy()
    weekly["month"] = weekly["week"].dt.strftime("%Y-%m")
    monthly = (
        weekly.groupby("month")
        .agg(search_index=("search_index", "mean"), week_count=("search_index", "count"))
        .reset_index()
    )
    monthly = monthly[monthly["week_count"] >= 3].drop(columns="week_count")
    return monthly.sort_values("month").reset_index(drop=True)


@st.cache_data(show_spinner=False)
def get_monthly_review_counts(brand: str, db_path: str) -> pd.DataFrame:
    """Monthly review count for `brand` from the reviews table (exact
    match — reviews.brand is already a normalized value, e.g. "Acuvue")."""
    conn = sqlite3.connect(db_path)
    try:
        return pd.read_sql_query(
            """
            SELECT substr(review_date, 1, 7) AS month, COUNT(*) AS review_count
            FROM reviews
            WHERE brand = ?
            GROUP BY month
            ORDER BY month
            """,
            conn,
            params=(brand,),
        )
    finally:
        conn.close()


@st.cache_data(show_spinner=False)
def get_monthly_xhs_counts(brand: str, db_path: str) -> pd.DataFrame:
    """Monthly XHS post count for `brand`. Matches brand_keyword OR
    brand_mentioned, case-insensitive partial match (so "Acuvue" matches
    "Acuvue Oasys"), de-duplicated by post_id so a post matching both
    columns isn't counted twice."""
    conn = sqlite3.connect(db_path)
    try:
        like = f"%{brand}%"
        return pd.read_sql_query(
            """
            SELECT strftime('%Y-%m', datetime(publish_date, 'unixepoch')) AS month,
                   COUNT(DISTINCT post_id) AS xhs_count
            FROM xhs_posts
            WHERE brand_keyword LIKE ? COLLATE NOCASE
               OR brand_mentioned LIKE ? COLLATE NOCASE
            GROUP BY month
            ORDER BY month
            """,
            conn,
            params=(like, like),
        )
    finally:
        conn.close()


@st.cache_data(show_spinner=False)
def load_campaigns(path: str = DEFAULT_CAMPAIGNS_PATH) -> list:
    """Campaign windows to shade on the Demand Signals chart — see
    campaigns.yaml. Schema per entry: brand, start_date, end_date, label,
    source_url."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def get_campaigns_for_brand(brand: str, path: str = DEFAULT_CAMPAIGNS_PATH) -> list:
    return [c for c in load_campaigns(path) if c.get("brand") == brand]


def find_divergence_alerts(
    monthly_search: pd.DataFrame,
    monthly_reviews: pd.DataFrame,
    search_spike_pct: float = DIVERGENCE_SEARCH_SPIKE_PCT,
    review_drop_pct: float = DIVERGENCE_REVIEW_DROP_PCT,
) -> list:
    """Plain-language flags for months where search index and review count
    move in opposite directions beyond the given thresholds."""
    merged = pd.merge(monthly_search, monthly_reviews, on="month", how="inner").sort_values("month")
    merged = merged.reset_index(drop=True)
    if len(merged) < 2:
        return []

    alerts = []
    for i in range(1, len(merged)):
        prev, curr = merged.iloc[i - 1], merged.iloc[i]
        if not prev["search_index"] or not prev["review_count"]:
            continue
        search_chg = (curr["search_index"] - prev["search_index"]) / prev["search_index"] * 100
        review_chg = (curr["review_count"] - prev["review_count"]) / prev["review_count"] * 100
        month_label = pd.to_datetime(curr["month"] + "-01").strftime("%B %Y")

        if search_chg >= search_spike_pct and review_chg <= review_drop_pct:
            alerts.append({
                "month": curr["month"],
                "text": (
                    f"{month_label}: search interest rose sharply ({search_chg:+.0f}%) "
                    f"while review volume fell ({review_chg:+.0f}%) — demand may be "
                    "converting through a channel outside current scraper coverage "
                    "(e.g. in-store/optometrist redemption)."
                ),
            })
        elif search_chg <= -search_spike_pct and review_chg >= -review_drop_pct and review_chg > 0:
            alerts.append({
                "month": curr["month"],
                "text": (
                    f"{month_label}: search interest fell sharply ({search_chg:+.0f}%) "
                    f"while review volume rose ({review_chg:+.0f}%) — organic reviews may "
                    "be lagging behind a search cooldown, or demand is shifting to a "
                    "different search term."
                ),
            })
    return alerts
