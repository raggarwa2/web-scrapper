"""
Customer Signals data layer — Instagram post discovery + comments, for the
"Customer Signals (Instagram)" tab in app.py.

Read-only: only ever SELECTs from instagram_data.db. Never writes to the db.

Same fields/shape as youtube_signals.py wherever the data lines up (sentiment,
is_purchase_barrier_signal, is_lens_relevant on comments) so the per-brand
view is directly comparable across sources — see that file's docstring for
the reasoning behind on-topic filtering and fail-open-on-NaN.

One relevance layer, not two (see youtube_signals.py for contrast):
  - is_lens_relevant (post-level): a cheap regex whitelist check from
    instagram_scraper.py, NOT an LLM call (see that file's
    _is_lens_relevant). Flagged, not dropped — same transparency pattern as
    YouTube's excluded_videos, but weaker signal: a post can still be about
    the wrong brand and pass this check, since there is no brand-relevance
    LLM pass for Instagram yet (unlike youtube_scraper.py's
    check_brand_relevance()). See instagram_context.md's "Open" section.
  - is_lens_relevant (comment-level) IS LLM-classified, same as YouTube's.

Instagram posts are discovered two ways (instagram_scraper.py's --source):
hashtag search or direct profile crawl. `source_type`/`source_value` record
which, per post — surfaced here as a transparency caption, not filtered on.

Lives in its own database (output/instagram_data.db by default), separate
from lensdata.db and youtube_data.db.
"""

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
    return posts, comments


def load_hk_dashboard_data(db_path: str = DEFAULT_DB_PATH):
    """posts/comments filtered to HK + lens-relevant posts only — same
    exclusion render() applies, factored out so other tabs that want to
    blend Instagram in (Brand Health, Trends & Demand, etc.) don't
    duplicate it. `comments` still includes off-topic ones (comment-level
    is_lens_relevant == 0) — callers that want sentiment/barrier signal
    should also apply on_topic_comments(). Returns (posts, comments,
    excluded_posts) — excluded_posts is exposed so callers can still show
    the same transparency note render() does. Empty DataFrames (not an
    error) if the db doesn't exist yet."""
    if not os.path.exists(db_path):
        empty = pd.DataFrame()
        return empty, empty, empty

    mtime = os.path.getmtime(db_path)
    posts, comments = load_instagram_data(db_path, mtime)

    posts = posts[posts["market"] == "HK"] if not posts.empty else posts
    comments = comments[comments["market"] == "HK"] if not comments.empty else comments
    if posts.empty:
        return posts, comments, pd.DataFrame()

    not_relevant = posts["is_lens_relevant"] == 0 if "is_lens_relevant" in posts.columns else pd.Series(False, index=posts.index)
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
        'to YouTube/LIHKG, but likes are a reach proxy, not a reception signal. Post-level '
        'relevance is a keyword whitelist, not an LLM brand check (unlike YouTube) — a post '
        'can still be about the wrong brand and pass it.</div>',
        unsafe_allow_html=True,
    )

    if not excluded_posts.empty:
        with st.expander(
            f"⚠ {len(excluded_posts)} post(s) excluded — no explicit contact-lens term in caption"
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
            n_off_topic = len(b_comments) - len(b_comments_on_topic)

            m_cols = st.columns(4)
            m_cols[0].metric("Posts", len(b_posts))
            m_cols[1].metric("Total likes", f"{int(b_posts['likes_count'].sum()):,}")
            m_cols[2].metric("Total comments (reported)", f"{int(b_posts['comments_count'].sum()):,}")
            m_cols[3].metric("Comments collected", len(b_comments))

            sent_counts = b_comments_on_topic["sentiment"].value_counts() if not b_comments_on_topic.empty else pd.Series(dtype=int)
            s_cols = st.columns(4)
            s_cols[0].metric("Negative", int(sent_counts.get("negative", 0)))
            s_cols[1].metric("Positive", int(sent_counts.get("positive", 0)))
            s_cols[2].metric("Mixed", int(sent_counts.get("mixed", 0)))
            barrier_n = int(b_comments_on_topic["is_purchase_barrier_signal"].sum()) if not b_comments_on_topic.empty else 0
            s_cols[3].metric(
                "Purchase-barrier comments", barrier_n,
                delta=f"{barrier_n / len(b_comments_on_topic) * 100:.0f}% of comments" if len(b_comments_on_topic) else None,
                delta_color="off",
            )
            if n_off_topic:
                st.caption(
                    f"{n_off_topic} comment(s) excluded from the metrics above as off-topic "
                    "(audience chatter unrelated to the product) — still visible in Comments below."
                )

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
