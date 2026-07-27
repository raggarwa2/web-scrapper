"""
Customer Feedback data layer — Facebook page reviews, for the "Customer
Feedback (Facebook)" tab in app.py.

Read-only: only ever SELECTs from facebook_data.db. Never writes to the db.

Facebook page reviews are solicited, first-party product reviews (like XHS's
"Customer Feedback" tab) — not unsolicited social chatter the way LIHKG/
YouTube/Instagram comments are (see lihkg_signals.py's docstring for that
distinction). Sentiment here is RULE-DERIVED from Facebook's own binary
isRecommended flag (True/False), not LLM-classified like every other source
in this dashboard — there is no "neutral" bucket a rule on a boolean can
produce, so Facebook's sentiment split will structurally never show one,
unlike XHS/LIHKG/YouTube/Instagram. mentioned_brands is comma-joined and
multi-valued per review (any of the 5 tracked brands can be named in the
review text, or none) — same shape as LIHKG's mentioned_brands, not XHS's
single-value brand_mentioned.

There's no discovery-then-relevance-filter pass here (unlike Instagram/
YouTube) — every review IS the analysis unit, so load_hk_dashboard_data()
returns a single DataFrame, no excluded-set/on-topic split.

Lives in its own database (output/facebook_data.db by default), separate
from lensdata.db, matching the youtube_data.db/instagram_data.db precedent —
see facebook_reviews_merge.py's module docstring for why.
"""

import os
import sqlite3

import pandas as pd
import plotly.express as px
import streamlit as st

DEFAULT_DB_PATH = os.path.join("output", "facebook_data.db")

# Mirrors BRAND_COLORS in app.py / instagram_signals.py — kept as a local
# copy (not imported) since app.py imports this module and importing back
# would be circular.
BRAND_COLORS = {
    "Acuvue": "#2563eb",
    "Alcon": "#16a34a",
    "Bausch & Lomb": "#dc2626",
    "CooperVision": "#7c3aed",
    "Olens": "#db2777",
}


@st.cache_data(show_spinner=False)
def load_facebook_reviews(db_path: str, mtime: float) -> pd.DataFrame:
    """All fb_reviews, one row per review. mentioned_brands is stored
    comma-joined — this adds mentioned_brands_list so callers can
    .explode() it, same convention as lihkg_signals.load_lihkg_posts().
    Returns an empty DataFrame if the db/table doesn't exist yet (e.g.
    facebook_reviews_merge.py hasn't been run)."""
    conn = sqlite3.connect(db_path)
    try:
        reviews = pd.read_sql_query("SELECT * FROM fb_reviews", conn)
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()

    if reviews.empty:
        return reviews

    reviews["mentioned_brands_list"] = reviews["mentioned_brands"].apply(
        lambda s: [b for b in s.split(",") if b] if s else []
    )
    return reviews


def load_hk_dashboard_data(db_path: str = DEFAULT_DB_PATH):
    """Returns (reviews,) — a single-element tuple for symmetry with the
    other *_signals modules' load_hk_dashboard_data() contract. No
    market filter (these 5 pages are HK-only by construction) and no
    excluded-set (no discovery-then-relevance-filter pass exists for this
    source). Empty DataFrame (not an error) if the db doesn't exist yet."""
    if not os.path.exists(db_path):
        return (pd.DataFrame(),)
    mtime = os.path.getmtime(db_path)
    return (load_facebook_reviews(db_path, mtime),)


def brand_exploded(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (review, brand) — a review mentioning two brands counts
    toward both. Drops reviews with no recognized brand mention."""
    if df.empty:
        return df
    exploded = df.explode("mentioned_brands_list")
    return exploded[exploded["mentioned_brands_list"].notna() & (exploded["mentioned_brands_list"] != "")]


def purchase_barrier_rate(exploded_df: pd.DataFrame) -> pd.DataFrame:
    """% of a brand's reviews flagged as a purchase-barrier signal. Same
    shape/contract as lihkg_signals.purchase_barrier_rate()/
    instagram_signals.purchase_barrier_rate(). Expects the output of
    brand_exploded()."""
    if exploded_df.empty:
        return pd.DataFrame(columns=["brand", "review_count", "barrier_count", "barrier_rate"])
    g = exploded_df.groupby("mentioned_brands_list").agg(
        review_count=("is_purchase_barrier_signal", "count"),
        barrier_count=("is_purchase_barrier_signal", "sum"),
    )
    g["barrier_rate"] = (g["barrier_count"] / g["review_count"] * 100).round(1)
    return g.reset_index().rename(columns={"mentioned_brands_list": "brand"})


def render(db_path: str = DEFAULT_DB_PATH):
    if not os.path.exists(db_path):
        st.info(
            f"No Facebook data found at `{db_path}`. Run `python facebook_reviews_merge.py` "
            "to populate fb_reviews."
        )
        return

    reviews, = load_hk_dashboard_data(db_path)

    if reviews.empty:
        st.info("No Facebook reviews loaded yet. Run `python facebook_reviews_merge.py` to populate fb_reviews.")
        return

    st.markdown(
        '<div class="caveat-box">Facebook page reviews are first-party, solicited product '
        'reviews (a reviewer explicitly recommends or does not recommend the page), unlike '
        'the unsolicited chatter on LIHKG/YouTube/Instagram — closer in spirit to the XHS '
        '"Customer Feedback" tab. Sentiment here is <b>rule-derived</b> from Facebook\'s own '
        'recommend/not-recommend flag, not LLM-classified like every other source in this '
        'dashboard, so there is no "neutral" bucket. Reviews come from 5 HK reseller/optician '
        'pages — the 5 official brand pages carry no reviews at all.</div>',
        unsafe_allow_html=True,
    )

    exploded = brand_exploded(reviews)
    brands = sorted(exploded["mentioned_brands_list"].unique())

    n_pages = reviews["page_name"].nunique()
    n_reviews = len(reviews)
    n_signal = int((reviews["mentioned_brands_list"].str.len() > 0).sum())
    n_signal_pct = (n_signal / n_reviews * 100) if n_reviews else 0

    st.caption(f"Facebook · {n_reviews:,} reviews across {n_pages} page(s)")
    st.markdown(
        f"**{n_signal:,} of {n_reviews:,} reviews ({n_signal_pct:.0f}%) carry a recognized brand "
        f"mention and are used as signal** in the charts and per-brand tabs below. The remaining "
        f"{n_reviews - n_signal:,} reviews are general feedback about the page/store that don't "
        f"name any of the 5 tracked brands."
    )

    if not brands:
        st.info("No reviews with a recognized brand mention yet.")
        return

    all_tab, *brand_tabs = st.tabs(["All Brands"] + brands)

    with all_tab:
        c1, c2 = st.columns(2)
        with c1:
            vol_by_brand = (
                exploded.groupby(["mentioned_brands_list", "sentiment"])
                .size().reset_index(name="count")
            )
            fig = px.bar(
                vol_by_brand, x="mentioned_brands_list", y="count", color="sentiment",
                barmode="stack",
                color_discrete_map={"positive": "#16a34a", "neutral": "#94a3b8", "negative": "#dc2626"},
                title="Review volume & sentiment by brand",
                labels={"mentioned_brands_list": "Brand", "count": "Reviews"},
            )
            st.plotly_chart(fig, width='stretch')
        with c2:
            barrier = purchase_barrier_rate(exploded)
            fig = px.bar(
                barrier, x="brand", y="barrier_rate",
                title="Purchase-barrier signal rate by brand (%)",
                labels={"brand": "Brand", "barrier_rate": "% of reviews"},
                color="brand", color_discrete_map=BRAND_COLORS,
            )
            fig.update_layout(showlegend=False)
            st.plotly_chart(fig, width='stretch')
        st.caption(
            "Purchase-barrier signal = the review states a reason for not buying/switching/"
            "recommending (price, comfort, trust, availability, service, etc.)."
        )

    for brand, brand_tab in zip(brands, brand_tabs):
        with brand_tab:
            b_reviews = exploded[exploded["mentioned_brands_list"] == brand]
            st.caption(f"{len(b_reviews)} reviews mentioning {brand}")

            sent_counts = b_reviews["sentiment"].value_counts()
            m_cols = st.columns(4)
            m_cols[0].metric("Reviews", len(b_reviews))
            m_cols[1].metric("Positive", int(sent_counts.get("positive", 0)))
            m_cols[2].metric("Negative", int(sent_counts.get("negative", 0)))
            barrier_n = int(b_reviews["is_purchase_barrier_signal"].sum())
            m_cols[3].metric(
                "Purchase-barrier reviews", barrier_n,
                delta=f"{barrier_n / len(b_reviews) * 100:.0f}% of reviews" if len(b_reviews) else None,
                delta_color="off",
            )

            st.subheader("Purchase-barrier reviews")
            barrier_reviews = b_reviews[b_reviews["is_purchase_barrier_signal"] == 1]
            if barrier_reviews.empty:
                st.caption("None flagged for this brand in current data.")
            else:
                for _, row in barrier_reviews.iterrows():
                    st.markdown(f"**{row['page_name']}** · sentiment: {row['sentiment']} · {row['review_date']}")
                    st.write(row["text_english"] or row["text_original"])
                    if row["review_url"]:
                        st.caption(row["review_url"])
                    st.divider()

            st.subheader("All reviews")
            for _, row in b_reviews.sort_values("review_date", ascending=False).iterrows():
                st.markdown(f"**{row['page_name']}** · sentiment: {row['sentiment']} · {row['review_date']}")
                st.write(row["text_english"] or row["text_original"])
                if row["text_english"] and row["text_english"] != row["text_original"]:
                    st.caption(f"Original: {row['text_original']}")
                st.divider()
