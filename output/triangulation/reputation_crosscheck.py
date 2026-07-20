# ============================================================
# Reputation Cross-Check — does our scraped sentiment agree with what the
# research team found in forums/press (Research/reputation.csv)?
# ============================================================
# Pools sentiment across every scraped signal we have per brand (HK reviews,
# XHS posts, XHS comments, LIHKG posts, YouTube comments — all five already
# carry a sentiment label, no LLM classification needed here) into one
# "scraped lean", then compares it against the manually-researched
# reputation.csv lean for that brand. Flags Match / Partial / Diverge, and
# for any Diverge brand pulls the actual reputation.csv rows + scraped
# examples side by side so a human can see why the two disagree — not just
# that they do.
#
# Usage:
#   python triangulation/reputation_crosscheck.py
#
# Scope: Hong Kong data only, same as the rest of triangulation/. YouTube
# comments live in a separate db (output/youtube_data.db, --youtube-db) —
# see youtube_scraper.py's module docstring for why.
# ============================================================

import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "output" / "lensdata.db"
DEFAULT_YOUTUBE_DB = ROOT / "output" / "youtube_data.db"
DEFAULT_OUT = ROOT / "output" / "triangulation"
DEFAULT_RESEARCH = ROOT / "Research"

# Net-sentiment dead zone: within +/-10pp of zero is "neutral" rather than
# forcing a lean out of noise. Deliberately narrower than share_of_voice.py's
# DIVERGENCE_THRESHOLD_PP=20 (that's a gap between two already-computed
# leans; this is a single-source lean classification).
LEAN_DEAD_ZONE_PP = 10.0

SOURCES = ["review", "xhs_post", "xhs_comment", "lihkg", "youtube_comment"]


# ============================================================
# DB / CSV loading (HK only)
# ============================================================

def load_reviews_hk(db_path: Path) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        "SELECT brand, rating, review_text_en, review_date FROM reviews "
        "WHERE market = 'HK' AND rating BETWEEN 1 AND 5",
        conn,
    )
    conn.close()
    return df


def load_xhs_posts_hk(db_path: Path) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        "SELECT brand_mentioned AS brand, sentiment, content_en, title, url FROM xhs_posts "
        "WHERE sentiment IS NOT NULL AND sentiment != 'warning'",
        conn,
    )
    conn.close()
    return df


def load_xhs_comments_hk(db_path: Path) -> pd.DataFrame:
    """xhs_comments has no brand column of its own — inherit brand_mentioned
    from the parent post via post_id."""
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        "SELECT p.brand_mentioned AS brand, c.sentiment, c.content_en FROM xhs_comments c "
        "JOIN xhs_posts p ON c.post_id = p.post_id "
        "WHERE c.sentiment IN ('positive', 'negative', 'neutral')",
        conn,
    )
    conn.close()
    return df


def load_lihkg_hk(db_path: Path) -> pd.DataFrame:
    """mentioned_brands is a comma-separated string (a post can mention
    multiple brands) — explode so each mentioned brand gets a vote."""
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        "SELECT mentioned_brands, sentiment, text_english FROM lihkg_posts "
        "WHERE sentiment IN ('positive', 'negative', 'neutral')",
        conn,
    )
    conn.close()
    df = df[df["mentioned_brands"].notna() & (df["mentioned_brands"].str.strip() != "")]
    df = df.assign(brand=df["mentioned_brands"].str.split(",")).explode("brand")
    df["brand"] = df["brand"].str.strip()
    return df[df["brand"] != ""][["brand", "sentiment", "text_english"]]


def load_youtube_comments_hk(youtube_db_path: Path) -> pd.DataFrame:
    """youtube_comments already carries its own brand column (copied down
    from the video at scrape time) — but only comments whose video passed
    brand_relevant AND are themselves on-topic (is_lens_relevant) count as
    brand sentiment, same filter youtube_signals.py's render() applies.
    Returns empty (not an error) if the youtube db doesn't exist yet."""
    if not Path(youtube_db_path).exists():
        return pd.DataFrame(columns=["brand", "sentiment", "comment_text_en"])
    conn = sqlite3.connect(youtube_db_path)
    df = pd.read_sql_query(
        "SELECT c.brand, c.sentiment, c.comment_text_en FROM youtube_comments c "
        "JOIN youtube_videos v ON v.video_id = c.video_id "
        "WHERE c.market = 'HK' AND v.brand_relevant = 1 AND c.is_lens_relevant = 1 "
        "AND c.sentiment IN ('positive', 'negative', 'neutral')",
        conn,
    )
    conn.close()
    return df


def count_excluded_labels(db_path: Path, youtube_db_path: Path = DEFAULT_YOUTUBE_DB) -> dict:
    """Counts rows dropped from pooling for non-standard sentiment labels —
    run separately from the filtered load_* functions above since those
    already exclude these rows at the SQL level."""
    conn = sqlite3.connect(db_path)
    xhs_warning = pd.read_sql_query(
        "SELECT COUNT(*) AS n FROM xhs_posts WHERE sentiment = 'warning'", conn
    )["n"].iloc[0]
    lihkg_mixed = pd.read_sql_query(
        "SELECT COUNT(*) AS n FROM lihkg_posts WHERE sentiment = 'mixed'", conn
    )["n"].iloc[0]
    xhs_comment_other = pd.read_sql_query(
        "SELECT COUNT(*) AS n FROM xhs_comments WHERE sentiment IS NULL OR sentiment NOT IN "
        "('positive', 'negative', 'neutral')", conn
    )["n"].iloc[0]
    conn.close()

    youtube_excluded = 0
    if Path(youtube_db_path).exists():
        yconn = sqlite3.connect(youtube_db_path)
        youtube_excluded = pd.read_sql_query(
            "SELECT COUNT(*) AS n FROM youtube_comments c "
            "JOIN youtube_videos v ON v.video_id = c.video_id "
            "WHERE c.market = 'HK' AND (v.brand_relevant = 0 OR c.is_lens_relevant = 0)",
            yconn,
        )["n"].iloc[0]
        yconn.close()

    return {
        "xhs_posts sentiment='warning'": int(xhs_warning),
        "lihkg_posts sentiment='mixed'": int(lihkg_mixed),
        "xhs_comments non-standard/blank sentiment": int(xhs_comment_other),
        "youtube_comments not brand-relevant/on-topic": int(youtube_excluded),
    }


def load_reputation_hk(research_dir: Path) -> pd.DataFrame:
    path = research_dir / "reputation.csv"
    df = pd.read_csv(path)
    return df[df["market"] == "HK"][["brand", "sentiment", "summary", "source_url", "platform", "date"]].copy()


def _normalize_brand_column(df: pd.DataFrame, canonical_brands: list, col: str = "brand") -> pd.DataFrame:
    """Collapses casing variants (e.g. 'OLENS' vs 'Olens') onto the
    canonical brand names from the reviews table. Unrecognized names
    (e.g. 'REVIA', 'other') are dropped — they're not one of the five
    brands this project tracks."""
    lookup = {b.lower(): b for b in canonical_brands}
    out = df.copy()
    out[col] = out[col].map(lambda b: lookup.get(str(b).strip().lower()))
    return out[out[col].notna()]


# ============================================================
# Sentiment bucketing + pooling
# ============================================================

def bucket_review_sentiment(reviews: pd.DataFrame) -> pd.DataFrame:
    def _bucket(rating):
        if rating >= 4:
            return "positive"
        if rating <= 2:
            return "negative"
        return "neutral"

    out = reviews.copy()
    out["sentiment"] = out["rating"].apply(_bucket)
    return out


def _source_counts(df: pd.DataFrame, brand: str) -> dict:
    sub = df[df["brand"] == brand]
    counts = sub["sentiment"].value_counts()
    return {
        "positive": int(counts.get("positive", 0)),
        "negative": int(counts.get("negative", 0)),
        "neutral": int(counts.get("neutral", 0)),
        "total": int(len(sub)),
    }


def pool_scraped_sentiment(reviews: pd.DataFrame, xhs_posts: pd.DataFrame,
                            xhs_comments: pd.DataFrame, lihkg: pd.DataFrame,
                            youtube_comments: pd.DataFrame, brands: list) -> pd.DataFrame:
    source_frames = {
        "review": reviews, "xhs_post": xhs_posts, "xhs_comment": xhs_comments,
        "lihkg": lihkg, "youtube_comment": youtube_comments,
    }
    rows = []
    for brand in brands:
        row = {"brand": brand}
        pooled_positive = pooled_negative = pooled_neutral = pooled_total = 0
        for source, df in source_frames.items():
            c = _source_counts(df, brand)
            row[f"{source}_positive"] = c["positive"]
            row[f"{source}_negative"] = c["negative"]
            row[f"{source}_neutral"] = c["neutral"]
            row[f"{source}_total"] = c["total"]
            pooled_positive += c["positive"]
            pooled_negative += c["negative"]
            pooled_neutral += c["neutral"]
            pooled_total += c["total"]

        row["pooled_positive"] = pooled_positive
        row["pooled_negative"] = pooled_negative
        row["pooled_neutral"] = pooled_neutral
        row["pooled_total"] = pooled_total
        net_pct = (pooled_positive - pooled_negative) / pooled_total * 100 if pooled_total else 0.0
        row["pooled_net_pct"] = round(net_pct, 1)
        if net_pct > LEAN_DEAD_ZONE_PP:
            row["scraped_lean"] = "positive"
        elif net_pct < -LEAN_DEAD_ZONE_PP:
            row["scraped_lean"] = "negative"
        else:
            row["scraped_lean"] = "neutral"
        rows.append(row)
    return pd.DataFrame(rows)


# ============================================================
# reputation.csv lean + agreement classification
# ============================================================

REPUTATION_TIE_BREAK = ["mixed", "positive", "negative", "neutral"]


def reputation_lean(reputation: pd.DataFrame, brands: list) -> pd.DataFrame:
    rows = []
    for brand in brands:
        sub = reputation[reputation["brand"] == brand]
        counts = sub["sentiment"].value_counts()
        row = {
            "brand": brand,
            "reputation_positive": int(counts.get("positive", 0)),
            "reputation_negative": int(counts.get("negative", 0)),
            "reputation_mixed": int(counts.get("mixed", 0)),
            "reputation_neutral": int(counts.get("neutral", 0)),
            "reputation_total": int(len(sub)),
        }
        if row["reputation_total"] == 0:
            row["reputation_lean"] = "no data"
        else:
            max_count = max(counts.get(k, 0) for k in REPUTATION_TIE_BREAK)
            candidates = [k for k in REPUTATION_TIE_BREAK if counts.get(k, 0) == max_count]
            row["reputation_lean"] = candidates[0]  # first by tie-break priority
        rows.append(row)
    return pd.DataFrame(rows)


def classify_agreement(pooled: pd.DataFrame, reputation: pd.DataFrame) -> pd.DataFrame:
    merged = pooled.merge(reputation, on="brand", how="left")

    def _agreement(r):
        rep, scraped = r["reputation_lean"], r["scraped_lean"]
        if rep == "no data":
            return "No reputation.csv data"
        if rep == "mixed":
            return "Match"
        if rep == scraped:
            return "Match"
        if "neutral" in (rep, scraped):
            return "Partial"
        return "Diverge"  # only remaining case: positive vs negative

    def _notes(r):
        rep_counts = (
            f"pos={r['reputation_positive']}, neg={r['reputation_negative']}, "
            f"mixed={r['reputation_mixed']}, neu={r['reputation_neutral']}"
        )
        return (
            f"Scraped lean {r['scraped_lean']} ({r['pooled_net_pct']:+.1f}% net, n={r['pooled_total']}) "
            f"vs. reputation.csv lean {r['reputation_lean']} ({rep_counts}, n={r['reputation_total']})."
        )

    merged["agreement"] = merged.apply(_agreement, axis=1)
    merged["notes"] = merged.apply(_notes, axis=1)
    return merged


# ============================================================
# Evidence for Diverge brands
# ============================================================

def build_diverge_evidence(diverge_brands: list, reviews: pd.DataFrame, xhs_posts: pd.DataFrame,
                            youtube_comments: pd.DataFrame, reputation: pd.DataFrame,
                            scraped_lean_by_brand: dict) -> dict:
    evidence = {}
    for brand in diverge_brands:
        rep_rows = reputation[reputation["brand"] == brand][
            ["sentiment", "summary", "platform", "date", "source_url"]
        ].to_dict("records")

        lean = scraped_lean_by_brand.get(brand)
        brand_reviews = reviews[(reviews["brand"] == brand) & reviews["review_text_en"].notna()
                                 & (reviews["review_text_en"].str.strip() != "")]
        if lean == "positive":
            examples = brand_reviews.sort_values("rating", ascending=False).head(2)
        elif lean == "negative":
            examples = brand_reviews.sort_values("rating", ascending=True).head(2)
        else:
            examples = brand_reviews.head(2)
        scraped_examples = [
            {"source": "review", "rating": r.rating, "text": r.review_text_en[:200]}
            for r in examples.itertuples()
        ]

        brand_xhs = xhs_posts[(xhs_posts["brand"] == brand) & (xhs_posts["sentiment"] == lean)
                               & xhs_posts["content_en"].notna()]
        if not brand_xhs.empty:
            r = brand_xhs.iloc[0]
            scraped_examples.append({"source": "xhs_post", "rating": r["sentiment"], "text": str(r["content_en"])[:200]})

        brand_youtube = youtube_comments[(youtube_comments["brand"] == brand) & (youtube_comments["sentiment"] == lean)
                                          & youtube_comments["comment_text_en"].notna()]
        if not brand_youtube.empty:
            r = brand_youtube.iloc[0]
            scraped_examples.append({"source": "youtube_comment", "rating": r["sentiment"], "text": str(r["comment_text_en"])[:200]})

        evidence[brand] = {"reputation_rows": rep_rows, "scraped_examples": scraped_examples[:3]}
    return evidence


# ============================================================
# Output
# ============================================================

def write_outputs(result: pd.DataFrame, evidence: dict, exclusions: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    result.to_csv(out_dir / "reputation_crosscheck.csv", index=False, encoding="utf-8-sig")

    lines = ["# External Validation — Reputation.csv Cross-Check\n"]
    lines.append(
        "Compares our own scraped sentiment (HK reviews + XHS posts + XHS comments + LIHKG posts + "
        "YouTube comments, pooled — none of these needed new LLM classification here, all five "
        "already carry a sentiment label from their own scraper) against `Research/reputation.csv`'s "
        "manually-researched forum/press sentiment, per brand. This is a QA/credibility check: do the "
        "two independent signals agree?\n"
    )
    lines.append(
        "**Pooling method:** each source's positive/negative/neutral counts are simply summed "
        "(unweighted) into one pooled lean per brand — a brand with far more posts than reviews (or "
        "vice versa) will have its pooled lean dominated by whichever source has more volume. "
        f"Net sentiment within ±{LEAN_DEAD_ZONE_PP:.0f}pp of zero is classified neutral.\n"
    )
    excl_note = "; ".join(f"{k}: {v} rows excluded" for k, v in exclusions.items())
    lines.append(f"_Excluded from pooling as non-standard labels — {excl_note}._\n")

    lines.append("## Brand comparison\n")
    lines.append("| Brand | Scraped lean | Reputation.csv lean | Agreement | Notes |")
    lines.append("|---|---|---|---|---|")
    for r in result.itertuples():
        lines.append(f"| {r.brand} | {r.scraped_lean} | {r.reputation_lean} | {r.agreement} | {r.notes} |")

    lines.append("\n## Evidence for diverging brands\n")
    if not evidence:
        lines.append("None — no brand diverged.")
    else:
        for brand, ev in evidence.items():
            lines.append(f"### {brand}\n")
            lines.append("**Research/reputation.csv rows:**\n")
            if ev["reputation_rows"]:
                for row in ev["reputation_rows"]:
                    lines.append(
                        f"- [{row['sentiment']}] {row['summary']} "
                        f"({row['platform']}, {row['date']}) — [source]({row['source_url']})"
                    )
            else:
                lines.append("- (no reputation.csv rows for this brand)")
            lines.append("\n**Scraped examples:**\n")
            for ex in ev["scraped_examples"]:
                tag = f"rating={ex['rating']}" if ex["source"] == "review" else f"sentiment={ex['rating']}"
                lines.append(f"- [{ex['source']}, {tag}] {ex['text']}")
            lines.append("")

    (out_dir / "reputation_crosscheck.md").write_text("\n".join(lines), encoding="utf-8")
    print("  External Validation written -> reputation_crosscheck.md, reputation_crosscheck.csv")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Reputation.csv cross-check against scraped sentiment")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--youtube-db", default=str(DEFAULT_YOUTUBE_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--research", default=str(DEFAULT_RESEARCH))
    args = parser.parse_args()

    db_path = Path(args.db)
    youtube_db_path = Path(args.youtube_db)
    out_dir = Path(args.out)
    research_dir = Path(args.research)

    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return

    reviews_raw = load_reviews_hk(db_path)
    canonical_brands = sorted(reviews_raw["brand"].dropna().unique().tolist())
    print(f"Canonical brands: {canonical_brands}")

    reviews = bucket_review_sentiment(reviews_raw)
    xhs_posts_raw = load_xhs_posts_hk(db_path)
    xhs_comments_raw = load_xhs_comments_hk(db_path)
    lihkg_raw = load_lihkg_hk(db_path)
    youtube_raw = load_youtube_comments_hk(youtube_db_path)

    xhs_posts = _normalize_brand_column(xhs_posts_raw, canonical_brands)
    xhs_comments = _normalize_brand_column(xhs_comments_raw, canonical_brands)
    lihkg = _normalize_brand_column(lihkg_raw, canonical_brands)
    youtube_comments = _normalize_brand_column(youtube_raw, canonical_brands)

    print(
        f"Loaded: {len(reviews)} reviews, {len(xhs_posts)}/{len(xhs_posts_raw)} XHS posts (post normalization), "
        f"{len(xhs_comments)}/{len(xhs_comments_raw)} XHS comments, {len(lihkg)}/{len(lihkg_raw)} LIHKG brand-mentions, "
        f"{len(youtube_comments)}/{len(youtube_raw)} YouTube comments"
    )

    reputation = load_reputation_hk(research_dir)
    print(f"Loaded {len(reputation)} HK reputation.csv rows")

    pooled = pool_scraped_sentiment(reviews, xhs_posts, xhs_comments, lihkg, youtube_comments, canonical_brands)
    rep_lean = reputation_lean(reputation, canonical_brands)
    result = classify_agreement(pooled, rep_lean)

    diverge_brands = result[result["agreement"] == "Diverge"]["brand"].tolist()
    print(f"Diverge brands: {diverge_brands or 'none'}")
    scraped_lean_by_brand = dict(zip(result["brand"], result["scraped_lean"]))
    evidence = build_diverge_evidence(diverge_brands, reviews, xhs_posts, youtube_comments, reputation, scraped_lean_by_brand)

    exclusions = count_excluded_labels(db_path, youtube_db_path)

    write_outputs(result, evidence, exclusions, out_dir)
    print(f"\nDone. Outputs in: {out_dir}")


if __name__ == "__main__":
    main()
