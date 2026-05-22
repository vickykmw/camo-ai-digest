"""
Markdown → YAML parser for CAMO gatekeeper output.

Reads the gatekeeper's Reading-Room markdown (the format produced by your
content cluster template) and emits a YAML that generate.py can consume.

Usage:
    python parse_markdown.py path/to/cluster.md
    # writes path/to/cluster.yaml alongside

CLI flags:
    --output PATH       Write to a specific path instead of auto-derived
    --strict            Fail on any missing optional field (default: warn)

Field mapping (markdown → YAML):
    H1 'Content Cluster — ...'      → ignored (title only)
    'Cluster id: ...'               → carousel.id
    'Primary pillar: ...'           → carousel.pillar
    '## Anchor CAMO research'       → carousel.camo_anchor.*
        anchor link text            → camo_anchor.title (split on ': ' for subtitle)
        emphasised authors line     → camo_anchor.authors
        Abstract blockquote         → camo_anchor.abstract (for key_finding extraction)
    'Draft LinkedIn carousel caption' → ignored (caption is for the post body)
    '### N. Article Title'          → papers[N-1].title
        '**Source:** ...'           → papers[N-1].source
        '**Pillar:** ...'           → papers[N-1].article_pillar (informational only)
        '**Key takeaway:** ...'     → papers[N-1].takeaway
        '**Suggested visual:** ...' → papers[N-1].visual_concept

Defaults applied:
    camo_anchor.explanation_line = "Similar topics we explored."
    camo_anchor.cta_statement    = "More from HKU Centre AI, management, and organisations at camo.hku.hk"
    camo_anchor.paper_cover_url  = "" (empty — gatekeeper drops cover into Canva manually)
    camo_anchor.key_finding      = "" (empty — gatekeeper fills in for slide 5 pull quote)
    total_slides                 = 1 (cover) + len(papers) + 2 (bridges)
    slide_position for paper N   = N + 1 (slide 1 is cover)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml


DEFAULT_EXPLANATION = "Similar topics we explored."
DEFAULT_CTA = "More from HKU Centre AI, management, and organisations at camo.hku.hk"

VALID_PILLARS = {
    "AI Adoption",
    "AI & Incentives",
    "AI & Jobs",
    "AI Algorithms & Data",
}


# Matches the approval checkbox when it's ticked: "[x]" or "[X]" before
# "APPROVE FOR VISUAL CREATION" (the bold markers around APPROVE are tolerated).
# Unticked "[ ]" returns False so drafts can be committed without triggering.
APPROVAL_PATTERN = re.compile(
    r"-\s*\[\s*[xX]\s*\]\s*\*{0,2}\s*APPROVE FOR VISUAL CREATION",
    re.IGNORECASE,
)


def is_approved(md_text: str) -> bool:
    """True iff the markdown contains a TICKED 'APPROVE FOR VISUAL CREATION'
    checkbox. Returns False for unticked or missing checkboxes."""
    return bool(APPROVAL_PATTERN.search(md_text))


class ParseError(Exception):
    """Raised when the markdown is missing required structure."""


def _find_line(text: str, pattern: str, group: int = 1) -> str | None:
    """Return first regex group match, or None."""
    m = re.search(pattern, text, re.MULTILINE)
    return m.group(group).strip() if m else None


def _strip_md_link(text: str) -> tuple[str, str | None]:
    """'[Title](url)' → ('Title', 'url'). Plain text → (text, None)."""
    m = re.match(r"\[(.+?)\]\((.+?)\)", text.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return text.strip(), None


def _split_title_subtitle(full_title: str) -> tuple[str, str]:
    """'Title: Subtitle' → ('Title', 'Subtitle'). No colon → (full, '')."""
    if ": " in full_title:
        head, tail = full_title.split(": ", 1)
        return head.strip(), tail.strip()
    return full_title.strip(), ""


def parse_anchor_section(text: str) -> dict[str, Any]:
    """Extract camo_anchor.* from the '## Anchor CAMO research' section."""
    # Section runs from 'Anchor CAMO research' to the next H2
    m = re.search(
        r"## Anchor CAMO research\s*\n(.*?)(?=\n## )",
        text, re.DOTALL,
    )
    if not m:
        raise ParseError("Missing '## Anchor CAMO research' section")
    section = m.group(1)

    # First bold link gives the paper title + (optional) URL
    link_match = re.search(r"\*\*\[(.+?)\]\((.+?)\)\*\*", section)
    if not link_match:
        # Fallback: any bold line
        bold_match = re.search(r"\*\*(.+?)\*\*", section)
        if not bold_match:
            raise ParseError("Anchor section missing bold paper title")
        full_title = bold_match.group(1).strip()
    else:
        full_title = link_match.group(1).strip()

    title, subtitle = _split_title_subtitle(full_title)

    # Authors line: italic line directly under the title, often containing dots
    authors = ""
    auth_match = re.search(r"_([^_\n]+ · [^_\n]+)_", section)
    if auth_match:
        authors = auth_match.group(1).strip()
        # Format the authors as "By X · YEAR · TYPE" for slide consistency
        # If the line is "Name1, Name2 · YYYY · Type", prepend "By "
        if not authors.lower().startswith("by "):
            authors = "By " + authors

    return {
        "title": title,
        "subtitle": subtitle,
        "authors": authors,
        "key_finding": "",  # Gatekeeper fills manually for slide 5 pull quote
        "paper_cover_url": "",  # Manual drop into Canva for now
        "explanation_line": DEFAULT_EXPLANATION,
        "cta_statement": DEFAULT_CTA,
    }


def parse_articles(text: str) -> list[dict[str, Any]]:
    """Extract papers[] from the '## Articles in this cluster' section."""
    # Find all '### N. Title' headings and extract their blocks
    article_blocks = re.split(r"\n### \d+\. ", text)
    # First chunk is preamble — drop it
    if len(article_blocks) < 2:
        raise ParseError("No article blocks found (expected '### N. Title' headings)")

    papers = []
    for i, block in enumerate(article_blocks[1:], start=1):
        # Title is the first line of the block
        title = block.split("\n", 1)[0].strip()

        source = _find_line(block, r"^[-*]?\s*\*\*Source:\*\*\s+(.+?)$")
        if not source:
            raise ParseError(f"Article {i} ({title[:50]}...) missing '**Source:**'")

        # Strip markdown link from source if present
        source = _strip_md_link(source)[0]

        takeaway = _find_line(block, r"^[-*]?\s*\*\*Key takeaway:\*\*\s+(.+?)$")
        if not takeaway:
            raise ParseError(f"Article {i} ({title[:50]}...) missing '**Key takeaway:**'")

        visual = _find_line(block, r"^[-*]?\s*\*\*Suggested visual:\*\*\s+(.+?)$")
        if not visual:
            raise ParseError(f"Article {i} ({title[:50]}...) missing '**Suggested visual:**'")

        # Pillar is informational only — drives nothing in this version
        article_pillar = _find_line(block, r"^[-*]?\s*\*\*Pillar:\*\*\s+(.+?)$") or ""

        papers.append({
            "slide_position": i + 1,  # slide 1 is the cover
            "source": source,
            "title": title,
            "takeaway": takeaway,
            "visual_concept": visual,
            "article_pillar": article_pillar,
        })

    return papers


def parse_markdown(md_text: str) -> dict[str, Any]:
    """Parse the full gatekeeper markdown into a carousel dict."""
    cluster_id = _find_line(md_text, r"Cluster id:\s*`([^`]+)`")
    if not cluster_id:
        raise ParseError("Missing 'Cluster id: `...`' in header")

    primary_pillar = _find_line(md_text, r"Primary pillar:\*?\*?\s*(.+?)$")
    if not primary_pillar:
        raise ParseError(
            "Missing 'Primary pillar: ...' field. "
            "Add a line near the top: 'Primary pillar: AI & Jobs' (or another valid pillar)."
        )
    # Clean trailing markdown emphasis if any
    primary_pillar = primary_pillar.rstrip("*_ ").strip()
    if primary_pillar not in VALID_PILLARS:
        raise ParseError(
            f"Primary pillar '{primary_pillar}' is not one of {sorted(VALID_PILLARS)}"
        )

    camo_anchor = parse_anchor_section(md_text)
    papers = parse_articles(md_text)

    total_slides = 1 + len(papers) + 2  # cover + papers + 2 bridges

    return {
        "carousel": {
            "id": cluster_id,
            "pillar": primary_pillar,
            "total_slides": total_slides,
            "camo_anchor": camo_anchor,
            "papers": papers,
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse CAMO gatekeeper markdown to YAML")
    parser.add_argument("markdown_path", type=Path, help="Input .md file")
    parser.add_argument("--output", type=Path, default=None, help="Output .yaml path")
    parser.add_argument("--strict", action="store_true", help="Fail on missing optional fields")
    args = parser.parse_args()

    if not args.markdown_path.exists():
        print(f"File not found: {args.markdown_path}", file=sys.stderr)
        return 1

    md_text = args.markdown_path.read_text(encoding="utf-8")

    try:
        data = parse_markdown(md_text)
    except ParseError as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 2

    out_path = args.output or args.markdown_path.with_suffix(".yaml")
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=100)

    c = data["carousel"]
    print(f"✓ Parsed: {c['id']}")
    print(f"  Pillar: {c['pillar']}")
    print(f"  Papers: {len(c['papers'])} (total slides: {c['total_slides']})")
    print(f"  Anchor: {c['camo_anchor']['title']}")
    print(f"  → {out_path}")

    # Soft warnings for empty defaults
    if not c['camo_anchor']['key_finding']:
        print("  ⚠ key_finding empty — fill in for slide 5 pull quote")
    if not c['camo_anchor']['paper_cover_url']:
        print("  ⚠ paper_cover_url empty — drop PDF cover into Canva slide 5 manually")

    return 0


if __name__ == "__main__":
    sys.exit(main())
