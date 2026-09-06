#!/usr/bin/env python3
"""Overwrite the stored memory-protocol row with the current default.

The protocol is a database row, seeded once by `db.seed_protocol_if_missing`
at first boot and thereafter owned by the user — nothing in the app writes
it again. That is deliberate: it is hand-edited, and an upgrade silently
rewriting it would discard the user's wording.

The cost is that improvements to `onboard.DEFAULT_PROTOCOL` never reach an
install that has already booted. The agent-agnostic rewrite is the case
that made this matter: the opening line changed from naming Claude Code to
naming either agent, so on any existing deployment a Codex session still
reads a preamble addressed to Claude Code. There was no supported way to
correct that short of hand-editing SQLite.

This script is that way. It prints a diff and asks before writing, because
the row it replaces may be hand-maintained.

Usage:
    python scripts/reseed-protocol.py            # show the diff, then confirm
    python scripts/reseed-protocol.py --diff     # show the diff, never write
    python scripts/reseed-protocol.py --print    # show the default, write nothing
    python scripts/reseed-protocol.py --yes      # no prompt (for automation)

`--yes` writes to the live database with no confirmation. Use `--diff` to
inspect what would change; that mode cannot write under any circumstances.
"""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

from app import db, onboard  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="print the current default and exit")
    parser.add_argument("--diff", action="store_true",
                        help="show what would change and exit without writing")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt and write")
    parser.add_argument("--key", default="default",
                        help="protocol key to rewrite (default: 'default')")
    args = parser.parse_args()

    if args.print_only:
        print(onboard.DEFAULT_PROTOCOL, end="")
        return 0

    db.init_db()
    stored = db.get_protocol(args.key)

    if stored is None:
        if args.diff:
            print(f"no protocol row for {args.key!r}; it would be seeded")
            return 0
        db.set_protocol(onboard.DEFAULT_PROTOCOL, args.key)
        print(f"no protocol row for {args.key!r} — seeded it")
        return 0

    if stored.strip() == onboard.DEFAULT_PROTOCOL.strip():
        print(f"protocol {args.key!r} already matches the current default")
        return 0

    diff = list(difflib.unified_diff(
        stored.splitlines(), onboard.DEFAULT_PROTOCOL.splitlines(),
        fromfile=f"stored ({args.key})", tofile="DEFAULT_PROTOCOL", lineterm=""))
    print("\n".join(diff))
    print()
    print(f"{sum(1 for d in diff if d.startswith('-') and not d.startswith('---'))} "
          f"line(s) removed, "
          f"{sum(1 for d in diff if d.startswith('+') and not d.startswith('+++'))} "
          f"added.")

    if args.diff:
        print()
        print("--diff: nothing written. Re-run without it to apply.")
        return 0

    if not args.yes:
        print()
        print("This replaces a row you may have hand-edited. There is no undo "
              "beyond your backups.")
        try:
            if input("overwrite it? [y/N] ").strip().lower() not in ("y", "yes"):
                print("left unchanged")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\nleft unchanged")
            return 1

    db.set_protocol(onboard.DEFAULT_PROTOCOL, args.key)
    print(f"protocol {args.key!r} rewritten from DEFAULT_PROTOCOL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
