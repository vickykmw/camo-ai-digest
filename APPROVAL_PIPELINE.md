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
        ▼  Generate Carousel workflow fires (.github/workflows/generate-carousel.yml)
        │
        ▼  runs scripts/generate.py
        │
   reads ready_for_visual/YYYY-MM/<file>.md, validates approval + pillar
        │
        ▼  writes three files alongside the source markdown:
           - <file>-report.md           (Higgsfield prompts, copy-ready)
           - <file>-article-canva.csv   (paper-slide rows, hero_image_url empty)
           - <file>-bridge-canva.csv    (bridge-slide rows for slides N-1 and N)
        │
        ▼ [ HUMAN ] open report on GitHub, copy each prompt to Higgsfield's
                    web UI (Seedream 4.5, 1K, 1:1), generate
        │
        ▼ [ HUMAN ] paste resulting CDN URLs into the article CSV's
                    hero_image_url column, commit
        │
        ▼ [ HUMAN ] in Canva, open the right pillar template, run
                    Bulk Create against the article CSV
        │
        ▼ [ HUMAN ] manually drag Higgsfield images onto each paper slide
                    (Canva Bulk Create doesn't accept image URLs as images)
        │
        ▼ [ HUMAN ] manually fill cover, paper-showcase, and CTA slides
                    using bridge-canva.csv as reference
        │
        ▼ export, publish
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
| `ready_for_visual/YYYY-MM/<cluster>-report.md` | Per-paper Seedream prompts in code blocks; copy-ready for Higgsfield's web UI | bot only (regenerated on every approved-cluster commit) |
| `ready_for_visual/YYYY-MM/<cluster>-article-canva.csv` | Paper-slide rows for Canva Bulk Create (source, title, takeaway, slide_indicator, hero_image_url). `hero_image_url` empty when written; human fills after Higgsfield generation. | bot (write), human (fill hero URLs) |
| `ready_for_visual/YYYY-MM/<cluster>-bridge-canva.csv` | Two rows for slides N-1 (CAMO paper showcase) and N (CTA close). Used as reference for manual Canva fill. | bot only |
| `scripts/style_envelopes.py` | Per-pillar palettes + the Seedream prompt envelope template. Editing here changes every future generation. | human |

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

---

# Visual Creation Stage

After the gatekeeper ticks both `[x] Primary pillar` and `[x] APPROVE FOR
VISUAL CREATION` on a cluster file and commits, a second workflow fires that
produces everything needed to take the cluster into Canva. This stage is
mostly automation with two manual handoffs: copying prompts to Higgsfield
for image generation, and finishing slides in Canva.

The two stages are independent — the visual stage doesn't read
`approval_state.json` or share code with `process_approvals.py`. It reads
the committed cluster markdown directly and writes its outputs alongside.

## What triggers it

The `Generate Carousel` workflow fires on push to:

- `ready_for_visual/**/*.md` — picks up newly ticked approval boxes
  (excluding `*-report.md` to avoid recursive triggering when the bot
  commits results back).
- Manual `workflow_dispatch` trigger from the Actions tab — useful for
  re-runs after editing style envelopes or fixing markdown.

The workflow runs `scripts/generate.py`, which scans `ready_for_visual/`
for files where:

1. The `APPROVE FOR VISUAL CREATION` checkbox is ticked.
2. Exactly one Primary pillar checkbox is ticked (or a plain
   `**Primary pillar:** X` line is present, for backward compatibility).

Files that fail either check are silently skipped. This is intentional so
drafts and edits in progress can be committed without firing the pipeline.

## What it produces

For each approved cluster file `ready_for_visual/YYYY-MM/CLUSTER.md`, the
pipeline writes three files in the same folder and commits them back to the
repo automatically:

- **`CLUSTER-report.md`** — A per-paper rendering of the Seedream prompts.
  Each section has the paper title, source, key takeaway, and a code block
  containing the full prompt (visual concept wrapped in the pillar's style
  envelope). The report is the gatekeeper's working artifact for the
  Higgsfield step.

- **`CLUSTER-article-canva.csv`** — One row per paper. Columns:
  `source`, `title`, `takeaway`, `slide_indicator`, `hero_image_url`. The
  `hero_image_url` column is empty when written; the gatekeeper fills it
  after generating each image in Higgsfield. This CSV is what gets loaded
  into Canva's Bulk Create.

- **`CLUSTER-bridge-canva.csv`** — Two rows representing the two bridge
  slides (paper showcase and CTA close). All bridge fields are present
  (kicker, explanation_line, title, subtitle, authors, key_finding,
  statement, slide_indicator); empty cells indicate fields that don't apply
  to that slide role. Reference for manual Canva fill — bridge slides
  aren't bulk-created.

End-to-end the workflow takes ~10 seconds (no external API calls).

## The style envelope

Pillar-specific palettes and the prompt template live in
`scripts/style_envelopes.py`. Each pillar has a three-tone palette:

| Pillar | Midtone | Highlight | Shadow |
| --- | --- | --- | --- |
| AI Adoption | `#3E5C76` (steel blue) | `#EACDCA` (pale pink) | `#1A2732` (dark slate) |
| AI & Incentives | `#9A7D3F` (brass) | `#F4EAD5` (soft cream) | `#2A2008` (dark brass) |
| AI & Jobs | `#977C6E` (clay-taupe) | `#F0E0D2` (bone) | `#2F211B` (dark clay) |
| AI Algorithms & Data | `#4A4467` (slate-violet) | `#D8CFE6` (pale lilac) | `#1A1626` (dark slate-violet) |

The prompt envelope wraps each paper's `Suggested visual` with the palette
plus fixed style instructions: pointillist stipple aesthetic, focal-subject
emphasis, calm bottom for text overlay, negative prompt covering banned
elements (no robotic hands, no glowing brains, no pixel art, etc.). Editing
the template propagates to every future generation; existing outputs
aren't refreshed automatically (manual workflow trigger required).

## Human workflow after the pipeline runs

1. **Open the report on GitHub.** Renders nicely in the web UI. Each paper
   section has a clearly labelled "Prompt for Higgsfield" code block with a
   copy button.

2. **Copy each prompt into Higgsfield's web UI** at higgsfield.ai. Settings:
   model Seedream 4.5, resolution 1K, aspect ratio 1:1. Generate. Each
   image takes 10-30 seconds.

3. **Paste CDN URLs into the article CSV.** Edit
   `CLUSTER-article-canva.csv` directly on GitHub (pencil icon → paste
   URLs into the `hero_image_url` column → commit), or download and edit
   locally.

4. **Open the right pillar template in Canva.** Steel-blue for Adoption,
   brass for Incentives, clay-taupe for Jobs, slate-violet for Algorithms.
   Four separate Canva files exist, one per pillar.

5. **Run Canva Bulk Create against the article CSV.** Map columns:
   `source` → source text field, `title` → title, `takeaway` → takeaway,
   `slide_indicator` → slide indicator. Canva generates one slide per CSV
   row, filled with text but with empty hero image placeholders.

6. **Manually drag images onto each slide.** Canva Bulk Create populates
   text fields only; it can't accept image URLs as image elements (URLs
   get treated as plain text). Open each Higgsfield CDN URL in a browser
   tab, save the image locally, drag it onto the matching paper slide.
   ~30 seconds for a 3-paper carousel.

7. **Manually build slides 1, N-1, and N:**
   - **Slide 1 (cover):** not yet templated — build by hand for now.
   - **Slide N-1 (paper showcase):** use the `paper_showcase` row from
     `bridge-canva.csv`. Drop the PDF first-page export into the cover
     frame.
   - **Slide N (CTA close):** use the `cta_close` row from
     `bridge-canva.csv`.

8. **Export and publish.**

Total manual time for a 3-paper carousel: roughly 5 minutes in Higgsfield
+ 5 minutes in Canva.

## What's still manual and why

**Higgsfield generation is manual** because the official Python SDK
currently returns 404s on the documented model endpoints — published
examples don't match the live API as of this writing. Rather than spend
more time debugging an undermaintained client library, the pipeline writes
prompts and the gatekeeper copies them. If the SDK situation improves
later, the pipeline can be extended to call Higgsfield directly with
minimal changes (the wrapper code exists; only the URL pattern needs
updating).

**Image dragging is manual** because Canva's Bulk Create doesn't accept
arbitrary image URLs as image elements. The supported workaround is
XLSX with embedded image data, which adds significant complexity (download
images server-side, embed in spreadsheet cells, upload XLSX to Canva)
that isn't justified at the current publishing scale.

**The cover slide is manual** because we deferred designing it. When the
cover template is built, the pipeline can be extended — add a
`cover_visual_concept` field to the markdown schema and the script will
write a cover-slide prompt to the report alongside the paper prompts.

## Edge cases (visual stage)

**Approval ticked but no pillar ticked.** Pipeline raises a parse error in
the Actions log: "No Primary pillar selected." No output files are
written. Fix: tick exactly one Primary pillar checkbox.

**Multiple pillars ticked.** Parser raises an explicit error listing the
ticked pillars. Same fix: tick exactly one.

**Required field missing from a paper block.** Parser fails on the
specific missing field (e.g., "Article 2 missing `**Key takeaway:**`").
Fix the markdown and recommit; the pipeline re-fires.

**Pipeline commits trigger a workflow loop.** The workflow's path filter
excludes `*-report.md` files. Updates to the article CSV (when the
gatekeeper pastes Higgsfield URLs) also don't re-fire the visual stage —
only changes to the source `.md` do.

**Bad image from Higgsfield.** Regenerate in the Higgsfield UI, paste the
new URL into the article CSV row. The CSV is the source of truth for
what enters Canva.

**Wrong pillar palette.** Edit `style_envelopes.py`, manually trigger the
workflow (Actions tab → Generate Carousel → Run workflow). The pipeline
rewrites all three output files with the new envelope.

**File renamed or moved.** Pipeline re-fires on the new filename; old
output files become orphaned but not deleted. Clean up by hand.

## Tuning knobs (visual stage)

- `DEFAULT_RESOLUTION` in `generate.py` — currently `"1K"`. Bump to `"2K"`
  if production quality matters more than credit spend.
- `VALID_PILLARS` in `approval_to_gen_visual.py` — the four pillar names.
  Adding a pillar requires also updating `PILLAR_ENVELOPES` in
  `style_envelopes.py` with a new palette.
- `STYLE_ENVELOPE_TEMPLATE` in `style_envelopes.py` — the prompt wrapper
  applied to every generation. Changes here propagate to all future
  carousels.

## Cost (visual stage)

The GitHub Actions side is free — the pipeline writes files and commits
back, no external API calls.

Higgsfield credits are spent in the manual generation step, charged
against your Higgsfield subscription's monthly quota. At Seedream 4.5,
1K, 1:1, each image costs 1 credit. A 5-paper carousel = 5 credits =
pennies on the Ultra plan's monthly allotment.

## Setup steps (done once for visual stage)

1. Repo `requirements-pipeline.txt` lists `PyYAML>=6.0`. No external API
   credentials required.
2. Workflow permissions: Settings → Actions → General → "Read and write
   permissions" so the bot can commit results back.
3. Four pillar templates exist in Canva (one per pillar — Adoption,
   Incentives, Jobs, Algorithms). Each uses the brand kit's pillar colour
   and the same text-field structure for Bulk Create.

## Stopping the visual stage

If you want to pause visual generation while keeping the upstream approval
pipeline running, comment out the `on:` block in `generate-carousel.yml`
or set `if: false` on the job. Approved cluster `.md` files will still be
created upstream; they just won't trigger the visual pipeline. When you're
ready to resume, re-enable the workflow and any approved-but-unprocessed
files will be picked up on the next manual trigger.
