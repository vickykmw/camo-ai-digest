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
  ready_for_visual/<date>-<camo>.md   -- a new cluster file when one fires
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
     queued items, FIRE a cluster:
       - call Claude to suggest 3 shared keyword tags
       - call Claude to draft one LinkedIn post PER article (3 captions
         per cluster, posting strategy: one post per article anchored
         to the same CAMO paper)
       - write a cluster .md to ready_for_visual/
       - mark the items state="clustered"
       - (if GH_TOKEN is set) open a GitHub issue

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

v2 changes (2026-05-20):
  - Cluster folder renamed from `clusters/` to `ready_for_visual/` to
    better describe what the folder contains (cluster files awaiting
    editorial sign-off for visual generation).
  - audience_relevance now stored on state records (used to inform LinkedIn
    caption tone).
  - At cluster firing, Claude drafts ONE LinkedIn carousel caption for the
    whole cluster, in the recurring "Reading Room" series voice (peer-
    among-peers, sharing companion research from the field alongside the
    centre's recent work; NOT making an argument or pressure-testing other
    work). The downstream visual stage produces three images (one per
    article) as the carousel slides; the caption is a single post body.
    Caption stored on state records as `cluster_linkedin_caption_draft`.
  - Series name + hashtag are constants (SERIES_NAME, SERIES_HASHTAG) so
    the brand can evolve without prompt edits.

v2.1 changes (2026-05-21):
  - Multi-match parser fix: the per-match checkbox UI in the digest now
    drives the anchor. Previously the parser ignored the editor's pick and
    always used matched_camo[0]. Now the parser captures the chosen CAMO
    id from the ticked line ("[x] APPROVE FOR SOCIAL -> `<id>` -- Title")
    and threads it through to build_state_record. If multiple checkboxes
    are ticked on the same item (multi-match misuse), only the first is
    honoured (Option B: deliberate over forgiving). Single-match items
    still render with one checkbox and no `-> <id>`; parser falls back to
    matched_camo[0] cleanly.
  - See repair_approval_state.py for the one-time cleanup of existing
    state records affected by the v2 bug.

v2.2 changes (2026-05-21):
  - Removed `primary_camo_reason` ("Connection to anchor") from BOTH the
    cluster .md article blocks AND the LinkedIn caption prompt. The reason
    sentence was written during weekly enrichment under the old
    "supporting evidence" framing -- carrying it forward could subtly nudge
    Claude away from the Reading Room peer voice. Caption-writing Claude
    derives the thematic link freshly from summary + centre_angle +
    key_takeaway, all of which it already receives.
  - Added `key_takeaway` display to the cluster .md article blocks, right
    after the CAMO angle. Same one-sentence takeaway the editor saw at
    digest approval time. Useful as on-image overlay text for the
    downstream visual stage.
  - `primary_camo_reason` is still stored on state records (for traceability
    and potential future use); it's just not surfaced in the cluster .md
    or used in the caption prompt anymore.

v2.3 changes (2026-05-21):
  - Cluster .md now includes richer anchor metadata pulled from
    camo_index.json at firing time: authors, year, type (working paper /
    survey report / nano case), the paper's own pillars, and the full
    abstract. Downstream automations (image generation, post composition,
    archival) can read either the rendered .md or the structured state.
  - Falls back gracefully to title + url only if camo_index.json is missing
    or doesn't contain the anchor paper's id.

v2.4 changes (2026-05-22):
  - Primary-pillar selection: cluster .md now renders one tickable
    checkbox per pillar in the anchor section (only when the paper has 2+
    pillars in camo_index.json -- single-pillar papers render as plain
    info, no checkboxes). The editor ticks one; the next approval workflow
    run writes `primary_pillar` onto every clustered item's state record.
    Multi-tick handling: honour the first ticked pillar, warn about the
    rest. The approval workflow trigger now also fires on commits to
    ready_for_visual/**/*.md so pillar ticks pick up automatically.

v2.5 changes (2026-05-22):
  - Cluster files now land in a monthly subfolder, mirroring the
    `digests/2026-MM/` convention. New layout:
      ready_for_visual/2026-05/2026-05-21-bad-job-economy-2026.md
    The cluster_file value persisted in approval_state.json reflects this
    full nested path. Scanner switched from glob() to rglob() so monthly
    subfolders are searched. Flat files at the top level (legacy) still
    match -- nothing existing breaks.
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

VERSION = "v2.5 (2026-05-22)"

CLUSTER_THRESHOLD = 3                # fire a cluster at >= this many queued items
SKIP_NO_CAMO_CLUSTERS = True         # items without a CAMO match never auto-cluster

ANTHROPIC_MODEL = "claude-sonnet-4-6"
KEYWORDS_MAX_TOKENS = 400
LINKEDIN_MAX_TOKENS = 1500           # 3 captions x ~250 tokens + JSON overhead

# Series identity for the LinkedIn carousel post. The centre treats each
# cluster's post as an installment of this recurring series -- a peer/companion
# framing where CAMO research is shared alongside related work from the field.
# Edit these two values if the brand evolves; the prompt picks them up
# automatically. SERIES_HASHTAG should be the CamelCase version of the name
# with a leading '#'.
SERIES_NAME = "Reading Room"
SERIES_HASHTAG = "#ReadingRoom"

OPEN_GITHUB_ISSUE = True             # set False to skip the gh CLI step

REPO_ROOT = Path(__file__).parent
DIGESTS_DIR = REPO_ROOT / "digests"
# Folder name change in v2: was "clusters/", now "ready_for_visual/".
# Conceptually still a "cluster" in code; folder name reflects what it holds.
CLUSTERS_DIR = REPO_ROOT / "ready_for_visual"
APPROVAL_STATE_FILE = REPO_ROOT / "approval_state.json"
QUEUE_STATUS_FILE = REPO_ROOT / "QUEUE_STATUS.md"
# Read at cluster-firing time so the rendered cluster .md can include the
# anchor paper's authors / year / abstract, which aren't stored in state.
CAMO_INDEX_FILE = REPO_ROOT / "camo_index.json"


# ---------------------------------------------------------------------------
# Regex patterns for parsing the digest markdown
# ---------------------------------------------------------------------------

# Items are introduced by "### 1. Title", "### 2. Title", etc.
ITEM_HEADING_RE = re.compile(r"^###\s+\d+\.\s+", re.MULTILINE)

# APPROVE_BOX_RE and LINK_LINE_RE are defined alongside the parser further
# below, since their shape is tied to the parsing strategy.


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


# v2.4: regex for the primary-pillar checkbox lines inside a cluster .md.
# Matches lines like "- [x] AI & Incentives" (case-insensitive on x).
# Anchored inside the "Primary pillar — tick one ..." block by the caller.
PILLAR_TICK_RE = re.compile(r"-\s*\[\s*[xX]\s*\]\s*(.+?)\s*$", re.MULTILINE)


def scan_cluster_ticks(state: dict) -> int:
    """Re-read every cluster .md in ready_for_visual/, parse any newly-ticked
    primary-pillar checkbox, and apply the choice to the matching state
    records. Returns the number of state records updated this run.

    Matching: each clustered item in approval_state.json carries a
    `cluster_file` path pointing at its cluster .md. We use that to map
    cluster files back to their items.

    Multi-tick handling: same Option B rule as multi-match approvals --
    honour the first ticked pillar, log a warning about the rest. Single-
    pillar papers render as plain info (no checkboxes) and are unaffected.
    Editor un-ticking the box does NOT clear primary_pillar -- to undo,
    edit approval_state.json manually."""
    if not CLUSTERS_DIR.exists():
        return 0
    # v2.5: rglob (not glob) so monthly subfolders like ready_for_visual/2026-05/
    # are searched. Flat files at the top level (legacy / v2.4 layout) still match.
    cluster_files = sorted(CLUSTERS_DIR.rglob("*.md"))
    if not cluster_files:
        return 0

    # Build a cluster_file -> [state records] map once, so we update
    # in-memory state and persist once at the end.
    by_cluster_file = defaultdict(list)
    for url, rec in state.items():
        cf = rec.get("cluster_file")
        if cf:
            by_cluster_file[cf].append(rec)

    updates = 0
    for cf in cluster_files:
        rel = cf.relative_to(REPO_ROOT).as_posix()
        members = by_cluster_file.get(rel, [])
        if not members:
            # Cluster file exists on disk but no state record references it
            # (e.g. orphan from manual file creation). Skip silently.
            continue

        text = cf.read_text(encoding="utf-8", errors="replace")
        # Limit the pillar-tick search to the anchor section -- avoids
        # accidentally matching ticks elsewhere in the file.
        section_start = text.find("**Primary pillar — tick one")
        if section_start == -1:
            # Either a single-pillar paper (no checkboxes rendered) or an
            # older-format file. Skip.
            continue
        # End of pillar block is the next blank line followed by a
        # non-checkbox line, but the simpler rule is "find ticks before the
        # next ## header" which is robust enough.
        section_end = text.find("\n## ", section_start)
        if section_end == -1:
            section_end = len(text)
        section = text[section_start:section_end]

        ticks = list(PILLAR_TICK_RE.finditer(section))
        if not ticks:
            continue
        if len(ticks) > 1:
            print(f"[warn]   {rel}: {len(ticks)} pillars ticked (tick only "
                  f"ONE). Honouring the first; ignoring the rest.")
        chosen_pillar = ticks[0].group(1).strip()

        # Apply to every state record in this cluster. Skip records that
        # already have the same pillar -- idempotent.
        changed_here = 0
        for rec in members:
            if rec.get("primary_pillar") != chosen_pillar:
                rec["primary_pillar"] = chosen_pillar
                changed_here += 1
        if changed_here:
            updates += changed_here
            print(f"[ok]  {rel}: primary_pillar set to '{chosen_pillar}' "
                  f"({changed_here} record(s) updated)")
    return updates


def load_camo_index_by_id() -> dict:
    """Read camo_index.json and return a dict keyed by paper id, so we can
    look up the anchor paper's full metadata (authors, year, abstract,
    pillars) when rendering a cluster .md.

    Returns {} if the file is missing or malformed; callers must handle the
    empty case (the cluster will fall back to showing just title + url)."""
    if not CAMO_INDEX_FILE.exists():
        print(f"[warn] {CAMO_INDEX_FILE.name} not found -- anchor block will "
              f"fall back to title/url only")
        return {}
    try:
        data = json.loads(CAMO_INDEX_FILE.read_text())
        if not isinstance(data, list):
            print(f"[warn] {CAMO_INDEX_FILE.name} is not a JSON list -- ignoring")
            return {}
        return {it.get("id"): it for it in data if it.get("id")}
    except Exception as e:  # noqa: BLE001
        print(f"[warn] could not parse {CAMO_INDEX_FILE.name}: {e}")
        return {}


# ---------------------------------------------------------------------------
# Parsing the digest markdown for approved items
# ---------------------------------------------------------------------------

# Two ticked-box patterns the digest can produce:
#
#   Single-match items (only one CAMO match):
#     "- [x] **APPROVE FOR SOCIAL** (pillar: `AI & Jobs`)"
#     -> chosen_camo_id is implicit (use matched_camo[0])
#
#   Multi-match items (2+ CAMO matches, one checkbox per match):
#     "- [x] **APPROVE FOR SOCIAL** → `bad-job-economy-2026` — Title goes here"
#     -> chosen_camo_id is explicit, captured from the `id` between backticks
#
# A single regex with an OPTIONAL trailing capture handles both forms.
APPROVE_BOX_RE = re.compile(
    r"-\s*\[\s*[xX]\s*\]\s*\*\*APPROVE FOR SOCIAL\*\*"
    r"(?:[^\n`]*?→\s*`([^`]+)`)?",   # optional: '→ `chosen-camo-id`'
    re.MULTILINE,
)

LINK_LINE_RE = re.compile(r"-\s+\*\*Link:\*\*\s+<([^>]+)>")


def parse_approved_links_from_md(md_path: Path) -> list:
    """Return a list of (link, chosen_camo_id_or_None) tuples for each item
    block that contains a ticked APPROVE FOR SOCIAL checkbox.

    Per v2 multi-match handling: if MORE than one checkbox is ticked in the
    same item block (user disregarded the "tick one" instruction), only the
    FIRST tick is honoured -- subsequent ticks are logged and ignored. This
    is Option B in the design discussion: one anchor per article, deliberate
    over forgiving.

    chosen_camo_id is None for single-match items (no `→ id` in the line);
    the caller falls back to matched_camo[0] in that case."""
    text = md_path.read_text(encoding="utf-8", errors="replace")
    positions = [m.start() for m in ITEM_HEADING_RE.finditer(text)]
    if not positions:
        return []
    positions.append(len(text))

    approved = []
    for start, end in zip(positions[:-1], positions[1:]):
        block = text[start:end]
        ticks = list(APPROVE_BOX_RE.finditer(block))
        if not ticks:
            continue
        if len(ticks) > 1:
            print(f"[warn]   item in {md_path.name} has {len(ticks)} ticked "
                  f"checkboxes (multi-match: tick only ONE). Honouring the "
                  f"first; ignoring the rest.")
        chosen_camo_id = ticks[0].group(1)  # may be None for single-match
        link_match = LINK_LINE_RE.search(block)
        if not link_match:
            print(f"[warn]   item in {md_path.name} has an approval tick "
                  f"but no parseable Link line; skipping")
            continue
        approved.append((link_match.group(1).strip(), chosen_camo_id))
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


def _select_camo_match(item: dict, chosen_camo_id) -> dict:
    """Pick the CAMO match that anchors this approval.

    For single-match items: chosen_camo_id is None, return the only match.
    For multi-match items: chosen_camo_id is the editor's pick (parsed from
    the ticked checkbox line). Find it in matched_camo. If it's somehow not
    in the list -- which would only happen if the digest .md was hand-edited
    after enrichment -- fall back to matched_camo[0] with a warning."""
    matches = item.get("matched_camo") or []
    if not matches:
        return {}
    if chosen_camo_id is None:
        return matches[0]
    for m in matches:
        if m.get("id") == chosen_camo_id:
            return m
    print(f"[warn]   ticked CAMO id {chosen_camo_id!r} not found in this "
          f"article's matched_camo list; falling back to first match "
          f"{matches[0].get('id')!r}.")
    return matches[0]


def build_state_record(item: dict, source_md: Path, today_iso: str,
                       chosen_camo_id=None) -> dict:
    """Reduce an enrichment record to the subset we need to keep in state.

    chosen_camo_id is parsed from the multi-match checkbox UI -- the
    specific CAMO paper the editor ticked the article under. For single-
    match items, chosen_camo_id is None and matched_camo[0] is used."""
    pcamo = _select_camo_match(item, chosen_camo_id)
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
        # v2: audience_relevance carried forward so cluster-stage LinkedIn
        # caption drafting can pick tone (managers/policy/general weighting).
        "audience_relevance": item.get("audience_relevance") or {},
        # Cluster-level fields (cluster_keywords, cluster_linkedin_caption_draft,
        # cluster_file, clustered_at) are populated when the cluster fires,
        # not here.
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
        approved_entries = parse_approved_links_from_md(md)
        if not approved_entries:
            continue
        # Only load the sidecar if at least one link in this file is NEW.
        new_entries_here = [(l, cid) for (l, cid) in approved_entries
                            if l not in state]
        if not new_entries_here:
            continue
        sidecar_map = load_sidecar(md)
        for link, chosen_camo_id in new_entries_here:
            item = sidecar_map.get(link)
            if not item:
                print(f"[warn] {md.name}: approved link {link[:60]} not "
                      f"found in sidecar -- skipping")
                continue
            state[link] = build_state_record(item, md, today_iso,
                                             chosen_camo_id=chosen_camo_id)
            added += 1
            anchor = state[link]['primary_camo_id'] or 'none'
            note = " (editor's pick)" if chosen_camo_id else ""
            print(f"[ok]  queued: {state[link]['title'][:60]}  "
                  f"(camo: {anchor}{note})")
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


def _aggregate_audience(items: list) -> dict:
    """Roll up audience_relevance across the cluster's articles by taking the
    strongest signal for each audience type. Used to weight caption tone."""
    rank = {"high": 3, "medium": 2, "low": 1}
    inv_rank = {v: k for k, v in rank.items()}
    aggregated = {}
    for key in ("managers_csuite", "policy_makers", "general_econ_public"):
        best = 0
        for it in items:
            v = (it.get("audience_relevance") or {}).get(key, "")
            best = max(best, rank.get(v, 0))
        if best:
            aggregated[key] = inv_rank[best]
    return aggregated


def call_claude_for_linkedin_caption(items: list, camo_title: str,
                                     camo_url: str, pillars: list,
                                     keywords: list) -> str:
    """Generate ONE LinkedIn carousel caption for the whole cluster, in the
    Reading Room series voice.

    Voice direction: peer-among-peers. The centre is sharing its recent work
    alongside companion pieces from the field on a shared theme, NOT making
    an argument or pressure-testing other research. Punchy but invitational.
    The downstream visual stage will produce one image per article (three
    slides); this caption is the single post body anchoring all three.

    Returns the caption string, or empty string on any failure (cluster
    still fires; rendering just omits the section)."""
    if anthropic is None:
        print("[warn] anthropic package not installed -- skipping LinkedIn caption")
        return ""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[warn] ANTHROPIC_API_KEY not set -- skipping LinkedIn caption")
        return ""

    aggregated_aud = _aggregate_audience(items)
    aud_str = ", ".join(f"{k}={v}" for k, v in aggregated_aud.items()) \
        if aggregated_aud else "unspecified"
    pillars_str = ", ".join(pillars) if pillars else "unspecified"
    keyword_line = "  ".join(keywords)

    article_blocks = []
    for i, it in enumerate(items, 1):
        article_blocks.append(
            f"COMPANION PIECE {i}:\n"
            f"  title: {it.get('title', '')}\n"
            f"  source: {it.get('source', '')}\n"
            f"  pillar: {it.get('pillar', '')}\n"
            f"  summary: {(it.get('claude_summary') or '')[:500]}\n"
            f"  key takeaway: {it.get('key_takeaway', '')}"
        )

    prompt = (
        "You are an editorial assistant for the HKU Centre for AI, "
        f"Management and Organization (CAMO). Draft ONE LinkedIn carousel "
        f"caption for the recurring \"{SERIES_NAME}\" series.\n\n"
        f"VOICE of {SERIES_NAME}: peer-among-peers. The centre shares a "
        f"piece of its own recent work alongside companion pieces from "
        f"across the field, on a shared theme. We are NOT making an "
        f"argument or testing other research; we are generously sharing "
        f"what we've been thinking with, noticing what's resonating in the "
        f"field, and inviting the reader into the conversation. Punchy "
        f"but invitational. Never adversarial.\n\n"
        f"CENTRE'S RECENT PAPER (our contribution to this theme):\n"
        f"  title: {camo_title}\n"
        f"  url: {camo_url}\n"
        f"  pillars: {pillars_str}\n\n"
        f"CLUSTER KEYWORDS (use exactly as hashtags):\n  {keyword_line}\n\n"
        f"AGGREGATE AUDIENCE WEIGHTING (strongest signal across the 3 pieces):\n"
        f"  {aud_str}\n\n"
        f"COMPANION PIECES on the same theme (the three carousel slides):\n\n"
        + "\n\n".join(article_blocks) + "\n\n"
        "Caption requirements:\n"
        f"- 180-260 words total.\n"
        f"- OPEN with the series tag, naming the shared theme of this "
        f"  edition. Example forms: \"{SERIES_NAME} — <short theme>:\" or "
        f"  \"From the {SERIES_NAME}: <short theme>.\" Lead with the THEME "
        f"  or question shared across all four pieces, NOT with a finding "
        f"  from any one piece.\n"
        f"- Note the centre's paper briefly as our recent thinking on this "
        f"  theme (1-2 sentences). Visible but humble -- \"we've been "
        f"  working on\" / \"our recent paper examines\", NOT \"we argue "
        f"  that\" or \"our finding shows\".\n"
        f"- Bridge to the slides: \"Three pieces in this conversation:\" "
        f"  (or similar) followed by ONE short phrase per companion piece "
        f"  (max 8 words each).\n"
        f"- A line of generous synthesis -- a curator's noticing, not a "
        f"  conclusion. What do these pieces together suggest about the "
        f"  theme? Use plain prose.\n"
        f"- Close with a reader-centred invitation. Examples: \"What are "
        f"  you reading on this?\" / \"What would you add to the "
        f"  conversation?\" / \"What's resonating in your context?\"\n"
        f"- End the post with hashtags in this exact order: {SERIES_HASHTAG} "
        f"  + the three cluster keywords above (as given) + 1-2 pillar-"
        f"  specific hashtags.\n"
        f"- Do NOT include the paper URL in the body -- it goes in a "
        f"  separate first comment on LinkedIn.\n\n"
        "Tone weighting (use AGGREGATE AUDIENCE WEIGHTING above):\n"
        "  high managers_csuite      => decision-relevant framing\n"
        "  high policy_makers        => systemic / institutional framing\n"
        "  high general_econ_public  => accessible explainer framing\n"
        "  multiple high             => blend proportionally\n\n"
        "Restrained, peer voice. BANNED words and phrases:\n"
        "  - hype: \"fascinating\", \"must-read\", \"game-changing\", "
        "\"transformative\", \"revolutionary\", \"unlock\", \"harness\", "
        "\"leverage\" (as verb), \"in today's fast-paced world\"\n"
        "  - adversarial: \"argue\", \"argues\", \"argument\", \"challenge\", "
        "\"pushback\", \"contrarian\", \"wrong\", \"vs.\", \"debunk\", "
        "\"counter\", \"against\"\n\n"
        "0-2 emoji maximum. None preferred.\n\n"
        'Return ONLY a JSON object: {"caption": "<the full post body>"}'
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=LINKEDIN_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", None) == "text").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        if not text.startswith("{"):
            s, e = text.find("{"), text.rfind("}")
            if s != -1 and e != -1:
                text = text[s:e + 1]
        data = json.loads(text)
        caption = (data.get("caption") or "").strip()
        if caption:
            print(f"[ok]  {SERIES_NAME} caption: {len(caption)} chars drafted")
        else:
            print(f"[warn] {SERIES_NAME} caption returned empty")
        return caption
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {SERIES_NAME} caption generation failed: {e}")
        return ""


def render_cluster_md(camo_id: str, items: list, keywords: list,
                      caption: str, today_iso: str,
                      camo_index_map: dict = None) -> str:
    first = items[0]
    camo_title = first.get("primary_camo_title") or "(unknown CAMO paper)"
    camo_url = first.get("primary_camo_url") or ""
    cluster_article_pillars = sorted(
        {i.get("pillar", "") for i in items if i.get("pillar")}
    )

    # v2.3: pull richer metadata for the anchor paper from camo_index.json
    # (title prefer-overrides the cached one, authors/year/abstract/paper-
    # pillars only available here). Gracefully degrade if the index isn't
    # available or doesn't have this paper id.
    anchor_meta = (camo_index_map or {}).get(camo_id, {}) if camo_id else {}
    anchor_title = anchor_meta.get("title") or camo_title
    anchor_authors = anchor_meta.get("authors") or []
    anchor_year = anchor_meta.get("year")
    anchor_abstract = anchor_meta.get("abstract") or ""
    anchor_pillars = anchor_meta.get("pillars") or []
    anchor_type = anchor_meta.get("type") or ""

    lines = []
    lines.append(f"# Content Cluster — {anchor_title}")
    lines.append("")
    lines.append(f"_Created: {today_iso}  ·  Cluster id: `{camo_id}`  ·  "
                 f"{len(items)} articles_")
    lines.append("")
    lines.append("## Anchor CAMO research")
    lines.append("")
    if camo_url:
        lines.append(f"**[{anchor_title}]({camo_url})**")
    else:
        lines.append(f"**{anchor_title}**")
    # Author / year / type line (only when index lookup succeeded)
    byline_parts = []
    if anchor_authors:
        byline_parts.append(", ".join(anchor_authors))
    if anchor_year:
        byline_parts.append(str(anchor_year))
    if anchor_type:
        # human-friendly: working_paper -> "Working Paper", etc.
        byline_parts.append(anchor_type.replace("_", " ").title())
    if byline_parts:
        lines.append(f"_{'  ·  '.join(byline_parts)}_")
    lines.append("")
    if anchor_pillars:
        if len(anchor_pillars) == 1:
            # Single-pillar paper: nothing to choose, render as plain info
            # (skips the checkbox UI; downstream automations can still infer
            # primary_pillar from the camo_index if they need to).
            lines.append(f"**Primary pillar:** {anchor_pillars[0]}")
        else:
            # Multi-pillar paper: render one tickable checkbox per pillar.
            # Parser honours the FIRST ticked one (Option B, same as multi-
            # match approval boxes). Tick anytime; the next approval workflow
            # run writes primary_pillar onto each clustered item's state.
            lines.append("**Primary pillar — tick one to anchor the visual + lead hashtag:**")
            lines.append("")
            for p in anchor_pillars:
                lines.append(f"- [ ] {p}")
        lines.append("")
    if anchor_abstract:
        lines.append("**Abstract:**")
        lines.append("")
        for ln in anchor_abstract.split("\n"):
            lines.append(f"> {ln}" if ln.strip() else ">")
        lines.append("")
    # Carry the article-pillar aggregation through as a separate, smaller
    # line -- different signal from the paper's own pillars, kept in case
    # the editor wants to see what pillars the three companions span.
    if cluster_article_pillars:
        lines.append(f"_Pillars across the three companion pieces: "
                     f"{', '.join(cluster_article_pillars)}_")
        lines.append("")
    lines.append("## Shared keywords (Claude-suggested)")
    lines.append("")
    lines.append("  ".join(keywords))
    lines.append("")
    lines.append("## Draft LinkedIn carousel caption")
    lines.append("")
    if caption:
        lines.append(f"_The single post body for this {SERIES_NAME} edition. "
                     f"Edit inline if needed. The three companion pieces below "
                     f"will become the three carousel slides._")
        lines.append("")
        for ln in caption.split("\n"):
            lines.append(f"> {ln}" if ln.strip() else ">")
        lines.append("")
    else:
        lines.append("_Caption draft was not generated this run (API failure "
                     "or missing key). Write one here manually, or re-run the "
                     "approval workflow once the issue is resolved._")
        lines.append("")
    lines.append("## Final approval")
    lines.append("")
    lines.append("- [ ] **APPROVE FOR VISUAL CREATION**")
    lines.append("")
    lines.append("_Tick the box once the caption above is editorially sound. "
                 "The next stage (Higgsfield image generation) will produce "
                 "**three images, one per article**, used as the slides of a "
                 "**single LinkedIn carousel post** anchored to the CAMO "
                 "paper. The caption above drives that post; the article "
                 "blocks below brief each slide. The visual stage is not yet "
                 "wired up._")
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
        if it.get("key_takeaway"):
            # Pulled forward from the weekly digest enrichment -- the same
            # single-sentence takeaway the editor saw at approval time. Useful
            # downstream (e.g. as on-image overlay text in the visual stage).
            lines.append(f"**Key takeaway:** {it['key_takeaway']}")
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
    # Load camo_index once per run -- shared across all clusters fired in
    # this run. Falls through to {} cleanly if the file is missing.
    camo_index_map = load_camo_index_by_id()
    if camo_index_map:
        print(f"[info] camo_index.json loaded: {len(camo_index_map)} papers "
              f"available for anchor metadata")
    created = 0
    for camo_id, items in sorted(ready.items()):
        camo_title = items[0].get("primary_camo_title") or camo_id
        camo_url = items[0].get("primary_camo_url") or ""
        pillars = sorted({i.get("pillar", "") for i in items if i.get("pillar")})
        print(f"[info] firing cluster: {camo_id}  ({len(items)} items) — {camo_title[:60]}")

        keywords = call_claude_for_keywords(items, camo_title)

        # v2: ONE carousel caption per cluster (the CAMO paper is the post's
        # anchor; the three articles become the slides). Stored on each
        # clustered item's state record as `cluster_linkedin_caption_draft`
        # -- same value on all three, parallel to how cluster_keywords is
        # stored. Empty string on API failure; renderer handles that.
        caption = call_claude_for_linkedin_caption(
            items, camo_title, camo_url, pillars, keywords
        )

        body = render_cluster_md(camo_id, items, keywords, caption, today_iso,
                                 camo_index_map=camo_index_map)
        # v2.5: organise cluster files by month, mirroring the digests/2026-MM/
        # convention. today_iso is YYYY-MM-DD, so today_iso[:7] is YYYY-MM.
        month_subdir = CLUSTERS_DIR / today_iso[:7]
        month_subdir.mkdir(parents=True, exist_ok=True)
        cluster_path = month_subdir / f"{today_iso}-{camo_id}.md"
        cluster_path.write_text(body, encoding="utf-8")
        print(f"[ok]  wrote {cluster_path.relative_to(REPO_ROOT).as_posix()}")

        # Mark items as clustered + persist cluster-level fields onto each.
        rel = cluster_path.relative_to(REPO_ROOT).as_posix()
        for item in items:
            link = item["link"]
            state[link]["state"] = "clustered"
            state[link]["cluster_file"] = rel
            state[link]["clustered_at"] = today_iso
            state[link]["cluster_keywords"] = keywords
            state[link]["cluster_linkedin_caption_draft"] = caption

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

    # Step 5 (v2.4): scan existing cluster .md files for primary-pillar
    # ticks. Lets the editor pick a primary pillar at any time after a
    # cluster has fired; the workflow auto-picks it up on the next run.
    # Cluster files written above by fire_clusters get scanned too -- a no-op
    # in practice since they have no ticks yet, but harmless.
    pillar_updates = scan_cluster_ticks(state)
    if pillar_updates:
        print(f"[info] primary_pillar set/changed on {pillar_updates} state record(s)")

    # Persist state regardless of whether anything changed -- cheap, safer.
    save_approval_state(state)

    # Write the human-readable status dashboard. Always runs, even when no
    # changes happened, so the timestamp inside the file reflects the latest
    # workflow run -- a clear signal the script did execute.
    save_queue_status(state)

    print(f"[ok] approval_state.json saved.  "
          f"Summary this run: +{added} queued, +{created} clusters created, "
          f"+{pillar_updates} primary-pillar update(s).")


if __name__ == "__main__":
    main()
