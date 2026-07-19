"""
Customer Signals data layer — YouTube video discovery + comments, for the
"Customer Signals (YouTube)" tab in app.py.

Read-only: only ever SELECTs from youtube_data.db. Never writes to the db.

Comments carry the same sentiment + is_purchase_barrier_signal fields as
lihkg_posts (youtube_scraper.py's _llm_classify_batch, run on
comment_text_en) so the per-brand view mirrors the LIHKG pane. Unlike
LIHKG, YouTube also has video-level reach metrics (views/likes), which
LIHKG has no equivalent of — those stay as an extra metrics row rather
than replacing the sentiment view.

Two relevance layers, both from youtube_scraper.py, both flagged-not-
dropped (mirrors lihkg_signals.py's likely_collision pattern — excluded
from metrics, still visible in an expander for transparency):
  - brand_relevant (video-level): a video can pass the lens-relevance
    whitelist and still not be about the brand it was tagged with — a
    keyword search surfaces adjacent content (a different company's
    product, a clinic's generic myopia content). Excluded videos and
    their comments never enter the metrics/tables at all.
  - is_lens_relevant (comment-level): even under a genuinely relevant
    video, individual comments drift off-topic (e.g. a brand/K-pop
    sponsorship draws comments about the group, not the product).
    Excluded from sentiment/barrier metrics but still shown in the raw
    comment browser, marked, since it's real audience content.

Lives in its own database (output/youtube_data.db by default), separate
from lensdata.db — see youtube_scraper.py's module docstring.
"""

import os
import sqlite3

import pandas as pd
import plotly.express as px
import streamlit as st

DEFAULT_DB_PATH = os.path.join("output", "youtube_data.db")

# Mirrors BRAND_COLORS in app.py — kept as a local copy (not imported) since
# app.py imports this module and importing back would be circular.
BRAND_COLORS = {
    "Acuvue": "#2563eb",
    "Alcon": "#16a34a",
    "Bausch & Lomb": "#dc2626",
    "CooperVision": "#7c3aed",
    "Olens": "#db2777",
}


@st.cache_data(show_spinner=False)
def load_youtube_data(db_path: str, mtime: float):
    """youtube_videos and youtube_comments, one row each. Returns two empty
    DataFrames if the db or tables don't exist yet (e.g. youtube_scraper.py
    hasn't been run)."""
    conn = sqlite3.connect(db_path)
    try:
        videos = pd.read_sql_query("SELECT * FROM youtube_videos", conn)
    except Exception:
        videos = pd.DataFrame()
    try:
        comments = pd.read_sql_query("SELECT * FROM youtube_comments", conn)
    except Exception:
        comments = pd.DataFrame()
    finally:
        conn.close()
    return videos, comments


def load_hk_dashboard_data(db_path: str = DEFAULT_DB_PATH):
    """videos/comments filtered to HK + brand-relevant videos only — the
    same exclusion render() applies, factored out so other tabs that want
    to blend YouTube in (Brand Health, Trends & Demand, etc.) don't
    duplicate it. `comments` still includes off-topic ones (is_lens_relevant
    == 0) — callers that want sentiment/barrier signal should also apply
    on_topic_comments(). Returns (videos, comments, excluded_videos) —
    excluded_videos is exposed so callers can still show the same
    transparency note render() does. Empty DataFrames (not an error) if the
    db doesn't exist yet."""
    if not os.path.exists(db_path):
        empty = pd.DataFrame()
        return empty, empty, empty

    mtime = os.path.getmtime(db_path)
    videos, comments = load_youtube_data(db_path, mtime)

    videos = videos[videos["market"] == "HK"] if not videos.empty else videos
    comments = comments[comments["market"] == "HK"] if not comments.empty else comments
    if videos.empty:
        return videos, comments, pd.DataFrame()

    not_relevant = videos["brand_relevant"] == 0 if "brand_relevant" in videos.columns else pd.Series(False, index=videos.index)
    excluded_videos = videos[not_relevant]
    videos = videos[~not_relevant]
    comments = comments[comments["video_id"].isin(videos["video_id"])] if not comments.empty else comments
    return videos, comments, excluded_videos


def on_topic_comments(comments: pd.DataFrame) -> pd.DataFrame:
    """Comments actually about the product (is_lens_relevant == 1), for
    sentiment/barrier aggregation — mirrors lihkg_signals.brand_exploded()'s
    role of pre-filtering before scoring. NaN (not yet classified) fails
    open and counts as on-topic, same rule render() uses."""
    if comments.empty:
        return comments
    mask = comments["is_lens_relevant"] != 0
    return comments[mask]


def purchase_barrier_rate(on_topic_df: pd.DataFrame) -> pd.DataFrame:
    """% of a brand's on-topic comments flagged as a purchase-barrier
    signal. Same shape/contract as lihkg_signals.purchase_barrier_rate() so
    callers can treat the two sources identically. Expects the output of
    on_topic_comments()."""
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
    Signals / Monthly Trends charts. published_at is a YouTube API ISO
    8601 timestamp (e.g. "2026-03-10T11:29:36Z") — unlike LIHKG's relative
    age text, this is a real date, so YouTube can be time-windowed/trended
    the same way Reviews/XHS are."""
    videos, comments, _ = load_hk_dashboard_data(db_path)
    if comments.empty:
        return pd.DataFrame(columns=["month", "youtube_count"])
    b_comments = on_topic_comments(comments[comments["brand"] == brand])
    if b_comments.empty:
        return pd.DataFrame(columns=["month", "youtube_count"])
    months = pd.to_datetime(b_comments["published_at"], errors="coerce", utc=True).dt.strftime("%Y-%m")
    return (
        months.dropna().value_counts().reset_index()
        .rename(columns={"published_at": "month", "count": "youtube_count"})
        .sort_values("month").reset_index(drop=True)
    )


def render(db_path: str = DEFAULT_DB_PATH):
    if not os.path.exists(db_path):
        st.info(
            f"No YouTube data found at `{db_path}`. Run `python youtube_scraper.py` "
            "to populate youtube_videos/youtube_comments."
        )
        return

    videos, comments, excluded_videos = load_hk_dashboard_data(db_path)

    if videos.empty:
        st.info("No YouTube videos loaded yet. Run `python youtube_scraper.py` to discover videos.")
        return

    st.markdown(
        '<div class="caveat-box">YouTube comments are unsolicited viewer reactions to a '
        'video, not product reviews — sentiment/purchase-barrier scoring here is comparable '
        'to LIHKG, but video view/like counts are a reach proxy, not a reception signal.</div>',
        unsafe_allow_html=True,
    )

    if not excluded_videos.empty:
        with st.expander(
            f"⚠ {len(excluded_videos)} video(s) excluded — keyword-matched but not actually about the tagged brand"
        ):
            st.dataframe(
                excluded_videos[["brand", "title_en", "channel_title", "url"]].rename(columns={
                    "brand": "Tagged brand", "title_en": "Title", "channel_title": "Channel", "url": "Link",
                }),
                width='stretch', hide_index=True,
                column_config={"Link": st.column_config.LinkColumn("Link", display_text="Open ↗")},
            )

    if videos.empty:
        st.info("No brand-relevant YouTube videos in current data.")
        return

    brands = sorted(videos["brand"].dropna().unique())
    n_videos = len(videos)
    n_comments = len(comments)

    st.caption(
        f"YouTube · HK · {n_videos:,} videos · {n_comments:,} comments collected · {len(brands)} brand(s)"
    )

    if not brands:
        st.info("No videos with a recognized brand tag yet.")
        return

    all_tab, *brand_tabs = st.tabs(["All Brands"] + brands)

    with all_tab:
        c1, c2 = st.columns(2)
        with c1:
            vol_by_brand = videos.groupby("brand").size().reset_index(name="count")
            fig = px.bar(
                vol_by_brand, x="brand", y="count",
                title="Video count by brand",
                labels={"brand": "Brand", "count": "Videos"},
                color="brand", color_discrete_map=BRAND_COLORS,
            )
            fig.update_layout(showlegend=False)
            st.plotly_chart(fig, width='stretch')
        with c2:
            views_by_brand = videos.groupby("brand")["view_count"].sum().reset_index()
            fig = px.bar(
                views_by_brand, x="brand", y="view_count",
                title="Total views by brand",
                labels={"brand": "Brand", "view_count": "Views"},
                color="brand", color_discrete_map=BRAND_COLORS,
            )
            fig.update_layout(showlegend=False)
            st.plotly_chart(fig, width='stretch')
        st.caption("Views summed across all discovered videos per brand — a reach proxy, not unique viewers.")

    for brand, brand_tab in zip(brands, brand_tabs):
        with brand_tab:
            b_videos = videos[videos["brand"] == brand]
            b_comments = comments[comments["brand"] == brand] if not comments.empty else comments

            # Sentiment/barrier metrics count only on-topic comments (is_lens_relevant == 1) —
            # off-topic ones (audience chatter unrelated to the product, e.g. discussing a
            # featured sponsor/celebrity rather than the lenses) would otherwise skew brand
            # sentiment on tangents that have nothing to do with the product. NaN (not yet
            # classified) fails open and counts as on-topic.
            on_topic_mask = b_comments["is_lens_relevant"] != 0 if not b_comments.empty else pd.Series(dtype=bool)
            b_comments_on_topic = b_comments[on_topic_mask] if not b_comments.empty else b_comments
            n_off_topic = len(b_comments) - len(b_comments_on_topic)

            m_cols = st.columns(4)
            m_cols[0].metric("Videos", len(b_videos))
            m_cols[1].metric("Total views", f"{int(b_videos['view_count'].sum()):,}")
            m_cols[2].metric("Total likes", f"{int(b_videos['like_count'].sum()):,}")
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

            st.subheader("Videos")
            b_videos_display = b_videos.assign(
                title_display=b_videos["title_en"].fillna(b_videos["title"])
                if "title_en" in b_videos.columns else b_videos["title"]
            )
            st.dataframe(
                b_videos_display[[
                    "title_display", "channel_title", "keyword", "published_at",
                    "view_count", "like_count", "comment_count", "url",
                ]].rename(columns={
                    "title_display": "Title", "channel_title": "Channel",
                    "keyword": "Keyword", "published_at": "Published",
                    "view_count": "Views", "like_count": "Likes",
                    "comment_count": "Comments", "url": "Link",
                }).sort_values("Views", ascending=False),
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
