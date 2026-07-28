"""
Customer Signals data layer — Instagram post discovery + comments, for the
"Customer Signals (Instagram)" tab in app.py.

Read-only: only ever SELECTs from instagram_data.db. Never writes to the db.

Same fields/shape as youtube_signals.py wherever the data lines up (sentiment,
is_purchase_barrier_signal, is_lens_relevant on comments) so the per-brand
view is directly comparable across sources — see that file's docstring for
the reasoning behind on-topic filtering and fail-open-on-NaN.

Two relevance layers, both from instagram_scraper.py, both flagged-not-
dropped (mirrors youtube_signals.py's pattern — excluded from metrics, still
visible in an expander for transparency):
  - is_lens_relevant (post-level): a cheap regex whitelist check, NOT an LLM
    call (see that file's _is_lens_relevant) — catches posts with no
    contact-lens term in the caption at all.
  - brand_relevant (post-level): an LLM check (check_brand_relevance(),
    mirrors youtube_scraper.py's) — catches the subtler case: a post can pass
    the lens-relevance whitelist and still not be about the TAGGED brand, since
    HK resellers commonly hashtag-stuff many brand names onto one post
    regardless of which brand the actual product is (confirmed: a Korean
    LACELLE/Clalen post co-tagged #博士倫隱形眼鏡 alongside a dozen other
    brand hashtags). Both checks must pass for a post to enter the metrics.
  - is_lens_relevant (comment-level) IS LLM-classified, same as YouTube's.

Instagram posts are discovered two ways (instagram_scraper.py's --source):
hashtag search or direct profile crawl. `source_type`/`source_value` record
which, per post — surfaced here as a transparency caption, not filtered on.

Lives in its own database (output/instagram_data.db by default), separate
from lensdata.db and youtube_data.db.
"""

import json
import os
import sqlite3

import pandas as pd
import plotly.express as px
import streamlit as st

DEFAULT_DB_PATH = os.path.join("output", "instagram_data.db")

# Mirrors BRAND_COLORS in app.py / youtube_signals.py — kept as a local copy
# (not imported) since app.py imports this module and importing back would
# be circular.
BRAND_COLORS = {
    "Acuvue": "#2563eb",
    "Alcon": "#16a34a",
    "Bausch & Lomb": "#dc2626",
    "CooperVision": "#7c3aed",
    "Olens": "#db2777",
}


@st.cache_data(show_spinner=False)
def load_instagram_data(db_path: str, mtime: float):
    """ig_posts and ig_comments, one row each. Returns two empty DataFrames
    if the db or tables don't exist yet (e.g. instagram_scraper.py hasn't
    been run)."""
    conn = sqlite3.connect(db_path)
    try:
        posts = pd.read_sql_query("SELECT * FROM ig_posts", conn)
    except Exception:
        posts = pd.DataFrame()
    try:
        comments = pd.read_sql_query("SELECT * FROM ig_comments", conn)
    except Exception:
        comments = pd.DataFrame()
    finally:
        conn.close()

    if not comments.empty:
        def _parse_themes(val):
            try:
                return json.loads(val) if val else []
            except Exception:
                return []
        comments["themes_list"] = comments["themes"].apply(_parse_themes) if "themes" in comments.columns else [[] for _ in range(len(comments))]

    return posts, comments


def load_hk_dashboard_data(db_path: str = DEFAULT_DB_PATH):
    """posts/comments filtered to HK + lens-relevant + brand-relevant posts
    only — same exclusion render() applies, factored out so other tabs that
    want to blend Instagram in (Brand Health, Trends & Demand, etc.) don't
    duplicate it. `comments` still includes off-topic ones (comment-level
    is_lens_relevant == 0) — callers that want sentiment/barrier signal
    should also apply on_topic_comments(). Returns (posts, comments,
    excluded_posts) — excluded_posts is exposed so callers can still show
    the same transparency note render() does. NaN (not yet checked, e.g.
    brand_relevant before backfill ran) fails open and counts as relevant —
    only an explicit 0 excludes. Empty DataFrames (not an error) if the db
    doesn't exist yet."""
    if not os.path.exists(db_path):
        empty = pd.DataFrame()
        return empty, empty, empty

    mtime = os.path.getmtime(db_path)
    posts, comments = load_instagram_data(db_path, mtime)

    posts = posts[posts["market"] == "HK"] if not posts.empty else posts
    comments = comments[comments["market"] == "HK"] if not comments.empty else comments
    if posts.empty:
        return posts, comments, pd.DataFrame()

    lens_not_relevant = posts["is_lens_relevant"] == 0 if "is_lens_relevant" in posts.columns else pd.Series(False, index=posts.index)
    brand_not_relevant = posts["brand_relevant"] == 0 if "brand_relevant" in posts.columns else pd.Series(False, index=posts.index)
    not_relevant = lens_not_relevant | brand_not_relevant
    excluded_posts = posts[not_relevant]
    posts = posts[~not_relevant]
    comments = comments[comments["post_id"].isin(posts["post_id"])] if not comments.empty else comments
    return posts, comments, excluded_posts


def on_topic_comments(comments: pd.DataFrame) -> pd.DataFrame:
    """Comments actually about the product (is_lens_relevant == 1), for
    sentiment/barrier aggregation — same rule as youtube_signals.py's
    on_topic_comments(). NaN (not yet classified) fails open and counts as
    on-topic."""
    if comments.empty:
        return comments
    mask = comments["is_lens_relevant"] != 0
    return comments[mask]


def purchase_barrier_rate(on_topic_df: pd.DataFrame) -> pd.DataFrame:
    """% of a brand's on-topic comments flagged as a purchase-barrier
    signal. Same shape/contract as youtube_signals.purchase_barrier_rate()
    and lihkg_signals.purchase_barrier_rate() so callers can treat all
    three sources identically. Expects the output of on_topic_comments()."""
    if on_topic_df.empty:
        return pd.DataFrame(columns=["brand", "post_count", "barrier_count", "barrier_rate"])
    g = on_topic_df.groupby("brand").agg(
        post_count=("is_purchase_barrier_signal", "count"),
        barrier_count=("is_purchase_barrier_signal", "sum"),
    )
    g["barrier_rate"] = (g["barrier_count"] / g["post_count"] * 100).round(1)
    return g.reset_index()


@st.cache_data(show_spinner=False)
def get_monthly_comment_counts(brand: str, db_path: str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """Monthly on-topic HK comment count for `brand`, for the Demand
    Signals / Monthly Trends charts. published_at is an Instagram API ISO
    8601 timestamp — same format as YouTube's, so this can be windowed/
    trended the same way."""
    posts, comments, _ = load_hk_dashboard_data(db_path)
    if comments.empty:
        return pd.DataFrame(columns=["month", "instagram_count"])
    b_comments = on_topic_comments(comments[comments["brand"] == brand])
    if b_comments.empty:
        return pd.DataFrame(columns=["month", "instagram_count"])
    months = pd.to_datetime(b_comments["published_at"], errors="coerce", utc=True).dt.strftime("%Y-%m")
    return (
        months.dropna().value_counts().reset_index()
        .rename(columns={"published_at": "month", "count": "instagram_count"})
        .sort_values("month").reset_index(drop=True)
    )


def _render_summary_metrics(posts_df: pd.DataFrame, comments_df: pd.DataFrame, on_topic_df: pd.DataFrame) -> None:
    """Two-row metric-card summary: reach (posts/likes/reported comments/
    comments analyzed), then the FULL sentiment breakdown of on-topic
    comments (positive/neutral/negative/mixed — all four labels, not just
    three) plus purchase-barrier rate. Used for both the All Brands
    aggregate and each per-brand tab so the two views are numerically
    consistent.

    "Comments analyzed" and the sentiment breakdown are both scored on
    on-topic comments only (is_lens_relevant == 1); off-topic comments are
    excluded from both and only surfaced via the footnote caption below, so
    every number on the cards themselves reconciles. See youtube_signals.py's
    version of this function for why "Neutral" has to be its own metric —
    earlier cards omitted it, making most of the total look unaccounted
    for."""
    off_topic_n = len(comments_df) - len(on_topic_df)

    r1 = st.columns(4)
    r1[0].metric("Posts", len(posts_df))
    r1[1].metric("Total likes", f"{int(posts_df['likes_count'].sum()):,}")
    r1[2].metric("Total comments (reported)", f"{int(posts_df['comments_count'].sum()):,}")
    r1[3].metric("Comments analyzed", len(on_topic_df))

    sent_counts = on_topic_df["sentiment"].value_counts() if not on_topic_df.empty else pd.Series(dtype=int)
    barrier_n = int(on_topic_df["is_purchase_barrier_signal"].sum()) if not on_topic_df.empty else 0
    r2 = st.columns(5)
    r2[0].metric("Positive", int(sent_counts.get("positive", 0)))
    r2[1].metric("Neutral", int(sent_counts.get("neutral", 0)))
    r2[2].metric("Negative", int(sent_counts.get("negative", 0)))
    r2[3].metric("Mixed", int(sent_counts.get("mixed", 0)))
    r2[4].metric(
        "Purchase-barrier", barrier_n,
        delta=f"{barrier_n / len(on_topic_df) * 100:.0f}% of on-topic" if len(on_topic_df) else None,
        delta_color="off",
    )
    st.caption(
        f"{len(comments_df):,} comments collected in total — {off_topic_n:,} excluded above as "
        "off-topic (audience chatter unrelated to the product, e.g. a celebrity tie-in post "
        "drawing comments about the celebrity rather than the lenses)."
    )


def render(db_path: str = DEFAULT_DB_PATH):
    if not os.path.exists(db_path):
        st.info(
            f"No Instagram data found at `{db_path}`. Run `python instagram_scraper.py` "
            "to populate ig_posts/ig_comments."
        )
        return

    posts, comments, excluded_posts = load_hk_dashboard_data(db_path)

    if posts.empty:
        st.info("No Instagram posts loaded yet. Run `python instagram_scraper.py` to discover posts.")
        return

    st.markdown(
        '<div class="caveat-box">Instagram comments are unsolicited viewer reactions to a '
        'post, not product reviews — sentiment/purchase-barrier scoring here is comparable '
        'to YouTube/LIHKG, but likes are a reach proxy, not a reception signal. Posts pass '
        'two checks before counting toward a brand: an explicit contact-lens term in the '
        'caption, and an LLM check that the post is genuinely about the tagged brand (HK '
        'resellers commonly hashtag-stuff multiple brand names onto one post).</div>',
        unsafe_allow_html=True,
    )

    if not excluded_posts.empty:
        with st.expander(
            f"⚠ {len(excluded_posts)} post(s) excluded — no contact-lens term in caption, "
            "or not actually about the tagged brand"
        ):
            st.dataframe(
                excluded_posts[["brand", "caption_en", "owner_username", "url"]].rename(columns={
                    "brand": "Tagged brand", "caption_en": "Caption", "owner_username": "Account", "url": "Link",
                }),
                width='stretch', hide_index=True,
                column_config={"Link": st.column_config.LinkColumn("Link", display_text="Open ↗")},
            )

    if posts.empty:
        st.info("No lens-relevant Instagram posts in current data.")
        return

    brands = sorted(posts["brand"].dropna().unique())
    n_posts = len(posts)
    n_comments = len(comments)

    hashtag_n = int((posts["source_type"] == "hashtag").sum()) if "source_type" in posts.columns else 0
    profile_n = int((posts["source_type"] == "profile").sum()) if "source_type" in posts.columns else 0
    st.caption(
        f"Instagram · HK · {n_posts:,} posts ({hashtag_n} via hashtag, {profile_n} via profile crawl) · "
        f"{n_comments:,} comments collected · {len(brands)} brand(s)"
    )

    if not brands:
        st.info("No posts with a recognized brand tag yet.")
        return

    all_tab, *brand_tabs = st.tabs(["All Brands"] + brands)

    with all_tab:
        _render_summary_metrics(posts, comments, on_topic_comments(comments))
        st.divider()

        c1, c2 = st.columns(2)
        with c1:
            vol_by_brand = posts.groupby("brand").size().reset_index(name="count")
            fig = px.bar(
                vol_by_brand, x="brand", y="count",
                title="Post count by brand",
                labels={"brand": "Brand", "count": "Posts"},
                color="brand", color_discrete_map=BRAND_COLORS,
            )
            fig.update_layout(showlegend=False)
            st.plotly_chart(fig, width='stretch')
        with c2:
            likes_by_brand = posts.groupby("brand")["likes_count"].sum().reset_index()
            fig = px.bar(
                likes_by_brand, x="brand", y="likes_count",
                title="Total likes by brand",
                labels={"brand": "Brand", "likes_count": "Likes"},
                color="brand", color_discrete_map=BRAND_COLORS,
            )
            fig.update_layout(showlegend=False)
            st.plotly_chart(fig, width='stretch')
        st.caption("Likes summed across all discovered posts per brand — a reach proxy, not unique viewers.")

        _on_topic_all = on_topic_comments(comments)
        if not _on_topic_all.empty:
            theme_brand = (
                _on_topic_all.explode("themes_list")
                .groupby(["brand", "themes_list"])
                .size().reset_index(name="count")
            )
            theme_brand = theme_brand[theme_brand["themes_list"].notna() & (theme_brand["themes_list"] != "")]
            if not theme_brand.empty:
                theme_order = (
                    theme_brand.groupby("themes_list")["count"].sum()
                    .sort_values(ascending=False)
                    .head(15)
                    .index
                )
                theme_brand = theme_brand[theme_brand["themes_list"].isin(theme_order)]
                fig = px.bar(
                    theme_brand, x="count", y="themes_list", color="brand",
                    orientation="h",
                    category_orders={"themes_list": list(reversed(list(theme_order)))},
                    title="Top 15 themes across all brands",
                    labels={"themes_list": "Theme", "count": "Mentions", "brand": "Brand"},
                )
                fig.update_layout(barmode="stack")
                st.plotly_chart(fig, width='stretch')

    for brand, brand_tab in zip(brands, brand_tabs):
        with brand_tab:
            b_posts = posts[posts["brand"] == brand]
            b_comments = comments[comments["brand"] == brand] if not comments.empty else comments

            # Sentiment/barrier metrics count only on-topic comments (is_lens_relevant == 1) —
            # off-topic ones (audience chatter unrelated to the product, e.g. a celebrity
            # tie-in post drawing comments about the celebrity, not the lenses) would otherwise
            # skew brand sentiment on tangents that have nothing to do with the product. NaN
            # (not yet classified) fails open and counts as on-topic.
            on_topic_mask = b_comments["is_lens_relevant"] != 0 if not b_comments.empty else pd.Series(dtype=bool)
            b_comments_on_topic = b_comments[on_topic_mask] if not b_comments.empty else b_comments
            _render_summary_metrics(b_posts, b_comments, b_comments_on_topic)

            theme_sentiment = (
                b_comments_on_topic.explode("themes_list")
                .groupby(["themes_list", "sentiment"])
                .size().reset_index(name="count")
            ) if not b_comments_on_topic.empty else pd.DataFrame()
            theme_sentiment = theme_sentiment[theme_sentiment["themes_list"].notna() & (theme_sentiment["themes_list"] != "")] if not theme_sentiment.empty else theme_sentiment
            if not theme_sentiment.empty:
                theme_order = (
                    theme_sentiment.groupby("themes_list")["count"].sum()
                    .sort_values().index
                )
                fig = px.bar(
                    theme_sentiment, x="count", y="themes_list", color="sentiment",
                    orientation="h",
                    category_orders={"themes_list": list(theme_order)},
                    color_discrete_map={"positive": "#16a34a", "neutral": "#94a3b8", "negative": "#dc2626", "mixed": "#e8a33d"},
                    title="Most discussed themes, by sentiment",
                    labels={"themes_list": "Theme", "count": "Mentions"},
                )
                fig.update_layout(barmode="stack")
                st.plotly_chart(fig, width='stretch')

            st.subheader("Purchase-barrier comments")
            barrier_comments = b_comments_on_topic[b_comments_on_topic["is_purchase_barrier_signal"] == 1] if not b_comments_on_topic.empty else b_comments_on_topic
            if barrier_comments.empty:
                st.caption("None flagged for this brand in current data.")
            else:
                for _, row in barrier_comments.iterrows():
                    st.markdown(f"**{row['author']}** · 👍 {row['like_count']} · sentiment: {row['sentiment']}")
                    st.write(row["comment_text_en"] or row["comment_text"])
                    st.divider()

            st.subheader("Posts")
            b_posts_display = b_posts.assign(
                caption_display=b_posts["caption_en"].fillna(b_posts["caption"])
                if "caption_en" in b_posts.columns else b_posts["caption"]
            )
            st.dataframe(
                b_posts_display[[
                    "caption_display", "owner_username", "source_type", "source_value",
                    "published_at", "likes_count", "comments_count", "url",
                ]].rename(columns={
                    "caption_display": "Caption", "owner_username": "Account",
                    "source_type": "Found via", "source_value": "Hashtag/Profile",
                    "published_at": "Published", "likes_count": "Likes",
                    "comments_count": "Comments", "url": "Link",
                }).sort_values("Likes", ascending=False),
                width='stretch', hide_index=True,
                column_config={"Link": st.column_config.LinkColumn("Link", display_text="Open ↗")},
            )

            st.subheader("Comments")
            if b_comments.empty:
                st.caption("No comments collected for this brand yet.")
            else:
                for _, row in b_comments.sort_values("like_count", ascending=False).iterrows():
                    off_topic_tag = " · _off-topic, excluded from metrics_" if row["is_lens_relevant"] == 0 else ""
                    st.markdown(f"**{row['author']}** · 👍 {row['like_count']} · sentiment: {row['sentiment']}{off_topic_tag}")
                    st.write(row["comment_text_en"] or row["comment_text"])
                    if row["comment_text_en"] and row["comment_text_en"] != row["comment_text"]:
                        st.caption(f"Original: {row['comment_text']}")
                    st.divider()
