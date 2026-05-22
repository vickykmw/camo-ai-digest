# CAMO carousel pipeline

GitHub Actions pipeline that turns gatekeeper-approved papers into
Canva-ready carousel data.

## How it works (the human view)

1. Scraper produces a content cluster markdown.
2. You review the markdown. When ready, tick the checkbox:
   `- [x] **APPROVE FOR VISUAL CREATION**`
3. Commit the file into `ready_for_visual/YYYY-MM/`.
4. GitHub Actions detects the approval and fires the pipeline.
5. About a minute later, three new files appear next to the source markdown:
   - `*-report.md` — preview report with image links and prompts
   - `*-article-canva.csv` — paper-slide rows for Canva Bulk Create
   - `*-bridge-canva.csv` — bridge-slide rows (paper showcase + CTA close)
6. Open the report in GitHub; check the images. If any look off, regenerate
   in Higgsfield, copy the new URL, and replace it in the article CSV row.
7. Download the approved Higgsfield images to your machine.
8. Open the right pillar template in Canva, run Bulk Create with the article
   CSV (text fields), then manually drag each downloaded image into its
   slide.
9. Manually fill slide 1 (cover), the last-but-one slide (CAMO paper
   showcase, using the bridge CSV), and the final slide (CTA close).
10. Export and publish.

## Folder structure

```
ready_for_visual/
├── 2026-05/
│   ├── 2026-05-21-bad-job-economy-2026.md                  ← source
│   ├── 2026-05-21-bad-job-economy-2026-report.md           ← pipeline output
│   ├── 2026-05-21-bad-job-economy-2026-article-canva.csv   ← pipeline output
│   └── 2026-05-21-bad-job-economy-2026-bridge-canva.csv    ← pipeline output
└── 2026-06/
    └── ...
```

Year-month subfolder is derived from the markdown's filename prefix
(`2026-05-21-...` → `2026-05/`). All outputs sit alongside the source.

## Setup (one time)

### 1. Higgsfield credentials

Get credentials from cloud.higgsfield.ai (Settings → API keys).
Format: `KEY_ID:KEY_SECRET`.

GitHub → Settings → Secrets and variables → Actions → New repository secret.
Name: `HF_CREDENTIALS`. Value: the colon-joined string.

### 2. Workflow permissions

Settings → Actions → General → Workflow permissions → "Read and write
permissions." The pipeline needs this to commit results back.

### 3. Local test (optional)

```bash
export HF_CREDENTIALS="KEY:SECRET"
pip install -r requirements.txt

# Dry run — synthesises prompts, writes a report stub, no real generations
python scripts/generate.py --dry-run

# Real run — single file
python scripts/generate.py --file ready_for_visual/2026-05/2026-05-21-bad-job-economy-2026.md
```

## Markdown schema

The source markdown must contain:

- A `Cluster id:` line (used as the carousel ID)
- A `**Primary pillar:**` line — one of: `AI Adoption`, `AI & Incentives`,
  `AI & Jobs`, `AI Algorithms & Data`
- A `## Anchor CAMO research` section with the paper's bold title and an
  italic line of `Authors · Year · Type`
- Per article: `**Source:**`, `**Key takeaway:**`, `**Suggested visual:**`
  (focal-subject phrasing)
- The approval checkbox: `- [x] **APPROVE FOR VISUAL CREATION**` (ticked)

The pipeline skips any .md whose checkbox isn't ticked, so drafts are safe
to commit.

### Visual concepts: focal-subject phrasing

The pointillist style needs a single focal subject to cluster dots around.
Diffuse-scene phrasing produces weaker output.

Good:
- "A single articulated robotic factory arm..."
- "A magnifying glass held mid-air over a document..."

Avoid:
- "An aerial view of an open-plan office..."
- "A network of connected nodes spreading across..."

The scraper's enrichment prompt should produce focal-subject phrasing in
the `Suggested visual:` field upstream.

## Style envelope

Pillar palettes and the prompt template live in `scripts/style_envelopes.py`.
Edit there to tune the system. Every future generation inherits.

## Costs

Seedream 4.5 at 1K resolution: 1 credit per image. A typical 3-5 paper
carousel = 3-5 credits. Pennies.

## Troubleshooting

**Run did nothing.** Approval checkbox isn't ticked, or the .md isn't under
`ready_for_visual/`. The workflow logs which files it found.

**`HF_CREDENTIALS not set`.** Repository secret not configured. See Setup
step 1.

**`NotEnoughCredits`.** Top up at cloud.higgsfield.ai.

**Image looks wrong for one paper.** Open Higgsfield's media library,
regenerate that paper's image with a tweaked prompt, copy the new URL,
replace the URL in the corresponding row of the `*-article-canva.csv`.

## Files

```
scripts/
├── read_approval.py          ← parses the markdown, detects approval ticked
├── style_envelopes.py        ← pillar palettes + prompt template
├── higgsfield_client_wrapper.py  ← Higgsfield SDK wrapper with retries
└── generate.py               ← orchestrator (the script Actions runs)
.github/workflows/
└── generate-carousel.yml     ← Actions config
requirements.txt
README.md
ready_for_visual/             ← gatekeeper output + pipeline output
```
