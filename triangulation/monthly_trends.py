# ============================================================
# Monthly Trends — month x brand aggregate grid of review + XHS + YouTube +
# Instagram activity and sentiment, with trailing-3-month anomaly flags.
# ============================================================
# This is a prerequisite for future event-study / lead-lag analysis: the
# persisted monthly_trends.csv grid (every month x every brand, zero/NaN
# filled for continuity) is the actual deliverable, the charts and anomaly
# flags in the dashboard are secondary. Anomaly flags are directional only
# — a 3-month trailing window is a small, noisy sample, not a statistical
# test — and are NOT explained here, just surfaced as candidates worth a
# closer look later.
#
# Usage:
#   python triangulation/monthly_trends.py
#
# Scope: Hong Kong reviews + XHS posts + YouTube comments + Instagram
# comments + Facebook reviews (LIHKG excluded — it has no real absolute post
# date, only a relative "N months ago" string, so it can't be placed on a
# monthly timeline; YouTube/Instagram comments and Facebook reviews DO carry
# a real date, so unlike LIHKG they CAN be time-windowed here). YouTube
# comments are on-topic + brand-relevant filtered the same way
# youtube_signals.py's render() filters them — see load_youtube_dated().
# Instagram comments are filtered the same way instagram_signals.py's
# load_hk_dashboard_data() + on_topic_comments() filter them together — see
# load_instagram_dated(). Facebook reviews carry an absolute ISO8601
# review_date on every row (verified full 2014-2026 range) — see
# load_facebook_dated(). Plots the FULL available date range rather than
# restricting to a fixed window — early months are sparse/scattered and
# will read as thin on the charts, which is honest about data coverage.
# Instagram and Facebook, like YouTube, are deliberately NOT in
# ANOMALY_METRICS — volume per brand-month is still thin for any of them.
# ============================================================

import sqlite3
from itertools import product as _iproduct
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "output" / "lensdata.db"
DEFAULT_YOUTUBE_DB = ROOT / "output" / "youtube_data.db"
DEFAULT_INSTAGRAM_DB = ROOT / "output" / "instagram_data.db"
DEFAULT_FACEBOOK_DB = ROOT / "output" / "facebook_data.db"
DEFAULT_OUT = ROOT / "output" / "triangulation"

SENTIMENT_SCORE = {"positive": 1, "neutral": 0, "negative": -1}
TRAILING_WINDOW = 3  # months
ANOMALY_METRICS = ["review_count", "avg_xhs_sentiment"]


# ============================================================
# Loading (HK only)
# ============================================================

def load_reviews_hk_dated(db_path: Path) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        "SELECT brand, rating, review_date FROM reviews "
        "WHERE market = 'HK' AND rating BETWEEN 1 AND 5 AND review_date IS NOT NULL",
        conn,
    )
    conn.close()
    df["month"] = pd.to_datetime(df["review_date"], errors="coerce").dt.to_period("M")
    return df[df["month"].notna()]


def load_xhs_dated(db_path: Path, canonical_brands: list) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        "SELECT brand_mentioned AS brand, sentiment, publish_date FROM xhs_posts "
        "WHERE publish_date IS NOT NULL AND sentiment != 'warning'",
        conn,
    )
    conn.close()
    lookup = {b.lower(): b for b in canonical_brands}
    df["brand"] = df["brand"].map(lambda b: lookup.get(str(b).strip().lower()))
    df = df[df["brand"].notna()]
    df["month"] = pd.to_datetime(
        pd.to_numeric(df["publish_date"], errors="coerce"), unit="s", errors="coerce"
    ).dt.to_period("M")
    return df[df["month"].notna()]


def load_youtube_dated(youtube_db_path: Path, canonical_brands: list) -> pd.DataFrame:
    """On-topic HK comments from brand-relevant videos only (same filter as
    youtube_signals.py's render()) — brand is already canonical (copied
    down from the video at scrape time), no normalization needed. Returns
    empty (not an error) if the youtube db doesn't exist yet."""
    if not Path(youtube_db_path).exists():
        return pd.DataFrame(columns=["brand", "sentiment", "month"])
    conn = sqlite3.connect(youtube_db_path)
    df = pd.read_sql_query(
        "SELECT c.brand, c.sentiment, c.published_at FROM youtube_comments c "
        "JOIN youtube_videos v ON v.video_id = c.video_id "
        "WHERE c.market = 'HK' AND v.brand_relevant = 1 AND c.is_lens_relevant = 1 "
        "AND c.sentiment IN ('positive', 'negative', 'neutral')",
        conn,
    )
    conn.close()
    df = df[df["brand"].isin(canonical_brands)]
    parsed = pd.to_datetime(df["published_at"], errors="coerce", utc=True).dt.tz_localize(None)
    df["month"] = parsed.dt.to_period("M")
    return df[df["month"].notna()]


def load_instagram_dated(instagram_db_path: Path, canonical_brands: list) -> pd.DataFrame:
    """On-topic HK comments from brand-relevant, lens-relevant posts only
    (same three-way filter as instagram_signals.py's load_hk_dashboard_data()
    + on_topic_comments() combined) — brand is already canonical (copied
    down from the post at scrape time), no normalization needed. Returns
    empty (not an error) if the instagram db doesn't exist yet."""
    if not Path(instagram_db_path).exists():
        return pd.DataFrame(columns=["brand", "sentiment", "month"])
    conn = sqlite3.connect(instagram_db_path)
    df = pd.read_sql_query(
        "SELECT c.brand, c.sentiment, c.published_at FROM ig_comments c "
        "JOIN ig_posts p ON p.post_id = c.post_id "
        "WHERE c.market = 'HK' AND p.brand_relevant = 1 AND p.is_lens_relevant = 1 "
        "AND c.is_lens_relevant = 1 AND c.sentiment IN ('positive', 'negative', 'neutral')",
        conn,
    )
    conn.close()
    df = df[df["brand"].isin(canonical_brands)]
    parsed = pd.to_datetime(df["published_at"], errors="coerce", utc=True).dt.tz_localize(None)
    df["month"] = parsed.dt.to_period("M")
    return df[df["month"].notna()]


def load_facebook_dated(facebook_db_path: Path, canonical_brands: list) -> pd.DataFrame:
    """Dated HK Facebook page reviews, one row per (review, brand) —
    mentioned_brands is comma-joined and multi-valued (any of the 5
    brands, or none), unlike YouTube/Instagram's single pre-scoped brand
    column, so this explodes it the same way facebook_signals.py's
    brand_exploded() does. sentiment is already rule-derived (positive/
    negative/neutral only, from is_recommended) — no dirty-label
    filtering needed, unlike xhs/lihkg. Returns empty (not an error) if
    the facebook db doesn't exist yet."""
    if not Path(facebook_db_path).exists():
        return pd.DataFrame(columns=["brand", "sentiment", "month"])
    conn = sqlite3.connect(facebook_db_path)
    df = pd.read_sql_query(
        "SELECT mentioned_brands, sentiment, review_date FROM fb_reviews "
        "WHERE review_date IS NOT NULL AND mentioned_brands != ''",
        conn,
    )
    conn.close()
    if df.empty:
        return pd.DataFrame(columns=["brand", "sentiment", "month"])
    df["brand"] = df["mentioned_brands"].str.split(",")
    df = df.explode("brand")
    df = df[df["brand"].isin(canonical_brands)]
    parsed = pd.to_datetime(df["review_date"], errors="coerce", utc=True).dt.tz_localize(None)
    df["month"] = parsed.dt.to_period("M")
    return df[df["month"].notna()][["brand", "sentiment", "month"]]


# ============================================================
# Month x brand grid
# ============================================================

def build_month_brand_grid(
    reviews: pd.DataFrame, xhs: pd.DataFrame, youtube: pd.DataFrame,
    instagram: pd.DataFrame, facebook: pd.DataFrame, brands: list,
) -> pd.DataFrame:
    month_bounds = [
        df["month"] for df in (reviews, xhs, youtube, instagram, facebook)
        if not df.empty and df["month"].notna().any()
    ]
    all_months = pd.period_range(
        min(m.min() for m in month_bounds),
        max(m.max() for m in month_bounds),
        freq="M",
    )

    rev_agg = reviews.groupby(["month", "brand"]).agg(
        review_count=("rating", "count"), avg_rating=("rating", "mean")
    )
    xhs["sentiment_score"] = xhs["sentiment"].map(SENTIMENT_SCORE)
    xhs_agg = xhs.groupby(["month", "brand"]).agg(
        xhs_post_count=("sentiment", "count"), avg_xhs_sentiment=("sentiment_score", "mean")
    )
    youtube = youtube.copy()
    youtube["sentiment_score"] = youtube["sentiment"].map(SENTIMENT_SCORE)
    youtube_agg = youtube.groupby(["month", "brand"]).agg(
        youtube_comment_count=("sentiment", "count"), avg_youtube_sentiment=("sentiment_score", "mean")
    )
    instagram = instagram.copy()
    instagram["sentiment_score"] = instagram["sentiment"].map(SENTIMENT_SCORE)
    instagram_agg = instagram.groupby(["month", "brand"]).agg(
        instagram_comment_count=("sentiment", "count"), avg_instagram_sentiment=("sentiment_score", "mean")
    )
    facebook = facebook.copy()
    facebook["sentiment_score"] = facebook["sentiment"].map(SENTIMENT_SCORE)
    facebook_agg = facebook.groupby(["month", "brand"]).agg(
        facebook_review_count=("sentiment", "count"), avg_facebook_sentiment=("sentiment_score", "mean")
    )

    rows = []
    for month, brand in _iproduct(all_months, brands):
        review_count = int(rev_agg["review_count"].get((month, brand), 0))
        avg_rating = rev_agg["avg_rating"].get((month, brand), float("nan"))
        xhs_post_count = int(xhs_agg["xhs_post_count"].get((month, brand), 0))
        avg_xhs_sentiment = xhs_agg["avg_xhs_sentiment"].get((month, brand), float("nan"))
        youtube_comment_count = int(youtube_agg["youtube_comment_count"].get((month, brand), 0))
        avg_youtube_sentiment = youtube_agg["avg_youtube_sentiment"].get((month, brand), float("nan"))
        instagram_comment_count = int(instagram_agg["instagram_comment_count"].get((month, brand), 0))
        avg_instagram_sentiment = instagram_agg["avg_instagram_sentiment"].get((month, brand), float("nan"))
        facebook_review_count = int(facebook_agg["facebook_review_count"].get((month, brand), 0))
        avg_facebook_sentiment = facebook_agg["avg_facebook_sentiment"].get((month, brand), float("nan"))
        rows.append({
            "month": str(month),
            "brand": brand,
            "review_count": review_count,
            "avg_rating": round(avg_rating, 2) if pd.notna(avg_rating) else None,
            "xhs_post_count": xhs_post_count,
            "avg_xhs_sentiment": round(avg_xhs_sentiment, 3) if pd.notna(avg_xhs_sentiment) else None,
            "youtube_comment_count": youtube_comment_count,
            "avg_youtube_sentiment": round(avg_youtube_sentiment, 3) if pd.notna(avg_youtube_sentiment) else None,
            "instagram_comment_count": instagram_comment_count,
            "avg_instagram_sentiment": round(avg_instagram_sentiment, 3) if pd.notna(avg_instagram_sentiment) else None,
            "facebook_review_count": facebook_review_count,
            "avg_facebook_sentiment": round(avg_facebook_sentiment, 3) if pd.notna(avg_facebook_sentiment) else None,
        })
    return pd.DataFrame(rows).sort_values(["brand", "month"]).reset_index(drop=True)


# ============================================================
# Anomaly flagging
# ============================================================

def _activity_column_for_metric(metric: str) -> str:
    return "review_count" if metric == "review_count" else "xhs_post_count"


def flag_anomalies(grid: pd.DataFrame, metrics: list = ANOMALY_METRICS) -> pd.DataFrame:
    events = []
    for brand, brand_grid in grid.groupby("brand"):
        brand_grid = brand_grid.sort_values("month").reset_index(drop=True)
        for metric in metrics:
            activity_col = _activity_column_for_metric(metric)
            values = brand_grid[metric]
            activity = brand_grid[activity_col]
            for i in range(TRAILING_WINDOW, len(brand_grid)):
                window_activity = activity.iloc[i - TRAILING_WINDOW: i + 1]
                if window_activity.sum() == 0:
                    continue  # no real activity anywhere in [t-3, t] — pure zero-padding, not eligible

                trailing = values.iloc[i - TRAILING_WINDOW: i].dropna()
                if len(trailing) < TRAILING_WINDOW:
                    continue  # need a full 3-month trailing baseline
                value_t = values.iloc[i]
                if pd.isna(value_t):
                    continue

                trailing_mean = trailing.mean()
                trailing_std = trailing.std(ddof=1)
                deviation = abs(value_t - trailing_mean)
                flagged = deviation > 0 if trailing_std == 0 else deviation > trailing_std
                if flagged:
                    events.append({
                        "brand": brand,
                        "month": brand_grid["month"].iloc[i],
                        "metric": metric,
                        "value": round(float(value_t), 3),
                        "trailing_mean": round(float(trailing_mean), 3),
                        "trailing_std": round(float(trailing_std), 3),
                    })
    return pd.DataFrame(events)


# ============================================================
# Output
# ============================================================

def write_outputs(grid: pd.DataFrame, events: pd.DataFrame, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    grid.to_csv(out_dir / "monthly_trends.csv", index=False, encoding="utf-8-sig")
    events.to_csv(out_dir / "monthly_trends_events.csv", index=False, encoding="utf-8-sig")

    months_covered = sorted(grid["month"].unique())
    lines = ["# Monthly Trends — Review + XHS + YouTube + Instagram + Facebook Activity Over Time\n"]
    lines.append(
        f"Full available range: {months_covered[0]} to {months_covered[-1]} ({len(months_covered)} months), "
        "per brand, zero/blank-filled for months with no activity. LIHKG is excluded — it only carries "
        "a relative \"N months ago\" age string, no absolute post date, so it can't be placed on a "
        "monthly timeline. YouTube and Instagram comments, and Facebook reviews, ARE included (real "
        "absolute timestamps) — YouTube/Instagram filtered to on-topic comments on brand-relevant "
        "content only, see youtube_scraper.py/youtube_signals.py and instagram_scraper.py/"
        "instagram_signals.py; Facebook reviews carry a rule-derived sentiment (from the reviewer's own "
        "recommend/not-recommend flag, not LLM-labeled like the other sources) — see "
        "facebook_reviews_merge.py/facebook_signals.py.\n"
    )
    lines.append(
        "**This CSV (`monthly_trends.csv`) is the reusable artifact** — a full month x brand grid "
        "intended for reuse in future event-study/lead-lag scripts, not just the charts below.\n"
    )

    lines.append("## Data availability per brand\n")
    lines.append("| Brand | First month with reviews | First month with XHS posts | First month with YouTube comments | First month with Instagram comments | First month with Facebook reviews |")
    lines.append("|---|---|---|---|---|---|")
    for brand, grp in grid.groupby("brand"):
        first_review = grp.loc[grp["review_count"] > 0, "month"]
        first_xhs = grp.loc[grp["xhs_post_count"] > 0, "month"]
        first_youtube = grp.loc[grp["youtube_comment_count"] > 0, "month"]
        first_instagram = grp.loc[grp["instagram_comment_count"] > 0, "month"]
        first_facebook = grp.loc[grp["facebook_review_count"] > 0, "month"]
        lines.append(
            f"| {brand} | {first_review.min() if not first_review.empty else 'none'} | "
            f"{first_xhs.min() if not first_xhs.empty else 'none'} | "
            f"{first_youtube.min() if not first_youtube.empty else 'none'} | "
            f"{first_instagram.min() if not first_instagram.empty else 'none'} | "
            f"{first_facebook.min() if not first_facebook.empty else 'none'} |"
        )

    lines.append(
        f"\n## Flagged anomalies (trailing {TRAILING_WINDOW}-month window)\n"
        f"_Directional flags only — a {TRAILING_WINDOW}-month trailing baseline is a small, noisy "
        "sample, not a statistical test. These are candidates worth a closer look, not explained "
        "causes. A month is only eligible to be flagged if the brand has real activity somewhere in "
        "its trailing window — pure zero-padding before a brand's first real month is never flagged._\n"
    )
    if events.empty:
        lines.append("None found.")
    else:
        for r in events.sort_values(["brand", "month"]).itertuples():
            lines.append(
                f"- **{r.brand} — {r.month}**: {r.metric} = {r.value} vs. trailing "
                f"{TRAILING_WINDOW}-month average {r.trailing_mean} (±{r.trailing_std} std)."
            )

    (out_dir / "monthly_trends.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"  Monthly Trends written -> monthly_trends.md, monthly_trends.csv, monthly_trends_events.csv "
          f"({len(events)} flagged events)")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Monthly review + XHS + YouTube + Instagram activity/sentiment trends")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--youtube-db", default=str(DEFAULT_YOUTUBE_DB))
    parser.add_argument("--instagram-db", default=str(DEFAULT_INSTAGRAM_DB))
    parser.add_argument("--facebook-db", default=str(DEFAULT_FACEBOOK_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    db_path = Path(args.db)
    youtube_db_path = Path(args.youtube_db)
    instagram_db_path = Path(args.instagram_db)
    facebook_db_path = Path(args.facebook_db)
    out_dir = Path(args.out)

    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return

    reviews = load_reviews_hk_dated(db_path)
    canonical_brands = sorted(reviews["brand"].dropna().unique().tolist())
    xhs = load_xhs_dated(db_path, canonical_brands)
    youtube = load_youtube_dated(youtube_db_path, canonical_brands)
    instagram = load_instagram_dated(instagram_db_path, canonical_brands)
    facebook = load_facebook_dated(facebook_db_path, canonical_brands)
    print(f"Loaded {len(reviews)} dated HK reviews, {len(xhs)} dated+brand-attributed XHS posts, "
          f"{len(youtube)} dated+on-topic YouTube comments, {len(instagram)} dated+on-topic Instagram "
          f"comments, {len(facebook)} dated+brand-attributed Facebook reviews, brands: {canonical_brands}")

    grid = build_month_brand_grid(reviews, xhs, youtube, instagram, facebook, canonical_brands)
    events = flag_anomalies(grid)

    write_outputs(grid, events, out_dir)
    print(f"\nDone. Outputs in: {out_dir}")


if __name__ == "__main__":
    main()
