#!/usr/bin/env python3
"""
Daily HBR digest: top 10 items matching 'AI' AND at least one of
{organization, firm, strategy, work, management}, ranked by keyword
density times recency. No API key required -- uses RSS excerpts.

Pulls from HBR articles feed + IdeaCast + Cold Call podcast feeds, dedupes.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import feedparser

# ---------------------------------------------------------------------------
# Version stamp -- look for this in the workflow log to confirm you have the
# right file. If you don't see "DIGEST SCRIPT v3" near the top of the
# 'Generate today's digest' step output, the new digest.py was NOT committed.
# ---------------------------------------------------------------------------
VERSION = "v3 (2026-05-13)"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FEED_URLS = [
    "https://hbr.org/the-latest/feed",                              # written articles
    "http://feeds.harvardbusiness.org/harvardbusiness/ideacast",    # IdeaCast podcast
    "http://feeds.harvardbusiness.org/harvardbusiness/cold-call",   # Cold Call podcast (HBS cases) -- note the hyphen
    # Optional additional HBR podcasts -- uncomment to enable later:
    # "http://feeds.harvardbusiness.org/harvardbusiness/hbrontheworkfeed",
    # "http://feeds.harvardbusiness.org/harvardbusiness/womenatwork",
    # "http://feeds.harvardbusiness.org/harvardbusiness/skydeck",
]

PER_FEED_ITEM_LIMIT = 30
TOP_N = 10
RECENCY_WINDOW_DAYS = 14
REAPPEAR_LOOKBACK_DAYS = 7

AI_PATTERNS = [
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
]

DOMAIN_PATTERNS = [
    r"\borgani[sz]ations?\b",
    r"\borgani[sz]ational\b",
    r"\bfirms?\b",
    r"\bstrateg(?:y|ies|ic|ically)\b",
    r"\bwork(?:place|places|force|forces|ers?|ing)?\b",
    r"\bmanage(?:ment|r|rs|ial|ing)?\b",
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
    """Defensive: podcast feeds expose authors in varied shapes."""
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
    if "ideacast" in url:
        return "IdeaCast (podcast)"
    if "cold-call" in url or "coldcall" in url:
        return "Cold Call (podcast)"
    if "hbrontheworkfeed" in url:
        return "HBR On Work (podcast)"
    if "womenatwork" in url:
        return "Women at Work (podcast)"
    if "skydeck" in url:
        return "Skydeck (podcast)"
    if "the-latest" in url:
        return "HBR Article"
    return "HBR"


# ---------------------------------------------------------------------------
# Main pipeline
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
            print(f"[warn] feed parse warning for {url}: {feed.bozo_exception}")
        print(f"[info] {url} -> {len(feed.entries)} entries")
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
    ai_only = 0
    domain_only = 0
    for feed_url, entry in raw_entries:
        link = entry.get("link", "")
        if not link or link in seen_links:
            continue
        seen_links.add(link)

        title = (entry.get("title") or "").strip()
        summary = strip_html(entry.get("summary") or entry.get("description") or "")
        haystack = f"{title}\n{summary}"

        ai_count = count_matches(haystack, AI_PATTERNS)
        domain_count = count_matches(haystack, DOMAIN_PATTERNS)
        if ai_count == 0 and domain_count == 0:
            continue
        if ai_count == 0:
            domain_only += 1
            continue
        if domain_count == 0:
            ai_only += 1
            continue

        pub_dt = parse_pub_date(entry)
        if pub_dt is None:
            continue
        age_days = (now_utc - pub_dt).total_seconds() / 86400
        recency = max(0.0, 1.0 - age_days / RECENCY_WINDOW_DAYS)
        if recency == 0.0:
            continue

        keyword_score = ai_count + domain_count
        final_score = keyword_score * recency

        candidates.append({
            "title": title,
            "link": link,
            "source": feed_label(feed_url),
            "published_display": pub_dt.strftime("%b %d, %Y"),
            "authors": extract_authors(entry),
            "summary": summary,
            "ai_matches": ai_count,
            "domain_matches": domain_count,
            "score": round(final_score, 2),
        })

    print(f"[info] candidates passing both filters: {len(candidates)}  "
          f"(AI-only skipped: {ai_only}, domain-only skipped: {domain_only})")

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top = candidates[:TOP_N]

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

    DIGESTS_DIR.mkdir(exist_ok=True)
    digest_path = DIGESTS_DIR / f"{today_str}.md"
    out = [f"# HBR AI x Business Digest -- {today.strftime('%B %d, %Y')}", ""]

    if not top:
        out.append("_No items in today's RSS window matched the filter._")
    else:
        out.append(
            f"_Top {len(top)} items from HBR articles + IdeaCast + Cold Call matching "
            f"**AI** + (organization / firm / strategy / work / management), "
            f"ranked by keyword density x recency._"
        )
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
                f"(AI matches: {art['ai_matches']}, domain matches: {art['domain_matches']})"
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
        "# HBR AI x Business -- Daily Digest",
        "",
        "Automated daily scrape of Harvard Business Review's RSS feeds "
        "(articles + IdeaCast + Cold Call podcasts). "
        "Filters for items mentioning **AI** (expanded: artificial intelligence, generative AI, "
        "machine learning, LLM, deep learning, neural networks, ChatGPT, Copilot, algorithms) "
        "**and** at least one of: *organization, firm, strategy, work, management*. "
        "Top 10 per day, ranked by keyword density x recency.",
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
