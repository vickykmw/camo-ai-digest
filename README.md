# AI x Business -- Daily Digest

Automated daily scrape across multiple sources: HBR (latest + topic feeds + IdeaCast + Cold Call), NBER Working Papers, MIT Sloan Management Review, arXiv (econ.GN, cs.CY), and McKinsey Insights.

Filter: items must contain at least one **strong AI term** (AI, machine learning, LLM, generative AI, Copilot, agentic, etc.) and at least one **business-domain term** (organization, firm, strategy, work, management, leadership, executive, team, culture, innovation). Booster terms (digital transformation, automation, analytics, predictive) only count if a strong AI term is also present.

Top 10 per day, ranked by keyword density x recency. Max 4 items per source for diversity. Recency window: 30 days with linear decay.

Each item is then enriched by the Claude API (claude-sonnet-4-6): a clean summary, a CAMO pillar tag, a centre angle, matched CAMO research, draft LinkedIn/X captions, and a visual concept. Every item carries an `[ ] APPROVE FOR SOCIAL` checkbox for the editorial team, and a `<date>.enriched.json` sidecar is written for the downstream image step.

Enrichment results are cached in `enrichment_cache.json`: when an item re-appears in a later run it is reused from the cache with no API call, so running daily stays inexpensive even though most items re-appear.

Runs daily at **00:00 UTC** (about 7 pm CDT / 6 pm CST).

## Recent digests

- [2026-05-16](digests/2026-05-16.md)
- [2026-05-15](digests/2026-05-15.md)
- [2026-05-14](digests/2026-05-14.md)
- [2026-05-13](digests/2026-05-13.md)