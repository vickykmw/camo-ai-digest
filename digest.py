#!/usr/bin/env python3
"""
Daily AI x Business digest across HBR + NBER + MIT SMR + arXiv + McKinsey.

Filter rule (strict AND):
  - at least one STRONG AI term, OR a BOOSTER AI term accompanied by a STRONG AI term
  - at least one business-domain term
Ranking: (AI matches + booster bonus + domain matches) * recency
Diversity: cap of MAX_PER_SOURCE in the daily top N.

v5 adds a Claude enrichment step: after the top N is selected, each item is
sent to the Claude API together with the CAMO research index (camo_index.json).
Claude returns a structured summary, a pillar tag, a CAMO "centre angle",
matched CAMO papers, draft LinkedIn/X captions, and a visual concept. The
enriched markdown carries an "[ ] APPROVE FOR SOCIAL" checkbox per item for
the editorial team, and a machine-readable sidecar (<date>.enriched.json) is
written alongside it for the downstream Higgsfield step.

Enrichment degrades gracefully: if the anthropic package is missing or
ANTHROPIC_API_KEY is unset, the digest is still written using the raw RSS
excerpts (v4 behaviour). A failure on one item never blocks the others.

To avoid paying to "re-cook the same plate", enrichment results are cached
in enrichment_cache.json (keyed by article link). When an item re-appears in
a later run, its enrichment is reused from the cache with no API call -- so
running daily is cheap even though most items re-appear. Only genuinely new
items cost tokens. The cache must be committed back to the repo (like
seen.json) for this to work across runs.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

import feedparser

try:
    import anthropic
except ImportError:
    anthropic = None

# ---------------------------------------------------------------------------
# Version stamp -- check the workflow log for "DIGEST SCRIPT v5" to confirm
# this file is the one running.
# ---------------------------------------------------------------------------
VERSION = "v5 (2026-05-14)"

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

FEED_URLS = [
    # HBR -- written articles (main + topic-specific)
    "https://hbr.org/the-latest/feed",
    "https://hbr.org/topic/subject/artificial-intelligence/feed",
    "https://hbr.org/topic/subject/strategy/feed",
    "https://hbr.org/topic/subject/managing-people/feed",
    "https://hbr.org/topic/subject/organizational-culture/feed",

    # HBR podcasts
    "http://feeds.harvardbusiness.org/harvardbusiness/ideacast",
    "http://feeds.harvardbusiness.org/harvardbusiness/cold-call",

    # NBER Working Papers
    "https://www.nber.org/papers.rss",

    # MIT Sloan Management Review
    "https://sloanreview.mit.edu/feed/",

    # arXiv -- economics + computers & society (broader categories like
    # cs.AI / cs.LG are intentionally omitted to avoid 100+/day technical
    # ML papers that won't match the business filter anyway).
    "https://rss.arxiv.org/rss/econ.GN",
    "https://rss.arxiv.org/rss/cs.CY",

    # McKinsey Insights (includes McKinsey Global Institute items)
    "https://www.mckinsey.com/insights/rss",
]

PER_FEED_ITEM_LIMIT = 30
TOP_N = 10
MAX_PER_SOURCE = 4              # diversity cap: no more than this many items from any one source in the top N
RECENCY_WINDOW_DAYS = 30
REAPPEAR_LOOKBACK_DAYS = 7

# ---------------------------------------------------------------------------
# Enrichment configuration
# ---------------------------------------------------------------------------

ENABLE_ENRICHMENT = True                     # master switch for the Claude step
ANTHROPIC_MODEL = "claude-sonnet-4-6"        # Opus-level quality at Sonnet price
ENRICH_MAX_TOKENS = 1500                     # generous headroom for the JSON payload
ENRICH_RETRIES = 2                           # attempts per item before giving up

# Enrichment cache: re-appearing items reuse their prior enrichment instead of
# calling the API again. Set REUSE_CACHED_ENRICHMENT = False to force a fresh
# call for every item -- do this after editing the prompt below, so the change
# actually takes effect on items already in the cache. A model change is
# detected automatically (cached entries from a different model are ignored).
REUSE_CACHED_ENRICHMENT = True
ENRICHMENT_CACHE_MAX_AGE_DAYS = 60           # prune cache entries older than this

# ---------------------------------------------------------------------------
# Keyword pools
# ---------------------------------------------------------------------------

# STRONG AI terms -- match alone is enough on the AI side.
AI_STRONG_PATTERNS = [
    r"\bAI\b",
    r"\bA\.I\.",
    r"\bartificial intelligence\b",
    r"\bgenerative AI\b",
    r"\bgen[ -]?AI\b",
    r"\bmachine learning\b",
    r"\bML\b",
    r"\bLLMs?\b",
    r"\blarge language models?\b",
    r"\bdeep learning\b",
    r"\bneural networks?\b",
    r"\bChatGPT\b",
    r"\bCopilot\b",
    r"\bchatbots?\b",
    r"\balgorithmic\b",
    r"\balgorithms?\b",
    r"\bagentic\b",
    r"\bdata science\b",
    r"\bfoundation models?\b",
    r"\bGPT-?\d+\b",
    r"\bClaude\b",
    r"\bGemini\b",
]

# BOOSTER AI terms -- only count if at least one STRONG term is also present.
# Per your rule: "digital transformation" must co-occur with a real AI term.
AI_BOOSTER_PATTERNS = [
    r"\bdigital transformation\b",
    r"\bdigitali[sz]ation\b",
    r"\bautomation\b",
    r"\banalytics\b",
    r"\bpredictive\b",
]

# Business-context terms (existing five plus the five you added).
DOMAIN_PATTERNS = [
    r"\borgani[sz]ations?\b",
    r"\borgani[sz]ational\b",
    r"\bfirms?\b",
    r"\bstrateg(?:y|ies|ic|ically)\b",
    r"\bwork(?:place|places|force|forces|ers?|ing)?\b",
    r"\bmanage(?:ment|r|rs|ial|ing)?\b",
    r"\bleader(?:s|ship)?\b",
    r"\bexecutives?\b",
    r"\bteams?\b",
    r"\bculture\b",
    r"\bcultural\b",
    r"\binnovation\b",
]

REPO_ROOT = Path(__file__).parent
DIGESTS_DIR = REPO_ROOT / "digests"
SEEN_FILE = REPO_ROOT / "seen.json"
README_FILE = REPO_ROOT / "README.md"
CAMO_INDEX_FILE = REPO_ROOT / "camo_index.json"
ENRICHMENT_CACHE_FILE = REPO_ROOT / "enrichment_cache.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True))


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def count_matches(text: str, patterns: list) -> int:
    total = 0
    for p in patterns:
        total += len(re.findall(p, text, flags=re.IGNORECASE))
    return total


def parse_pub_date(entry):
    if getattr(entry, "published_parsed", None):
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    if getattr(entry, "updated_parsed", None):
        return datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
    return None


def extract_authors(entry) -> str:
    try:
        authors = getattr(entry, "authors", None)
        if authors:
            names = []
            for a in authors:
                if isinstance(a, dict):
                    name = (a.get("name") or "").strip()
                elif isinstance(a, str):
                    name = a.strip()
                else:
                    name = ""
                if name:
                    names.append(name)
            if names:
                return ", ".join(names)
    except Exception:
        pass
    try:
        author = getattr(entry, "author", None)
        if isinstance(author, str) and author.strip():
            return author.strip()
    except Exception:
        pass
    try:
        itunes_author = getattr(entry, "itunes_author", None)
        if isinstance(itunes_author, str) and itunes_author.strip():
            return itunes_author.strip()
    except Exception:
        pass
    return "Unknown"


def feed_label(url: str) -> str:
    if "hbr.org/the-latest" in url:
        return "HBR Article"
    if "hbr.org/topic/subject/artificial-intelligence" in url:
        return "HBR Topic: AI"
    if "hbr.org/topic/subject/strategy" in url:
        return "HBR Topic: Strategy"
    if "hbr.org/topic/subject/managing-people" in url:
        return "HBR Topic: Managing People"
    if "hbr.org/topic/subject/organizational-culture" in url:
        return "HBR Topic: Org Culture"
    if "ideacast" in url:
        return "HBR IdeaCast (podcast)"
    if "cold-call" in url or "coldcall" in url:
        return "HBR Cold Call (podcast)"
    if "nber.org" in url:
        return "NBER Working Paper"
    if "sloanreview.mit.edu" in url:
        return "MIT Sloan Management Review"
    if "arxiv.org/rss/econ.GN" in url:
        return "arXiv: Economics (econ.GN)"
    if "arxiv.org/rss/cs.CY" in url:
        return "arXiv: Computers & Society (cs.CY)"
    if "mckinsey.com" in url:
        return "McKinsey Insights"
    return url


# ---------------------------------------------------------------------------
# CAMO index + Claude enrichment
# ---------------------------------------------------------------------------

def load_camo_index() -> list:
    """Load the centre's research index. Returns [] if the file is absent
    so the pipeline still runs (enrichment just won't have papers to match)."""
    if not CAMO_INDEX_FILE.exists():
        print(f"[warn] {CAMO_INDEX_FILE.name} not found -- enrichment will run "
              f"without CAMO paper matching")
        return []
    try:
        data = json.loads(CAMO_INDEX_FILE.read_text())
        if not isinstance(data, list):
            print(f"[warn] {CAMO_INDEX_FILE.name} is not a JSON list -- ignoring")
            return []
        print(f"[info] loaded CAMO index: {len(data)} items")
        return data
    except Exception as e:
        print(f"[warn] could not parse {CAMO_INDEX_FILE.name}: {e}")
        return []


def build_camo_context(camo_index: list) -> str:
    """Format the CAMO index into a compact text block for the prompt."""
    if not camo_index:
        return ("### CAMO RESEARCH INDEX\n\n"
                "(No index available. Leave matched_camo as an empty list.)")
    lines = [
        f"### CAMO RESEARCH INDEX ({len(camo_index)} items)",
        "",
        "These are the published research outputs of the HKU Centre for AI, "
        "Management and Organization (CAMO). Use them to find genuine "
        "connections to the article being enriched. Only claim a match when "
        "the topical link is real -- an empty match list is better than a "
        "forced one.",
        "",
    ]
    for it in camo_index:
        lines.append(f'[{it.get("type","item")}] "{it.get("title","(untitled)")}" '
                     f'({it.get("year","n.d.")})')
        lines.append(f'  id: {it.get("id","")} | url: {it.get("url","")}')
        lines.append(f'  pillars: {", ".join(it.get("pillars", [])) or "none"}')
        lines.append(f'  tags: {", ".join(it.get("tags", [])) or "none"}')
        lines.append(f'  abstract: {(it.get("abstract") or "").strip()}')
        lines.append("")
    return "\n".join(lines)


ENRICHMENT_INSTRUCTIONS = """\
You are an editorial assistant for the HKU Centre for AI, Management and
Organization (CAMO). For each scraped article, produce structured enrichment
that lets a human editor quickly decide whether to publish it as a LinkedIn/X
post, and gives them a strong draft to work from.

CENTRE PILLARS (every post should map to exactly one):
1. AI Adoption -- how AI reshapes organizational management, hierarchies, and
   job boundaries inside firms.
2. AI & Incentives -- how AI changes effort, contracts, motivation, and the
   conditions for excellent work.
3. AI & Jobs -- what AI means for careers, accountability, and the path from
   pilot deployment to org-wide adoption.
4. AI Algorithms & Data -- statistical and algorithmic foundations for
   trustworthy AI-driven decisions.

AUDIENCE:
- Primary: business managers, C-suite, policy makers.
- Secondary: informed general public interested in AI's effect on economics
  and markets.

TONE: empirical, measured, accessible. Authoritative without academic jargon.
Confident without promotional language. You are a research centre, not a
marketing agency."""


ENRICHMENT_OUTPUT_SPEC = """\
Return ONLY a JSON object -- no preamble, no markdown code fences -- with
exactly these fields:

{
  "summary": "100-150 word plain-English summary. Lead with the question or
              finding, not the author.",
  "pillar": "Exactly one of: AI Adoption | AI & Incentives | AI & Jobs |
             AI Algorithms & Data | No fit",
  "pillar_relevance": "integer 0-100; 80+ = directly on-topic, 50-79 =
             adjacent, below 50 = a stretch",
  "centre_angle": "2-3 sentences a CAMO editor could publish under the
             centre's name. Pattern: 'This [study/article] argues X. From
             CAMO's perspective on [pillar topic], the implication is Y.'
             If a CAMO paper below is genuinely related, reference it here
             naturally. Sound like a researcher, not a press release.",
  "matched_camo": [
      {"id": "<id from the CAMO index>", "title": "<title>", "url": "<url>",
       "reason": "<one sentence on the genuine connection>"}
  ],
  "key_takeaway": "Single sentence -- what a busy manager carries away.",
  "audience_relevance": {
      "managers_csuite": "high|medium|low",
      "policy_makers": "high|medium|low",
      "general_econ_public": "high|medium|low"
  },
  "visual_concept": "1-2 sentence brief for an image generator. A concrete
             metaphor or scene. Avoid cliches: robotic hands, glowing brains,
             handshakes, lightbulbs, binary streams, circuit boards. Prefer
             architectural, geometric, organizational, or data-shape
             metaphors.",
  "linkedin_caption_draft": "120-200 words. Hook line, 2-3 lines of substance,
             one line of CAMO angle, a closing question, then 3-5 hashtags.
             At most 1-2 emoji.",
  "x_caption_draft": "1-2 posts, max 280 characters each. Hook + finding +
             CAMO take + link. Separate a second post with a blank line.",
  "red_flags": ["any concerns: paywalled, not peer-reviewed, contested
             finding, conflict of interest, near-duplicate of recent
             coverage; empty list if none"]
}

RULES:
- matched_camo: include 0-2 items. Only real topical links. An empty list is
  correct when nothing in the index genuinely connects. Never invent an id.
- Never fabricate findings beyond the excerpt. If the excerpt is thin, say so
  in the summary and lower pillar_relevance.
- Banned words: "fascinating", "must-read", "game-changing", "revolutionary",
  "transformative", "unlock", "harness", "leverage" (as a verb), "in today's
  fast-paced world".
- centre_angle is the most important field. Make it specific and grounded.
- Output valid JSON only."""


def build_enrichment_prompt(article: dict, camo_context: str) -> str:
    return (
        ENRICHMENT_INSTRUCTIONS
        + "\n\n"
        + camo_context
        + "\n\n### ARTICLE TO ENRICH\n\n"
        + f"Title: {article['title']}\n"
        + f"Source: {article['source']}\n"
        + f"Authors: {article['authors']}\n"
        + f"Published: {article['published_display']}\n"
        + f"Link: {article['link']}\n"
        + f"RSS excerpt: {article['summary']}\n\n"
        + ENRICHMENT_OUTPUT_SPEC
    )


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response, tolerating code fences."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # If there's stray prose around it, grab the outermost { ... }.
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


def enrich_one_item(client, article: dict, camo_context: str) -> dict:
    """Call Claude for a single article. Raises on persistent failure."""
    prompt = build_enrichment_prompt(article, camo_context)
    last_err = None
    for attempt in range(1, ENRICH_RETRIES + 1):
        try:
            resp = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=ENRICH_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(
                block.text for block in resp.content
                if getattr(block, "type", None) == "text"
            )
            return _extract_json(text)
        except Exception as e:  # noqa: BLE001 -- we want to retry on anything
            last_err = e
            print(f"[warn]   attempt {attempt}/{ENRICH_RETRIES} failed: {e}")
    raise RuntimeError(f"enrichment failed after {ENRICH_RETRIES} attempts: {last_err}")


# Fields written onto each article dict by the enrichment step (internal names).
ENRICH_FIELDS = (
    "claude_summary", "pillar", "pillar_relevance", "centre_angle",
    "matched_camo", "key_takeaway", "audience_relevance", "visual_concept",
    "linkedin_caption_draft", "x_caption_draft", "red_flags",
)


def load_enrichment_cache() -> dict:
    """Load prior enrichment results, keyed by article link. Returns {} if the
    file is absent or unreadable so the run still proceeds (just without reuse)."""
    if ENRICHMENT_CACHE_FILE.exists():
        try:
            data = json.loads(ENRICHMENT_CACHE_FILE.read_text())
            if isinstance(data, dict):
                return data
            print(f"[warn] {ENRICHMENT_CACHE_FILE.name} is not a JSON object -- ignoring")
        except Exception as e:
            print(f"[warn] could not parse {ENRICHMENT_CACHE_FILE.name}: {e}")
    return {}


def save_enrichment_cache(cache: dict, today: date) -> None:
    """Persist the cache, pruning entries older than ENRICHMENT_CACHE_MAX_AGE_DAYS
    (those items can't re-enter the top N anyway, given the recency window).
    Wrapped so a cache-write problem can never break the digest run."""
    try:
        cutoff = today - timedelta(days=ENRICHMENT_CACHE_MAX_AGE_DAYS)
        pruned = {}
        for link, rec in cache.items():
            stamp = rec.get("enriched_at", "")
            try:
                keep = date.fromisoformat(stamp) >= cutoff
            except Exception:
                keep = True  # unparseable date -> keep rather than lose data
            if keep:
                pruned[link] = rec
        ENRICHMENT_CACHE_FILE.write_text(
            json.dumps(pruned, indent=2, ensure_ascii=False, sort_keys=True)
        )
        dropped = len(cache) - len(pruned)
        note = f", pruned {dropped} stale" if dropped > 0 else ""
        print(f"[ok] wrote {ENRICHMENT_CACHE_FILE.name} ({len(pruned)} entries{note})")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] could not save {ENRICHMENT_CACHE_FILE.name}: {e}")


def _normalize_enrichment(data: dict) -> dict:
    """Map a raw Claude JSON response to the internal field names. This is what
    gets stored in the cache and applied to article dicts, so both the API path
    and the cache path stay perfectly consistent."""
    return {
        "claude_summary": (data.get("summary") or "").strip(),
        "pillar": (data.get("pillar") or "No fit").strip(),
        "pillar_relevance": data.get("pillar_relevance", ""),
        "centre_angle": (data.get("centre_angle") or "").strip(),
        "matched_camo": data.get("matched_camo") or [],
        "key_takeaway": (data.get("key_takeaway") or "").strip(),
        "audience_relevance": data.get("audience_relevance") or {},
        "visual_concept": (data.get("visual_concept") or "").strip(),
        "linkedin_caption_draft": (data.get("linkedin_caption_draft") or "").strip(),
        "x_caption_draft": (data.get("x_caption_draft") or "").strip(),
        "red_flags": data.get("red_flags") or [],
    }


def _apply_enrichment(art: dict, normalized: dict) -> None:
    """Copy normalized enrichment fields onto an article dict and flag it done."""
    for field in ENRICH_FIELDS:
        art[field] = normalized.get(field)
    art["enriched"] = True


def _mark_unenriched(top: list, reason: str) -> None:
    for art in top:
        art["enriched"] = False
        art["enrichment_error"] = reason


def enrich_with_claude(top: list, camo_index: list, today: date) -> None:
    """Enrich the top-N items in place. Never raises -- on any setup problem
    it marks items unenriched and the digest falls back to RSS excerpts.

    Items already in enrichment_cache.json (same model) are reused with no API
    call. Only genuinely new items cost tokens."""
    if not ENABLE_ENRICHMENT:
        print("[info] enrichment disabled (ENABLE_ENRICHMENT=False)")
        _mark_unenriched(top, "enrichment disabled")
        return
    if anthropic is None:
        print("[warn] 'anthropic' package not installed -- skipping enrichment "
              "(add it to the workflow's pip install step)")
        _mark_unenriched(top, "anthropic package not installed")
        return
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[warn] ANTHROPIC_API_KEY not set -- skipping enrichment "
              "(add it as a GitHub Actions secret)")
        _mark_unenriched(top, "ANTHROPIC_API_KEY not set")
        return

    client = anthropic.Anthropic(api_key=api_key)
    camo_context = build_camo_context(camo_index)
    cache = load_enrichment_cache() if REUSE_CACHED_ENRICHMENT else {}
    if REUSE_CACHED_ENRICHMENT:
        print(f"[info] enrichment cache: {len(cache)} entries loaded")
    print(f"[info] enriching {len(top)} items with {ANTHROPIC_MODEL} "
          f"(reuse cache: {REUSE_CACHED_ENRICHMENT}) ...")

    api_calls = 0
    cache_hits = 0
    fail = 0
    for idx, art in enumerate(top, 1):
        link = art["link"]
        cached = cache.get(link) if REUSE_CACHED_ENRICHMENT else None

        # Cache hit -- reuse, but only if it was produced by the current model.
        if cached and cached.get("model") == ANTHROPIC_MODEL and cached.get("data"):
            _apply_enrichment(art, cached["data"])
            art["enrichment_cached"] = True
            cache_hits += 1
            print(f"[ok]  ({idx}/{len(top)}) [cached] {art['title'][:60]}")
            continue

        # Cache miss -- call the API, then store the result for next time.
        try:
            raw = enrich_one_item(client, art, camo_context)
            normalized = _normalize_enrichment(raw)
            _apply_enrichment(art, normalized)
            art["enrichment_cached"] = False
            cache[link] = {
                "enriched_at": today.isoformat(),
                "model": ANTHROPIC_MODEL,
                "title": art["title"],          # human-readable, for eyeballing the cache
                "data": normalized,
            }
            api_calls += 1
            print(f"[ok]  ({idx}/{len(top)}) [api]    {art['title'][:60]}")
        except Exception as e:  # noqa: BLE001
            art["enriched"] = False
            art["enrichment_error"] = str(e)
            fail += 1
            print(f"[warn] ({idx}/{len(top)}) enrichment failed -- "
                  f"{art['title'][:60]}: {e}")

    if REUSE_CACHED_ENRICHMENT:
        save_enrichment_cache(cache, today)

    print(f"[info] enrichment complete: {api_calls} new API call(s), "
          f"{cache_hits} reused from cache, {fail} failed")


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _blockquote(text: str) -> list:
    """Render multi-line text as a markdown blockquote, preserving blank lines."""
    out = []
    for line in (text or "").split("\n"):
        out.append(f"> {line}" if line.strip() else ">")
    return out


def render_item_md(i: int, art: dict) -> list:
    """Return the markdown lines for one digest item."""
    lines = []
    badge = "  `(re-appear)`" if art.get("reappear") else ""
    lines.append(f"### {i}. {art['title']}{badge}")
    lines.append("")

    # Approval checkbox -- the editorial team ticks this; a downstream
    # workflow reads the [x] state to decide what flows to Higgsfield.
    pillar_hint = art.get("pillar") or "TBD"
    lines.append(f"- [ ] **APPROVE FOR SOCIAL**  (pillar: `{pillar_hint}`)")
    lines.append("")

    lines.append(f"- **Source:** {art['source']}")
    lines.append(f"- **Link:** <{art['link']}>")
    lines.append(f"- **Published:** {art['published_display']}")
    lines.append(f"- **Author(s):** {art['authors']}")
    lines.append(
        f"- **Keyword score:** {art['score']} "
        f"(AI strong: {art['ai_strong']}, AI booster: {art['ai_booster']}, "
        f"domain: {art['domain_matches']})"
    )

    if art.get("enriched"):
        rel = art.get("pillar_relevance", "")
        lines.append(f"- **Pillar:** {art.get('pillar', '')}  ·  "
                     f"**Pillar relevance:** {rel}/100")
        if art.get("enrichment_cached"):
            lines.append("- _Enrichment reused from a previous run (no new API "
                         "call). Re-run with `REUSE_CACHED_ENRICHMENT = False` "
                         "to refresh._")
        aud = art.get("audience_relevance") or {}
        if aud:
            lines.append(
                "- **Audience fit:** "
                f"managers/C-suite: {aud.get('managers_csuite', '?')} · "
                f"policy makers: {aud.get('policy_makers', '?')} · "
                f"general public: {aud.get('general_econ_public', '?')}"
            )
        lines.append("")

        lines.append("**Summary (Claude):**")
        lines.append("")
        lines.extend(_blockquote(art.get("claude_summary", "")))
        lines.append("")

        lines.append("**CAMO angle:**")
        lines.append("")
        lines.extend(_blockquote(art.get("centre_angle", "")))
        lines.append("")

        matched = art.get("matched_camo") or []
        if matched:
            lines.append("**Matched CAMO research:**")
            lines.append("")
            for m in matched:
                title = m.get("title", "(untitled)")
                url = m.get("url", "")
                reason = m.get("reason", "")
                lines.append(f"- [{title}]({url}) — {reason}")
            lines.append("")
        else:
            lines.append("**Matched CAMO research:** _none flagged_")
            lines.append("")

        lines.append(f"**Key takeaway:** {art.get('key_takeaway', '')}")
        lines.append("")
        lines.append(f"**Visual concept:** {art.get('visual_concept', '')}")
        lines.append("")

        lines.append("**Draft LinkedIn caption:**")
        lines.append("")
        lines.extend(_blockquote(art.get("linkedin_caption_draft", "")))
        lines.append("")

        lines.append("**Draft X post:**")
        lines.append("")
        lines.extend(_blockquote(art.get("x_caption_draft", "")))
        lines.append("")

        flags = art.get("red_flags") or []
        if flags:
            lines.append("**⚠ Red flags:** " + "; ".join(str(f) for f in flags))
        else:
            lines.append("**Red flags:** none")
        lines.append("")
    else:
        # Enrichment unavailable -- fall back to the raw RSS excerpt (v4 behaviour).
        err = art.get("enrichment_error")
        if err:
            lines.append(f"- _Enrichment unavailable: {err}_")
        lines.append("")
        lines.extend(_blockquote(art.get("summary", "")))
        lines.append("")

    lines.append("---")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def collect_entries() -> list:
    all_entries = []
    for url in FEED_URLS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[error] could not parse {url}: {e}")
            continue
        if feed.bozo:
            # bozo warnings are common (e.g. encoding mismatches) and don't prevent parsing
            print(f"[warn] feed parse warning for {url}: {feed.bozo_exception}")
        n = len(feed.entries)
        print(f"[info] {url} -> {n} entries")
        for entry in feed.entries[:PER_FEED_ITEM_LIMIT]:
            all_entries.append((url, entry))
    return all_entries


def build_digest() -> None:
    print(f"=== DIGEST SCRIPT {VERSION} ===")

    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    today_str = today.isoformat()

    raw_entries = collect_entries()
    print(f"[info] total raw entries across feeds: {len(raw_entries)}")

    seen_links = set()
    candidates = []
    skipped_ai = 0
    skipped_domain = 0
    skipped_age = 0
    for feed_url, entry in raw_entries:
        link = entry.get("link", "")
        if not link or link in seen_links:
            continue
        seen_links.add(link)

        title = (entry.get("title") or "").strip()
        summary = strip_html(entry.get("summary") or entry.get("description") or "")
        haystack = f"{title}\n{summary}"

        # --- AI side: strong required; booster counts only if strong present ---
        ai_strong = count_matches(haystack, AI_STRONG_PATTERNS)
        if ai_strong == 0:
            skipped_ai += 1
            continue
        ai_booster = count_matches(haystack, AI_BOOSTER_PATTERNS)
        ai_count = ai_strong + ai_booster

        # --- domain side ---
        domain_count = count_matches(haystack, DOMAIN_PATTERNS)
        if domain_count == 0:
            skipped_domain += 1
            continue

        # --- recency ---
        pub_dt = parse_pub_date(entry)
        if pub_dt is None:
            skipped_age += 1
            continue
        age_days = (now_utc - pub_dt).total_seconds() / 86400
        recency = max(0.0, 1.0 - age_days / RECENCY_WINDOW_DAYS)
        if recency == 0.0:
            skipped_age += 1
            continue

        score = (ai_count + domain_count) * recency

        candidates.append({
            "title": title,
            "link": link,
            "source": feed_label(feed_url),
            "published_display": pub_dt.strftime("%b %d, %Y"),
            "authors": extract_authors(entry),
            "summary": summary,
            "ai_strong": ai_strong,
            "ai_booster": ai_booster,
            "domain_matches": domain_count,
            "score": round(score, 2),
        })

    print(f"[info] candidates passing all filters: {len(candidates)}  "
          f"(skipped -- no AI: {skipped_ai}, no domain: {skipped_domain}, "
          f"no/old date: {skipped_age})")

    candidates.sort(key=lambda x: x["score"], reverse=True)

    # --- per-source cap to keep the digest diverse ---
    top = []
    source_counts = defaultdict(int)
    for art in candidates:
        if source_counts[art["source"]] >= MAX_PER_SOURCE:
            continue
        top.append(art)
        source_counts[art["source"]] += 1
        if len(top) >= TOP_N:
            break

    # --- re-appear tagging ---
    seen = load_seen()
    cutoff = today - timedelta(days=REAPPEAR_LOOKBACK_DAYS)
    for art in top:
        prior = seen.get(art["link"], [])
        recent_prior = [
            d for d in prior
            if d != today_str and date.fromisoformat(d) >= cutoff
        ]
        art["reappear"] = len(recent_prior) > 0
        if today_str not in prior:
            seen[art["link"]] = prior + [today_str]
    save_seen(seen)

    # --- Claude enrichment for the top N items -------------------------------
    # Runs AFTER the diversity cap (so we only spend API calls on items that
    # actually get published) and AFTER re-appear tagging (so Claude can see
    # whether an item is a repeat). Re-appearing items are served from
    # enrichment_cache.json with no API call. Mutates each dict in `top` in
    # place, adding the ENRICH_FIELDS plus "enriched" / "enrichment_cached".
    # Never raises.
    camo_index = load_camo_index()
    enrich_with_claude(top, camo_index, today)
    # -------------------------------------------------------------------------

    # --- write today's digest ---
    DIGESTS_DIR.mkdir(exist_ok=True)
    digest_path = DIGESTS_DIR / f"{today_str}.md"
    out = [f"# AI x Business Digest -- {today.strftime('%B %d, %Y')}", ""]

    if not top:
        out.append("_No items in today's RSS window matched the filter._")
    else:
        enriched_n = sum(1 for a in top if a.get("enriched"))
        sources_used = sorted(set(a["source"] for a in top))
        out.append(
            f"_Top {len(top)} items across HBR + NBER + MIT SMR + arXiv + McKinsey "
            f"matching **AI** (strong terms required) + (organization / firm / strategy / work / "
            f"management / leadership / executive / team / culture / innovation), "
            f"ranked by keyword density x recency. Max {MAX_PER_SOURCE} items per source._"
        )
        out.append("")
        out.append(f"_Sources represented today: {', '.join(sources_used)}._")
        out.append("")
        if enriched_n == len(top):
            out.append(f"_All {len(top)} items enriched by {ANTHROPIC_MODEL}. "
                       f"Tick **APPROVE FOR SOCIAL** on the items to publish, edit the "
                       f"CAMO angle / captions as needed, then commit._")
        elif enriched_n > 0:
            out.append(f"_{enriched_n}/{len(top)} items enriched by {ANTHROPIC_MODEL}; "
                       f"the rest show the raw RSS excerpt (see note on each)._")
        else:
            out.append(f"_Enrichment unavailable this run -- items show the raw RSS "
                       f"excerpt. See the note on each item._")
        out.append("")
        for i, art in enumerate(top, 1):
            out.extend(render_item_md(i, art))

    digest_path.write_text("\n".join(out))
    print(f"[ok] wrote {digest_path}  ({len(top)} items, script {VERSION})")

    # --- machine-readable sidecar for the downstream Higgsfield step ---
    sidecar_path = DIGESTS_DIR / f"{today_str}.enriched.json"
    sidecar = {
        "date": today_str,
        "script_version": VERSION,
        "model": ANTHROPIC_MODEL if any(a.get("enriched") for a in top) else None,
        "item_count": len(top),
        "items": top,
    }
    sidecar_path.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False, default=str))
    print(f"[ok] wrote {sidecar_path}")

    update_readme()


def update_readme() -> None:
    digests = sorted(DIGESTS_DIR.glob("*.md"), reverse=True) if DIGESTS_DIR.exists() else []
    lines = [
        "# AI x Business -- Daily Digest",
        "",
        "Automated daily scrape across multiple sources: "
        "HBR (latest + topic feeds + IdeaCast + Cold Call), "
        "NBER Working Papers, "
        "MIT Sloan Management Review, "
        "arXiv (econ.GN, cs.CY), "
        "and McKinsey Insights.",
        "",
        "Filter: items must contain at least one **strong AI term** "
        "(AI, machine learning, LLM, generative AI, Copilot, agentic, etc.) "
        "and at least one **business-domain term** "
        "(organization, firm, strategy, work, management, leadership, executive, team, culture, innovation). "
        "Booster terms (digital transformation, automation, analytics, predictive) only count if a strong AI term is also present.",
        "",
        "Top 10 per day, ranked by keyword density x recency. "
        f"Max {MAX_PER_SOURCE} items per source for diversity. "
        f"Recency window: {RECENCY_WINDOW_DAYS} days with linear decay.",
        "",
        f"Each item is then enriched by the Claude API ({ANTHROPIC_MODEL}): a clean "
        "summary, a CAMO pillar tag, a centre angle, matched CAMO research, draft "
        "LinkedIn/X captions, and a visual concept. Every item carries an "
        "`[ ] APPROVE FOR SOCIAL` checkbox for the editorial team, and a "
        "`<date>.enriched.json` sidecar is written for the downstream image step.",
        "",
        "Enrichment results are cached in `enrichment_cache.json`: when an item "
        "re-appears in a later run it is reused from the cache with no API call, "
        "so running daily stays inexpensive even though most items re-appear.",
        "",
        "Runs daily at **00:00 UTC** (about 7 pm CDT / 6 pm CST).",
        "",
        "## Recent digests",
        "",
    ]
    if not digests:
        lines.append("_No digests yet -- first run will populate this list._")
    else:
        for d in digests[:30]:
            lines.append(f"- [{d.stem}](digests/{d.name})")
        if len(digests) > 30:
            lines.append("")
            lines.append("_Older digests in [`digests/`](digests/)._")
    README_FILE.write_text("\n".join(lines))


if __name__ == "__main__":
    build_digest()
