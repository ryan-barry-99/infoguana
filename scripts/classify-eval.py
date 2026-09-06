#!/usr/bin/env python3
"""Compare classifier backends against the labels already in the corpus.

Labelled notes are a ready-made evaluation set: replay their content
through a candidate backend and measure how often it agrees with what is
stored. This measures **agreement with the corpus as it stands today** —
not accuracy, and not agreement with any particular backend.

That distinction is load-bearing and the framing here used to overstate
it. The obvious reading is "the stored labels are Haiku's, so this scores
a candidate against Haiku" — but nothing filters on which backend wrote a
label, because the `notes` table has no column recording one. Once the
default backend changes, later notes carry the *new* backend's labels,
and a run increasingly scores a backend against its own past output.
Treat a high score as "this backend is consistent with what is already
here", which is the right question for a migration and the wrong one for
"which backend is better".

    # local server
    scripts/classify-eval.py --http http://127.0.0.1:1234/v1 --model gemma-2-9b-it

    # hosted; export INFOGUANA_CLASSIFY_API_KEY first
    scripts/classify-eval.py --http https://api.openai.com/v1 --model gpt-4o-mini

    # the current default (Claude CLI), to sanity-check the harness itself
    scripts/classify-eval.py --claude --model claude-haiku-4-5

Sampling is newest-first (`db.list_notes` orders by created_at DESC), so
two runs compare the same notes only if nothing was added in between. In
a corpus agents write to continuously, runs weeks apart grade different
sets — compare percentages within a session, not across them. `--seed-id`
pins an upper bound on note id to make a run reproducible.

Type agreement is the headline number; tag recall is reported because a
backend can pick a different-but-defensible type while still tagging
usefully, and the two failure modes have different consequences.
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Types that are lifecycle-bearing rather than descriptive. A backend that
# confuses these changes behaviour (pinning, plan tracking), not just
# presentation, so they are called out separately in the summary.
STRUCTURAL = {"plan", "task", "rule", "skill"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--http", metavar="BASE_URL",
                     help="OpenAI-compatible /v1 endpoint")
    src.add_argument("--claude", action="store_true",
                     help="use the Claude CLI backend")
    ap.add_argument("--model", required=True)
    # No key flag on purpose: a secret in argv is world-readable through
    # /proc/<pid>/cmdline for the life of the run, and lands in shell
    # history besides. Export INFOGUANA_CLASSIFY_API_KEY instead.
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--project", default=None, help="restrict to one project")
    ap.add_argument("--seed-id", type=int, default=None, metavar="MAX_ID",
                    help="only consider notes with id <= MAX_ID, so a run is "
                         "reproducible as the corpus grows. Print the highest "
                         "id from a baseline run and reuse it for comparisons.")
    ap.add_argument("--verbose", action="store_true",
                    help="print every disagreement, not just the summary")
    args = ap.parse_args()

    os.environ["INFOGUANA_CLASSIFY_MODEL"] = args.model
    if args.http:
        os.environ["INFOGUANA_CLASSIFY_BASE_URL"] = args.http
    else:
        os.environ.pop("INFOGUANA_CLASSIFY_BASE_URL", None)

    # Settings are read at import; reload so the env above takes effect even
    # when something already imported app.config.
    import app.config  # noqa: F401
    importlib.reload(app.config)
    from app import classify, db
    importlib.reload(classify)

    db.init_db()
    # Over-fetch, then filter: list_notes has no "labelled only" predicate,
    # and the id bound is applied here for the same reason.
    candidates = db.list_notes(project=args.project, limit=args.limit * 10)
    notes = [
        n for n in candidates
        if n.type and n.type != "unsorted" and (n.content or "").strip()
        and (args.seed_id is None or n.id <= args.seed_id)
    ][:args.limit]

    if not notes:
        print("no labelled notes found to evaluate against", file=sys.stderr)
        return 1

    backend = args.http or "claude-cli"
    print(f"backend : {backend}")
    print(f"model   : {args.model}")
    print(f"notes   : {len(notes)}")
    # Print the bound so a later run can reproduce this sample exactly.
    print(f"id range: {min(n.id for n in notes)}..{max(n.id for n in notes)}"
          f"  (re-run with --seed-id {max(n.id for n in notes)} to compare "
          f"against this same set)\n")

    agree = failures = 0
    # Tag recall is averaged over notes that HAVE reference tags. Dividing
    # by every scored note counted an untagged note as a 0% miss, but
    # there is nothing there to agree with — on a 20-note sample with 6
    # untagged that put a hard 70% ceiling on the printed figure with
    # nothing in the output saying so, and the ceiling moved between runs
    # because the sample does.
    tag_recall_total = 0.0
    tag_scored = untagged = 0
    # Recall alone rewards a backend that emits many tags, which is the
    # failure mode it is least able to see, so carry the emitted count too.
    got_tag_counts: list[int] = []
    structural_confusions: list[str] = []
    started = time.time()

    for i, n in enumerate(notes, 1):
        t0 = time.time()
        got = classify.classify(n.content)
        elapsed = time.time() - t0
        if got is None:
            failures += 1
            print(f"[{i:>3}] #{n.id} FAILED to classify ({elapsed:.1f}s)")
            continue

        match = got.type == n.type
        agree += match
        want_tags, got_tags = set(n.tags or []), set(got.tags or [])
        got_tag_counts.append(len(got_tags))
        if want_tags:
            overlap = len(want_tags & got_tags) / len(want_tags)
            tag_recall_total += overlap
            tag_scored += 1
        else:
            overlap = None
            untagged += 1

        if not match and (n.type in STRUCTURAL or got.type in STRUCTURAL):
            structural_confusions.append(f"#{n.id} {n.type} -> {got.type}")
        if args.verbose or not match:
            flag = "ok " if match else "MISS"
            recall = "n/a " if overlap is None else f"{overlap:.0%}"
            print(f"[{i:>3}] {flag} #{n.id} want={n.type:<9} got={got.type:<9} "
                  f"tag recall={recall:<5} ({len(got_tags)} emitted) "
                  f"({elapsed:.1f}s)")

    n = len(notes)
    scored = n - failures
    print(f"\n{'-' * 52}")
    print(f"type agreement : {agree}/{scored} ({agree / scored:.0%})" if scored
          else "type agreement : n/a (every call failed)")
    if tag_scored:
        mean_emitted = sum(got_tag_counts) / len(got_tag_counts)
        print(f"tag recall     : {tag_recall_total / tag_scored:.0%} "
              f"(mean over {tag_scored} note(s) that have stored tags; "
              f"{untagged} untagged excluded)")
        print(f"tags emitted   : {mean_emitted:.1f}/note (mean) — recall alone "
              f"rewards emitting more")
    elif scored:
        print("tag recall     : n/a (no sampled note has stored tags)")
    print(f"failed calls   : {failures}")
    print(f"wall time      : {time.time() - started:.0f}s "
          f"({(time.time() - started) / max(n, 1):.1f}s/note)")
    if structural_confusions:
        # Worth separating: mislabelling a plan as a memory loses it from
        # plan tracking entirely, which is not the same class of error as
        # picking idea over memory.
        print(f"\nstructural type confusions ({len(structural_confusions)}):")
        for c in structural_confusions:
            print(f"  {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
