#!/usr/bin/env python3
"""One-shot backfill: populate the new `preview` column for every note that
doesn't have one yet (plan #322).

Walks notes ordered by id ascending; for each null-preview row, runs
pipeline.process_note which will:
  - call classify (Haiku) for a fresh preview, since note.preview is None;
  - apply the preview but leave already-classified type/tags/project alone;
  - re-derive a fallback preview if Haiku is unavailable.

Idempotent — safe to re-run; already-populated notes are skipped on the SQL
side, so a re-run only touches what's still missing.

Usage (from repo root, with the venv active):
    python -m scripts.backfill_previews
    python -m scripts.backfill_previews --limit 50      # smoke test
    python -m scripts.backfill_previews --dry-run       # list, don't process
"""
import argparse
import logging
import sys
import time
from pathlib import Path

# Allow `python scripts/backfill_previews.py` from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, pipeline  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap notes processed (0 = no cap).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print ids that would be processed and exit.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("backfill_previews")

    db.init_db()
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id FROM notes WHERE preview IS NULL OR preview = '' "
        "ORDER BY id ASC"
    ).fetchall()
    ids = [r["id"] for r in rows]
    if args.limit > 0:
        ids = ids[: args.limit]
    log.info("found %d notes needing preview backfill", len(ids))

    if args.dry_run:
        for nid in ids:
            print(nid)
        return 0

    started = time.time()
    ok = fail = 0
    for i, nid in enumerate(ids, 1):
        try:
            pipeline.process_note(nid)
            ok += 1
        except Exception:
            log.exception("process_note failed for id=%d", nid)
            fail += 1
        if i % 25 == 0 or i == len(ids):
            log.info("progress: %d/%d (ok=%d fail=%d, %.1fs elapsed)",
                     i, len(ids), ok, fail, time.time() - started)

    log.info("done: ok=%d fail=%d total=%d", ok, fail, len(ids))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
