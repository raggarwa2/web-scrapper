# External Validation — Reputation.csv Cross-Check

Compares our own scraped sentiment (HK reviews + XHS posts + XHS comments + LIHKG posts, pooled — none of these needed new LLM classification, all four already carry a sentiment label) against `Research/reputation.csv`'s manually-researched forum/press sentiment, per brand. This is a QA/credibility check: do the two independent signals agree?

**Pooling method:** each source's positive/negative/neutral counts are simply summed (unweighted) into one pooled lean per brand — a brand with far more posts than reviews (or vice versa) will have its pooled lean dominated by whichever source has more volume. Net sentiment within ±10pp of zero is classified neutral.

_Excluded from pooling as non-standard labels — xhs_posts sentiment='warning': 2 rows excluded; lihkg_posts sentiment='mixed': 18 rows excluded; xhs_comments non-standard/blank sentiment: 240 rows excluded._

## Brand comparison

| Brand | Scraped lean | Reputation.csv lean | Agreement | Notes |
|---|---|---|---|---|
| Acuvue | positive | negative | Diverge | Scraped lean positive (+66.8% net, n=2857) vs. reputation.csv lean negative (pos=1, neg=4, mixed=2, neu=2, n=9). |
| Alcon | positive | positive | Match | Scraped lean positive (+51.0% net, n=993) vs. reputation.csv lean positive (pos=3, neg=0, mixed=0, neu=0, n=3). |
| Bausch & Lomb | positive | mixed | Match | Scraped lean positive (+62.8% net, n=2274) vs. reputation.csv lean mixed (pos=1, neg=1, mixed=2, neu=0, n=4). |
| CooperVision | positive | positive | Match | Scraped lean positive (+56.0% net, n=805) vs. reputation.csv lean positive (pos=4, neg=0, mixed=1, neu=0, n=5). |
| Olens | positive | neutral | Partial | Scraped lean positive (+44.4% net, n=765) vs. reputation.csv lean neutral (pos=0, neg=0, mixed=0, neu=2, n=2). |

## Evidence for diverging brands

### Acuvue

**Research/reputation.csv rows:**

- [mixed] Thread comparing 1-day vs 2-week lenses; users split on whether Acuvue or B&L was drier — some switched away from Acuvue to B&L for relief, others switched back. No clean brand winner. (Baby Kingdom, 2014-06-01) — [source](https://www.baby-kingdom.com/forum.php?mod=viewthread&tid=11878730)
- [negative] User reported Acuvue Moist left her eyes dry to the point of falling out; switched to CooperVision and found it both cheaper and better-fitting. (Baby Kingdom, 2015-09-01) — [source](https://www.baby-kingdom.com/forum.php?mod=viewthread&tid=16012155)
- [mixed] One Day Acuvue described as comfortable to wear but very hard to insert because the lens is so thin it keeps folding on itself. (Baby Kingdom, 2021-06-01) — [source](https://www.baby-kingdom.com/forum.php?mod=viewthread&tid=23471365)
- [negative] Long-time wearer started getting conjunctivitis and red eyes after years on Acuvue 2-week lenses; replies suggested switching to B&L or moving to dailies for hygiene. Health concern raised. (Baby Kingdom, 2023-12-01) — [source](https://www.baby-kingdom.com/forum.php?mod=viewthread&tid=23714756)
- [positive] Poster summarised their own verdict after trying multiple brands: positive for Acuvue, negative for CooperVision. (HKGolden, 2014-01-01) — [source](https://forum.hkgolden.com/view.aspx?message=4920483&page=3)
- [negative] Long-running price-comparison thread: One Day Acuvue reported at HK$155–180 per 30-pack at small independent shops vs HK$220–290 at Optical 88; users directed each other to online sellers for lower prices. Mild frustration at chain markup. (HKGolden, 2012-01-01) — [source](https://forum.hkgolden.com/thread/5758294)
- [neutral] Users compared per-box Acuvue Moist pricing across shops ranging from HK$130 (no astigmatism) to HK$530 (with astigmatism, two boxes); mild frustration at astigmatism premium. (Baby Kingdom, 2018-12-01) — [source](https://www.baby-kingdom.com/forum.php?mod=viewthread&tid=22174432)
- [negative] First-time wearer spent about an hour getting the lens in and had even worse trouble removing it; the shop's own tutorial videos did not help. Onboarding friction complaint. (HKGolden, 2014-12-01) — [source](https://forum.hkgolden.com/view.aspx?message=5577774&type=BW)
- [neutral] Discussion concluded removal difficulty with Acuvue One Day was about base-curve mismatch (8.5mm) rather than the brand itself; users self-corrected an initial brand complaint into a general fit issue. (Baby Kingdom, 2017-06-01) — [source](https://www.baby-kingdom.com/forum.php?mod=viewthread&tid=19905143)

**Scraped examples:**

- [review, rating=5.0] I've bought this many times.
- [review, rating=5.0] Expiration date is until 2027, and each box includes disinfecting solution 👍🏻
- [xhs_post, sentiment=positive] Ladies! Acuvue Oasys contact lenses are great to wear.
