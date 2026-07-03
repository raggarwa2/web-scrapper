# Purchasing — What We Can and Can't Show

## The limitation, stated plainly

`lensdata.db` stores one row per product with the *current* price — each
pipeline run upserts in place rather than logging a dated snapshot. There is
no price history table, so **we cannot produce a true "price over time" trend
line from our own scraped data.** Don't present anything as a price trend
unless it's sourced from Google Trends (below), which genuinely is a
time series.

## What we can show instead

### 1. Current discount depth by brand (HKTVmall, HK, snapshot 2026-07-02/03)

| Brand | Products | Avg. discount depth | % of products discounted |
|---|---|---|---|
| Olens | 27 | 39.9% | 100% |
| CooperVision | 76 | 38.8% | 100% |
| Alcon | 114 | 34.7% | 100% |
| Bausch & Lomb | 206 | 31.1% | ~99.5% |
| Acuvue | 354 | 30.5% | ~99.4% |

Virtually every product on HKTVmall is listed at a discount off a stated
"original price" — this is a structural feature of the marketplace's pricing
display, not brand-specific promotional behavior. Higher-priced, lower-volume
brands (Olens, CooperVision) show deeper average discount rates than the two
mass brands (Acuvue, B&L).

### 2. Search-interest trend (real time series — see `trends_synthesis.md`)

Google Trends is the one source where we do have genuine week-by-week
movement across the Wave 6→Wave 7 window:
- Acuvue's "Health"-category search interest rose from a 53.4 to 75.9 average
  (first quarter vs. last quarter of the tracked year) — a genuine upward
  trend, not a snapshot.
- Olens leads "Shopping"-category search consistently across the whole period.
- Alcon and CooperVision search interest is only starting to register in the
  final weeks of the period — an emerging-but-small-base signal.

### 3. Purchase-channel-shift corroboration (directional, low volume)

The reference Wave 7 research shows overseas purchasing rising (18%→22%
overall, 30%→41% for new wearers, China as top source). We searched our own
XHS and LIHKG text for organic mentions of overseas/forwarding-agent buying
behavior (代購, 淘寶/Taobao, 集運/轉運, 天猫/Tmall, "overseas"): **12 mentions
found** (11 XHS across CooperVision/Alcon/B&L/Olens, 1 LIHKG). This is a small
but real signal pointing the same direction as the reference tracking data —
present it as light corroboration, not independent proof; the volume is too
low to stand alone.

## Bottom line for the deck

Frame this section as "search demand trend + discount positioning +
directional social corroboration," not "pricing trend" — the reference
Wave 6/7 research remains the authoritative source for actual
purchasing-behavior trend lines (e.g. the market-of-purchase shift data).
Our external data's role here is corroboration, not replacement.
