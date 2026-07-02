"""
Contact Lens Market Intelligence — Hong Kong
Streamlit dashboard reading from lensdata.db (HKTVmall + 393lens + Sorra + XHS)

Run with:
    streamlit run app.py

By default this looks for the database at output/lensdata.db, relative
to wherever you launch streamlit from. If your project folder is the
standard one, run this from:
    G:\\My Drive\\Business\\AI Native SMB\\Web scrapper\\
and the dashboard will find output/lensdata.db automatically.

If it can't find the file, use the sidebar to point to it manually.
"""

import json
import os
import sqlite3
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

import demand_signals
import lihkg_signals

# ----------------------------------------------------------------------------
# Page config + light styling
# ----------------------------------------------------------------------------

st.set_page_config(
    page_title="Contact Lens Market Intelligence — Hong Kong",
    page_icon="\U0001F441\uFE0F",
    layout="wide",
)

st.markdown(
    """
    <style>
    .block-container {padding-top: 2rem; padding-bottom: 2rem;}
    div[data-testid="stMetric"] {
        background-color: #f8f9fb;
        border: 1px solid #e6e6e6;
        border-radius: 10px;
        padding: 14px 16px 10px 16px;
    }
    div[data-testid="stMetricLabel"] {font-size: 0.85rem; color: #555;}
    .caveat-box {
        background-color: #fff8e6;
        border-left: 4px solid #e8a33d;
        padding: 10px 16px;
        border-radius: 4px;
        margin-bottom: 1rem;
        font-size: 0.9rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

BRAND_COLORS = {
    "Acuvue": "#2563eb",
    "Alcon": "#16a34a",
    "Bausch & Lomb": "#dc2626",
    "CooperVision": "#7c3aed",
    "Olens": "#db2777",
}

_SITES_DISPLAY = "HKTVmall · 393lens · Sorra · Xiaohongshu (customer feedback) · LIHKG (customer signals)"

DEFAULT_DB_PATH = os.path.join("output", "lensdata.db")

# ----------------------------------------------------------------------------
# Data loading
# ----------------------------------------------------------------------------


@st.cache_data(ttl=3600, show_spinner=False)
def load_data(db_path: str):
    """Load all three tables. Cached for an hour (ttl) rather than keyed on
    the .db file's mtime — a background scraper writes to this DB
    continuously, so mtime-keyed caching busted (and re-queried the whole,
    slow DB) on almost every rerun. An hour-old view is an acceptable
    tradeoff for a dashboard that isn't watching live scrape progress."""
    conn = sqlite3.connect(db_path)
    products = pd.read_sql_query("SELECT * FROM products", conn)
    reviews = pd.read_sql_query("SELECT * FROM reviews", conn)
    try:
        xhs = pd.read_sql_query("SELECT * FROM xhs_posts", conn)
    except Exception:
        xhs = pd.DataFrame()
    try:
        xhs_comments = pd.read_sql_query("SELECT * FROM xhs_comments", conn)
    except Exception:
        xhs_comments = pd.DataFrame()
    conn.close()

    def parse_themes(val):
        try:
            return json.loads(val) if val else []
        except Exception:
            return []

    # --- light cleanup ---
    if not reviews.empty:
        reviews["review_date"] = pd.to_datetime(reviews["review_date"], errors="coerce")

    if not xhs.empty:
        xhs["publish_date"] = pd.to_datetime(
            pd.to_numeric(xhs["publish_date"], errors="coerce"), unit="s", errors="coerce"
        )
        xhs["themes_list"] = xhs["themes"].apply(parse_themes)

    if not xhs_comments.empty:
        xhs_comments["themes_list"] = xhs_comments["themes"].apply(parse_themes)

    if not products.empty:
        products["discount_pct"] = 0.0
        mask = (products["original_price"] > 0) & (
            products["original_price"] > products["selling_price"]
        )
        products.loc[mask, "discount_pct"] = (
            (products.loc[mask, "original_price"] - products.loc[mask, "selling_price"])
            / products.loc[mask, "original_price"]
            * 100
        )

    return products, reviews, xhs, xhs_comments


RESEARCH_FILES = {
    "Pricing": "pricing.csv",
    "Distribution": "distribution.csv",
    "Reputation": "reputation.csv",
    "New Product Launches": "new_launches.csv",
    "News & Partnerships": "news_partnerships.csv",
    "Public Listing Signals": "public_listings.csv",
}


@st.cache_data(show_spinner=False)
def load_research(research_dir: str, dir_mtime: float):
    """Load whichever research CSVs exist. Missing or empty files are
    returned as empty DataFrames so the dashboard can show an honest
    'not yet researched' state rather than fabricating anything."""
    out = {}
    for pillar, filename in RESEARCH_FILES.items():
        path = os.path.join(research_dir, filename)
        if os.path.exists(path):
            try:
                df = pd.read_csv(path)
            except Exception:
                df = pd.DataFrame()
        else:
            df = pd.DataFrame()
        out[pillar] = df
    return out


TRIANGULATION_FILES = {
    "Channel Coverage": ("prompt_a_channel_coverage.md", "prompt_a_channel_coverage.csv"),
    "Barrier Matches": ("prompt_b_barrier_matches.md", "prompt_b_barrier_matches.csv"),
    "Attribute Quadrant": ("prompt_c_attribute_quadrant.md", "prompt_c_attribute_quadrant.csv"),
    "Combined Summary": ("prompt_d_triangulation_summary.md", None),
    "Share of Voice": ("share_of_voice.md", "share_of_voice.csv"),
    "Negative Tail": ("negative_tail_analysis.md", "negative_tail_analysis.csv"),
    "Price vs. Sentiment": ("price_sentiment.md", "price_sentiment_tiers.csv"),
}

PRICE_TIER_ORDER = ["Budget", "Mid", "Premium"]

NEGATIVE_TAIL_CATEGORY_ORDER = [
    "product mismatch/counterfeit concern",
    "comfort/dryness",
    "shipping/fulfillment",
    "customer service",
    "packaging",
    "price",
    "quality/defect (other)",
    "unclear / insufficient detail",
    "no complaint / positive feedback",
]
NEGATIVE_TAIL_SMALL_BASE = 15  # per-brand review count below this = directional only
XHS_SMALL_BASE_THRESHOLD = 200  # matches triangulation/share_of_voice.py's XHS_SMALL_BASE_THRESHOLD
# Same rough conceptual links as triangulation/negative_tail_analysis.py's
# COMPLAINT_TO_ATTRIBUTE, used for the live polarization check below.
NEGATIVE_TAIL_TO_ATTRIBUTE = {
    "comfort/dryness": "comfortable for eyes",
    "product mismatch/counterfeit concern": "trustworthy brand",
}
NEGATIVE_TAIL_POLARIZATION_MIN_POSITIVE = 3


@st.cache_data(show_spinner=False)
def load_triangulation(tri_dir: str, dir_mtime: float):
    """Load the triangulation-analysis markdown + CSV outputs (see
    triangulation/run_triangulation.py). Missing files degrade to empty
    so the dashboard works before the analysis has ever been run."""
    out = {}
    for label, (md_name, csv_name) in TRIANGULATION_FILES.items():
        md_path = os.path.join(tri_dir, md_name)
        md_text = ""
        if os.path.exists(md_path):
            try:
                md_text = open(md_path, encoding="utf-8").read()
            except Exception:
                md_text = ""
        csv_df = pd.DataFrame()
        if csv_name and os.path.exists(os.path.join(tri_dir, csv_name)):
            try:
                csv_df = pd.read_csv(os.path.join(tri_dir, csv_name))
            except Exception:
                csv_df = pd.DataFrame()
        out[label] = {"md": md_text, "csv": csv_df, "csv_name": csv_name}
    return out


@st.cache_data(show_spinner=False)
def load_negative_tail_reviews(tri_dir: str, dir_mtime: float) -> pd.DataFrame:
    """One row per HK review (all ratings 1-5) with its complaint category —
    see triangulation/negative_tail_analysis.py. Empty if not yet generated."""
    path = os.path.join(tri_dir, "negative_tail_reviews.csv")
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


@st.cache_data(show_spinner=False)
def load_theme_sentiment(tri_dir: str, dir_mtime: float) -> pd.DataFrame:
    """Per brand x XHS theme tag x sentiment counts — see
    triangulation/share_of_voice.py's compute_theme_sentiment(). Empty if
    not yet generated (older share_of_voice.py runs won't have this file)."""
    path = os.path.join(tri_dir, "share_of_voice_theme_sentiment.csv")
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


@st.cache_data(show_spinner=False)
def load_price_sentiment_brand(tri_dir: str, dir_mtime: float) -> pd.DataFrame:
    """Brand x tier sentiment breakdown — see triangulation/price_sentiment.py.
    Empty if not yet generated."""
    path = os.path.join(tri_dir, "price_sentiment_brand.csv")
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def weighted_rating(df: pd.DataFrame) -> float:
    """Review-volume-weighted average rating, ignoring unrated SKUs."""
    rated = df[df["total_reviews"] > 0]
    if rated.empty or rated["total_reviews"].sum() == 0:
        return float("nan")
    return (rated["avg_rating"] * rated["total_reviews"]).sum() / rated["total_reviews"].sum()


_SITE_DISPLAY_NAMES = {
    "hktvmall":  "HKTVmall",
    "393lens":   "393lens",
    "sorra":     "Sorra",
    "ta_to":     "ta-to",
    "lazada_th": "Lazada TH",
}


def _site_caption(df: pd.DataFrame, date_col: str = None, count_label: str = "items") -> str:
    """Build a 'Site · date range · count' caption string grouped by site."""
    if df.empty or "site" not in df.columns:
        return ""
    parts = []
    for site, grp in df.groupby("site"):
        n = len(grp)
        display = _SITE_DISPLAY_NAMES.get(site, site)
        if date_col and date_col in grp.columns:
            dates = pd.to_datetime(grp[date_col], errors="coerce").dropna()
            if not dates.empty:
                lo = dates.min().strftime("%b %Y")
                hi = dates.max().strftime("%b %Y")
                parts.append(f"{display} · {lo} – {hi} · {n:,} {count_label}")
            else:
                parts.append(f"{display} · {n:,} {count_label}")
        else:
            parts.append(f"{display} · {n:,} {count_label}")
    return "  |  ".join(parts)


# ----------------------------------------------------------------------------
# Sidebar — data source + filters
# ----------------------------------------------------------------------------

st.sidebar.title("Contact Lens Intelligence")
st.sidebar.caption("Hong Kong \u2014 Acuvue, Alcon, Bausch & Lomb, CooperVision, Olens")

db_path = st.sidebar.text_input("Database path", value=DEFAULT_DB_PATH)

if not os.path.exists(db_path):
    st.error(
        f"Can't find a database at `{db_path}`. Update the path in the "
        "sidebar — point it at your output/lensdata.db file."
    )
    st.stop()

mtime = os.path.getmtime(db_path)  # display only now — no longer part of load_data's cache key
products, reviews, xhs, xhs_comments = load_data(db_path)

research_dir = st.sidebar.text_input("Research findings folder", value="research")
if os.path.isdir(research_dir):
    research_mtime = max(
        (os.path.getmtime(os.path.join(research_dir, f)) for f in os.listdir(research_dir)),
        default=0,
    )
else:
    research_mtime = 0
research_data = load_research(research_dir, research_mtime) if os.path.isdir(research_dir) else {
    p: pd.DataFrame() for p in RESEARCH_FILES
}

triangulation_dir = st.sidebar.text_input(
    "Triangulation analysis folder", value=os.path.join("output", "triangulation")
)
if os.path.isdir(triangulation_dir):
    tri_mtime = max(
        (os.path.getmtime(os.path.join(triangulation_dir, f)) for f in os.listdir(triangulation_dir)),
        default=0,
    )
else:
    tri_mtime = 0
triangulation_data = (
    load_triangulation(triangulation_dir, tri_mtime) if os.path.isdir(triangulation_dir)
    else {label: {"md": "", "csv": pd.DataFrame(), "csv_name": c} for label, (_, c) in TRIANGULATION_FILES.items()}
)
negative_tail_reviews = (
    load_negative_tail_reviews(triangulation_dir, tri_mtime) if os.path.isdir(triangulation_dir) else pd.DataFrame()
)
theme_sentiment_data = (
    load_theme_sentiment(triangulation_dir, tri_mtime) if os.path.isdir(triangulation_dir) else pd.DataFrame()
)
price_sentiment_brand = (
    load_price_sentiment_brand(triangulation_dir, tri_mtime) if os.path.isdir(triangulation_dir) else pd.DataFrame()
)

trends_dir = st.sidebar.text_input(
    "Google Trends folder", value=demand_signals.DEFAULT_TRENDS_DIR
)

_HK_PRODUCTS = products[products["market"] == "HK"]
all_brands = sorted(_HK_PRODUCTS["brand"].dropna().unique().tolist())
selected_brands = st.sidebar.multiselect("Brands", all_brands, default=all_brands)

st.sidebar.divider()
st.sidebar.caption(
    f"Database last updated:\n{datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')}"
)
_xhs_attributed = xhs[xhs["brand_mentioned"].notna() & (xhs["brand_mentioned"] != "other")]
st.sidebar.caption(
    f"{len(products)} products \u00b7 {len(reviews)} reviews \u00b7 "
    f"{len(xhs)} XHS posts ({len(_xhs_attributed)} brand-attributed) \u00b7 "
    f"{len(xhs_comments)} XHS comments"
)

products_f = products[
    products["brand"].isin(selected_brands) & (products["market"] == "HK")
]
reviews_f = reviews[
    reviews["brand"].isin(selected_brands) & (reviews["market"] == "HK")
]

# ----------------------------------------------------------------------------
# Header + top-line KPIs
# ----------------------------------------------------------------------------

_currency_sym = "HK$"
_currency_lbl = "HKD"

st.title("Contact Lens Market Intelligence \u2014 Hong Kong")
st.caption(f"{_SITES_DISPLAY} \u00b7 Monthly intelligence brief")

st.markdown("""
<style>
[data-testid="stMetric"] {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 18px 16px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
    min-height: 115px;
    display: flex;
    flex-direction: column;
    justify-content: center;
}
[data-testid="stMetricLabel"] p {
    font-size: 0.70rem !important;
    font-weight: 600 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.07em !important;
    color: #64748b !important;
}
[data-testid="stMetricValue"] {
    font-size: 1.75rem !important;
    font-weight: 700 !important;
    color: #1e293b !important;
}
</style>
""", unsafe_allow_html=True)

# \u2500\u2500 Row 1: review intelligence KPIs \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
ins_cols = st.columns(4)

# 1) Reviews analyzed
ins_cols[0].metric(
    "Reviews analyzed", f"{len(reviews_f):,}",
    help="Total number of reviews matching the current brand / market filter.",
)

# 2) Positive sentiment % with 90-day trend delta
if not reviews_f.empty:
    _rated = reviews_f[reviews_f["rating"] > 0]
    _pos_pct = (_rated["rating"] >= 4).sum() / max(len(_rated), 1) * 100
    _now  = reviews_f["review_date"].dropna().max()
    _cut  = _now  - pd.DateOffset(days=90)
    _prev = _cut  - pd.DateOffset(days=90)
    _recent = reviews_f[reviews_f["review_date"] >= _cut]
    _prior  = reviews_f[(reviews_f["review_date"] >= _prev) & (reviews_f["review_date"] < _cut)]
    _r_pos  = (_recent["rating"] >= 4).mean() * 100 if len(_recent) else float("nan")
    _p_pos  = (_prior["rating"]  >= 4).mean() * 100 if len(_prior)  else float("nan")
    _delta  = round(_r_pos - _p_pos, 1) if pd.notna(_r_pos) and pd.notna(_p_pos) else None
    ins_cols[1].metric(
        "Positive sentiment",
        f"{_pos_pct:.0f}%",
        delta=f"{_delta:+.1f}pp vs prev 90d" if _delta is not None else None,
        help=(
            "% of rated reviews with rating \u2265 4 stars.\n\n"
            "Formula: (reviews with rating \u2265 4) \u00f7 (all rated reviews) \u00d7 100\n\n"
            "Trend (pp): positive % in latest 90 days minus positive % in prior 90 days."
        ),
    )
else:
    ins_cols[1].metric("Positive sentiment", "\u2014",
        help="% of rated reviews with rating \u2265 4 stars.")

# 3) Review leader \u2014 brand with the most reviews
if not reviews_f.empty:
    _brand_counts = reviews_f.groupby("brand").size().sort_values(ascending=False)
    _top_brand = _brand_counts.index[0]
    _top_count = int(_brand_counts.iloc[0])
    ins_cols[2].metric("Review leader", _top_brand, delta=f"{_top_count:,} reviews", delta_color="off",
        help="Brand with the highest review count in the current filter. Delta shows that brand's total reviews.")
else:
    ins_cols[2].metric("Review leader", "\u2014",
        help="Brand with the highest review count in the current filter.")

# 4) Data window
if not reviews_f.empty:
    _dated = reviews_f["review_date"].dropna()
    if not _dated.empty:
        _lo = _dated.min().strftime("%b %Y")
        _hi = _dated.max().strftime("%b %Y")
        ins_cols[3].metric("Data window", f"{_lo} \u2013 {_hi}",
            help="Earliest to latest review date in the current filter.")
    else:
        ins_cols[3].metric("Data window", "\u2014",
            help="Earliest to latest review date in the current filter.")
else:
    ins_cols[3].metric("Data window", "\u2014",
        help="Earliest to latest review date in the current filter.")

# \u2500\u2500 Row 2: product / store KPIs \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
st.write("")
kpi_cols = st.columns(4)

kpi_cols[0].metric("Products tracked", f"{len(products_f):,}",
    help="Distinct product listings in the current filter (Acuvue brand, HK market).")
kpi_cols[1].metric("Stores covered", f"{products_f['store_name'].nunique():,}",
    help="Number of unique store names carrying tracked products in the current filter.")
overall_rating = weighted_rating(products_f)
kpi_cols[2].metric(
    "Weighted avg. rating",
    f"{overall_rating:.2f} \u2605" if pd.notna(overall_rating) else "\u2014",
    help=(
        "Weighted mean rating across all tracked products.\n\n"
        "Formula: \u03a3(rating \u00d7 review_count) \u00f7 \u03a3(review_count)"
    ),
)
kpi_cols[3].metric(
    "Brands monitored",
    f"{reviews_f['brand'].nunique():,}" if not reviews_f.empty else "\u2014",
    help="Number of distinct brands present in the filtered review dataset.",
)

st.divider()

# ----------------------------------------------------------------------------
# Tabs
# ----------------------------------------------------------------------------

# --- Acuvue sub-brand helpers (shared across tabs) ---
_ACUVUE_SUBS = ["Moist", "Oneday", "Define"]
_ACUVUE_SUB_COLORS = {
    "Acuvue – Moist":  "#1d4ed8",
    "Acuvue – Oneday": "#0ea5e9",
    "Acuvue – Define": "#7c3aed",
    "Acuvue – Other":  "#94a3b8",
}
_ACUVUE_SUB_RAW_COLORS = {
    "Moist":  "#1d4ed8",
    "Oneday": "#0ea5e9",
    "Define": "#7c3aed",
    "Other":  "#94a3b8",
}

def _acuvue_subbrand(name):
    if pd.isna(name):
        return "Other"
    n = name.lower()
    if "define" in n:
        return "Define"
    if "moist" in n:
        return "Moist"
    if "1 day" in n or "1-day" in n or "oneday" in n or "one day" in n:
        return "Oneday"
    return "Other"

tab_overview, tab_price, tab_reviews, tab_sentiment, tab_social, tab_lihkg, tab_stores, tab_explorer, tab_research, tab_triangulation, tab_demand, tab_notes = st.tabs(
    [
        "Brand Overview",
        "Price Intelligence",
        "Review Intelligence",
        "Sentiment Intelligence",
        "Customer Feedback (XHS)",
        "Customer Signals (LIHKG)",
        "Store Ranking",
        "Product Explorer",
        "Research Findings",
        "Triangulation Analysis",
        "Demand Signals",
        "Data Notes",
    ]
)

# ---- Brand Overview ---------------------------------------------------------
with tab_overview:
    st.subheader("Brand health scorecard")
    st.caption(_site_caption(reviews_f, "review_date", "reviews"))

    rows = []
    for b in selected_brands:
        bp = products_f[products_f["brand"] == b]
        br = reviews_f[reviews_f["brand"] == b]
        rows.append(
            {
                "Brand": b,
                "Products": len(bp),
                "Stores": bp["store_name"].nunique(),
                "Weighted rating": round(weighted_rating(bp), 2),
                "Reviews collected": len(br),
            }
        )
    scorecard = pd.DataFrame(rows)

    _sc_html = scorecard.to_html(index=False, border=0)
    st.markdown(
        f"""
<style>
.sc-table {{width:100%;border-collapse:collapse;font-size:0.88rem;}}
.sc-table th {{background:#f8f9fb;color:#555;font-weight:600;
               padding:10px 14px;border-bottom:2px solid #e6e6e6;text-align:center;}}
.sc-table td {{padding:9px 14px;border-bottom:1px solid #f0f0f0;text-align:center;}}
.sc-table tr:hover td {{background:#f8f9fb;}}
</style>
{_sc_html.replace('<table', '<table class="sc-table"').replace('<tr>', '<tr>').replace('border="0"', '')}
""",
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns(2)
    with c1:
        fig = px.bar(
            scorecard,
            x="Brand",
            y="Weighted rating",
            color="Brand",
            color_discrete_map=BRAND_COLORS,
            title="Weighted average rating by brand",
            range_y=[0, 5],
        )
        fig.update_layout(showlegend=False)
        st.plotly_chart(fig, width='stretch')
    with c2:
        fig = px.bar(
            scorecard,
            x="Brand",
            y="Reviews collected",
            color="Brand",
            color_discrete_map=BRAND_COLORS,
            title="Reviews collected by brand",
        )
        fig.update_layout(showlegend=False)
        st.plotly_chart(fig, width='stretch')

# ---- Price Intelligence -----------------------------------------------------
with tab_price:
    st.subheader("Price distribution by brand")
    st.caption(_site_caption(products_f, count_label="products"))
    priced_f = products_f[products_f["selling_price"].notna()]

    if "Acuvue" in selected_brands:
        priced_f = priced_f.copy()
        priced_f["_acuvue_sub"] = priced_f.apply(
            lambda r: _acuvue_subbrand(r["name_en"]) if r["brand"] == "Acuvue" else None,
            axis=1,
        )

        st.markdown("**Acuvue sub-brand filter**")
        sel_subs = st.multiselect(
            "Acuvue sub-brands",
            _ACUVUE_SUBS + ["All"],
            default=_ACUVUE_SUBS + ["All"],
            label_visibility="collapsed",
        )
        if not sel_subs:
            sel_subs = _ACUVUE_SUBS + ["All"]

        # Build plot dataframe: non-Acuvue rows + one row-set per selected Acuvue series
        _non_acuvue = priced_f[priced_f["brand"] != "Acuvue"].copy()
        _non_acuvue["_plot_brand"] = _non_acuvue["brand"]
        _acuvue_rows = priced_f[priced_f["brand"] == "Acuvue"].copy()
        _parts = [_non_acuvue]

        if "All" in sel_subs:
            _all = _acuvue_rows.copy()
            _all["_plot_brand"] = "Acuvue – All"
            _parts.append(_all)
        for _sub in _ACUVUE_SUBS:
            if _sub in sel_subs:
                _s = _acuvue_rows[_acuvue_rows["_acuvue_sub"] == _sub].copy()
                _s["_plot_brand"] = f"Acuvue – {_sub}"
                _parts.append(_s)

        priced_f = pd.concat(_parts, ignore_index=True)
        _plot_colors = {
            **BRAND_COLORS,
            "Acuvue – All":    "#2563eb",
            **_ACUVUE_SUB_COLORS,
        }
        _plot_brand_col = "_plot_brand"
    else:
        priced_f = priced_f.copy()
        priced_f["_plot_brand"] = priced_f["brand"]
        _plot_brand_col = "_plot_brand"
        _plot_colors = BRAND_COLORS

    if priced_f.empty:
        st.info("No priced products in current filter.")
    else:
        p95 = priced_f.groupby("brand")["selling_price"].transform(
            lambda s: s.quantile(0.95)
        )
        trimmed = priced_f[priced_f["selling_price"] <= p95]
        excluded_counts = (
            priced_f[priced_f["selling_price"] > p95]
            .groupby("brand")["product_code"].count()
        )

        _acuvue_order = (
            [f"Acuvue – {s}" for s in _ACUVUE_SUBS] + ["Acuvue – All"]
            if "Acuvue" in selected_brands
            else []
        )
        _other_brands = [b for b in sorted(trimmed[_plot_brand_col].unique()) if not b.startswith("Acuvue")]
        _x_order = _acuvue_order + _other_brands

        fig = px.box(
            trimmed,
            x=_plot_brand_col,
            y="selling_price",
            color=_plot_brand_col,
            color_discrete_map=_plot_colors,
            points="outliers",
            category_orders={_plot_brand_col: _x_order},
            labels={"selling_price": f"Selling price ({_currency_lbl})", _plot_brand_col: "Brand"},
            title="Price distribution by brand (capped at 95th percentile per brand)",
        )
        fig.update_layout(showlegend=True if _plot_brand_col == "_plot_brand" and "Acuvue" in selected_brands else False)

        for plot_label in trimmed[_plot_brand_col].unique():
            raw_brand = "Acuvue" if plot_label.startswith("Acuvue –") else plot_label
            n = excluded_counts.get(raw_brand, 0)
            if n > 0:
                fig.add_annotation(
                    x=plot_label,
                    y=trimmed["selling_price"].max() * 1.05,
                    text=f"+{n} SKUs<br>above p95<br>(not shown)",
                    showarrow=False,
                    font=dict(size=10, color="dimgray"),
                )

        st.plotly_chart(fig, width='stretch')
        st.caption(
            "Each brand is capped at its own 95th percentile so specialty/multifocal "
            "SKUs don't dominate the scale. Excluded counts are labeled, not hidden."
        )

        st.subheader("Discounting behaviour by store")
        store_disc = (
            priced_f.groupby(["store_name", "brand"])
            .agg(avg_price=("selling_price", "mean"), avg_discount=("discount_pct", "mean"), n=("product_code", "count"))
            .reset_index()
            .sort_values("avg_discount", ascending=False)
        )
        st.dataframe(
            store_disc.rename(
                columns={
                    "store_name": "Store",
                    "brand": "Brand",
                    "avg_price": f"Avg. price ({_currency_lbl})",
                    "avg_discount": "Avg. discount %",
                    "n": "Products",
                }
            ).round(1),
            width='stretch',
            hide_index=True,
            height=400,
        )
        st.caption(
            "Sorted by deepest average discount. Stores discounting heavily "
            "may be the ones drawing price-sensitive customers away from "
            "full-price listings."
        )

# ---- Review Intelligence -----------------------------------------------------
with tab_reviews:
    st.subheader("Review volume & rating trend")
    st.caption(_site_caption(reviews_f, "review_date", "reviews"))

    if reviews_f.empty:
        st.info("No reviews in current filter.")
    else:
        rv = reviews_f.dropna(subset=["review_date"]).copy()
        rv["month"] = rv["review_date"].dt.to_period("M").astype(str)
        monthly = (
            rv.groupby(["month", "brand"])
            .agg(reviews=("id", "count"), avg_rating=("rating", "mean"))
            .reset_index()
        )

        c1, c2 = st.columns(2)
        with c1:
            fig = px.bar(
                monthly,
                x="month",
                y="reviews",
                color="brand",
                color_discrete_map=BRAND_COLORS,
                title="Review volume by month",
                labels={"reviews": "Reviews", "month": "Month"},
            )
            st.plotly_chart(fig, width='stretch')
        with c2:
            fig = px.line(
                monthly,
                x="month",
                y="avg_rating",
                color="brand",
                color_discrete_map=BRAND_COLORS,
                markers=True,
                title="Average rating by month",
                labels={"avg_rating": "Avg. rating", "month": "Month"},
            )
            fig.update_yaxes(range=[0, 5])
            st.plotly_chart(fig, width='stretch')

        st.subheader("Rating distribution")
        fig = px.histogram(
            reviews_f,
            x="rating",
            color="brand",
            color_discrete_map=BRAND_COLORS,
            barmode="group",
            nbins=5,
        )
        st.plotly_chart(fig, width='stretch')

        st.subheader("Lowest-rated reviews (translated)")
        low = (
            reviews_f[reviews_f["rating"] <= 2]
            .sort_values("rating")
            .loc[:, ["brand", "store_name", "review_date", "rating", "review_text_en"]]
        )
        st.dataframe(
            low.rename(
                columns={
                    "brand": "Brand",
                    "store_name": "Store",
                    "review_date": "Date",
                    "rating": "Rating",
                    "review_text_en": "Review (EN)",
                }
            ),
            width='stretch',
            hide_index=True,
            height=300,
        )

# ---- Sentiment Intelligence -------------------------------------------------
with tab_sentiment:
    st.subheader("Consumer Sentiment Intelligence")
    st.caption(_site_caption(reviews_f, "review_date", "reviews"))
    st.caption("Sentiment is rule-based from rating: 4–5 stars = positive, 3 = neutral, 1–2 = negative.")

    if reviews_f.empty:
        st.info("No reviews in current filter.")
    else:
        sv = reviews_f.dropna(subset=["review_date", "rating"]).copy()
        sv = sv[sv["rating"] > 0]
        sv["sentiment"] = sv["rating"].apply(
            lambda r: "positive" if r >= 4 else ("neutral" if r == 3 else "negative")
        )
        sv["month"] = sv["review_date"].dt.to_period("M").astype(str)

        view = st.radio(
            "View",
            ["📊  All Brands Overview", "🔍  Brand Deep-Dive", "🏷️  Acuvue Sub-brands"],
            horizontal=True,
            label_visibility="collapsed",
        )

        if view == "📊  All Brands Overview":
            min_d = sv["review_date"].min().strftime("%b %Y").upper()
            max_d = sv["review_date"].max().strftime("%b %Y").upper()
            sites_label = " · ".join(
                sorted(_SITE_DISPLAY_NAMES.get(s, s) for s in sv["site"].unique())
            ) if "site" in sv.columns else "All sites"
            st.caption(
                f"{sites_label} · {min_d} – {max_d} · "
                f"{len(sv):,} reviews across {sv['brand'].nunique()} brands"
            )

            grid = st.columns(2)
            for i, brand in enumerate(sorted(sv["brand"].unique())):
                bv = sv[sv["brand"] == brand]
                total = len(bv)
                pos_pct = (bv["sentiment"] == "positive").sum() / total * 100
                neg_pct = (bv["sentiment"] == "negative").sum() / total * 100
                nps = round(
                    (bv["rating"] == 5).sum() / total * 100
                    - (bv["rating"] <= 2).sum() / total * 100
                )

                cutoff = bv["review_date"].max() - pd.DateOffset(months=3)
                prior_cutoff = cutoff - pd.DateOffset(months=3)
                recent_pos = (bv[bv["review_date"] >= cutoff]["sentiment"] == "positive").mean()
                prior_pos = (
                    bv[
                        (bv["review_date"] >= prior_cutoff)
                        & (bv["review_date"] < cutoff)
                    ]["sentiment"]
                    == "positive"
                ).mean()
                delta = (recent_pos - prior_pos) * 100 if pd.notna(recent_pos) and pd.notna(prior_pos) else 0
                trend_icon = "↑" if delta > 2 else ("↓" if delta < -2 else "→")
                trend_color = "#16a34a" if delta > 2 else ("#dc2626" if delta < -2 else "#888888")

                bc = BRAND_COLORS.get(brand, "#2563eb")
                card = f"""
                <div style="background:#f8f9fb;border:1px solid #e6e6e6;border-radius:12px;
                            padding:16px 20px;margin-bottom:14px;">
                  <div style="display:flex;justify-content:space-between;align-items:flex-start;
                              margin-bottom:4px;">
                    <div>
                      <div style="color:{bc};font-weight:700;font-size:0.85rem;
                                  letter-spacing:0.07em;text-transform:uppercase;">{brand}</div>
                      <div style="color:#888;font-size:0.78rem;margin-top:2px;">{total:,} reviews</div>
                    </div>
                    <div style="color:{trend_color};font-size:1.25rem;font-weight:bold;
                                line-height:1;">{trend_icon}</div>
                  </div>
                  <div style="display:flex;justify-content:space-between;
                              margin:10px 0 5px 0;font-size:0.83rem;">
                    <span style="color:#16a34a;font-weight:600;">▲ {pos_pct:.0f}% positive</span>
                    <span style="color:#dc2626;font-weight:600;">▼ {neg_pct:.0f}% negative</span>
                  </div>
                  <div style="background:#e5e7eb;border-radius:4px;height:7px;
                              overflow:hidden;margin-bottom:10px;">
                    <div style="background:linear-gradient(to right,
                                {bc} {pos_pct:.1f}%,
                                #dc2626 {pos_pct:.1f}% {pos_pct+neg_pct:.1f}%,
                                #e5e7eb {pos_pct+neg_pct:.1f}% 100%);
                                height:100%;"></div>
                  </div>
                  <div style="color:#555;font-size:0.78rem;">
                    NPS <b>{nps}</b> &nbsp;·&nbsp; Avg +{pos_pct:.1f}%
                  </div>
                </div>"""
                with grid[i % 2]:
                    st.markdown(card, unsafe_allow_html=True)

        elif view == "🔍  Brand Deep-Dive":
            brands_avail = sorted(sv["brand"].unique())
            sel_brand = st.radio(
                "Brand",
                brands_avail,
                horizontal=True,
                label_visibility="collapsed",
            )

            bv = sv[sv["brand"] == sel_brand]
            total = len(bv)
            pos_pct = (bv["sentiment"] == "positive").sum() / total * 100
            neg_pct = (bv["sentiment"] == "negative").sum() / total * 100
            nps = round(
                (bv["rating"] == 5).sum() / total * 100
                - (bv["rating"] <= 2).sum() / total * 100
            )

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total Reviews", f"{total:,}")
            m2.metric("Avg Positive", f"{pos_pct:.1f}%")
            m3.metric("Avg Negative", f"{neg_pct:.1f}%")
            m4.metric("NPS Score", nps)

            monthly_sent = (
                bv.groupby(["month", "sentiment"])
                .size()
                .reset_index(name="count")
            )
            monthly_total = bv.groupby("month").size().reset_index(name="total")
            monthly_sent = monthly_sent.merge(monthly_total, on="month")
            monthly_sent["pct"] = monthly_sent["count"] / monthly_sent["total"] * 100

            SENT_COLORS = {"positive": "#16a34a", "neutral": "#94a3b8", "negative": "#dc2626"}

            chart_type = st.radio(
                "Chart",
                ["📈  Line", "📊  Bar"],
                horizontal=True,
                label_visibility="collapsed",
            )
            if chart_type == "📈  Line":
                fig = px.line(
                    monthly_sent,
                    x="month", y="pct", color="sentiment",
                    color_discrete_map=SENT_COLORS,
                    markers=True,
                    title=f"{sel_brand} — Monthly Sentiment Breakdown",
                    labels={"pct": "% of reviews", "month": "", "sentiment": "Sentiment"},
                )
            else:
                fig = px.bar(
                    monthly_sent,
                    x="month", y="pct", color="sentiment",
                    color_discrete_map=SENT_COLORS,
                    title=f"{sel_brand} — Monthly Sentiment Breakdown",
                    labels={"pct": "% of reviews", "month": "", "sentiment": "Sentiment"},
                )
                fig.update_layout(barmode="stack")

            fig.update_yaxes(range=[0, 105], ticksuffix="%")
            fig.update_xaxes(tickangle=-45)
            st.plotly_chart(fig, width='stretch')

            st.subheader("Critical reviews (rating ≤ 2)")
            critical = (
                bv[bv["rating"] <= 2]
                .sort_values("review_date", ascending=False)
                [["store_name", "review_date", "rating", "review_text_en"]]
            )
            if critical.empty:
                st.success("No critical reviews for this brand in the current filter.")
            else:
                st.dataframe(
                    critical.rename(columns={
                        "store_name": "Store",
                        "review_date": "Date",
                        "rating": "Rating",
                        "review_text_en": "Review (EN)",
                    }),
                    width='stretch',
                    hide_index=True,
                    height=300,
                )

        elif view == "🏷️  Acuvue Sub-brands":
            acv_sv = sv[sv["brand"] == "Acuvue"].copy()
            if acv_sv.empty:
                st.info("No Acuvue reviews in current filter.")
            else:
                # Join with products to get product name for sub-brand classification
                if "product_code" in acv_sv.columns and "product_code" in products_f.columns:
                    _prod_names = (
                        products_f[products_f["brand"] == "Acuvue"][["product_code", "name_en"]]
                        .drop_duplicates("product_code")
                    )
                    acv_sv = acv_sv.merge(_prod_names, on="product_code", how="left")
                    acv_sv["sub_brand"] = acv_sv["name_en"].apply(_acuvue_subbrand)
                else:
                    acv_sv["sub_brand"] = "Unknown"

                # --- Overview cards for each sub-brand ---
                st.caption(f"Acuvue — {len(acv_sv):,} reviews broken down by sub-brand")
                grid_sub = st.columns(3)
                for i, sub in enumerate(_ACUVUE_SUBS):
                    bv = acv_sv[acv_sv["sub_brand"] == sub]
                    if bv.empty:
                        with grid_sub[i]:
                            st.info(f"{sub}: no reviews")
                        continue
                    total = len(bv)
                    pos_pct = (bv["sentiment"] == "positive").sum() / total * 100
                    neg_pct = (bv["sentiment"] == "negative").sum() / total * 100
                    nps = round(
                        (bv["rating"] == 5).sum() / total * 100
                        - (bv["rating"] <= 2).sum() / total * 100
                    )
                    bc = _ACUVUE_SUB_RAW_COLORS.get(sub, "#2563eb")
                    card = f"""
                    <div style="background:#f8f9fb;border:1px solid #e6e6e6;border-radius:12px;
                                padding:16px 20px;margin-bottom:14px;">
                      <div style="color:{bc};font-weight:700;font-size:0.85rem;
                                  letter-spacing:0.07em;text-transform:uppercase;margin-bottom:4px;">
                        Acuvue – {sub}</div>
                      <div style="color:#888;font-size:0.78rem;margin-bottom:10px;">{total:,} reviews</div>
                      <div style="display:flex;justify-content:space-between;
                                  margin-bottom:5px;font-size:0.83rem;">
                        <span style="color:#16a34a;font-weight:600;">▲ {pos_pct:.0f}% positive</span>
                        <span style="color:#dc2626;font-weight:600;">▼ {neg_pct:.0f}% negative</span>
                      </div>
                      <div style="background:#e5e7eb;border-radius:4px;height:7px;
                                  overflow:hidden;margin-bottom:10px;">
                        <div style="background:linear-gradient(to right,
                                    {bc} {pos_pct:.1f}%,
                                    #dc2626 {pos_pct:.1f}% {pos_pct+neg_pct:.1f}%,
                                    #e5e7eb {pos_pct+neg_pct:.1f}% 100%);
                                    height:100%;"></div>
                      </div>
                      <div style="color:#555;font-size:0.78rem;">NPS <b>{nps}</b></div>
                    </div>"""
                    with grid_sub[i]:
                        st.markdown(card, unsafe_allow_html=True)

                # --- Monthly sentiment trend by sub-brand ---
                st.subheader("Monthly sentiment trend by sub-brand")
                SENT_COLORS_SUB = {"positive": "#16a34a", "neutral": "#94a3b8", "negative": "#dc2626"}
                sub_tabs = st.tabs(_ACUVUE_SUBS)
                for sub_tab, sub in zip(sub_tabs, _ACUVUE_SUBS):
                    with sub_tab:
                        bv = acv_sv[acv_sv["sub_brand"] == sub]
                        if bv.empty:
                            st.info(f"No reviews for Acuvue – {sub}.")
                            continue
                        ms = bv.groupby(["month", "sentiment"]).size().reset_index(name="count")
                        mt = bv.groupby("month").size().reset_index(name="total")
                        ms = ms.merge(mt, on="month")
                        ms["pct"] = ms["count"] / ms["total"] * 100
                        fig = px.bar(
                            ms, x="month", y="pct", color="sentiment",
                            color_discrete_map=SENT_COLORS_SUB,
                            title=f"Acuvue – {sub} · Monthly Sentiment",
                            labels={"pct": "% of reviews", "month": "", "sentiment": "Sentiment"},
                        )
                        fig.update_layout(barmode="stack")
                        fig.update_yaxes(range=[0, 105], ticksuffix="%")
                        fig.update_xaxes(tickangle=-45)
                        st.plotly_chart(fig, width='stretch')

                        st.markdown("**Critical reviews (rating ≤ 2)**")
                        crit = (
                            bv[bv["rating"] <= 2]
                            .sort_values("review_date", ascending=False)
                            [["store_name", "review_date", "rating", "review_text_en"]]
                        )
                        if crit.empty:
                            st.success(f"No critical reviews for Acuvue – {sub}.")
                        else:
                            st.dataframe(
                                crit.rename(columns={
                                    "store_name": "Store", "review_date": "Date",
                                    "rating": "Rating", "review_text_en": "Review (EN)",
                                }),
                                width='stretch', hide_index=True, height=280,
                            )

# ---- Customer Feedback (XHS) ------------------------------------------------
with tab_social:
    if xhs.empty:
        st.info("No XHS data loaded.")
    else:
        xhs_brands = sorted(
            b for b in xhs["brand_mentioned"].dropna().unique() if b != "other"
        )
        xhs_filtered = xhs[xhs["brand_mentioned"].isin(xhs_brands)]

        all_tab, *brand_tabs = st.tabs(["All Brands"] + xhs_brands)

        brand_post_counts = xhs_filtered.groupby("brand_mentioned").size()
        _xhs_min = xhs_filtered["publish_date"].min()
        _xhs_max = xhs_filtered["publish_date"].max()
        _xhs_date_range = (
            f"{_xhs_min.strftime('%b %Y')} – {_xhs_max.strftime('%b %Y')}"
            if pd.notna(_xhs_min) and pd.notna(_xhs_max) else "date range unknown"
        )
        _xhs_summary = (
            f"Xiaohongshu · {_xhs_date_range} · "
            f"{len(xhs_filtered):,} posts across {len(xhs_brands)} brands"
        )

        with all_tab:
            st.caption(_xhs_summary)

            c1, c2 = st.columns([1, 2])
            with c1:
                vol_by_brand = (
                    xhs_filtered.groupby(["brand_mentioned", "sentiment"])
                    .size()
                    .reset_index(name="count")
                )
                fig = px.bar(
                    vol_by_brand,
                    x="brand_mentioned",
                    y="count",
                    color="sentiment",
                    barmode="stack",
                    color_discrete_map={"positive": "#16a34a", "neutral": "#94a3b8", "negative": "#dc2626"},
                    title="Post volume & sentiment by brand",
                    labels={"brand_mentioned": "Brand", "count": "Posts"},
                )
                st.plotly_chart(fig, width='stretch')
            with c2:
                sentiment_pct = (
                    xhs_filtered.groupby(["brand_mentioned", "sentiment"])
                    .size()
                    .reset_index(name="count")
                )
                totals = sentiment_pct.groupby("brand_mentioned")["count"].transform("sum")
                sentiment_pct["pct"] = (sentiment_pct["count"] / totals * 100).round(1)
                fig = px.bar(
                    sentiment_pct,
                    x="brand_mentioned",
                    y="pct",
                    color="sentiment",
                    barmode="stack",
                    color_discrete_map={"positive": "#16a34a", "neutral": "#94a3b8", "negative": "#dc2626"},
                    title="Sentiment share by brand (%)",
                    labels={"brand_mentioned": "Brand", "pct": "%"},
                )
                fig.update_layout(yaxis_range=[0, 100])
                st.plotly_chart(fig, width='stretch')

            theme_brand = (
                xhs_filtered.explode("themes_list")
                .groupby(["brand_mentioned", "themes_list"])
                .size()
                .reset_index(name="count")
            )
            theme_order = (
                theme_brand.groupby("themes_list")["count"].sum()
                .sort_values(ascending=False)
                .head(15)
                .index
            )
            theme_brand = theme_brand[theme_brand["themes_list"].isin(theme_order)]
            fig = px.bar(
                theme_brand,
                x="count",
                y="themes_list",
                color="brand_mentioned",
                orientation="h",
                category_orders={"themes_list": list(reversed(list(theme_order)))},
                title="Top 15 themes across all brands",
                labels={"themes_list": "Theme", "count": "Mentions", "brand_mentioned": "Brand"},
            )
            fig.update_layout(barmode="stack")
            st.plotly_chart(fig, width='stretch')

            # ── Insight 1: Sentiment divergence (All Brands) ──────────────────
            if not xhs_comments.empty:
                _cmt_branded = xhs_comments.merge(
                    xhs_filtered[["post_id", "brand_mentioned"]].drop_duplicates(),
                    on="post_id", how="inner",
                )
                _sent_colors = {"positive": "#16a34a", "neutral": "#94a3b8", "negative": "#dc2626"}

                st.subheader("Insight — Post vs Comment sentiment divergence")
                st.caption(
                    "A large gap between post positivity and comment positivity signals "
                    "that the audience disagrees with the creator — a key authenticity flag."
                )

                _post_pos_pct = (
                    xhs_filtered.groupby("brand_mentioned")
                    .apply(lambda g: round((g["sentiment"] == "positive").mean() * 100, 1))
                    .rename("Post positive %")
                )
                _cmt_pos_pct = (
                    _cmt_branded.groupby("brand_mentioned")
                    .apply(lambda g: round((g["sentiment"] == "positive").mean() * 100, 1))
                    .rename("Comment positive %")
                )
                _div_df = pd.concat([_post_pos_pct, _cmt_pos_pct], axis=1).reset_index()
                _div_df["Divergence (pp)"] = (
                    _div_df["Post positive %"] - _div_df["Comment positive %"]
                ).round(1)
                _div_df = _div_df.sort_values("Divergence (pp)", ascending=False)

                _div_melt = _div_df.melt(
                    id_vars="brand_mentioned",
                    value_vars=["Post positive %", "Comment positive %"],
                    var_name="Source", value_name="Positive %",
                )

                c1, c2 = st.columns([2, 1])
                with c1:
                    fig = px.bar(
                        _div_melt, x="brand_mentioned", y="Positive %", color="Source",
                        barmode="group",
                        color_discrete_map={"Post positive %": "#2563eb", "Comment positive %": "#f97316"},
                        title="Positive sentiment: Posts vs Comments (%)",
                        labels={"brand_mentioned": "Brand"},
                    )
                    fig.update_yaxes(range=[0, 100], ticksuffix="%")
                    st.plotly_chart(fig, width='stretch')
                with c2:
                    st.markdown("**Divergence score by brand**")
                    st.caption("Posts positive % minus Comments positive %. Red = audience more negative than posts suggest.")
                    for _, row in _div_df.iterrows():
                        div = row["Divergence (pp)"]
                        color = "#dc2626" if div > 15 else ("#e8a33d" if div > 5 else "#16a34a")
                        icon  = "⚠️" if div > 15 else ("△" if div > 5 else "✓")
                        st.markdown(
                            f"<div style='padding:8px 12px;margin-bottom:6px;border-radius:8px;"
                            f"background:#f8f9fb;border-left:4px solid {color};'>"
                            f"<b>{row['brand_mentioned']}</b><br>"
                            f"<span style='color:{color};font-size:1.1rem;font-weight:700;'>"
                            f"{icon} {div:+.1f} pp</span>"
                            f"<span style='color:#888;font-size:0.78rem;'> divergence</span></div>",
                            unsafe_allow_html=True,
                        )

                # ── Insight 3: Authenticity flags (All Brands) ────────────────
                st.subheader("Insight — Authenticity risk flags")
                st.caption(
                    "Posts with positive sentiment where ≥50% of comments are negative "
                    "— possible sponsored content or community disagreement."
                )

                _neg_likes_by_post = (
                    _cmt_branded[_cmt_branded["sentiment"] == "negative"]
                    .groupby("post_id")["likes"].sum()
                    .rename("neg_comment_likes")
                )
                _cmt_stats = (
                    _cmt_branded.groupby(["post_id", "brand_mentioned"])
                    .agg(total_comments=("comment_id", "count"),
                         negative_pct=("sentiment", lambda x: round((x == "negative").mean() * 100, 1)))
                    .reset_index()
                    .merge(_neg_likes_by_post, on="post_id", how="left")
                )
                _cmt_stats["neg_comment_likes"] = _cmt_stats["neg_comment_likes"].fillna(0).astype(int)

                _flagged = (
                    xhs_filtered[xhs_filtered["sentiment"] == "positive"]
                    .merge(
                        _cmt_stats[
                            (_cmt_stats["total_comments"] >= 2) &
                            (_cmt_stats["negative_pct"] >= 50)
                        ],
                        on=["post_id", "brand_mentioned"],
                    )
                    .sort_values("neg_comment_likes", ascending=False)
                )

                if _flagged.empty:
                    st.success("No authenticity risk flags detected across all brands.")
                else:
                    st.warning(f"{len(_flagged)} post(s) flagged across all brands.")
                    st.dataframe(
                        _flagged[[
                            "brand_mentioned", "content_en", "likes",
                            "total_comments", "negative_pct", "neg_comment_likes", "url",
                        ]].rename(columns={
                            "brand_mentioned": "Brand",
                            "content_en": "Post content (EN)",
                            "likes": "Post likes",
                            "total_comments": "Comments",
                            "negative_pct": "Neg comment %",
                            "neg_comment_likes": "Neg comment likes",
                            "url": "Link",
                        }),
                        column_config={"Link": st.column_config.LinkColumn("Link", display_text="Open ↗")},
                        width='stretch',
                        hide_index=True,
                        height=350,
                    )

        _acuvue_subproducts = {
            "Moist": "moist",
            "OneDay": r"1-day|1day|one day|oneday",
            "Define": "define",
        }

        for brand_tab, brand in zip(brand_tabs, xhs_brands):
            with brand_tab:
                xhs_b = xhs[xhs["brand_mentioned"] == brand]

                if brand == "Acuvue":
                    selected_subs = st.multiselect(
                        "Sub-product",
                        list(_acuvue_subproducts.keys()),
                        placeholder="All sub-products",
                        label_visibility="collapsed",
                        key=f"xhs_sub_{brand}",
                    )
                    if selected_subs:
                        combined_kw = "|".join(_acuvue_subproducts[s] for s in selected_subs)
                        xhs_b = xhs_b[
                            xhs_b["content_en"].str.contains(combined_kw, case=False, na=False, regex=True)
                        ]

                st.caption(f"Xiaohongshu · {_xhs_date_range} · {len(xhs_b):,} posts")

                c1, c2 = st.columns([1, 2])
                with c1:
                    sent_counts = xhs_b["sentiment"].value_counts().reset_index()
                    sent_counts.columns = ["sentiment", "count"]
                    fig = px.pie(
                        sent_counts,
                        names="sentiment",
                        values="count",
                        title=f"Sentiment breakdown ({brand})",
                        color="sentiment",
                        color_discrete_map={"positive": "#16a34a", "neutral": "#94a3b8", "negative": "#dc2626"},
                    )
                    st.plotly_chart(fig, width='stretch')
                with c2:
                    theme_sentiment = (
                        xhs_b.explode("themes_list")
                        .groupby(["themes_list", "sentiment"])
                        .size()
                        .reset_index(name="count")
                    )
                    theme_order = (
                        theme_sentiment.groupby("themes_list")["count"].sum()
                        .sort_values()
                        .index
                    )
                    fig = px.bar(
                        theme_sentiment,
                        x="count",
                        y="themes_list",
                        color="sentiment",
                        orientation="h",
                        category_orders={"themes_list": list(theme_order)},
                        color_discrete_map={
                            "positive": "#16a34a",
                            "neutral": "#94a3b8",
                            "negative": "#dc2626",
                        },
                        title="Most discussed themes, by sentiment",
                        labels={"themes_list": "Theme", "count": "Mentions"},
                    )
                    fig.update_layout(barmode="stack")
                    st.plotly_chart(fig, width='stretch')

                st.subheader("Most-engaged posts")
                top_posts = xhs_b.sort_values("likes", ascending=False).head(10)
                st.dataframe(
                    top_posts.loc[:, ["sentiment", "themes", "content_en", "likes", "publish_date"]].rename(
                        columns={
                            "sentiment": "Sentiment",
                            "themes": "Themes",
                            "content_en": "Content (EN)",
                            "likes": "Likes",
                            "publish_date": "Date",
                        }
                    ),
                    width='stretch',
                    hide_index=True,
                    height=350,
                )


                # \u2500\u2500 Comments section \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
                st.subheader("Comments")
                _cmt_b = (
                    xhs_comments[xhs_comments["post_id"].isin(xhs_b["post_id"])]
                    if not xhs_comments.empty else pd.DataFrame()
                )
                if _cmt_b.empty:
                    st.caption("No comments collected yet for this brand.")
                else:
                    st.caption(f"{len(_cmt_b):,} comments collected across {_cmt_b['post_id'].nunique():,} posts")
                    _sent_colors = {"positive": "#16a34a", "neutral": "#94a3b8", "negative": "#dc2626"}

                    ca, cb = st.columns(2)
                    with ca:
                        _cs = _cmt_b["sentiment"].value_counts().reset_index()
                        _cs.columns = ["sentiment", "count"]
                        fig = px.pie(
                            _cs, names="sentiment", values="count",
                            title="Comment sentiment",
                            color="sentiment", color_discrete_map=_sent_colors,
                        )
                        st.plotly_chart(fig, width='stretch')
                    with cb:
                        _ct = (
                            _cmt_b.explode("themes_list")
                            .groupby(["themes_list", "sentiment"])
                            .size().reset_index(name="count")
                        )
                        _ct_order = (
                            _ct.groupby("themes_list")["count"].sum()
                            .sort_values().index
                        )
                        fig = px.bar(
                            _ct, x="count", y="themes_list", color="sentiment",
                            orientation="h",
                            category_orders={"themes_list": list(_ct_order)},
                            color_discrete_map=_sent_colors,
                            title="Comment themes by sentiment",
                            labels={"themes_list": "Theme", "count": "Comments"},
                        )
                        fig.update_layout(barmode="stack")
                        st.plotly_chart(fig, width='stretch')

                    # ── Divergence metric (per brand) ────────────────────────
                    _b_post_pos = round((xhs_b["sentiment"] == "positive").mean() * 100, 1)
                    _b_cmt_pos  = round((_cmt_b["sentiment"] == "positive").mean() * 100, 1)
                    _b_div      = round(_b_post_pos - _b_cmt_pos, 1)
                    _b_color    = "#dc2626" if _b_div > 15 else ("#e8a33d" if _b_div > 5 else "#16a34a")
                    _b_icon     = "⚠️ High divergence" if _b_div > 15 else ("△ Moderate" if _b_div > 5 else "✓ Aligned")
                    d1, d2, d3 = st.columns(3)
                    d1.metric("Post positive %", f"{_b_post_pos:.1f}%")
                    d2.metric("Comment positive %", f"{_b_cmt_pos:.1f}%")
                    d3.metric("Divergence", f"{_b_div:+.1f} pp", help="Post positive % minus comment positive %. Large positive gap = audience more negative than posts suggest.")
                    st.markdown(
                        f"<div style='padding:8px 14px;border-radius:8px;border-left:4px solid {_b_color};"
                        f"background:#f8f9fb;margin-bottom:12px;font-size:0.88rem;color:{_b_color};font-weight:600;'>"
                        f"{_b_icon}</div>",
                        unsafe_allow_html=True,
                    )

                    # ── Authenticity flags (per brand) ───────────────────────
                    _b_neg_likes = (
                        _cmt_b[_cmt_b["sentiment"] == "negative"]
                        .groupby("post_id")["likes"].sum()
                        .rename("neg_comment_likes")
                    )
                    _b_cmt_stats = (
                        _cmt_b.groupby("post_id")
                        .agg(total_comments=("comment_id", "count"),
                             negative_pct=("sentiment", lambda x: round((x == "negative").mean() * 100, 1)))
                        .reset_index()
                        .merge(_b_neg_likes, on="post_id", how="left")
                    )
                    _b_cmt_stats["neg_comment_likes"] = _b_cmt_stats["neg_comment_likes"].fillna(0).astype(int)
                    _b_flagged = (
                        xhs_b[xhs_b["sentiment"] == "positive"]
                        .merge(
                            _b_cmt_stats[
                                (_b_cmt_stats["total_comments"] >= 2) &
                                (_b_cmt_stats["negative_pct"] >= 50)
                            ],
                            on="post_id",
                        )
                        .sort_values("neg_comment_likes", ascending=False)
                    )
                    if not _b_flagged.empty:
                        st.markdown(f"**⚠️ Authenticity risk flags — {len(_b_flagged)} post(s)**")
                        st.caption("Positive posts where ≥50% of comments are negative.")
                        st.dataframe(
                            _b_flagged[[
                                "content_en", "likes", "total_comments",
                                "negative_pct", "neg_comment_likes", "url",
                            ]].rename(columns={
                                "content_en": "Post content (EN)",
                                "likes": "Post likes",
                                "total_comments": "Comments",
                                "negative_pct": "Neg comment %",
                                "neg_comment_likes": "Neg comment likes",
                                "url": "Link",
                            }),
                            column_config={"Link": st.column_config.LinkColumn("Link", display_text="Open ↗")},
                            width='stretch', hide_index=True, height=250,
                        )

                    st.markdown("**Most-liked comments**")
                    st.dataframe(
                        _cmt_b.sort_values("likes", ascending=False)
                        .head(20)
                        [["author", "content_en", "sentiment", "themes", "likes"]]
                        .rename(columns={
                            "author": "Author",
                            "content_en": "Comment (EN)",
                            "sentiment": "Sentiment",
                            "themes": "Themes",
                            "likes": "Likes",
                        }),
                        width='stretch',
                        hide_index=True,
                        height=350,
                    )

                    st.markdown("**Negative comments**")
                    _neg_cmt = _cmt_b[_cmt_b["sentiment"] == "negative"].sort_values("likes", ascending=False)
                    if _neg_cmt.empty:
                        st.caption("No negative comments found.")
                    else:
                        st.dataframe(
                            _neg_cmt.head(20)
                            [["author", "content_en", "themes", "likes"]]
                            .rename(columns={
                                "author": "Author",
                                "content_en": "Comment (EN)",
                                "themes": "Themes",
                                "likes": "Likes",
                            }),
                            width='stretch',
                            hide_index=True,
                            height=280,
                        )

                st.subheader("Negative-sentiment posts")
                neg = xhs_b[xhs_b["sentiment"] == "negative"]
                if neg.empty:
                    st.caption("No negative-sentiment posts found in current data.")
                else:
                    for _, row in neg.iterrows():
                        st.markdown(f"**Themes:** {row['themes']}")
                        st.write(row["content_en"])
                        st.caption(f"{row['likes']} likes · {row['publish_date']}")
                        st.divider()

# ---- Customer Signals (LIHKG) -------------------------------------------------
with tab_lihkg:
    lihkg_df = lihkg_signals.load_lihkg_posts(db_path, mtime)

    if lihkg_df.empty:
        st.info("No LIHKG data loaded. Run `python lihkg_scraper.py` to populate lihkg_search_results/lihkg_posts.")
    else:
        st.markdown(
            '<div class="caveat-box">LIHKG posts are unsolicited forum chatter, not '
            'product reviews — there\'s no star rating, and post age is only a relative '
            'string (e.g. "11 個月前"), so this source cannot be time-windowed or charted '
            'as a trend the way Reviews/XHS are. Treat it as a snapshot, not a series.</div>',
            unsafe_allow_html=True,
        )

        lihkg_exploded = lihkg_signals.brand_exploded(lihkg_df)
        lihkg_brands = sorted(lihkg_exploded["mentioned_brands_list"].unique())

        n_threads = lihkg_df["thread_url"].nunique()
        n_posts = len(lihkg_df)
        n_keywords = lihkg_df["keyword"].nunique()
        n_signal = int((lihkg_df["mentioned_brands_list"].str.len() > 0).sum())
        n_signal_pct = (n_signal / n_posts * 100) if n_posts else 0

        st.caption(f"LIHKG · {n_posts:,} posts across {n_threads:,} threads · {n_keywords} keyword(s) searched")
        st.markdown(
            f"**{n_signal:,} of {n_posts:,} posts ({n_signal_pct:.0f}%) carry a recognized brand mention "
            f"and are used as signal** in the charts and per-brand tabs below. The remaining "
            f"{n_posts - n_signal:,} posts were retrieved by keyword search but don't reference any of "
            f"the 5 tracked brands — general forum chatter, or a keyword collision (see the flagged "
            f"threads below and the Data Notes tab)."
        )

        collisions = lihkg_df[lihkg_df["likely_collision"]].drop_duplicates(subset="thread_url")
        if not collisions.empty:
            with st.expander(f"⚠ {len(collisions)} thread(s) flagged as likely keyword collisions, not brand mentions"):
                st.caption(
                    "These threads matched a brand search keyword but fall in a category "
                    "(汽車台/cars, 體育台/sports) where a genuine contact-lens discussion is "
                    "very unlikely — e.g. 'Alcon' also matches a car brake-caliper brand. "
                    "Included below for transparency; check mentioned_brands per post before citing."
                )
                st.dataframe(
                    collisions[["keyword", "category", "thread_title", "thread_url"]].rename(columns={
                        "keyword": "Keyword", "category": "Category",
                        "thread_title": "Thread", "thread_url": "URL",
                    }),
                    width='stretch', hide_index=True,
                )

        if not lihkg_brands:
            st.info("No posts with a recognized brand mention yet.")
        else:
            all_tab, *brand_tabs = st.tabs(["All Brands"] + lihkg_brands)

            with all_tab:
                c1, c2 = st.columns(2)
                with c1:
                    vol_by_brand = (
                        lihkg_exploded.groupby(["mentioned_brands_list", "sentiment"])
                        .size().reset_index(name="count")
                    )
                    fig = px.bar(
                        vol_by_brand, x="mentioned_brands_list", y="count", color="sentiment",
                        barmode="stack",
                        color_discrete_map={"positive": "#16a34a", "neutral": "#94a3b8", "negative": "#dc2626", "mixed": "#e8a33d"},
                        title="Post volume & sentiment by brand",
                        labels={"mentioned_brands_list": "Brand", "count": "Posts"},
                    )
                    st.plotly_chart(fig, width='stretch')
                with c2:
                    barrier = lihkg_signals.purchase_barrier_rate(lihkg_exploded)
                    fig = px.bar(
                        barrier, x="brand", y="barrier_rate",
                        title="Purchase-barrier signal rate by brand (%)",
                        labels={"brand": "Brand", "barrier_rate": "% of posts"},
                        color="brand", color_discrete_map=BRAND_COLORS,
                    )
                    fig.update_layout(showlegend=False)
                    st.plotly_chart(fig, width='stretch')
                st.caption(
                    "Purchase-barrier signal = the post states a reason for not buying/switching "
                    "(price, comfort, trust, availability, habit, etc.)."
                )

            for brand, brand_tab in zip(lihkg_brands, brand_tabs):
                with brand_tab:
                    b_posts = lihkg_exploded[lihkg_exploded["mentioned_brands_list"] == brand]
                    st.caption(f"{len(b_posts)} posts mentioning {brand}")

                    sent_counts = b_posts["sentiment"].value_counts()
                    m_cols = st.columns(4)
                    m_cols[0].metric("Posts", len(b_posts))
                    m_cols[1].metric("Negative", int(sent_counts.get("negative", 0)))
                    m_cols[2].metric("Positive", int(sent_counts.get("positive", 0)))
                    barrier_n = int(b_posts["is_purchase_barrier_signal"].sum())
                    m_cols[3].metric("Purchase-barrier posts", barrier_n,
                        delta=f"{barrier_n / len(b_posts) * 100:.0f}% of posts" if len(b_posts) else None,
                        delta_color="off")

                    st.subheader("Purchase-barrier posts")
                    barrier_posts = b_posts[b_posts["is_purchase_barrier_signal"] == 1]
                    if barrier_posts.empty:
                        st.caption("None flagged for this brand in current data.")
                    else:
                        for _, row in barrier_posts.iterrows():
                            st.markdown(f"**{row['thread_title']}** · {row['category']} · sentiment: {row['sentiment']}")
                            st.write(row["text_english"])
                            st.caption(f"↑{row['upvotes']} ↓{row['downvotes']} · {row['thread_url']}")
                            st.divider()

# ---- Store Ranking ------------------------------------------------------------
with tab_stores:
    st.subheader("Store ranking by brand")
    st.caption(_site_caption(products_f, count_label="products"))
    st.caption("Ranked by review-volume-weighted average rating. Stores with fewer than 5 reviews are flagged as low-confidence.")

    rated = products_f[products_f["total_reviews"] > 0].copy()
    if rated.empty:
        st.info("No rated products in current filter.")
    else:
        store_rank = (
            rated.groupby(["store_name", "brand"])
            .apply(
                lambda g: pd.Series(
                    {
                        "products": len(g),
                        "total_reviews": g["total_reviews"].sum(),
                        "weighted_rating": (g["avg_rating"] * g["total_reviews"]).sum()
                        / g["total_reviews"].sum(),
                    }
                )
            )
            .reset_index()
            .sort_values(["brand", "weighted_rating"], ascending=[True, False])
        )
        store_rank["confidence"] = store_rank["total_reviews"].apply(
            lambda n: "Low (<5 reviews)" if n < 5 else "OK"
        )

        for b in selected_brands:
            st.markdown(f"**{b}**")
            bsr = store_rank[store_rank["brand"] == b].drop(columns=["brand"])
            st.dataframe(
                bsr.rename(
                    columns={
                        "store_name": "Store",
                        "products": "Products",
                        "total_reviews": "Reviews",
                        "weighted_rating": "Weighted rating",
                        "confidence": "Confidence",
                    }
                ).round(2),
                width='stretch',
                hide_index=True,
            )

# ---- Product Explorer -------------------------------------------------------
with tab_explorer:
    st.subheader("Product Explorer")
    st.caption(_site_caption(products_f, count_label="products"))

    _CAT_TABS = [
        "All", "Daily", "Monthly", "Color / Cosmetic",
        "Toric (Astigmatism)", "Multifocal", "Eye Care & Solution",
    ]
    cat_tabs = st.tabs(_CAT_TABS)

    search = st.text_input("Search brand or product", "")
    sort_choice = st.selectbox(
        "Sort by", ["Rating", "Reviews", "Price: low to high", "Price: high to low"]
    )

    explorer_df = products_f.copy()
    if search:
        mask = (
            explorer_df["name_en"].str.contains(search, case=False, na=False)
            | explorer_df["brand"].str.contains(search, case=False, na=False)
        )
        explorer_df = explorer_df[mask]

    sort_map = {
        "Rating": ("avg_rating", False),
        "Reviews": ("total_reviews", False),
        "Price: low to high": ("selling_price", True),
        "Price: high to low": ("selling_price", False),
    }
    field, asc = sort_map[sort_choice]
    explorer_df = explorer_df.sort_values(field, ascending=asc, na_position="last")

    explorer_df["discount_pct"] = (
        (explorer_df["original_price"] - explorer_df["selling_price"])
        / explorer_df["original_price"]
        * 100
    ).round(1)

    _DISPLAY_COLS = {
        "name_en": "Product",
        "brand": "Brand",
        "category": "Category",
        "store_name": "Store",
        "avg_rating": "Rating",
        "total_reviews": "Reviews",
        "selling_price": "Price (HKD)",
        "discount_pct": "Discount %",
        "url": "Link",
    }

    _COL_CONFIG = {
        "Link": st.column_config.LinkColumn("Link", display_text="Open ↗"),
    }

    for _tab_widget, _cat_label in zip(cat_tabs, _CAT_TABS):
        with _tab_widget:
            if _cat_label == "All":
                _view = explorer_df
            else:
                _view = explorer_df[explorer_df["category"] == _cat_label]
            st.dataframe(
                _view[list(_DISPLAY_COLS.keys())].rename(columns=_DISPLAY_COLS).round(1),
                column_config=_COL_CONFIG,
                width='stretch',
                hide_index=True,
                height=600,
            )

# ---- Research Findings ------------------------------------------------------
with tab_research:
    st.subheader("Local intelligence — Deep Research findings")
    st.caption(
        "From the six-pillar local-only framework (Hong Kong). Each "
        "pillar below is empty until you run its Deep Research prompt and "
        "fill in the matching CSV in the research/ folder."
    )

    total_rows = sum(len(df) for df in research_data.values())
    total_validated = sum(
        (df["geo_validated"].astype(str).str.lower().isin(["yes", "y", "true", "1"])).sum()
        if "geo_validated" in df.columns and not df.empty
        else 0
        for df in research_data.values()
    )
    total_failed = total_rows - total_validated

    a1, a2, a3 = st.columns(3)
    a1.metric("Total findings loaded", f"{total_rows:,}")
    a2.metric("Passed geo-validation", f"{total_validated:,}")
    a3.metric(
        "Failed / unvalidated",
        f"{total_failed:,}",
        delta=None if total_failed == 0 else "review these",
        delta_color="inverse",
    )

    if total_rows == 0:
        st.info(
            "No research findings loaded yet. Point the sidebar 'Research "
            "findings folder' at your filled-in CSVs, or run the Deep "
            "Research prompts first \u2014 see research/README.md."
        )

    for pillar, df in research_data.items():
        with st.expander(f"{pillar}  \u2014  {len(df)} findings", expanded=(len(df) > 0)):
            if df.empty:
                st.caption("Not yet researched.")
                continue

            if "geo_validated" in df.columns:
                failed = df[
                    ~df["geo_validated"].astype(str).str.lower().isin(["yes", "y", "true", "1"])
                ]
                if not failed.empty:
                    st.warning(
                        f"{len(failed)} row(s) in this pillar failed geo-validation "
                        "and should be reviewed or removed before this goes to the client."
                    )

            display_df = df.copy()
            if "source_url" in display_df.columns:
                display_df["source_url"] = display_df["source_url"].apply(
                    lambda u: f"[link]({u})" if isinstance(u, str) and u.startswith("http") else u
                )
            st.dataframe(display_df, width="stretch", hide_index=True)

# ---- Triangulation Analysis --------------------------------------------------
with tab_triangulation:
    st.subheader("Triangulation vs. research-agency framework")
    st.caption(
        "Checks our own scraped review/social data against the client's research-agency "
        "slides (channel taxonomy, switching barriers, attribute-importance quadrant) "
        "described in insight.txt. External data only. Regenerate via "
        "`python triangulation/run_triangulation.py` — see triangulation/README.md."
    )
    if tri_mtime:
        st.caption(f"Last generated: {datetime.fromtimestamp(tri_mtime).strftime('%Y-%m-%d %H:%M')}")

    if not any(v["md"] for v in triangulation_data.values()):
        st.info(
            "No triangulation output found yet. Run `python triangulation/run_triangulation.py` "
            "from the project root, or point the sidebar 'Triangulation analysis folder' at "
            "existing output."
        )
    else:
        sub_tabs = st.tabs(list(TRIANGULATION_FILES.keys()))
        for sub_tab, (label, content) in zip(sub_tabs, triangulation_data.items()):
            with sub_tab:
                if not content["md"]:
                    st.caption("Not yet generated.")
                    continue

                if label == "Share of Voice" and not content["csv"].empty:
                    sov = content["csv"]
                    max_axis = max(sov["pct_products"].max(), sov["pct_xhs_posts"].max()) * 1.15
                    fig = px.scatter(
                        sov,
                        x="pct_products",
                        y="pct_xhs_posts",
                        size="review_count",
                        color="brand",
                        color_discrete_map=BRAND_COLORS,
                        text="brand",
                        size_max=60,
                        labels={
                            "pct_products": "% of product catalog",
                            "pct_xhs_posts": "Share of voice (% of brand-attributed XHS posts)",
                        },
                    )
                    fig.add_shape(
                        type="line", x0=0, y0=0, x1=max_axis, y1=max_axis,
                        line=dict(color="#94a3b8", dash="dash"),
                    )
                    fig.update_traces(textposition="top center", cliponaxis=False)
                    fig.update_layout(
                        xaxis_range=[0, max_axis], yaxis_range=[0, max_axis],
                        height=520, showlegend=False,
                    )
                    st.plotly_chart(fig, width="stretch")
                    st.caption(
                        "Bubble size = review count. Above the dashed line = overindexed on social "
                        "relative to shelf; below = underindexed."
                    )

                    if "net_sentiment_pct" in sov.columns:
                        st.divider()
                        st.markdown("**Volume vs. sentiment** — is high social voice backed by positive sentiment, or just loud?")
                        max_x2 = sov["pct_products"].max() * 1.15
                        y_span = max(abs(sov["net_sentiment_pct"].min()), abs(sov["net_sentiment_pct"].max()), 10) * 1.15
                        if "xhs_small_base_flag" in sov.columns:
                            sov = sov.copy()
                            sov["bubble_label"] = sov.apply(
                                lambda r: f"{r['brand']} ⚠ only {r['xhs_post_count']} posts"
                                if r["xhs_small_base_flag"] else r["brand"],
                                axis=1,
                            )
                        else:
                            sov["bubble_label"] = sov["brand"]
                        fig2 = px.scatter(
                            sov,
                            x="pct_products",
                            y="net_sentiment_pct",
                            size="xhs_post_count",
                            color="brand",
                            color_discrete_map=BRAND_COLORS,
                            text="bubble_label",
                            size_max=60,
                            labels={
                                "pct_products": "% of product catalog",
                                "net_sentiment_pct": "Net sentiment (% positive − % negative, own posts)",
                            },
                        )
                        fig2.add_shape(
                            type="line", x0=0, y0=0, x1=max_x2, y1=0,
                            line=dict(color="#94a3b8", dash="dash"),
                        )
                        fig2.update_traces(textposition="top center", cliponaxis=False, textfont=dict(size=13))
                        if "xhs_small_base_flag" in sov.columns:
                            low_conf_brands = set(sov.loc[sov["xhs_small_base_flag"], "brand"])
                            fig2.for_each_trace(
                                lambda t: t.update(marker=dict(opacity=0.55, line=dict(width=3, color="#f59e0b")))
                                if t.name in low_conf_brands else None
                            )
                            # One worked example of each: point an arrow at an actual faded/outlined
                            # bubble and explain what the outline means, and likewise for a solid one.
                            low_conf_example = sov[sov["brand"].isin(low_conf_brands)]
                            high_conf_example = sov[~sov["brand"].isin(low_conf_brands)]
                            if not low_conf_example.empty:
                                ex = low_conf_example.iloc[0]
                                fig2.add_annotation(
                                    x=ex["pct_products"], y=ex["net_sentiment_pct"],
                                    text="Yellow outline = fewer posts<br>(lower-confidence sentiment score)",
                                    showarrow=True, arrowhead=2, arrowwidth=2, arrowcolor="#f59e0b",
                                    ax=70, ay=60,
                                    font=dict(size=11, color="#92400e"),
                                    bgcolor="white", bordercolor="#f59e0b", borderwidth=1,
                                )
                            if not high_conf_example.empty:
                                ex = high_conf_example.iloc[0]
                                fig2.add_annotation(
                                    x=ex["pct_products"], y=ex["net_sentiment_pct"],
                                    text="No outline = enough posts<br>(sentiment score is more reliable)",
                                    showarrow=True, arrowhead=2, arrowwidth=2, arrowcolor="#64748b",
                                    ax=-70, ay=-50,
                                    font=dict(size=11, color="#334155"),
                                    bgcolor="white", bordercolor="#64748b", borderwidth=1,
                                )
                        fig2.update_layout(
                            xaxis_range=[0, max_x2], yaxis_range=[-y_span, y_span],
                            height=520, showlegend=False,
                        )
                        st.plotly_chart(fig2, width="stretch")
                        st.caption(
                            "Bubble size = XHS post count (volume). Above the dashed line = net "
                            "positive sentiment; below = net negative. A brand can have high volume "
                            "(big bubble) with weak sentiment (near or below zero) — that's a "
                            "different story than high volume with strongly positive sentiment."
                        )
                        if "xhs_small_base_flag" in sov.columns and sov["xhs_small_base_flag"].any():
                            low_conf = sov[sov["xhs_small_base_flag"]]
                            listing = "; ".join(f"{r.brand} ({r.xhs_post_count} posts)" for r in low_conf.itertuples())
                            st.caption(
                                f"⚠ The amber outline is NOT about sentiment — it only flags brands with "
                                f"fewer than {XHS_SMALL_BASE_THRESHOLD} brand-attributed XHS posts, where the "
                                f"sentiment score is based on a smaller sample and less stable: {listing}. "
                                "Its position on the chart (up/down) still shows the actual sentiment value."
                            )

                    if "platform_divergence_pp" in sov.columns:
                        st.divider()
                        st.markdown("**Platform divergence — XHS sentiment vs. review sentiment**")
                        st.caption(
                            "XHS net sentiment (social posts) vs. a review-based sentiment proxy "
                            "(% five-star − % one-star). A gap over 20pp is flagged — not necessarily "
                            "a contradiction: XHS often reflects aspirational/pre-purchase sentiment, "
                            "reviews reflect post-purchase experience — different moments in the "
                            "customer journey."
                        )
                        div_display = sov[[
                            "brand", "net_sentiment_pct", "review_sentiment_proxy",
                            "platform_divergence_pp", "platform_divergence_flag",
                        ]].copy()
                        div_display["platform_divergence_flag"] = div_display["platform_divergence_flag"].map(
                            {True: "⚠️ Platform divergence", False: ""}
                        )
                        div_display = div_display.rename(columns={
                            "net_sentiment_pct": "XHS net sentiment (%)",
                            "review_sentiment_proxy": "Review net sentiment (%5★−%1★)",
                            "platform_divergence_pp": "Gap (pp)",
                            "platform_divergence_flag": "Flag",
                        })
                        st.dataframe(div_display, width="stretch", hide_index=True)
                        for r in sov.itertuples():
                            st.markdown(f"- {r.divergence_sentence}")

                    if not theme_sentiment_data.empty:
                        st.divider()
                        st.markdown("**Theme-level sentiment breakdown** — where each brand's score is coming from")
                        st.caption(
                            "% of posts positive/neutral/negative within each XHS theme tag, per brand. "
                            "A post can carry multiple theme tags. Rare themes are dropped for readability."
                        )
                        sent_colors = {"positive": "#16a34a", "neutral": "#94a3b8", "negative": "#dc2626"}
                        fig4 = px.bar(
                            theme_sentiment_data,
                            x="theme", y="pct_within_brand_theme", color="sentiment",
                            barmode="stack", facet_col="brand", facet_col_wrap=3,
                            color_discrete_map=sent_colors,
                            category_orders={"sentiment": ["positive", "neutral", "negative"]},
                            labels={"pct_within_brand_theme": "% of posts", "theme": "Theme"},
                        )
                        fig4.update_layout(height=700, xaxis_tickangle=-30)
                        fig4.for_each_xaxis(lambda ax: ax.update(tickangle=-30))
                        fig4.for_each_annotation(lambda a: a.update(text=a.text.split("=")[-1]))
                        st.plotly_chart(fig4, width="stretch")

                if label == "Negative Tail" and not negative_tail_reviews.empty:
                    max_rating = st.slider(
                        "Include reviews rated ≤", min_value=1, max_value=5, value=1,
                        key="negtail_rating_slider",
                        help="1 = the classic 'negative tail' (1-star only). Slide up to see how the "
                             "complaint mix changes as more moderate reviews are included.",
                    )
                    neg_f = negative_tail_reviews[negative_tail_reviews["rating"] <= max_rating]
                    total_in_scope = len(neg_f)
                    st.caption(
                        f"{total_in_scope} HK reviews rated ≤{max_rating} stars in scope — a small base "
                        "if this number is low. Brands with an asterisk (*) below have fewer than "
                        f"{NEGATIVE_TAIL_SMALL_BASE} reviews in scope and should be read as directional only."
                    )
                    with st.expander("What's in each category? (see note on the three catch-all buckets)"):
                        st.markdown(
                            "The six specific categories (product mismatch/counterfeit, comfort/dryness, "
                            "shipping/fulfillment, customer service, packaging, price) are reviews that "
                            "clearly named one of those issues. Everything else — reviews that don't fit "
                            "any of those six — used to be lumped into one generic **\"other\"** bucket. "
                            "That bucket has been split into three more specific ones so \"other\" doesn't "
                            "hide what's actually going on:\n"
                            "- **quality/defect (other)** — a real complaint (defect, fit, prescription, "
                            "general quality) that isn't one of the six named issues above.\n"
                            "- **no complaint / positive feedback** — a happy review with nothing wrong "
                            "reported. Mostly shows up as you raise the rating slider above 1-2 stars.\n"
                            "- **unclear / insufficient detail** — blank, one-word, or otherwise "
                            "uninformative text that doesn't give a usable signal either way."
                        )

                    brand_totals = neg_f.groupby("brand").size()
                    breakdown_live = (
                        neg_f.groupby(["brand", "category"]).size().rename("count").reset_index()
                    )
                    breakdown_live["brand_total"] = breakdown_live["brand"].map(brand_totals)
                    breakdown_live["pct_of_brand"] = (breakdown_live["count"] / breakdown_live["brand_total"] * 100).round(1)
                    breakdown_live["small_base_flag"] = breakdown_live["brand_total"] < NEGATIVE_TAIL_SMALL_BASE

                    if breakdown_live.empty:
                        st.info("No reviews in scope at this rating cutoff.")
                    else:
                        present_categories = [c for c in NEGATIVE_TAIL_CATEGORY_ORDER if c in breakdown_live["category"].unique()]

                        st.markdown("**Complaint rate by brand (% of that brand's own reviews in scope)**")
                        st.caption(
                            "Each panel is normalized to that brand's own review count, so brands are "
                            "comparable regardless of how many reviews they have. Panels for brands with "
                            f"fewer than {NEGATIVE_TAIL_SMALL_BASE} reviews in scope are greyed out — the "
                            "sample is too small to trust a percentage."
                        )
                        brands_sorted = sorted(breakdown_live["brand"].unique())
                        ncols = 3
                        nrows = -(-len(brands_sorted) // ncols)  # ceil division
                        subplot_titles = [
                            f"{b} (n={int(brand_totals.get(b, 0))})" for b in brands_sorted
                        ]
                        fig_norm = make_subplots(
                            rows=nrows, cols=ncols, subplot_titles=subplot_titles,
                            horizontal_spacing=0.06, vertical_spacing=0.22,
                        )
                        for i, brand in enumerate(brands_sorted):
                            row, col = i // ncols + 1, i % ncols + 1
                            n_brand = int(brand_totals.get(brand, 0))
                            is_small = n_brand < NEGATIVE_TAIL_SMALL_BASE
                            brand_df = breakdown_live[breakdown_live["brand"] == brand].set_index("category")
                            y_vals = [brand_df["pct_of_brand"].get(c, 0) for c in present_categories]
                            fig_norm.add_trace(
                                go.Bar(
                                    x=present_categories,
                                    y=[0] * len(present_categories) if is_small else y_vals,
                                    marker_color="#d1d5db" if is_small else BRAND_COLORS.get(brand, "#64748b"),
                                    showlegend=False,
                                ),
                                row=row, col=col,
                            )
                            if is_small:
                                fig_norm.add_annotation(
                                    text="insufficient data", showarrow=False,
                                    xref="x domain", yref="y domain", x=0.5, y=0.5,
                                    font=dict(size=12, color="#6b7280"),
                                    row=row, col=col,
                                )
                        fig_norm.update_xaxes(
                            categoryorder="array", categoryarray=present_categories,
                            tickangle=-30, tickfont=dict(size=9),
                        )
                        fig_norm.update_yaxes(title_text="% of brand's reviews", title_font=dict(size=10))
                        fig_norm.update_layout(height=340 * nrows, margin=dict(t=60))
                        st.plotly_chart(fig_norm, width="stretch")

                        st.markdown(
                            "**Absolute volume** (reflects review count differences across brands, "
                            "not necessarily complaint rate)"
                        )
                        fig3 = px.bar(
                            breakdown_live,
                            x="category", y="count", color="brand",
                            barmode="group", color_discrete_map=BRAND_COLORS,
                            category_orders={"category": present_categories},
                            labels={"category": "Complaint category", "count": "Reviews"},
                        )
                        fig3.update_layout(height=480, xaxis_tickangle=-20)
                        st.plotly_chart(fig3, width="stretch")

                        display_df = breakdown_live.sort_values(["brand", "count"], ascending=[True, False]).copy()
                        display_df["brand"] = display_df.apply(
                            lambda r: f"{r['brand']} *" if r["small_base_flag"] else r["brand"], axis=1
                        )
                        st.dataframe(
                            display_df[["brand", "category", "count", "pct_of_brand"]].rename(
                                columns={"pct_of_brand": "% of brand's reviews in scope"}
                            ),
                            width="stretch", hide_index=True,
                        )
                        st.caption(f"* small-base brand (under {NEGATIVE_TAIL_SMALL_BASE} reviews in scope) — directional only.")

                        st.markdown("**Qualitative appendix — representative paraphrased complaints**")
                        for (brand, category), group in neg_f[neg_f["summary"].notna()].groupby(["brand", "category"]):
                            st.markdown(f"_{brand} — {category}_")
                            for r in group.head(3).itertuples():
                                st.markdown(f"- [{r.summary}]({r.source_url})" if r.source_url else f"- {r.summary}")

                        st.markdown("**Polarization candidates — complaints that are ALSO praised elsewhere**")
                        attr_df = triangulation_data.get("Attribute Quadrant", {}).get("csv", pd.DataFrame())
                        candidates_found = False
                        if not attr_df.empty:
                            for brand, brand_df in breakdown_live.groupby("brand"):
                                for category in set(brand_df.nlargest(2, "count")["category"]):
                                    attribute = NEGATIVE_TAIL_TO_ATTRIBUTE.get(category)
                                    if not attribute:
                                        continue
                                    match = attr_df[
                                        (attr_df["brand"] == brand) & (attr_df["attribute"] == attribute)
                                        & (attr_df["sentiment"] == "positive")
                                    ]
                                    positive_mentions = int(match["mentions"].sum()) if not match.empty else 0
                                    if positive_mentions >= NEGATIVE_TAIL_POLARIZATION_MIN_POSITIVE:
                                        candidates_found = True
                                        row = brand_df[brand_df["category"] == category].iloc[0]
                                        st.markdown(
                                            f"- {brand}: \"{category}\" is a top complaint ({int(row['count'])} reviews, "
                                            f"{row['pct_of_brand']}% in scope) — but \"{attribute}\" also gets "
                                            f"{positive_mentions} positive mentions elsewhere (Prompt C). Possible "
                                            "polarizing attribute."
                                        )
                        if not candidates_found:
                            st.caption("None found at this rating cutoff.")

                    st.download_button(
                        "Download negative_tail_reviews.csv (all ratings, raw)",
                        negative_tail_reviews.to_csv(index=False).encode("utf-8-sig"),
                        file_name="negative_tail_reviews.csv", mime="text/csv",
                        key="dl_negative_tail_reviews",
                    )
                    continue  # skip the generic static-md render below for this tab

                if label == "Price vs. Sentiment" and not content["csv"].empty:
                    tiers = content["csv"]

                    # Tiers are percentile splits, not fixed price bands, so the actual HK$
                    # breakpoints move slightly as new products get scraped — pull them from
                    # the CSV (written fresh each run by price_sentiment.py) rather than
                    # hardcoding, and print them right on the charts so "Budget/Mid/Premium"
                    # never appears without its price range next to it. Two variants: plain
                    # (for Plotly axis/facet text, which isn't markdown) and "\$"-escaped
                    # (for st.markdown/st.caption, where an unescaped "$...$" pair renders as
                    # LaTeX and mangles the currency into a formula).
                    def _fmt_tier_price(tier_name, lo, hi, escape_dollar):
                        d = "\\$" if escape_dollar else "$"
                        if tier_name == "Budget":
                            return f"≤HK{d}{hi:.0f}"
                        if tier_name == "Premium":
                            return f">HK{d}{lo:.0f}"
                        return f"HK{d}{lo:.0f}–HK{d}{hi:.0f}"

                    has_price_cols = {"price_min", "price_max"}.issubset(tiers.columns)
                    tier_price_plain = (
                        {r.tier: _fmt_tier_price(r.tier, r.price_min, r.price_max, False) for r in tiers.itertuples()}
                        if has_price_cols else {}
                    )
                    tier_price_md = (
                        {r.tier: _fmt_tier_price(r.tier, r.price_min, r.price_max, True) for r in tiers.itertuples()}
                        if has_price_cols else {}
                    )
                    tier_axis_labels = {
                        t: (f"{t}<br>{tier_price_plain[t]}" if t in tier_price_plain else t) for t in PRICE_TIER_ORDER
                    }
                    if tier_price_md:
                        st.caption(
                            "**Tier price ranges:** " + "  ·  ".join(
                                f"{t} {tier_price_md[t]}" for t in PRICE_TIER_ORDER if t in tier_price_md
                            )
                        )

                    melted = tiers.melt(
                        id_vars="tier", value_vars=["pct_one_star", "pct_five_star"],
                        var_name="metric", value_name="pct",
                    )
                    melted["metric"] = melted["metric"].map(
                        {"pct_one_star": "% 1-star", "pct_five_star": "% 5-star"}
                    )
                    fig4 = px.bar(
                        melted, x="tier", y="pct", color="metric", barmode="group",
                        category_orders={"tier": PRICE_TIER_ORDER, "metric": ["% 1-star", "% 5-star"]},
                        color_discrete_map={"% 1-star": "#dc2626", "% 5-star": "#16a34a"},
                        labels={"tier": "Price tier", "pct": "% of reviews", "metric": ""},
                    )
                    fig4.update_xaxes(
                        tickvals=PRICE_TIER_ORDER, ticktext=[tier_axis_labels[t] for t in PRICE_TIER_ORDER]
                    )
                    fig4.update_layout(height=440)
                    st.plotly_chart(fig4, width="stretch")

                    st.dataframe(
                        tiers.rename(columns={
                            "tier": "Tier", "product_count": "Products", "review_count": "Reviews",
                            "avg_rating": "Avg. rating", "pct_one_star": "% 1-star", "pct_five_star": "% 5-star",
                            "price_min": "Price from (HK$)", "price_max": "Price to (HK$)",
                        }),
                        width="stretch", hide_index=True,
                    )
                    st.caption(
                        "Tiers are percentile splits (bottom/middle/top third) of HK product price, "
                        "not fixed price bands. Statistical significance (chi-square, overall and per "
                        "brand) and the plain-language read are in the summary below the table."
                    )

                    if not price_sentiment_brand.empty:
                        st.divider()
                        st.markdown("**Compare a single brand across tiers**")
                        brand_options = sorted(price_sentiment_brand["brand"].dropna().unique().tolist())
                        picked_brand = st.selectbox(
                            "Brand", brand_options, key="price_sentiment_brand_pick",
                        )
                        brand_tiers = price_sentiment_brand[price_sentiment_brand["brand"] == picked_brand]

                        melted_b = brand_tiers.melt(
                            id_vars="tier", value_vars=["pct_one_star", "pct_five_star"],
                            var_name="metric", value_name="pct",
                        )
                        melted_b["metric"] = melted_b["metric"].map(
                            {"pct_one_star": "% 1-star", "pct_five_star": "% 5-star"}
                        )
                        fig5 = px.bar(
                            melted_b, x="tier", y="pct", color="metric", barmode="group",
                            category_orders={"tier": PRICE_TIER_ORDER, "metric": ["% 1-star", "% 5-star"]},
                            color_discrete_map={"% 1-star": "#dc2626", "% 5-star": "#16a34a"},
                            labels={"tier": "Price tier", "pct": "% of reviews", "metric": ""},
                            title=f"{picked_brand} — % 1-star / % 5-star by price tier",
                        )
                        fig5.update_xaxes(
                            tickvals=PRICE_TIER_ORDER, ticktext=[tier_axis_labels[t] for t in PRICE_TIER_ORDER]
                        )
                        fig5.update_layout(height=400)
                        st.plotly_chart(fig5, width="stretch")

                        st.dataframe(
                            brand_tiers[["tier", "review_count", "avg_rating", "pct_one_star", "pct_five_star", "thin_cell"]].rename(
                                columns={
                                    "tier": "Tier", "review_count": "Reviews", "avg_rating": "Avg. rating",
                                    "pct_one_star": "% 1-star", "pct_five_star": "% 5-star", "thin_cell": "Thin cell (<15 reviews)",
                                }
                            ),
                            width="stretch", hide_index=True,
                        )
                        thin_tiers = brand_tiers[brand_tiers["thin_cell"]]["tier"].tolist()
                        if thin_tiers:
                            st.caption(
                                f"{picked_brand}: {', '.join(thin_tiers)} tier(s) have fewer than 15 reviews — "
                                "read as directional only, and the significance test for this brand is skipped."
                            )

                        st.divider()
                        st.markdown("**All brands at once — bubble view**")
                        bubble_df = price_sentiment_brand[~price_sentiment_brand["thin_cell"]].copy()
                        thin_excluded = price_sentiment_brand[price_sentiment_brand["thin_cell"]]

                        if bubble_df.empty:
                            st.info("No brand x tier cell has enough reviews (15+) to plot reliably.")
                        else:
                            fig6 = px.scatter(
                                bubble_df,
                                x="pct_one_star", y="pct_five_star",
                                size="review_count", color="brand",
                                facet_col="tier",
                                category_orders={"tier": [t for t in PRICE_TIER_ORDER if t in bubble_df["tier"].unique()]},
                                color_discrete_map=BRAND_COLORS,
                                text="brand",
                                size_max=45,
                                hover_data={"review_count": True, "avg_rating": True},
                                labels={"pct_one_star": "% 1-star", "pct_five_star": "% 5-star", "brand": "Brand"},
                            )
                            # All three panels share ONE x scale and ONE y scale (padded around the
                            # actual plotted range) so a bubble's position means the same thing in
                            # every panel — independent per-panel scales looked like three unrelated
                            # charts even though the axis labels were identical.
                            x_lo = max(0, bubble_df["pct_one_star"].min() - 3)
                            x_hi = bubble_df["pct_one_star"].max() + 3
                            y_lo = max(0, bubble_df["pct_five_star"].min() - 5)
                            y_hi = min(100, bubble_df["pct_five_star"].max() + 5)
                            fig6.update_xaxes(matches="x", range=[x_lo, x_hi], showticklabels=True)
                            fig6.update_yaxes(matches="y", range=[y_lo, y_hi], showticklabels=True)
                            fig6.for_each_annotation(
                                lambda a: a.update(text=tier_axis_labels.get(a.text.split("=")[-1], a.text.split("=")[-1]))
                            )
                            fig6.update_traces(textposition="top center", cliponaxis=False)
                            fig6.update_layout(height=480)
                            st.plotly_chart(fig6, width="stretch")

                            caption = (
                                "One panel per price tier — all three share the same % 1-star (x) and "
                                "% 5-star (y) scale, so a bubble's position means the same thing in every "
                                "panel. Bubble size = review count. Bubbles near the top-left are the best "
                                "outcome (low 1-star, high 5-star), bottom-right the worst."
                            )
                            if not thin_excluded.empty:
                                excluded_desc = "; ".join(
                                    f"{r.brand} {r.tier} ({r.review_count} review{'s' if r.review_count != 1 else ''})"
                                    for r in thin_excluded.itertuples()
                                )
                                caption += f" Excluded as thin cells (<15 reviews, unreliable): {excluded_desc}."
                            st.caption(caption)

                st.markdown(content["md"])
                if content["csv_name"] and not content["csv"].empty:
                    st.download_button(
                        f"Download {content['csv_name']}",
                        content["csv"].to_csv(index=False).encode("utf-8-sig"),
                        file_name=content["csv_name"],
                        mime="text/csv",
                        key=f"dl_{content['csv_name']}",
                    )

# ---- Demand Signals ----------------------------------------------------------
with tab_demand:
    st.subheader("Demand Signals — search interest vs. scraped activity")
    st.caption(
        "Google Trends search index (Hong Kong) vs. monthly review and XHS post "
        "volume. Only brands with a manually verified, uncontaminated trend "
        "export are charted — see the warning below for why."
    )

    _demand_brand_options = sorted(set(all_brands) | demand_signals.RELIABLE_BRANDS)
    _demand_default = "Acuvue" if "Acuvue" in _demand_brand_options else _demand_brand_options[0]
    demand_brand = st.selectbox(
        "Brand", _demand_brand_options, index=_demand_brand_options.index(_demand_default)
    )

    if not demand_signals.classify_trend_reliability(demand_brand):
        st.warning(
            f"⚠ Search trend data for {demand_brand} may be contaminated by an "
            "unrelated same-name entity — see relatedEntities data for details. "
            "Re-pull with Shopping/Health category filter before using."
        )
    else:
        monthly_search = demand_signals.aggregate_monthly_search_index(demand_brand, trends_dir)

        if monthly_search.empty:
            st.info(f"No Google Trends timeline found for {demand_brand} in `{trends_dir}`.")
        else:
            monthly_reviews = demand_signals.get_monthly_review_counts(demand_brand, db_path)
            monthly_xhs = demand_signals.get_monthly_xhs_counts(demand_brand, db_path)

            combined = (
                monthly_search
                .merge(monthly_reviews, on="month", how="left")
                .merge(monthly_xhs, on="month", how="left")
                .sort_values("month")
            )
            combined["review_count"] = combined["review_count"].fillna(0)
            combined["xhs_count"] = combined["xhs_count"].fillna(0)

            fig = make_subplots(specs=[[{"secondary_y": True}]])
            fig.add_trace(
                go.Bar(x=combined["month"], y=combined["review_count"], name="Reviews", marker_color="#94a3b8"),
                secondary_y=False,
            )
            fig.add_trace(
                go.Bar(x=combined["month"], y=combined["xhs_count"], name="XHS Posts", marker_color="#f59e0b"),
                secondary_y=False,
            )
            fig.add_trace(
                go.Scatter(
                    x=combined["month"], y=combined["search_index"], name="Search Index",
                    mode="lines+markers",
                    line=dict(color=BRAND_COLORS.get(demand_brand, "#2563eb"), width=3),
                ),
                secondary_y=True,
            )

            campaigns = demand_signals.get_campaigns_for_brand(demand_brand)
            for camp in campaigns:
                c_start = pd.to_datetime(camp["start_date"]).strftime("%Y-%m")
                c_end = pd.to_datetime(camp["end_date"]).strftime("%Y-%m")
                if c_end < combined["month"].min() or c_start > combined["month"].max():
                    continue
                fig.add_vrect(
                    x0=c_start, x1=c_end,
                    fillcolor="#fde68a", opacity=0.3, line_width=0,
                    annotation_text=camp["label"], annotation_position="top left",
                )

            fig.update_layout(
                barmode="group",
                title=f"{demand_brand} — Search Index vs. Reviews vs. XHS Posts",
                height=480,
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            fig.update_yaxes(title_text="Reviews / XHS Posts (count)", secondary_y=False)
            fig.update_yaxes(title_text="Search Index (0–100)", secondary_y=True)
            st.plotly_chart(fig, width="stretch")

            for camp in campaigns:
                st.caption(
                    f"📌 **{camp['label']}** ({camp['start_date']} – {camp['end_date']}) — "
                    f"[source]({camp['source_url']})"
                )

            st.subheader("Divergence alerts")
            st.caption(
                "Flags months where search interest and review volume move in "
                "opposite directions by more than the threshold — a signal that "
                "demand may be converting through a channel this scraper doesn't cover."
            )
            alerts = demand_signals.find_divergence_alerts(monthly_search, monthly_reviews)
            if not alerts:
                st.success("No search/review divergence detected month-over-month.")
            else:
                for a in alerts:
                    st.warning(a["text"])

# ---- Data Notes ------------------------------------------------------------
with tab_notes:
    st.subheader("Data coverage & known limitations")
    st.markdown(
        """
**Hong Kong (HKTVmall + 393lens + Sorra)**
- **393lens** currently has no captured price or rating data \u2014 the Playwright
  extraction for this site needs a fix before pricing comparisons can include it.
- **Sorra** is newly added (12 products, HK market). Prices are not yet captured \u2014
  product counts are reflected but Sorra is excluded from price charts.
- **Bausch & Lomb (\u535a\u58eb\u502b) keyword coverage** is still being validated against
  HKTVmall's full catalogue \u2014 current count may understate true assortment.
- **CooperVision and Olens** are now scraped from HKTVmall; 393lens coverage
  for these brands has not yet been attempted.

**Customer feedback (XHS)**
- **Xiaohongshu (XHS) covers Acuvue only** (51 posts). Cross-brand social
  comparison is not yet possible from this source.

**Customer signals (LIHKG)**
- **No absolute dates** — LIHKG only exposes relative age text (e.g. "11
  個月前"), so this source can't be time-windowed or trended like Reviews/XHS.
- **Bare-keyword search collides with unrelated topics more than expected** —
  confirmed for "Alcon" (car brake-caliper brand), "歐彩"/Olens (25 of its
  search results were unrelated football/betting threads — 歐聯/派彩 share
  characters with 歐彩), "博士倫"/Bausch & Lomb (pulled an unrelated UK
  court-case news story), and "酷柏"/CooperVision (pulled a celebrity gossip
  thread). Threads in known-risky categories (汽車台, 體育台) are flagged in
  the Customer Signals tab; 財經台/娛樂台/時事台 collisions are not
  auto-flagged yet.
- **~0.5% residual false-positive brand tags** even after constraining the
  extraction LLM to the 5-brand allowlist (e.g. one post in a gambling-odds
  thread got tagged "Bausch & Lomb", one in a horse-racing complaint thread
  got tagged "Acuvue") — down from whole-thread pollution before the fix,
  but not zero. Accepted as a known limitation for directional sentiment
  reads; revisit if it recurs at higher volume.
- **Posts are LLM-translated and LLM-sentiment-scored** (gpt-4o-mini), same
  approach as XHS — not human-reviewed.

**General**
- **Review and post dates** span July 2025 to June 2026 (\u2248 12 months).
- **Currency**: all prices are in HKD, captured at scrape time.

This page exists so nothing here gets overstated to a client. Update it
as each gap gets closed.
        """
    )
    st.caption(f"Dashboard built from: {os.path.abspath(db_path)}")
