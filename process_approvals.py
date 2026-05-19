#!/usr/bin/env python3
"""
process_approvals.py -- Stage 2 of the CAMO content pipeline.

Reads:
  digests/**/*.md                     -- weekly digests with [x] ticks
  digests/**/*.enriched.json          -- the matching sidecar enrichments
  approval_state.json                 -- prior state of every approved link
  camo_index.json                     -- (read for cluster metadata if needed)

Writes:
  approval_state.json                 -- updated state
  clusters/<date>-<camo-id>.md        -- a new cluster file when one fires
  (optional) opens a GitHub issue per new cluster via the `gh` CLI

Pipeline:
  1. Scan every digest .md for lines matching the `[x] APPROVE FOR SOCIAL`
     checkbox pattern. Each tick gives us an article link (parsed from the
     same item's "- **Link:** <...>" line).
  2. For each newly ticked link not already in `approval_state.json`, load
     the full enrichment record from the corresponding `.enriched.json`
     sidecar and add the link to the state with state="queued".
  3. Group the still-queued items by their primary CAMO match
     (matched_camo[0].id). Items with no CAMO match are skipped
     (SKIP_NO_CAMO_CLUSTERS = True).
  4. For any CAMO id that has accumulated CLUSTER_THRESHOLD (3) or more
     queued items, fire a cluster: call Claude to suggest 3 shared keyword
     tags, write a cluster .md, mark the items state="clustered", and
     (if GH_TOKEN is set) open a GitHub issue.

Design notes:
  - Once an item is in the state file, it is never re-processed. Un-ticking
    the box in a digest .md does NOT remove an item from the queue or a
    cluster -- to undo, edit `approval_state.json` directly. This keeps the
    pipeline simple and deterministic.
  - One article anchors to its PRIMARY (first) matched_camo entry only.
    Secondary matches are still recorded for reference but don't influence
    grouping. This avoids one article counting toward multiple clusters.
  - Cluster files have their own `[ ] APPROVE FOR VISUAL CREATION` checkbox
    for the next stage (Higgsfield), which is not yet built.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections import defaultdict
from datetime import date
from pathlib import Path

try:
    import anthropic
except ImportError:
    anthropic = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VERSION = "v1 (2026-05-18)"

CLUSTER_THRESHOLD = 3                # fire a cluster at >= this many queued items
SKIP_NO_CAMO_CLUSTERS = True         # items without a CAMO match never auto-cluster

ANTHROPIC_MODEL = "claude-sonnet-4-6"
KEYWORDS_MAX_TOKENS = 400

OPEN_GITHUB_ISSUE = True             # set False to skip the gh CLI step

REPO_ROOT = Path(__file__).parent
DIGESTS_DIR = REPO_ROOT / "digests"
CLUSTERS_DIR = REPO_ROOT / "clusters"
APPROVAL_STATE_FILE = REPO_ROOT / "approval_state.json"
QUEUE_STATUS_FILE = REPO_ROOT / "QUEUE_STATUS.md"


# ---------------------------------------------------------------------------
# Regex patterns for parsing the digest markdown
# ---------------------------------------------------------------------------

# Items are introduced by "### 1. Title", "### 2. Title", etc.
ITEM_HEADING_RE = re.compile(r"^###\s+\d+\.\s+", re.MULTILINE)

# Checkbox line: "- [x] **APPROVE FOR SOCIAL**" (case-insensitive on x).
APPROVE_BOX_RE = re.compile(r"-\s*\[\s*[xX]\s*\]\s*\*\*APPROVE FOR SOCIAL\*\*")

# Link line: "- **Link:** <https://example.com/path>"
LINK_LINE_RE = re.compile(r"-\s+\*\*Link:\*\*\s+<([^>]+)>")


# ---------------------------------------------------------------------------
# State I/O
# ---------------------------------------------------------------------------

def load_approval_state() -> dict:
    if APPROVAL_STATE_FILE.exists():
        try:
            data = json.loads(APPROVAL_STATE_FILE.read_text())
            if isinstance(data, dict):
                return data
            print(f"[warn] {APPROVAL_STATE_FILE.name} is not a JSON object -- starting empty")
        except Exception as e:
            print(f"[warn] could not parse {APPROVAL_STATE_FILE.name}: {e} -- starting empty")
    return {}


def save_approval_state(state: dict) -> None:
    APPROVAL_STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True)
    )


# ---------------------------------------------------------------------------
# Parsing the digest markdown for approved items
# ---------------------------------------------------------------------------

def parse_approved_links_from_md(md_path: Path) -> list:
    """Return a list of links whose item block contains [x] APPROVE FOR SOCIAL.

    Logic: split the file into per-item blocks at "### N. ..." headings, then
    in each block look for both the approval checkbox and a Link line."""
    text = md_path.read_text(encoding="utf-8", errors="replace")
    # Find item heading positions; split into per-item segments.
    positions = [m.start() for m in ITEM_HEADING_RE.finditer(text)]
    if not positions:
        return []
    positions.append(len(text))
    approved = []
    for start, end in zip(positions[:-1], positions[1:]):
        block = text[start:end]
        if not APPROVE_BOX_RE.search(block):
            continue
        link_match = LINK_LINE_RE.search(block)
        if not link_match:
            print(f"[warn]   item in {md_path.name} has an approval tick "
                  f"but no parseable Link line; skipping")
            continue
        approved.append(link_match.group(1).strip())
    return approved


def load_sidecar(md_path: Path) -> dict:
    """Load the .enriched.json sidecar next to a digest .md and return a
    link -> item dict for fast lookup."""
    sidecar = md_path.with_name(md_path.stem + ".enriched.json")
    if not sidecar.exists():
        print(f"[warn] sidecar missing for {md_path.name} ({sidecar.name})")
        return {}
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        items = payload.get("items", [])
        return {it["link"]: it for it in items if it.get("link")}
    except Exception as e:
        print(f"[warn] could not parse sidecar {sidecar.name}: {e}")
        return {}


# ---------------------------------------------------------------------------
# Queueing approved items
# ---------------------------------------------------------------------------

def primary_camo(item: dict) -> dict:
    """Return the article's primary CAMO match (first entry) or {} if none."""
    matched = item.get("matched_camo") or []
    return matched[0] if matched else {}


def build_state_record(item: dict, source_md: Path, today_iso: str) -> dict:
    """Reduce an enrichment record to the subset we need to keep in state."""
    pcamo = primary_camo(item)
    return {
        "approved_at": today_iso,
        "approved_in_digest": str(source_md.relative_to(REPO_ROOT).as_posix()),
        "title": item.get("title", ""),
        "source": item.get("source", ""),
        "link": item.get("link", ""),
        "authors": item.get("authors", ""),
        "published_display": item.get("published_display", ""),
        "primary_camo_id": pcamo.get("id"),
        "primary_camo_title": pcamo.get("title"),
        "primary_camo_url": pcamo.get("url"),
        "primary_camo_reason": pcamo.get("reason"),
        "matched_camo": item.get("matched_camo") or [],
        "pillar": item.get("pillar"),
        "centre_angle": item.get("centre_angle"),
        "claude_summary": item.get("claude_summary"),
        "key_takeaway": item.get("key_takeaway"),
        "visual_concept": item.get("visual_concept"),
        "linkedin_caption_draft": item.get("linkedin_caption_draft"),
        "x_caption_draft": item.get("x_caption_draft"),
        "state": "queued",
        "cluster_file": None,
    }


def scan_and_queue_new_approvals(state: dict, today_iso: str) -> int:
    """Walk every digest .md, find new ticks, add them to state. Returns the
    number of newly added items."""
    if not DIGESTS_DIR.exists():
        print("[info] no digests/ directory yet -- nothing to scan")
        return 0
    md_files = sorted(DIGESTS_DIR.rglob("*.md"))
    print(f"[info] scanning {len(md_files)} digest .md file(s) for approvals")
    added = 0
    for md in md_files:
        approved_links = parse_approved_links_from_md(md)
        if not approved_links:
            continue
        # Only load the sidecar if at least one link in this file is NEW.
        new_links_here = [l for l in approved_links if l not in state]
        if not new_links_here:
            continue
        sidecar_map = load_sidecar(md)
        for link in new_links_here:
            item = sidecar_map.get(link)
            if not item:
                print(f"[warn] {md.name}: approved link {link[:60]} not "
                      f"found in sidecar -- skipping")
                continue
            state[link] = build_state_record(item, md, today_iso)
            added += 1
            print(f"[ok]  queued: {state[link]['title'][:60]}  "
                  f"(camo: {state[link]['primary_camo_id'] or 'none'})")
    return added


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def find_ready_clusters(state: dict) -> dict:
    """Group still-queued items by primary_camo_id. Return only groups at or
    above the threshold."""
    groups = defaultdict(list)
    for record in state.values():
        if record.get("state") != "queued":
            continue
        camo_id = record.get("primary_camo_id")
        if camo_id is None:
            if SKIP_NO_CAMO_CLUSTERS:
                continue
            camo_id = "no_camo_match"
        groups[camo_id].append(record)
    return {cid: items for cid, items in groups.items() if len(items) >= CLUSTER_THRESHOLD}


def call_claude_for_keywords(items: list, camo_title: str) -> list:
    """Ask Claude for 3 short shared keyword tags across the cluster's items.
    Returns a list of strings (hashtag-style) or a sensible fallback on any
    failure."""
    if anthropic is None:
        print("[warn] anthropic package not installed -- using fallback keywords")
        return ["#AI", "#research", "#management"]
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[warn] ANTHROPIC_API_KEY not set -- using fallback keywords")
        return ["#AI", "#research", "#management"]

    article_lines = []
    for it in items:
        article_lines.append(
            f"- Title: {it['title']}\n"
            f"  Pillar: {it.get('pillar', '?')}\n"
            f"  Summary: {(it.get('claude_summary') or '')[:400]}\n"
            f"  Key takeaway: {it.get('key_takeaway', '')}"
        )

    prompt = (
        "You are an editorial assistant for the HKU Centre for AI, Management "
        "and Organization (CAMO). The articles below have been editorially "
        f"approved and clustered around a CAMO paper: \"{camo_title}\".\n\n"
        "Suggest exactly 3 short keyword hashtags (lowercase, hyphen-separated, "
        "no spaces, no leading '#') that describe the SHARED theme across all "
        "of these articles. The tags will appear on a social post anchored to "
        "the CAMO paper, so they should be specific enough to convey the "
        "intersection, not generic ('ai', 'research'). Return ONLY a JSON "
        "object: {\"keywords\": [\"tag-one\", \"tag-two\", \"tag-three\"]}\n\n"
        "ARTICLES IN CLUSTER:\n" + "\n".join(article_lines)
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=KEYWORDS_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        # tolerate fences
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        if not text.startswith("{"):
            s, e = text.find("{"), text.rfind("}")
            if s != -1 and e != -1:
                text = text[s:e + 1]
        data = json.loads(text)
        tags = data.get("keywords", [])
        clean = []
        for t in tags[:3]:
            t = str(t).strip().lstrip("#").lower()
            if t:
                clean.append("#" + t)
        if len(clean) < 3:
            clean.extend(["#ai", "#management", "#research"][:3 - len(clean)])
        return clean
    except Exception as e:
        print(f"[warn] keyword generation failed: {e} -- using fallback")
        return ["#AI", "#research", "#management"]


def render_cluster_md(camo_id: str, items: list, keywords: list, today_iso: str) -> str:
    first = items[0]
    camo_title = first.get("primary_camo_title") or "(unknown CAMO paper)"
    camo_url = first.get("primary_camo_url") or ""
    pillars = sorted({i.get("pillar", "") for i in items if i.get("pillar")})

    lines = []
    lines.append(f"# Content Cluster — {camo_title}")
    lines.append("")
    lines.append(f"_Created: {today_iso}  ·  Cluster id: `{camo_id}`  ·  "
                 f"{len(items)} articles_")
    lines.append("")
    lines.append("## Anchor CAMO research")
    lines.append("")
    if camo_url:
        lines.append(f"**[{camo_title}]({camo_url})**")
    else:
        lines.append(f"**{camo_title}**")
    lines.append("")
    if pillars:
        lines.append(f"_Pillars represented: {', '.join(pillars)}_")
        lines.append("")
    lines.append("## Shared keywords (Claude-suggested)")
    lines.append("")
    lines.append("  ".join(keywords))
    lines.append("")
    lines.append("## Final approval")
    lines.append("")
    lines.append("- [ ] **APPROVE FOR VISUAL CREATION**")
    lines.append("")
    lines.append("_Tick the box once the framing below is editorially sound. "
                 "The next stage (Higgsfield image generation) reads this file, "
                 "but is not yet wired up._")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Articles in this cluster")
    lines.append("")
    for i, it in enumerate(items, 1):
        lines.append(f"### {i}. {it.get('title', '(untitled)')}")
        lines.append("")
        lines.append(f"- **Source:** {it.get('source', '')}")
        lines.append(f"- **Link:** <{it.get('link', '')}>")
        lines.append(f"- **Published:** {it.get('published_display', '')}")
        lines.append(f"- **Pillar:** {it.get('pillar', '')}")
        if it.get("primary_camo_reason"):
            lines.append(f"- **Connection to anchor:** {it['primary_camo_reason']}")
        lines.append("")
        if it.get("claude_summary"):
            lines.append("**Summary:**")
            lines.append("")
            for ln in it["claude_summary"].split("\n"):
                lines.append(f"> {ln}" if ln.strip() else ">")
            lines.append("")
        if it.get("centre_angle"):
            lines.append("**CAMO angle:**")
            lines.append("")
            for ln in it["centre_angle"].split("\n"):
                lines.append(f"> {ln}" if ln.strip() else ">")
            lines.append("")
        if it.get("visual_concept"):
            lines.append(f"**Suggested visual:** {it['visual_concept']}")
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def open_github_issue(camo_title: str, cluster_md: Path, items: list, keywords: list) -> None:
    """Open a GitHub issue announcing the new cluster. Uses the gh CLI, which
    is pre-installed on GitHub-hosted runners. Requires GH_TOKEN (or
    GITHUB_TOKEN) in the environment. Silent no-op outside a workflow."""
    if not OPEN_GITHUB_ISSUE:
        return
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("[info] GH_TOKEN not set -- skipping issue creation "
              "(only runs inside GitHub Actions by default)")
        return

    titles_bulleted = "\n".join(f"- {it.get('title', '')[:120]}" for it in items)
    keyword_line = "  ".join(keywords)
    body = (
        f"A new content cluster has formed around the CAMO paper:\n\n"
        f"**{camo_title}**\n\n"
        f"It contains {len(items)} editorially-approved articles, listed below.\n\n"
        f"### Articles\n{titles_bulleted}\n\n"
        f"### Suggested keywords\n{keyword_line}\n\n"
        f"### Cluster file\n[`{cluster_md.relative_to(REPO_ROOT).as_posix()}`]"
        f"({cluster_md.relative_to(REPO_ROOT).as_posix()})\n\n"
        f"Review the cluster file, edit the framing if needed, then tick "
        f"**APPROVE FOR VISUAL CREATION** at the top of the file and commit. "
        f"That triggers the Higgsfield image step (when built)."
    )
    issue_title = f"Cluster ready: {camo_title[:80]}"

    env = os.environ.copy()
    if "GH_TOKEN" not in env and "GITHUB_TOKEN" in env:
        env["GH_TOKEN"] = env["GITHUB_TOKEN"]
    try:
        result = subprocess.run(
            ["gh", "issue", "create", "--title", issue_title,
             "--body", body, "--label", "content-cluster"],
            check=False, capture_output=True, text=True, env=env, timeout=30,
        )
        if result.returncode == 0:
            print(f"[ok]  opened issue: {result.stdout.strip()}")
        else:
            # The "content-cluster" label may not exist on the repo; fall back
            # to creating the issue without a label rather than failing.
            print(f"[warn] gh issue create with label failed: {result.stderr.strip()}")
            result2 = subprocess.run(
                ["gh", "issue", "create", "--title", issue_title, "--body", body],
                check=False, capture_output=True, text=True, env=env, timeout=30,
            )
            if result2.returncode == 0:
                print(f"[ok]  opened issue (no label): {result2.stdout.strip()}")
            else:
                print(f"[warn] could not open issue: {result2.stderr.strip()}")
    except FileNotFoundError:
        print("[warn] gh CLI not available; skipping issue creation")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] issue creation errored: {e}")


def fire_clusters(state: dict, ready: dict, today_iso: str) -> int:
    """Materialise each ready cluster: write the .md, mark items, open issue.
    Returns the number of clusters created."""
    if not ready:
        return 0
    CLUSTERS_DIR.mkdir(exist_ok=True)
    created = 0
    for camo_id, items in sorted(ready.items()):
        camo_title = items[0].get("primary_camo_title") or camo_id
        print(f"[info] firing cluster: {camo_id}  ({len(items)} items) — {camo_title[:60]}")

        keywords = call_claude_for_keywords(items, camo_title)
        body = render_cluster_md(camo_id, items, keywords, today_iso)
        cluster_path = CLUSTERS_DIR / f"{today_iso}-{camo_id}.md"
        cluster_path.write_text(body, encoding="utf-8")
        print(f"[ok]  wrote {cluster_path.relative_to(REPO_ROOT).as_posix()}")

        # Mark items as clustered
        rel = cluster_path.relative_to(REPO_ROOT).as_posix()
        for item in items:
            state[item["link"]]["state"] = "clustered"
            state[item["link"]]["cluster_file"] = rel
            state[item["link"]]["clustered_at"] = today_iso
            state[item["link"]]["cluster_keywords"] = keywords

        open_github_issue(camo_title, cluster_path, items, keywords)
        created += 1
    return created


def render_queue_status(state: dict, now_str: str) -> str:
    """Build a human-readable dashboard of the current queue state. Written
    to QUEUE_STATUS.md at the repo root after every run."""
    queued = [r for r in state.values() if r.get("state") == "queued"]
    clustered = [r for r in state.values() if r.get("state") == "clustered"]

    # Group QUEUED items by primary CAMO id.
    by_camo = defaultdict(list)
    for r in queued:
        by_camo[r.get("primary_camo_id") or "__no_camo__"].append(r)

    # Group CLUSTERED items by their cluster file (so we can list each cluster once).
    clusters = defaultdict(list)
    for r in clustered:
        cf = r.get("cluster_file") or "(unknown cluster file)"
        clusters[cf].append(r)

    lines = []
    lines.append("# Approval Queue Status")
    lines.append("")
    lines.append(f"_Last updated: {now_str}_")
    lines.append("")
    lines.append("_Auto-generated by `process_approvals.py` after every approval "
                 "workflow run. Don't hand-edit -- next run will overwrite._")
    lines.append("")

    # --- summary ---
    distinct_camo = len([cid for cid in by_camo if cid != "__no_camo__"])
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Queued items waiting to cluster: **{len(queued)}**")
    lines.append(f"- Items already clustered: **{len(clustered)}** "
                 f"(across **{len(clusters)}** cluster file(s))")
    lines.append(f"- CAMO papers currently accumulating ticks: **{distinct_camo}**")
    lines.append(f"- Cluster threshold: **{CLUSTER_THRESHOLD} ticks per CAMO paper**")
    lines.append("")

    # --- queue by CAMO paper ---
    lines.append("## Queue by CAMO paper")
    lines.append("")
    if not queued:
        lines.append("_Nothing queued. Tick `[x] APPROVE FOR SOCIAL` on items in a "
                     "weekly digest to start filling the queue._")
        lines.append("")
    else:
        # Sort so threshold-reaching papers appear first, then by count desc,
        # then "no CAMO match" bucket last.
        def sort_key(cid_items):
            cid, items = cid_items
            is_no_camo = (cid == "__no_camo__")
            ready_to_fire = (not is_no_camo) and (len(items) >= CLUSTER_THRESHOLD)
            # tuple: (no-camo always last, ready first, then by count desc)
            return (1 if is_no_camo else 0,
                    0 if ready_to_fire else 1,
                    -len(items))

        for cid, items in sorted(by_camo.items(), key=sort_key):
            if cid == "__no_camo__":
                lines.append("### No CAMO match")
                lines.append("")
                lines.append(f"**Status: {len(items)} queued — won't auto-cluster "
                             f"(no anchor paper). To allow these to cluster anyway, "
                             f"set `SKIP_NO_CAMO_CLUSTERS = False` in "
                             f"`process_approvals.py`._**")
                lines.append("")
            else:
                title = items[0].get("primary_camo_title") or cid
                url = items[0].get("primary_camo_url") or ""
                heading = f"[{title}]({url})" if url else title
                lines.append(f"### {heading}")
                lines.append("")
                if len(items) >= CLUSTER_THRESHOLD:
                    lines.append(f"**Status: {len(items)}/{CLUSTER_THRESHOLD} queued — "
                                 f"READY TO FIRE on next workflow run.**")
                else:
                    need = CLUSTER_THRESHOLD - len(items)
                    plural = "" if need == 1 else "s"
                    lines.append(f"**Status: {len(items)}/{CLUSTER_THRESHOLD} queued — "
                                 f"needs {need} more tick{plural} to fire.**")
                lines.append("")

            # list the items
            for j, it in enumerate(sorted(items, key=lambda r: r.get("approved_at", "")), 1):
                title = it.get("title", "(untitled)")
                source = it.get("source", "")
                link = it.get("link", "")
                approved_at = it.get("approved_at", "")
                in_digest = it.get("approved_in_digest", "")
                lines.append(f"{j}. **{title}**  ")
                meta = []
                if source: meta.append(source)
                if approved_at: meta.append(f"queued {approved_at}")
                if in_digest: meta.append(f"from `{in_digest}`")
                if meta:
                    lines.append(f"   _{' · '.join(meta)}_  ")
                if link:
                    lines.append(f"   <{link}>")
                lines.append("")

    # --- recent clusters ---
    lines.append("## Clusters created so far")
    lines.append("")
    if not clusters:
        lines.append("_No clusters yet. The first one fires when any CAMO paper "
                     f"accumulates {CLUSTER_THRESHOLD} ticks._")
        lines.append("")
    else:
        # Sort newest first by latest item's clustered_at within the cluster.
        def cluster_sort_key(cf_items):
            _, items = cf_items
            return max((it.get("clustered_at") or "") for it in items)
        for cf, items in sorted(clusters.items(), key=cluster_sort_key, reverse=True):
            title = items[0].get("primary_camo_title") or "(unknown anchor)"
            keywords = items[0].get("cluster_keywords") or []
            clustered_at = items[0].get("clustered_at", "")
            kw_str = "  ".join(f"`{k}`" for k in keywords) if keywords else "_(no keywords)_"
            lines.append(f"- [{cf}]({cf}) — anchor: **{title}** — "
                         f"{len(items)} articles — fired {clustered_at}  ")
            lines.append(f"  keywords: {kw_str}")
            lines.append("")

    return "\n".join(lines)


def save_queue_status(state: dict) -> None:
    """Write QUEUE_STATUS.md. Never raises -- a status-file problem must not
    abort the run."""
    try:
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        QUEUE_STATUS_FILE.write_text(render_queue_status(state, now_str), encoding="utf-8")
        print(f"[ok]  wrote {QUEUE_STATUS_FILE.name}")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] could not write {QUEUE_STATUS_FILE.name}: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"=== process_approvals {VERSION} ===")
    today_iso = date.today().isoformat()

    state = load_approval_state()
    print(f"[info] approval_state.json: {len(state)} existing entries  "
          f"(queued: {sum(1 for r in state.values() if r.get('state') == 'queued')}, "
          f"clustered: {sum(1 for r in state.values() if r.get('state') == 'clustered')})")

    # Step 1+2: scan ticks, add new ones to queue
    added = scan_and_queue_new_approvals(state, today_iso)
    print(f"[info] new approvals queued this run: {added}")

    # Step 3+4: cluster groups that have hit the threshold
    ready = find_ready_clusters(state)
    if not ready:
        print(f"[info] no CAMO group has >= {CLUSTER_THRESHOLD} queued items yet "
              f"-- nothing to cluster")
    else:
        print(f"[info] clusters ready to fire: {len(ready)} "
              f"({', '.join(f'{cid}={len(items)}' for cid, items in ready.items())})")
    created = fire_clusters(state, ready, today_iso)

    # Persist state regardless of whether anything changed -- cheap, safer.
    save_approval_state(state)

    # Write the human-readable status dashboard. Always runs, even when no
    # changes happened, so the timestamp inside the file reflects the latest
    # workflow run -- a clear signal the script did execute.
    save_queue_status(state)

    print(f"[ok] approval_state.json saved.  "
          f"Summary this run: +{added} queued, +{created} clusters created.")


if __name__ == "__main__":
    main()
