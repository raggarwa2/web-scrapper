# Bug Fix & Data Quality Report — 2026-07-27

Scope: HK contact-lens dashboard (`app.py`, `youtube_signals.py`, `instagram_signals.py`).

**Update 2026-07-27: Fix 1, Fix 2, and the Instagram enhancement have all been
applied**, per approval. Details of what changed and how each was verified are
below; the diffs shown are what actually landed in `app.py`. Fix 3 required no
code change (see its section).

---

## Fix 1 — sidebar/KPI card counting off-topic YouTube comments (confirmed bug)

**Root cause:** two spots in `app.py` use `youtube_comments_df` (already filtered to
brand-relevant videos, HK market) but skip `youtube_signals.on_topic_comments()`,
which drops comments where `is_lens_relevant == 0` — i.e. audience chatter that
drifted off the product (e.g. a K-pop sponsorship draws comments about the group,
not the lenses). The `youtube_bh` variable a few hundred lines later ([app.py:874-877](app.py#L874-L877))
already does this correctly; these two didn't.

### Measured impact (current data, all 5 brands selected — the default)

| | Raw (current) | On-topic (fixed) | Difference |
|---|---:|---:|---:|
| Sidebar total / KPI card YouTube count | 335 | 137 | **−198 (−59%)** |

Verified two independent ways: direct SQL against `output/youtube_data.db`, and
calling the real `youtube_signals.on_topic_comments()` function directly — both
agree exactly (335 → 137).

### Location A — sidebar total + caption ([app.py:449-457](app.py#L449-L457))

```diff
 _xhs_attributed = xhs[xhs["brand_mentioned"].notna() & (xhs["brand_mentioned"] != "other")]
-_total_content = len(reviews) + len(xhs) + len(xhs_comments) + len(lihkg_df) + len(youtube_comments_df)
+_youtube_on_topic_all = youtube_signals.on_topic_comments(youtube_comments_df)
+_total_content = len(reviews) + len(xhs) + len(xhs_comments) + len(lihkg_df) + len(_youtube_on_topic_all)
 st.sidebar.markdown(f"**{_total_content:,} pieces of consumer content analyzed**")
 st.sidebar.caption(
     f"{len(products)} products · {len(reviews)} reviews · "
     f"{len(xhs)} XHS posts ({len(_xhs_attributed)} brand-attributed) · "
     f"{len(xhs_comments)} XHS comments · {len(lihkg_df)} LIHKG posts · "
-    f"{len(youtube_comments_df)} YouTube comments"
+    f"{len(_youtube_on_topic_all)} YouTube comments"
 )
```

### Location B — "Reviews & posts analyzed" KPI card ([app.py:530-538](app.py#L530-L538))

```diff
 _xhs_f_count = len(xhs[xhs["brand_mentioned"].isin(selected_brands)]) if not xhs.empty else 0
 _lihkg_f_count = (
     lihkg_df["mentioned_brands_list"].apply(lambda lst: any(b in selected_brands for b in lst)).sum()
     if not lihkg_df.empty else 0
 )
 _youtube_f_count = (
-    len(youtube_comments_df[youtube_comments_df["brand"].isin(selected_brands)])
+    len(youtube_signals.on_topic_comments(
+        youtube_comments_df[youtube_comments_df["brand"].isin(selected_brands)]
+    ))
     if not youtube_comments_df.empty else 0
 )
```

The KPI card's help-text bullet (`f"- {_youtube_f_count:,} YouTube comments"`,
[app.py:547](app.py#L547)) needs no separate edit — it already reads from the
now-corrected `_youtube_f_count`.

**Status: applied.** Both edits landed in `app.py`. Re-verified after applying, via
the real `youtube_signals.on_topic_comments()` function: raw 335 → on-topic 137,
same −198 difference as measured before the edit.

---

## Fix 2 — `avg_rating = 0.0` sentinel in `lensdata.db` (data quality)

**Root cause:** `products.avg_rating` uses `0.0` to mean "no reviews yet," which is
indistinguishable from a genuine (if implausible) 0-star average in any code that
averages `avg_rating` without excluding it — e.g. a naive `.mean()` over
`avg_rating` would silently pull every brand's average down.

### Verification the migration is safe

Checked whether any row has `avg_rating = 0.0` with `total_reviews > 0` (which would
mean a real zero rating, not a sentinel) — **zero such rows exist**. Every
`avg_rating = 0.0` row also has `total_reviews IS NULL` or `= 0`, so the migration
criterion below has no edge cases to special-case.

### Affected row counts (HK market only — no TH rows have this sentinel)

| Brand | Affected rows |
|---|---:|
| Acuvue | 194 |
| Bausch & Lomb | 117 |
| Olens | 88 |
| Alcon | 66 |
| CooperVision | 58 |
| **Total** | **523** |

For context: 1,431 rows already have `avg_rating IS NULL` (presumably already
correctly nulled at scrape time for some sources), and 2,405 total product rows
exist across HK+TH.

### Migration — executed

```sql
UPDATE products
SET avg_rating = NULL
WHERE avg_rating = 0.0
  AND (total_reviews IS NULL OR total_reviews = 0);
```

**Backup taken first:** `output/lensdata.db` copied to
`output/lensdata.db.bak-2026-07-27` before the `UPDATE` ran; copy verified
byte-identical (6,819,840 bytes both sides) before proceeding.

**Status: applied.** `523` rows updated (matches the pre-migration count exactly:
194+117+88+66+58). Post-migration checks: `avg_rating = 0.0` rows remaining = 0;
total `avg_rating IS NULL` rows = 1,954 (1,431 pre-existing + 523 newly migrated);
total `products` row count unchanged at 2,405 (only the column value changed, no
rows added/removed).

---

## Fix 3 — Is Olens missing from WateryEyes, or just from config? (resolved, not a bug)

**Finding: Olens is genuinely not stocked at WateryEyes.hk. No scraper change
needed.**

`wateryeyes_scraper.py`'s own module docstring already documented a 2026-07-04
investigation reaching this conclusion, and its `VENDOR_MAP` already contains
`"olens"` / `"o-lens"` entries ready to pick up real Olens stock automatically if
it's ever added — so this was never a missing-config gap.

Re-verified live today (2026-07-27), independent of the docstring:

- **`https://www.wateryeyes.hk/products.json`** — distinct vendor values across the
  full catalog: `Clalen, CooperVision, Bausch + Lomb, Acuvue, Alcon, FreshKon,
  水汪汪 (Wateryeyes)`. No `Olens` or `O-Lens` vendor anywhere.
- **`https://www.wateryeyes.hk/en/collections/olens`** — the collection page exists
  in the site's navigation but currently returns **0 products** ("No products
  found"). This is actually more clear-cut than the 2026-07-04 note, which found 7
  unrelated products (FreshLook/Clalen/Delight/FreshKon) listed there at the time —
  it looks like the store has since cleaned that collection up further, but still
  has no genuine Olens inventory.

**No action needed.** `wateryeyes_products` correctly has 0 Olens rows because
WateryEyes genuinely doesn't carry Olens.

---

## Enhancement — Instagram as an optional 5th Brand Health source

Adds Instagram alongside Reviews/XHS/LIHKG/YouTube in the Brand Health composite
score, using the same `MIN_N_FOR_SOURCE = 5` threshold and √n-weighting as the
other four sources. **Off by default** — existing scores/UI are unaffected until
explicitly turned on.

### Proposed diff

Load Instagram HK data next to the existing YouTube load ([app.py:393](app.py#L393)):

```diff
 youtube_videos_df, youtube_comments_df, _yt_excluded_videos = youtube_signals.load_hk_dashboard_data()
+# Same shape/relevance-filtering contract as YouTube — see instagram_signals.py's
+# module docstring. instagram_comments_df still includes off-topic comments;
+# call instagram_signals.on_topic_comments() before using for sentiment scoring.
+instagram_posts_df, instagram_comments_df, _ig_excluded_posts = instagram_signals.load_hk_dashboard_data()
```

Build `instagram_bh` next to `youtube_bh` ([app.py:872-877](app.py#L872-L877)):

```diff
 youtube_bh = (
     youtube_signals.on_topic_comments(youtube_comments_df[youtube_comments_df["brand"].isin(selected_brands)])
     if not youtube_comments_df.empty else pd.DataFrame()
 )
+
+# Instagram — optional 5th source, see _brand_score(include_instagram=...).
+instagram_bh = (
+    instagram_signals.on_topic_comments(instagram_comments_df[instagram_comments_df["brand"].isin(selected_brands)])
+    if not instagram_comments_df.empty else pd.DataFrame()
+)
```

Extend `_brand_score()` ([app.py:879-901](app.py#L879-L901)) with an opt-in flag:

```diff
-    def _brand_score(brand: str):
+    def _brand_score(brand: str, include_instagram: bool = False):
         components = []  # list of (label, pos_pct, n)
         pos, n = _source_pos_pct(rev_bh[rev_bh["brand"] == brand])
         if pos is not None and n >= MIN_N_FOR_SOURCE:
             components.append(("Reviews", pos, n))
         xb_ = xhs_bh[xhs_bh["brand_mentioned"] == brand] if not xhs_bh.empty else pd.DataFrame()
         pos, n = _source_pos_pct(xb_)
         if pos is not None and n >= MIN_N_FOR_SOURCE:
             components.append(("XHS", pos, n))
         lb_ = lihkg_bh[lihkg_bh["mentioned_brands_list"] == brand] if not lihkg_bh.empty else pd.DataFrame()
         pos, n = _source_pos_pct(lb_)
         if pos is not None and n >= MIN_N_FOR_SOURCE:
             components.append(("LIHKG", pos, n))
         yb_ = youtube_bh[youtube_bh["brand"] == brand] if not youtube_bh.empty else pd.DataFrame()
         pos, n = _source_pos_pct(yb_)
         if pos is not None and n >= MIN_N_FOR_SOURCE:
             components.append(("YouTube", pos, n))
+        if include_instagram:
+            ib_ = instagram_bh[instagram_bh["brand"] == brand] if not instagram_bh.empty else pd.DataFrame()
+            pos, n = _source_pos_pct(ib_)
+            if pos is not None and n >= MIN_N_FOR_SOURCE:
+                components.append(("Instagram", pos, n))
 
         if not components:
             return None, components
         weights = [n ** 0.5 for _, _, n in components]
         score = sum(pos * w for (_, pos, _), w in zip(components, weights)) / sum(weights)
         return score, components
```

`brand_scores = {b: dict(zip(("score", "components"), _brand_score(b))) for b in
selected_brands}` ([app.py:903](app.py#L903)) is unchanged — `include_instagram`
defaults to `False`, so today's UI renders identically. Wiring a toggle into the UI
(e.g. a checkbox that passes `include_instagram=True`) is a separate, later step —
this pass only adds the capability.

### Real sample comparison (computed against current data, not mocked)

Ran the actual scoring logic — reusing the real `lihkg_signals`,
`youtube_signals`, and `instagram_signals` modules plus the exact
Reviews/XHS aggregation `_brand_score()` uses — standalone against the live DBs:

| Brand | Score (without IG) | Score (with IG) | Instagram component |
|---|---:|---:|---|
| Acuvue | 78.1 | 76.7 | 54% positive (n=26) |
| Alcon | 76.7 | 76.7 | excluded — n<5 |
| Bausch & Lomb | 75.6 | 75.6 | excluded — n<5 |
| CooperVision | 71.3 | 71.3 | excluded (n=4, below `MIN_N_FOR_SOURCE=5`) |
| Olens | 75.5 | **79.3** | 93% positive (n=148) |

Notable: Instagram data only clears the `n≥5` threshold for Acuvue and Olens today.
It nudges Acuvue down slightly (its IG sentiment, 54% positive, is weaker than its
other sources) and pushes Olens up meaningfully (+3.8 points — IG sentiment there is
93% positive on real volume, n=148). Alcon, Bausch & Lomb, and CooperVision are
unaffected either way, which is expected — CooperVision has some IG comments (n=4)
but not enough to clear the threshold; Alcon and B&L have effectively none yet.

**Status: applied.** `_brand_score()` now accepts `include_instagram` (default
`False`); no existing call site passes `True` yet, so today's Brand Health tab
renders identically to before. The comparison table above was recomputed after
applying, with the real code, and matched the pre-apply numbers exactly.

---

## Summary — what changed

| Item | Status |
|---|---|
| Fix 1 (YouTube on-topic filtering) | **Applied** to `app.py`, re-verified (335→137) |
| Fix 2 (`avg_rating` sentinel → NULL) | **Applied** to `output/lensdata.db`, 523 rows migrated, backup at `output/lensdata.db.bak-2026-07-27` |
| Fix 3 (Olens @ WateryEyes) | **Resolved** — confirmed not a bug, no code change needed |
| Enhancement (Instagram as 5th source) | **Applied** to `app.py`, off by default (`include_instagram=False`) |

**Post-apply sanity check:** `python -m py_compile app.py` passed, and a full bare
(non-UI) execution of `app.py` end-to-end produced no tracebacks — this exercises
the exact code paths touched by both changes (sidebar/KPI counts and the Brand
Health tab's `_brand_score()`/`instagram_bh` block).

**Update 2026-07-27 (later same day): UI toggle added.** The Brand Health tab now
has an "Include Instagram as a 5th scoring source (beta)" toggle (`st.toggle`,
default off) right above the brand score cards. Turning it on:
- passes `include_instagram=True` into `_brand_score()` for every brand's score
- adds "Instagram" to the per-card "excluded: ..." note when a brand's Instagram
  volume is below `MIN_N_FOR_SOURCE`
- adds a 6th metric column ("Instagram % positive") in the Brand Deep-Dive detail
  view

Still off by default, so nothing changes unless a user explicitly switches it on.
`python -m py_compile` and a full bare-mode execution of `app.py` both passed after
this change with no tracebacks (toggle defaults to off in bare mode, so this
exercises the same code path as before). The `include_instagram=True` branch's
actual scoring math was already verified correct via the standalone comparison
script (Acuvue 78.1→76.7, Olens 75.5→79.3); the new toggle only decides whether
that branch gets reached, and the surrounding display code (dynamic column count,
dynamic excluded-sources list) was traced by hand for both toggle states rather
than clicked through live in a browser — flag if you'd like an actual browser
screenshot pass added as well.
