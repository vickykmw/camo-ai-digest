#!/usr/bin/env python3
"""
repair_approval_state.py -- One-time cleanup utility.

The original v2 parser ignored the per-match checkbox UI for multi-match
articles and always anchored on matched_camo[0]. This script re-reads every
digest .md, uses the v2.1 parser (which respects the editor's pick), and
repairs the affected entries in approval_state.json.

Safe to run multiple times -- it's idempotent. Items in state="clustered"
are NEVER modified (their cluster is already on disk; rewiring them would
desync). Items in state="queued" with an obvious mismatch are corrected.

Usage:
    python repair_approval_state.py            # dry-run, shows what would change
    python repair_approval_state.py --apply    # actually apply the changes
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Reuse the v2.1 parser, primary-match selector, and constants from the
# main script -- avoids divergent logic.
import process_approvals as pa


def main(apply: bool) -> int:
    state_path = pa.APPROVAL_STATE_FILE
    if not state_path.exists():
        print(f"[info] {state_path.name} doesn't exist; nothing to repair.")
        return 0

    state = json.loads(state_path.read_text())
    print(f"[info] loaded {state_path.name}: {len(state)} entries")
    if not state:
        return 0

    # Build a link -> chosen_camo_id map by re-parsing every digest .md.
    if not pa.DIGESTS_DIR.exists():
        print(f"[error] {pa.DIGESTS_DIR} doesn't exist -- can't re-read digests")
        return 1

    md_files = sorted(pa.DIGESTS_DIR.rglob("*.md"))
    print(f"[info] re-parsing {len(md_files)} digest .md file(s) with v2.1 parser")
    link_to_chosen = {}
    link_to_md = {}
    for md in md_files:
        for link, chosen_camo_id in pa.parse_approved_links_from_md(md):
            # If a link appears in multiple digests (re-appear), keep the
            # earliest record -- that matches build_state_record's "first
            # tick wins" semantics for state insertion.
            if link not in link_to_chosen:
                link_to_chosen[link] = chosen_camo_id
                link_to_md[link] = md
    print(f"[info] found {len(link_to_chosen)} ticked links across all digests")

    repairs = []
    untouched_clustered = 0
    untouched_clean = 0
    not_in_md = 0

    for link, rec in state.items():
        cur_state = rec.get("state")
        if cur_state == "clustered":
            untouched_clustered += 1
            continue

        chosen = link_to_chosen.get(link)
        if chosen is None and link not in link_to_chosen:
            # State has this link but no current .md tick points to it.
            # Could be: an old tick removed by the editor, or the .md was
            # deleted/renamed. Leave as-is and warn at end.
            not_in_md += 1
            continue

        # Recompute what the primary_camo_* fields *should* be using the
        # matched_camo list already stored on the state record.
        synth_item = {"matched_camo": rec.get("matched_camo") or []}
        correct = pa._select_camo_match(synth_item, chosen)
        cur_id = rec.get("primary_camo_id")
        correct_id = correct.get("id")

        if cur_id == correct_id:
            untouched_clean += 1
            continue

        repairs.append({
            "link": link,
            "title": rec.get("title", "(untitled)"),
            "current_id": cur_id,
            "correct_id": correct_id,
            "correct_record": correct,
        })

    # --- Report ---
    print()
    print("REPAIR SUMMARY")
    print("--------------")
    print(f"  queued + already correct: {untouched_clean}")
    print(f"  queued + need repair:     {len(repairs)}")
    print(f"  clustered (skipped):      {untouched_clustered}")
    print(f"  in state but no .md tick: {not_in_md}")
    print()

    if not repairs:
        print("Nothing to repair. State is consistent with the v2.1 parser.")
        return 0

    print("ITEMS TO REPAIR")
    print("---------------")
    for r in repairs:
        print(f"  - {r['title'][:70]}")
        print(f"      currently anchored to: {r['current_id']}")
        print(f"      should be anchored to: {r['correct_id']}")
        print()

    if not apply:
        print("[dry-run] No changes written. Re-run with --apply to commit them.")
        return 0

    # --- Apply ---
    for r in repairs:
        rec = state[r["link"]]
        c = r["correct_record"]
        rec["primary_camo_id"] = c.get("id")
        rec["primary_camo_title"] = c.get("title")
        rec["primary_camo_url"] = c.get("url")
        rec["primary_camo_reason"] = c.get("reason")

    state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True)
    )
    print(f"[ok] wrote {state_path.name} with {len(repairs)} repair(s)")

    # Regenerate QUEUE_STATUS.md so the dashboard reflects the corrected state.
    from datetime import datetime, timezone
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pa.QUEUE_STATUS_FILE.write_text(
        pa.render_queue_status(state, now_str), encoding="utf-8"
    )
    print(f"[ok] regenerated {pa.QUEUE_STATUS_FILE.name}")

    return 0


if __name__ == "__main__":
    apply_flag = "--apply" in sys.argv
    sys.exit(main(apply=apply_flag))
