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
        # trailing slash on src: copy contents, not the dir itself
        cmd = [rsync, "-a", "--delete", f"{src}/", f"{dst}/"]
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
