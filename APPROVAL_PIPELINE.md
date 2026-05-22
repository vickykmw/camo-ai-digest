# Approval Pipeline — How It Works

This document describes the multi-stage human-in-the-loop pipeline that turns a
weekly digest into a CAMO-anchored content cluster, ready for the visual
creation stage.

## At a glance

```
Weekly digest .md (created by digest.py)
        │
        ▼  human ticks  [x] APPROVE FOR SOCIAL  and commits
        │
Process Approvals workflow fires (.github/workflows/approval.yml)
        │
        ▼  runs process_approvals.py
        │
   approval_state.json updates:
   newly ticked link → state="queued"
   QUEUE_STATUS.md regenerated
        │
        ▼  is any CAMO paper now at ≥3 queued ticks?
        │
        ├── No  → script ends, items wait in queue
        │
        └── Yes → fire cluster:
                  - load anchor metadata from camo_index.json
                    (authors, year, type, pillars, abstract)
                  - call Claude for 3 shared keyword hashtags
                  - call Claude for ONE LinkedIn carousel caption
                    (Reading Room series voice)
                  - write ready_for_visual/YYYY-MM/<date>-<camo-id>.md
                  - mark items state="clustered"
                  - open GitHub issue (you receive an email)
        │
        ▼  human reviews cluster, edits caption + framing if needed,
           ticks one [x] Primary pillar, then
           [x] APPROVE FOR VISUAL CREATION  and commits
        │
        ▼  Process Approvals fires again (commits to ready_for_visual/**/*.md
           re-trigger the workflow):
           - parses the primary pillar tick
           - writes primary_pillar onto each clustered item's state record
        │
        ▼  (next stage) Higgsfield image generation reads primary_pillar +
           visual_concept from approval_state.json, produces three carousel
           slides for a single LinkedIn post anchored to the CAMO paper.
```

## File map

| File | Role | Modified by |
| --- | --- | --- |
| `digests/YYYY-MM/YYYY-MM-DD_YYYY-MM-DD.md` | Weekly digest, with tickable boxes | bot (write), human (tick) |
| `digests/YYYY-MM/YYYY-MM-DD_YYYY-MM-DD.enriched.json` | Structured enrichment for the same week | bot only |
| `camo_index.json` | CAMO research database (read for anchor metadata at cluster firing time) | human (add new papers) |
| `approval_state.json` | Per-link state: `queued` or `clustered`, plus cluster-level fields | bot only (don't hand-edit unless undoing) |
| `QUEUE_STATUS.md` | Human-readable dashboard. **This is the file to open to see what's happening.** Per-paper queue counts, list of clusters fired. | bot only (auto-regenerated every run) |
| `ready_for_visual/YYYY-MM/YYYY-MM-DD-<camo-id>.md` | A cluster of 3+ articles around one CAMO paper, with caption + primary pillar checkbox + final approval box | bot (write), human (tick + edit) |

## How the bot decides which items cluster

When you tick `[x] APPROVE FOR SOCIAL` in a weekly digest and push, the
approval workflow runs `process_approvals.py`. For each tick:

1. **Capture the chosen CAMO id from the ticked checkbox line.** Single-match
   articles have one checkbox and use the article's only CAMO match.
   Multi-match articles render one checkbox per CAMO paper (with the id
   visible on each line); the parser captures the chosen id from whichever
   checkbox was ticked.

2. **Add the article to the queue.** It joins `approval_state.json` with
   `state="queued"`, tagged by the chosen CAMO paper.

3. **Check the threshold.** For each CAMO paper id, count how many articles
   are currently queued. If any paper has reached **3 or more**, fire a
   cluster for it.

When a cluster fires:

- The 3+ queued articles are pulled out and packaged into a single
  `.md` file at `ready_for_visual/YYYY-MM/`.
- Anchor metadata (title, authors, year, type, pillars, full abstract) is
  pulled fresh from `camo_index.json` and rendered at the top.
- Claude is asked to suggest **3 short keyword hashtags** that capture the
  shared theme across the 3 articles.
- Claude is asked to draft **one LinkedIn carousel caption** in the
  recurring Reading Room series voice — peer-among-peers, sharing CAMO's
  recent work alongside companion research, never adversarial.
- A GitHub issue is opened with the title "Cluster ready: ..." — you'll
  get a notification email from GitHub.
- The articles transition to `state="clustered"` so they don't trigger
  the threshold again.

## What lives inside a cluster file

A cluster `.md` is the artifact your editorial reviewer interacts with.
Sections, top to bottom:

1. **Anchor CAMO research** — title (linked), byline (authors · year · type),
   primary-pillar checkboxes (or plain "Primary pillar: X" for single-pillar
   papers), full abstract as blockquote, the pillars represented across the
   three companion pieces.
2. **Shared keywords** — three Claude-suggested hashtags for the post.
3. **Draft LinkedIn carousel caption** — the single post body. Edit inline.
4. **Final approval** — the `[ ] APPROVE FOR VISUAL CREATION` checkbox.
5. **Articles in this cluster** — three article blocks, each with: source,
   link, published date, pillar, summary, CAMO angle, key takeaway, and
   suggested visual concept.

## The Reading Room series voice

LinkedIn captions are drafted in a fixed series identity: **Reading Room**,
hashtag `#ReadingRoom`. The framing is peer-among-peers — the CAMO paper is
the centre's recent thinking on a theme; the three articles are companion
pieces in the same conversation. Hooks should be punchy but invitational;
adversarial framing (argue, challenge, pressure-test, contrarian) is
explicitly banned in the prompt. The closing line is always reader-centred
("what are you reading on this?" / "what would you add?").

Series name and hashtag are controlled by two constants in
`process_approvals.py`: `SERIES_NAME` and `SERIES_HASHTAG`. Edit there if the
brand evolves; the prompt picks them up automatically.

## Primary pillar selection

A CAMO paper may have multiple pillars in `camo_index.json`. For a
multi-pillar paper, the cluster file renders one checkbox per pillar; the
editor ticks one to designate the primary pillar. The selection drives the
visual style choice and the lead pillar hashtag in the downstream image
stage. The chosen pillar is written to every clustered item's state record
as `primary_pillar`.

Single-pillar papers render a plain "Primary pillar: X" line instead of
checkboxes — there's nothing to choose. Downstream automation handles both
shapes by reading the `primary_pillar` field (multi-pillar after the editor
ticks) or falling back to the paper's only pillar (single-pillar).

If the editor ticks more than one pillar, the parser honours the first and
warns about the rest — same rule as multi-match article approvals.

## Edge cases

**An article matches no CAMO paper.** It sits in the queue indefinitely.
The `SKIP_NO_CAMO_CLUSTERS = True` flag in `process_approvals.py` means
these items don't auto-cluster (there's no CAMO paper to anchor a post
around). Flip it to `False` if you want a "no-CAMO" bucket that also
clusters at 3+.

**A re-appearing article is ticked in two different digests.** Idempotent:
the second tick is detected but ignored — the link is already in
`approval_state.json` keyed by URL.

**A multi-match article has two checkboxes ticked.** Parser honours the
first ticked checkbox and warns about the rest in the workflow log. Tick
only one.

**An article matches multiple CAMO papers and you ticked the wrong one.**
To re-anchor: delete the article's entry from `approval_state.json`
entirely, un-tick the wrong box and tick the right one in the source
digest `.md`, then commit. The next workflow run re-queues it under the
correct paper.

**Un-ticking.** Once an item is in `approval_state.json` (queued or
clustered), un-ticking the box in the digest does NOT remove it. To undo,
delete the entry from `approval_state.json` manually.

**Un-clustering** (e.g. to re-fire a cluster under updated code). For each
of the 3 clustered items: change `"state": "clustered"` back to `"queued"`,
delete the cluster-level fields (`cluster_file`, `clustered_at`,
`cluster_keywords`, `cluster_linkedin_caption_draft`). Then delete the old
cluster `.md` file. The next workflow run will see 3 queued items, hit the
threshold, and produce a fresh cluster file.

**An item with no `Link:` line.** Logged as a warning and skipped. Won't
crash the run.

**`camo_index.json` missing or doesn't have the anchor's id.** The cluster
file falls back to title + url only for the anchor section; everything else
proceeds normally. Add the paper to the index when you can; future cluster
firings on the same paper will then pick up the richer metadata.

## Tuning knobs (top of `process_approvals.py`)

- `CLUSTER_THRESHOLD = 3` — how many queued items it takes to fire.
- `SKIP_NO_CAMO_CLUSTERS = True` — see above.
- `ANTHROPIC_MODEL = "claude-sonnet-4-6"` — model for keywords + caption.
- `LINKEDIN_MAX_TOKENS = 1500` — output ceiling for the caption call.
- `OPEN_GITHUB_ISSUE = True` — set False to skip the issue creation step.
- `SERIES_NAME = "Reading Room"` / `SERIES_HASHTAG = "#ReadingRoom"` —
  the LinkedIn series identity. Used by the caption prompt and rendered
  at the top of the caption section in each cluster file.

## Cost

Per cluster firing:

- Keyword call: ~1k input + ~150 output tokens → ~$0.005
- LinkedIn caption call: ~1.5k input + ~400 output tokens → ~$0.012
- **Total: roughly $0.02 per cluster**

The bigger cost remains the weekly digest enrichment, which the cache
already handles. At a typical cadence of 1–3 clusters per month, the
approval pipeline adds a few cents per month to the API bill.

## Setup steps (done once)

1. The repo already has `requirements.txt` listing `anthropic`. No change.
2. `ANTHROPIC_API_KEY` is already a repo secret. No change.
3. `GH_TOKEN` is supplied automatically by GitHub Actions via
   `secrets.GITHUB_TOKEN` — the workflow file passes it through.
4. The `Process Approvals` workflow has `issues: write` permission so it can
   call `gh issue create`. This is declared in `approval.yml`.
5. (Optional) Create a label called `content-cluster` in the repo for nicer
   issue filtering. If it doesn't exist, the workflow falls back to creating
   the issue without a label — no manual setup required.

## Workflow triggers

The `Process Approvals` workflow fires automatically on commits to:

- `digests/**/*.md` — to pick up new APPROVE FOR SOCIAL ticks
- `ready_for_visual/**/*.md` — to pick up primary pillar ticks and final
  approval ticks
- `process_approvals.py` — so code changes redeploy cleanly
- `.github/workflows/approval.yml` — same

It can also be triggered manually via **Actions → Process Approvals → Run
workflow** (workflow_dispatch). The script always rewrites `QUEUE_STATUS.md`
on every run, so triggering manually is a safe way to refresh the dashboard
after any state surgery.

## Manual testing

You can trigger the approval workflow without ticking anything. The script
will run, find no new ticks, possibly find no new primary pillar selections,
and exit cleanly. Good for verifying the workflow is wired up before you
stake real approvals on it.

## Stopping the pipeline

If you want to pause the whole approval pipeline (e.g. during a holiday or
while restructuring the CAMO index), comment out the entire `on:` block in
`approval.yml` or set `if: false` on the job. The weekly digest will keep
flowing; only the queue / cluster / pillar logic will pause. Ticks
accumulate and will be picked up on the next run.
