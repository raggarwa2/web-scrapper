# Market Intelligence Agent System — Architecture Design

**Status:** Design doc, v1 — based on the HK contact lens pipeline as the reference implementation
**Author's stance:** written as a principal engineer would review this codebase and propose where it goes next — grounded in what's already built and working, not a green-field proposal

---

## 0. Design principles (derived from what already works and what already broke)

These aren't abstract best practices — each one maps to something we actually observed in this project:

1. **Discover → Extract → Classify-relevance → Store-with-flags, never silently drop.**
   Proven in `youtube_scraper.py` and `instagram_scraper.py`: a cheap regex/whitelist pass first, then an LLM brand-relevance check, both **flagged, not filtered out** at write time. This is why those two sources are auditable at all. The Lazada TH contamination happened precisely because TH discovery *didn't* do this — it wrote products with no relevance flag, so bad matches were indistinguishable from good ones until someone manually queried for them.

2. **Flags at write time are necessary but not sufficient — they must be enforced at every read site.**
   `app.py`'s own inline comment says "call `on_topic_comments()` before using for scoring" — and one part of the same file follows that rule (`youtube_bh`) while two others don't (`_total_content`, `_youtube_f_count`). A flag column is not a contract until something enforces it everywhere it's read. This is the single biggest recurring failure mode found this session and the primary justification for Section 4 below.

3. **Config-driven onboarding, not code changes, for brand/market additions within an existing channel.**
   Already proven true: CooperVision onboarded onto HKTVmall via a config block, no new architecture. This must remain the bar for any new pillar or channel agent — if adding a brand requires touching agent code, the agent's interface is wrong.

4. **Client/project isolation via separate config + DB, shared agent code as a library.**
   Already proven by the TH Acne-Aid (Inova) engagement being fully separate (`pipeline_th.py`, `config_th.yaml`, `acneaid_th.db`) from the HK contact lens pipeline, despite both being "the same kind of problem." This is the right isolation boundary — replicate it for projects #2 and #3, don't merge client data stores.

5. **SQLite WAL + single-writer-thread stays.** It works at current scale (thousands, not millions, of rows). Don't rearchitect storage prematurely — there is no evidence this is a bottleneck.

6. **Show-before-modify, protected-file governance extends to agents, not just humans.** Any agent capable of writing a client-facing artifact (a report, a recommendation) needs the same "show sample output, get sign-off" gate that already governs Claude Code sessions touching `pipeline_v2.py`.

---

## 1. Layered architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 5 — DELIVERY                                           │
│ Streamlit dashboard (app.py) · monthly report export         │
├─────────────────────────────────────────────────────────────┤
│ Layer 4 — ORCHESTRATOR AGENT                                 │
│ Combines pillar scores + evidence → ranked growth blockers   │
├─────────────────────────────────────────────────────────────┤
│ Layer 3 — PILLAR AGENTS (scoring, read-only)                 │
│ Reputation ✅ built · Pricing 🔲 next · Distribution 🔲 later │
├─────────────────────────────────────────────────────────────┤
│ Layer 2b — QA / AUDIT AGENT (gate)                            │
│ Validates Layer 2 before any pillar agent is allowed to run   │
├─────────────────────────────────────────────────────────────┤
│ Layer 2 — STORAGE                                             │
│ Per-project SQLite DBs, standardized content-column contract  │
├─────────────────────────────────────────────────────────────┤
│ Layer 1 — CHANNEL AGENTS (data collection)                    │
│ E-commerce · Video · Social/Photo · Forum/Text                │
└─────────────────────────────────────────────────────────────┘
```

Data flows up. Governance (config, protected-file rules, show-before-modify) applies at every layer, not just Layer 1.

---

## 2. Layer 1 — Channel agent contract

Every channel agent, regardless of what it scrapes, must implement the same interface:

**Input:** `{brand, competitor_list, market, keywords, site_config}` — never hardcoded values.

**Output:** rows written to storage with these mandatory columns, regardless of source-specific fields:
`market`, `brand`, `[content field]`, `scraped_at`/`discovered_at`, and **one or two relevance flags**.

**Behavior contract:**
- Two-layer relevance where the channel supports it: cheap whitelist/regex pre-filter (post mentions a lens term at all) + LLM brand-check (post mentions a lens term but is it about *this* brand). This is exactly the YouTube/Instagram pattern — codify it as the default, not a nice-to-have.
- Never drop non-matching rows. Flag and keep. Auditability depends on this.

**Current mapping of channel agent types to real implementations:**

| Agent type | Channels covered | Implementation | Reuse status |
|---|---|---|---|
| E-commerce agent | HKTVmall (Algolia API), 393lens (Playwright), WateryEyes (Shopify/Judge.me) | `pipeline_v2.py`, `wateryeyes` scraper | ✅ Working pattern; new storefront = new adapter |
| Video agent | YouTube | `youtube_scraper.py` + `youtube_signals.py` | ✅ The cleanest reference implementation — use as the template for new agents |
| Social/photo agent | Instagram | `instagram_scraper.py` + `instagram_signals.py` | ✅ Mirrors YouTube's pattern closely |
| Forum/text agent | LIHKG, XHS | `lihkg_scraper.py`, `xhs_scraper.py` (Apify) | ⚠️ Same shape, but currently separate one-off scripts — worth consolidating behind one interface even if execution differs per platform |

**Not yet decided:** whether XHS should formally live under "forum/text" or "social/photo" given it's closer to Instagram in content shape (posts + comments) than to LIHKG (threaded discussion). Low-stakes either way — flagging so it's a conscious choice, not an accident.

---

## 3. Layer 2 — Storage: standardize the contract, not the database

**Principal-engineer call: do not merge `lensdata.db`, `youtube_data.db`, `instagram_data.db`, and `wateryeyes.db` into one database right now.** At current scale (~2,400 products, ~8,000 reviews, low thousands of social rows per source) the migration risk and cross-team coordination cost outweighs the benefit. `app.py` already proves you can blend four separately-owned databases into one composite score without merging them physically.

**What should be standardized instead:** every table holding brand-attributed content must carry the same column contract that already, mostly by convention, exists:

- `market` (HK only now, but keep the column — it's what made the TH exclusion a one-line filter instead of a rewrite)
- `brand` (consistent spelling — the `"OLENS"` vs `"Olens"` bug already found and fixed once; this will recur without a shared lookup)
- Relevance flag(s), named consistently: prefer `brand_relevant` (post/video-level) and `is_lens_relevant` (comment/content-level) as the standard pair going forward, since that's what YouTube and Instagram both already use
- `scraped_at` / `discovered_at`

**Recommendation:** a lightweight `schemas.yaml` or similar registry — not a new database, just a documented contract the QA agent (Section 4) validates every project DB against before pillar agents are allowed to query it.

---

## 4. Layer 2b — QA / Audit agent (build this first)

This is the highest-leverage thing to build, and it's the one piece of this architecture with no build risk — every check in it is a bug we already found by hand this session. Codifying them turns manual detective work into a script that runs before every pillar-agent invocation.

**Checks, each traceable to a real finding:**

| Check | What it catches | Real example found |
|---|---|---|
| Sentinel-value audit | Fields using `0`/blank as "missing" instead of NULL | `avg_rating=0.0` corrupting HK average from ~4.4 to ~2.1 |
| Relevance-flag enforcement | Downstream code reading a flagged table without filtering on the flag | `app.py`'s `_total_content` / `_youtube_f_count` skipping `on_topic_comments()` |
| Cross-brand parity | A brand/market cell with anomalously low relevant-row count vs its siblings | CooperVision HK: 12 YouTube videos, 0 relevant |
| Price-pair / field completeness | % of rows missing a field needed for a pillar score, per brand | Only 855/2,405 products have both list and sale price; Alcon/CooperVision on WateryEyes have list price on <50% of rows |
| Discovery-source contamination | Rows attributed to a brand that don't actually mention it | Lazada TH mistagging (moot now TH is out of scope, but the check itself should stay in the agent — it will recur the moment a new channel or market is added) |
| Geo-validation audit | Domain + Language + Locus, pass 2 of 3 — for any manually-researched (non-scraped) source | Already specified in your Research_prompts framework; fold it into this same agent rather than keeping it a separate manual step |

**Contract:** takes a DB path + schema registry, outputs a markdown report with pass/fail per check, per brand/market. Pillar agents should refuse to run — or should visibly flag low-confidence — against a DB that hasn't passed this.

---

## 5. Layer 3 — Pillar agent contract

**Input:** `{brand, competitor_list, market, date_range}`
**Output:** a standard `Score` object:
```
{
  score: 0-100,
  n: sample size,
  confidence: derived from n vs a MIN_N threshold,
  evidence: [...],           # the specific facts backing the score
  data_quality_flags: [...]  # anything the QA agent surfaced that affects this score
}
```

**Reputation pillar — already built, needs extraction.** `_brand_score()` in `app.py` already does this: blends Reviews + XHS + LIHKG + YouTube, sqrt(n)-weighted, with a minimum-sample threshold per source, and correctly calls `on_topic_comments()`. It's currently coupled to Streamlit session state and sidebar selections. **Recommendation: extract the pure scoring function so it's callable from a script or future API, not just from inside a page render** — this is what makes it reusable by the Orchestrator (Section 6) and by projects #2/#3 without a Streamlit dependency.

**Pricing pillar — next to build, data is ready.** The discount-depth calculation from this session (`AVG((original_price - selling_price) / original_price)`) is the core of it. Needs: price-position banding (premium/parity/discount vs. competitor average), and the same confidence/n-threshold discipline as Reputation.

**Distribution pillar — later, low scraping cost.** Mostly a coverage-matrix calculation against the existing channel taxonomy (already documented in Brand_retail research) — brand's % of taxonomy channels present vs. competitors'. Doesn't need new scraping if channel-presence data is already captured by the e-commerce agents.

**Launches / News / Listing-signal pillars — lower priority, may not need automation.** These currently run fine as Deep-Research-prompt-driven manual passes at monthly cadence (per the six-pillar framework). Given no case study validates automating this class of research (see prior conversation), the principal-engineer call is: **don't automate these yet.** Monthly manual cadence is proportionate to how often they change.

---

## 6. Layer 4 — Orchestrator agent

The only agent allowed to see cross-pillar data. Takes every pillar's `Score` object for a brand + its competitor set, and:

- Applies **configurable pillar weights per client** (a skincare client may weight Reputation higher; a med-device-adjacent client may weight Distribution/compliance signals higher) — weights are config, not code.
- **Down-weights or excludes low-confidence pillars** rather than letting a thin sample (e.g. n=12) dominate a ranked output — this is the exact failure mode `MIN_N_FOR_SOURCE` already prevents inside `_brand_score()`; the Orchestrator needs the same discipline one level up, across pillars instead of across sources within one pillar.
- Ranks blockers by (competitor gap size) × (confidence), and outputs a structured diagnostic — a ranked list, each item citing back to the specific pillar evidence, not a synthesized paragraph that loses the trail.

**Build this only once there are 2+ real pillar scores** (Reputation + Pricing). Building it against Reputation alone would just be reformatting existing code with no new capability proven.

---

## 7. Config / onboarding layer

| Change | What's required |
|---|---|
| New brand, existing market, existing channel | Config edit only (proven: CooperVision → HKTVmall) |
| New market, existing client | New channel-agent adapters for that market's sites + a config block — this is real engineering work, not config-only |
| New client entirely | New config + new DB set, reusing all channel-agent, pillar-agent, and orchestrator code unchanged |

This table is the actual reusability promise of this whole architecture — worth checking any new build against it before starting.

---

## 8. Build sequence

1. **Now:** QA/Audit agent (Section 4) + fix the two confirmed bugs (`avg_rating` sentinel, `on_topic_comments()` enforcement in `app.py`)
2. **Next:** Extract Reputation pillar agent as a standalone module; build the Pricing pillar agent
3. **Then:** Orchestrator, combining Reputation + Pricing — first proof that ranked-blocker output actually works end to end
4. **Then:** Distribution pillar agent; expand Orchestrator to 3 pillars
5. **Only if a client engagement specifically needs it:** Launches/News/Listing-signal automation — otherwise keep these as manual Deep-Research runs

---

## 9. Explicit non-goals (guardrails)

- **No standalone "competitor agent."** Competitors are parameters passed into pillar agents, not a separate agent type — building one would duplicate every pillar agent's logic for no reason.
- **No database merge** at current scale — see Section 3.
- **No automated Launches/News/Listing pillars** without a specific client need — no case study justifies the build cost yet, and the manual cadence already fits a monthly deliverable.
- **No orchestrator output ships to a client without a human review gate** — same show-before-modify principle that already governs `pipeline_v2.py` changes should govern anything the Orchestrator drafts as a client-facing recommendation.

---

*One-line takeaway: the architecture that scales across projects #2 and #3 is exactly the layer boundary you already have working by accident in `app.py` and `youtube_scraper.py` — this doc just names it, codifies the QA gate that would have caught every bug found this session, and sequences the two pillars still missing before building the orchestrator on top.*
