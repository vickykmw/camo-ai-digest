# AI x Business -- Weekly Digest

Automated weekly scrape across multiple sources: HBR (latest + topic feeds + IdeaCast + Cold Call), NBER Working Papers, MIT Sloan Management Review, arXiv (econ.GN, cs.CY), and McKinsey Insights.

Filter: items must contain at least one **strong AI term** (AI, machine learning, LLM, generative AI, Copilot, agentic, etc.) and at least one **business-domain term** (organization, firm, strategy, work, management, leadership, executive, team, culture, innovation). Booster terms (digital transformation, automation, analytics, predictive) only count if a strong AI term is also present.

Top 10 items per week, ranked by keyword density x recency. Max 4 items per source for diversity. Recency window: 30 days with linear decay.

Each item is then enriched by the Claude API (claude-sonnet-4-6): a clean summary, a CAMO pillar tag, a centre angle, matched CAMO research, draft LinkedIn/X captions, and a visual concept. Every item carries an `[ ] APPROVE FOR SOCIAL` checkbox for the editorial team. A `<week>.enriched.json` sidecar is written alongside each `.md` for the downstream cluster + image steps.

Enrichment results are cached in `enrichment_cache.json`: when an item re-appears in a later run it is reused from the cache with no API call.

Runs weekly on **Monday 00:00 UTC** (Monday 7 pm CDT / 6 pm CST in Chicago).

## Recent digests

- [2026-05-18](digests/2026-05-18.md)
- [2026-05-17](digests/2026-05-17.md)
- [2026-05-16](digests/2026-05-16.md)
- [2026-05-15](digests/2026-05-15.md)
- [2026-05-14](digests/2026-05-14.md)
- [2026-05-13](digests/2026-05-13.md)
- [2026-05-13_2026-05-20](digests/2026-05/2026-05-13_2026-05-20.md)
- [2026-05-12_2026-05-19](digests/2026-05/2026-05-12_2026-05-19.md)
- [2026-05-11_2026-05-18](digests/2026-05/2026-05-11_2026-05-18.md)