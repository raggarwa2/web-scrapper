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


def render(db_path: str = DEFAULT_DB_PATH):
    if not os.path.exists(db_path):
        st.info(
            f"No YouTube data found at `{db_path}`. Run `python youtube_scraper.py` "
            "to populate youtube_videos/youtube_comments."
        )
        return

    mtime = os.path.getmtime(db_path)
    videos, comments = load_youtube_data(db_path, mtime)

    # Rest of the dashboard is HK-only (see market == "HK" filters in app.py) —
    # match that scope here and leave TH data out for now.
    videos = videos[videos["market"] == "HK"] if not videos.empty else videos
    comments = comments[comments["market"] == "HK"] if not comments.empty else comments

    if videos.empty:
        st.info("No YouTube videos loaded yet. Run `python youtube_scraper.py` to discover videos.")
        return

    st.markdown(
        '<div class="caveat-box">YouTube comments are unsolicited viewer reactions to a '
        'video, not product reviews — sentiment/purchase-barrier scoring here is comparable '
        'to LIHKG, but video view/like counts are a reach proxy, not a reception signal.</div>',
        unsafe_allow_html=True,
    )

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

            m_cols = st.columns(4)
            m_cols[0].metric("Videos", len(b_videos))
            m_cols[1].metric("Total views", f"{int(b_videos['view_count'].sum()):,}")
            m_cols[2].metric("Total likes", f"{int(b_videos['like_count'].sum()):,}")
            m_cols[3].metric("Comments collected", len(b_comments))

            sent_counts = b_comments["sentiment"].value_counts() if not b_comments.empty else pd.Series(dtype=int)
            s_cols = st.columns(4)
            s_cols[0].metric("Negative", int(sent_counts.get("negative", 0)))
            s_cols[1].metric("Positive", int(sent_counts.get("positive", 0)))
            s_cols[2].metric("Mixed", int(sent_counts.get("mixed", 0)))
            barrier_n = int(b_comments["is_purchase_barrier_signal"].sum()) if not b_comments.empty else 0
            s_cols[3].metric(
                "Purchase-barrier comments", barrier_n,
                delta=f"{barrier_n / len(b_comments) * 100:.0f}% of comments" if len(b_comments) else None,
                delta_color="off",
            )

            st.subheader("Purchase-barrier comments")
            barrier_comments = b_comments[b_comments["is_purchase_barrier_signal"] == 1] if not b_comments.empty else b_comments
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
                    st.markdown(f"**{row['author']}** · 👍 {row['like_count']} · sentiment: {row['sentiment']}")
                    st.write(row["comment_text_en"] or row["comment_text"])
                    if row["comment_text_en"] and row["comment_text_en"] != row["comment_text"]:
                        st.caption(f"Original: {row['comment_text']}")
                    st.divider()
