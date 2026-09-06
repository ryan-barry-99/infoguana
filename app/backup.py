"""Daily SQLite online backup with retention.

Uses SQLite's backup API (safe to run while the DB is being written to) rather
than raw file copies, so we get consistent snapshots without stopping the app.
"""
import asyncio
import logging
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings


log = logging.getLogger(__name__)


def _snapshot(src_path: Path, dest_path: Path) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(src_path))
    try:
        dest = sqlite3.connect(str(dest_path))
        try:
            with dest:
                src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()


def _prune(backup_dir: Path, retain: int) -> None:
    files = sorted(backup_dir.glob("infoguana-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[retain:]:
        try:
            old.unlink()
            log.info("pruned old backup %s", old.name)
        except OSError:
            log.exception("failed to prune %s", old)


def _sync_to_nas(target: Path) -> None:
    """Mirror backups/ and attachments/ to the NAS. Idempotent via rsync."""
    rsync = shutil.which("rsync")
    if not rsync:
        log.warning("rsync not found; skipping NAS sync")
        return
    target.mkdir(parents=True, exist_ok=True)
    pairs = [
        (settings.backup_dir, target / "backups"),
        (settings.attachments_dir, target / "attachments"),
    ]
    for src, dst in pairs:
        if not src.exists():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        # trailing slash on src: copy contents, not the dir itself.
        #
        # Deliberately NOT `-a`. That implies `-p`/`-o`/`-g`, and the usual
        # destination here is a network share (CIFS/SMB) that cannot honor
        # POSIX permissions or ownership. rsync writes to a temp file,
        # fails to chmod it, and then cannot rename it into place — the
        # whole run aborts with rc=23 partway through, so the mirror is
        # left incomplete and every later run fails the same way. Observed
        # against a CIFS mount: "failed to set permissions ... No such file
        # or directory (2)" followed by a rename failure on the same path.
        #
        # `-rlt` keeps what actually matters for a backup mirror: recurse,
        # preserve symlinks and mtimes.
        #
        # Deliberately NOT `--inplace`, even though it would also avoid the
        # failing rename. Without it rsync writes a temp file and renames,
        # so a destination snapshot is either the complete old file or the
        # complete new one. Writing in place means an interrupted run — the
        # share dropping, the container restarting, exactly the flakiness
        # this function has to survive — leaves a half-old, half-new file
        # under a valid snapshot name, and `--delete` may already have
        # pruned the last good copy. A backup that looks restorable and is
        # not is worse than a backup that is visibly missing.
        cmd = [rsync, "-rlt", "--no-perms", "--no-owner",
               "--no-group", "--delete", f"{src}/", f"{dst}/"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.warning("rsync %s -> %s failed (rc=%d): %s",
                        src, dst, result.returncode, result.stderr.strip())


def run_once() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = settings.backup_dir / f"infoguana-{ts}.db"
    _snapshot(settings.db_path, dest)
    log.info("backup snapshot -> %s (%.1f KiB)", dest, dest.stat().st_size / 1024)
    _prune(settings.backup_dir, settings.backup_retain)
    if settings.nas_sync_path:
        try:
            _sync_to_nas(settings.nas_sync_path)
        except Exception:
            log.exception("nas sync failed")
    return dest


async def scheduler() -> None:
    """Run the backup every `backup_interval_hours`. Survives individual failures."""
    interval = max(settings.backup_interval_hours * 3600, 60.0)
    # Small jitter/delay so we don't snapshot immediately on restart loops.
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.to_thread(run_once)
        except Exception:
            log.exception("backup failed")
        await asyncio.sleep(interval)
