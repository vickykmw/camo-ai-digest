"""
CAMO carousel pipeline — orchestrator.

Runs on GitHub Actions when a .md file is committed to ready_for_visual/
with the approval checkbox ticked. Produces three output files alongside
the .md, in the same year-month subfolder.

Inputs:
  ready_for_visual/YYYY-MM/CLUSTER.md   (the approved markdown)

Outputs (all written to the same folder as the input .md):
  CLUSTER-report.md           Preview report: image links + prompts used
  CLUSTER-article-canva.csv   Paper-slide rows for Canva Bulk Create
  CLUSTER-bridge-canva.csv    Bridge slide rows for Canva Bulk Create

Env vars required:
  HF_CREDENTIALS  — Higgsfield credentials in "KEY_ID:KEY_SECRET" format

CLI flags:
  --dry-run         Synthesise prompts, write report stub, no generations
  --file PATH       Process a single .md file (default: all approved files
                    in ready_for_visual/)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from approval_to_gen_visual import parse_markdown, is_approved, ParseError
from style_envelopes import synthesize_prompt
from higgsfield_client_wrapper import HiggsfieldClient


REPO_ROOT = Path(__file__).parent.parent
READY_DIR = REPO_ROOT / "ready_for_visual"

DEFAULT_MODEL = "seedream_v4_5"
DEFAULT_ASPECT_RATIO = "1:1"
DEFAULT_RESOLUTION = "1K"

ESTIMATED_COST_PER_IMAGE = {
    "seedream_v4_5": 1,
    "flux_2": 1,
    "nano_banana_2": 1.5,
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("camo-pipeline")


def output_stem(md_path: Path) -> Path:
    """ready_for_visual/2026-05/CLUSTER.md → ready_for_visual/2026-05/CLUSTER"""
    return md_path.with_suffix("")


def write_article_csv(path: Path, papers: list[dict], total_slides: int) -> None:
    """Paper-slides CSV. Hero URLs are plain text — gatekeeper downloads
    from Higgsfield and drags into Canva manually."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source", "title", "takeaway", "slide_indicator", "hero_image_url"])
        for p in papers:
            w.writerow([
                p["source"],
                p["title"],
                p["takeaway"],
                f"{p['slide_position']:02d} / {total_slides:02d}",
                p.get("hero_image_url", ""),
            ])


def write_bridge_csv(path: Path, anchor: dict, total_slides: int) -> None:
    """Bridge-slides CSV. Two rows = the two bridge slides (paper showcase
    and CTA close). All fields present, empty where not relevant for that
    slide — keeps the CSV regular and easy to read in Canva."""
    rows = [
        {
            "slide_role": "paper_showcase",
            "slide_indicator": f"{total_slides-1:02d} / {total_slides:02d}",
            "kicker": "CAMO RESEARCH",
            "explanation_line": anchor["explanation_line"],
            "title": anchor["title"],
            "subtitle": anchor["subtitle"],
            "authors": anchor["authors"],
            "key_finding": anchor.get("key_finding") or "[TO FILL: ≤30-word pull quote]",
            "statement": "",
        },
        {
            "slide_role": "cta_close",
            "slide_indicator": f"{total_slides:02d} / {total_slides:02d}",
            "kicker": "CAMO RESEARCH",
            "explanation_line": "",
            "title": "",
            "subtitle": "",
            "authors": "",
            "key_finding": "",
            "statement": anchor["cta_statement"],
        },
    ]
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def write_report(path: Path, carousel: dict, dry_run: bool) -> None:
    """Preview report — image link + prompt + takeaway per paper.
    Human checks this in Higgsfield/GitHub and regens any bad images."""
    c = carousel
    article_csv_name = path.name.replace("-report.md", "-article-canva.csv")
    lines = [
        f"# Visual production report — {c['id']}",
        "",
        f"_Pillar: **{c['pillar']}**  ·  Model: `{c.get('model_used', DEFAULT_MODEL)}`  ·  Papers: {len(c['papers'])}_",
        "",
        f"_Preview each image below. If any image is off, regenerate it in Higgsfield's media library, "
        f"copy the new URL, and replace the URL in the corresponding row of `{article_csv_name}`._",
        "",
        "---",
        "",
    ]
    for p in c["papers"]:
        lines.extend([
            f"## Slide {p['slide_position']:02d} — {p['title']}",
            "",
            f"**Source:** {p['source']}",
            "",
            f"**Key takeaway:** {p['takeaway']}",
            "",
        ])
        url = p.get("hero_image_url", "")
        if url and not dry_run:
            lines.extend([
                f"**Generated image:** {url}",
                "",
                f"![Slide {p['slide_position']} hero]({url})",
                "",
            ])
        elif dry_run:
            lines.extend(["_Dry-run — no image generated._", ""])
        else:
            lines.extend([
                "**⚠ Image generation failed for this paper.** "
                "Regenerate in Higgsfield manually and update the article CSV.",
                "",
            ])
        lines.extend([
            "<details>",
            "<summary>Prompt used</summary>",
            "",
            "```",
            p.get("prompt_used", "(not recorded)"),
            "```",
            "",
            "</details>",
            "",
            "---",
            "",
        ])
    lines.extend([
        "## Anchor CAMO paper (for bridge slides)",
        "",
        f"**Title:** {c['camo_anchor']['title']}  ",
        f"**Subtitle:** {c['camo_anchor']['subtitle']}  ",
        f"**Authors:** {c['camo_anchor']['authors']}  ",
        "",
        "These fields are also in the bridge-canva.csv for Canva use.",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def process_file(
    md_path: Path,
    client: HiggsfieldClient | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """End-to-end for one approved markdown file."""
    log.info(f"Reading {md_path}")
    md_text = md_path.read_text(encoding="utf-8")

    if not is_approved(md_text):
        log.info("  Skipped: approval checkbox not ticked")
        return {"file": str(md_path), "skipped": "not approved"}

    data = parse_markdown(md_text)
    c = data["carousel"]
    pillar = c["pillar"]
    model = c.get("model_override", DEFAULT_MODEL)
    c["model_used"] = model

    log.info(
        f"Carousel: {c['id']} | pillar: {pillar} | papers: {len(c['papers'])} | "
        f"total slides: {c['total_slides']} | model: {model}"
    )

    total_cost = 0.0
    errors = []

    for paper in c["papers"]:
        prompt = synthesize_prompt(paper["visual_concept"], pillar)
        paper["prompt_used"] = prompt

        if dry_run:
            log.info(f"  [DRY] '{paper['title'][:60]}...' — prompt {len(prompt)} chars")
            paper["hero_image_url"] = ""
            total_cost += ESTIMATED_COST_PER_IMAGE.get(model, 1)
        else:
            try:
                result = client.generate_image(
                    prompt=prompt,
                    model=model,
                    aspect_ratio=DEFAULT_ASPECT_RATIO,
                    resolution=DEFAULT_RESOLUTION,
                )
                paper["hero_image_url"] = result.image_url
                total_cost += result.cost or 0
                log.info(f"  ✓ '{paper['title'][:60]}...' → image generated")
            except Exception as e:
                log.error(f"  ✗ '{paper['title'][:60]}...' failed: {e}")
                errors.append({"title": paper["title"], "error": str(e)})
                paper["hero_image_url"] = ""

    stem = output_stem(md_path)
    write_report(stem.parent / f"{stem.name}-report.md", c, dry_run)
    write_article_csv(stem.parent / f"{stem.name}-article-canva.csv",
                      c["papers"], c["total_slides"])
    write_bridge_csv(stem.parent / f"{stem.name}-bridge-canva.csv",
                     c["camo_anchor"], c["total_slides"])

    log.info(f"  Outputs written to {stem.parent}/")
    return {
        "file": str(md_path),
        "carousel_id": c["id"],
        "papers_processed": len(c["papers"]),
        "errors": errors,
        "total_cost_credits": round(total_cost, 2),
        "dry_run": dry_run,
    }


def discover_approved_files(root: Path) -> list[Path]:
    """Find every approved source .md under ready_for_visual/*/ — skipping
    -report.md outputs and unticked drafts."""
    if not root.exists():
        return []
    targets = []
    for md in sorted(root.glob("**/*.md")):
        if md.name.endswith("-report.md"):
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if is_approved(text):
            targets.append(md)
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description="CAMO carousel pipeline")
    parser.add_argument("--dry-run", action="store_true",
                        help="Synthesise prompts; no real generations")
    parser.add_argument("--file", type=Path, default=None,
                        help="Process a specific .md file (default: scan ready_for_visual/)")
    args = parser.parse_args()

    if args.file:
        if not args.file.exists():
            log.error(f"File not found: {args.file}")
            return 1
        targets = [args.file]
    else:
        targets = discover_approved_files(READY_DIR)

    if not targets:
        log.info("No approved files found. Exiting cleanly.")
        return 0

    log.info(f"Found {len(targets)} approved file(s). Dry-run: {args.dry_run}")

    client = None
    if not args.dry_run:
        creds = os.environ.get("HF_CREDENTIALS")
        if not creds:
            log.error("HF_CREDENTIALS not set in environment")
            return 2
        client = HiggsfieldClient(credentials=creds)

    summaries = []
    for md_path in targets:
        try:
            summary = process_file(md_path, client, dry_run=args.dry_run)
            summaries.append(summary)
        except ParseError as e:
            log.error(f"{md_path}: parse error: {e}")
            summaries.append({"file": str(md_path), "parse_error": str(e)})
        except Exception as e:
            log.error(f"{md_path}: unexpected error: {e}", exc_info=True)
            summaries.append({"file": str(md_path), "fatal_error": str(e)})

    log.info("─" * 60)
    log.info("RUN SUMMARY")
    print(json.dumps(summaries, indent=2))

    any_errors = any(
        s.get("errors") or s.get("fatal_error") or s.get("parse_error")
        for s in summaries
    )
    return 1 if any_errors else 0


if __name__ == "__main__":
    sys.exit(main())
