#!/usr/bin/env python3
"""
Daily AI x Business digest across HBR + NBER + MIT SMR + arXiv + McKinsey.

Filter rule (strict AND):
  - at least one STRONG AI term, OR a BOOSTER AI term accompanied by a STRONG AI term
  - at least one business-domain term
Ranking: (AI matches + booster bonus + domain matches) * recency
Diversity: cap of MAX_PER_SOURCE in the daily top N.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

import feedparser

# ---------------------------------------------------------------------------
# Version stamp -- check the workflow log for "DIGEST SCRIPT v4" to confirm
# this file is the one running.
# ---------------------------------------------------------------------------
VERSION = "v4 (2026-05-13)"

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

    # --- write today's digest ---
    DIGESTS_DIR.mkdir(exist_ok=True)
    digest_path = DIGESTS_DIR / f"{today_str}.md"
    out = [f"# AI x Business Digest -- {today.strftime('%B %d, %Y')}", ""]

    if not top:
        out.append("_No items in today's RSS window matched the filter._")
    else:
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
        for i, art in enumerate(top, 1):
            badge = "  `(re-appear)`" if art["reappear"] else ""
            out.append(f"### {i}. {art['title']}{badge}")
            out.append("")
            out.append(f"- **Source:** {art['source']}")
            out.append(f"- **Link:** <{art['link']}>")
            out.append(f"- **Published:** {art['published_display']}")
            out.append(f"- **Author(s):** {art['authors']}")
            out.append(
                f"- **Score:** {art['score']} "
                f"(AI strong: {art['ai_strong']}, AI booster: {art['ai_booster']}, "
                f"domain: {art['domain_matches']})"
            )
            out.append("")
            out.append(f"> {art['summary']}")
            out.append("")

    digest_path.write_text("\n".join(out))
    print(f"[ok] wrote {digest_path}  ({len(top)} items, script {VERSION})")

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
