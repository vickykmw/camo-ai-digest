# Approval Pipeline — How It Works

This document describes the multi-stage human-in-the-loop pipeline that turns a
weekly digest into a CAMO-anchored content cluster.

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
        │
        ▼  is any CAMO paper now at ≥3 queued ticks?
        │
        ├── No  → script ends, items wait in queue
        │
        └── Yes → fire cluster:
                  - call Claude for 3 shared keyword tags
                  - write clusters/<date>-<camo-id>.md
                  - mark items state="clustered"
                  - open GitHub issue (you receive an email)
        │
        ▼  human reviews cluster, edits if needed, ticks
           [x] APPROVE FOR VISUAL CREATION  (stage 5 — not yet built)
        │
        ▼  (future) Higgsfield image generation workflow
```

## File map

| File | Role | Modified by |
| --- | --- | --- |
| `digests/YYYY-MM/YYYY-MM-DD_YYYY-MM-DD.md` | Weekly digest, with tickable boxes | bot (write), human (tick) |
| `digests/YYYY-MM/YYYY-MM-DD_YYYY-MM-DD.enriched.json` | Structured enrichment for the same week | bot only |
| `approval_state.json` | Per-link state: `queued` or `clustered` | bot only (don't hand-edit unless undoing) |
| `clusters/YYYY-MM-DD-<camo-id>.md` | A cluster of 3+ articles around one CAMO paper | bot (write), human (final tick) |

## How the bot decides which items cluster

When you tick `[x] APPROVE FOR SOCIAL` in a weekly digest and push, the
approval workflow runs `process_approvals.py`. For each tick:

1. **Look up the article's primary CAMO match.** The article's enrichment
   sidecar lists 0–2 `matched_camo` entries (Claude found them when the
   digest was first built). We use the **first** entry — that's the
   strongest connection.

2. **Add the article to the queue.** It joins `approval_state.json` with
   `state="queued"`, tagged by its primary CAMO id.

3. **Check the threshold.** For each CAMO paper id, count how many articles
   are currently queued. If any paper has reached **3 or more**, fire a
   cluster for it.

When a cluster fires:

- The 3+ queued articles are pulled out and packaged into a single
  `.md` file in `clusters/`.
- Claude is asked to suggest **3 short keyword hashtags** that capture
  the shared theme.
- A GitHub issue is opened with the title "Cluster ready: ..." — you'll
  get a notification email from GitHub.
- The articles transition to `state="clustered"` so they don't trigger
  the threshold again.

## Edge cases

**An article matches no CAMO paper.** It sits in the queue indefinitely.
The `SKIP_NO_CAMO_CLUSTERS = True` flag in `process_approvals.py` means
these items don't auto-cluster (there's no CAMO paper to anchor a post
around). Flip it to `False` if you want a "no-CAMO" bucket that also
clusters at 3+.

**A re-appearing article is ticked twice.** Idempotent: the second tick is
detected but ignored — the link is already in `approval_state.json`.

**An article matches two CAMO papers.** Only the primary (first) match
counts toward grouping. The full `matched_camo` list is preserved in the
state record for reference.

**Untickling.** Once an item is in `approval_state.json` (queued or
clustered), untickling the box in the digest does NOT remove it. To undo,
delete the entry from `approval_state.json` manually.

**An item with no `Link:` line.** Logged as a warning and skipped. Won't
crash the run.

## Tuning knobs (top of `process_approvals.py`)

- `CLUSTER_THRESHOLD = 3` — how many queued items it takes to fire.
- `SKIP_NO_CAMO_CLUSTERS = True` — see above.
- `ANTHROPIC_MODEL = "claude-sonnet-4-6"` — model for the keyword tags.
- `OPEN_GITHUB_ISSUE = True` — set False to skip the issue creation step.

## Cost

Each cluster fire = one Claude call of ~1k input tokens for the keyword
suggestion. At Sonnet 4.6 rates, **roughly $0.005 per cluster** — fractions
of a cent. The bigger cost remains the weekly digest enrichment, which the
cache already handles.

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

## Manual testing

You can trigger the approval workflow without ticking anything via
**Actions → Process Approvals → Run workflow**. The script will run, find
no new ticks, and exit cleanly. Good for verifying the workflow is wired up
before you stake real approvals on it.

## Stopping the pipeline

If you want to pause the whole approval pipeline (e.g. during a holiday or
while restructuring the CAMO index), comment out the entire `on:` block in
`approval.yml` or set `if: false` on the job. The weekly digest will keep
flowing; only the queue/cluster logic will pause. Ticks accumulate and will
be picked up on the next run.
