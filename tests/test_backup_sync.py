"""Tests for the NAS mirror's rsync invocation.

The destination is normally a network share that cannot honor POSIX
permissions or ownership. `rsync -a` implies `-p`/`-o`/`-g`, so it writes a
temp file, fails to chmod it, then cannot rename it into place — the run
aborts with rc=23 partway through and the mirror is left incomplete.

This asserts the flag set rather than reproducing the failure: the abort is
environmental and intermittent (it depends on the share and on load), so
there is no honest way to make a unit test fail against the old flags. What
is pinned here is the decision — that permission-preserving flags stay
out, and that `--inplace` stays out so each snapshot lands atomically.
"""
from __future__ import annotations

from pathlib import Path

from app import backup


def _captured_cmds(monkeypatch, tmp_path):
    src = tmp_path / "backups"; src.mkdir()
    (src / "snap.db").write_bytes(b"x")
    monkeypatch.setattr(backup.settings, "backup_dir", src)
    monkeypatch.setattr(backup.settings, "attachments_dir", tmp_path / "nope")
    monkeypatch.setattr(backup.shutil, "which", lambda _: "/usr/bin/rsync")
    cmds = []

    class _R:
        returncode = 0
        stderr = ""
    monkeypatch.setattr(backup.subprocess, "run",
                        lambda cmd, **kw: (cmds.append(cmd), _R())[1])
    backup._sync_to_nas(tmp_path / "target")
    return cmds


def test_archive_flag_is_not_used(monkeypatch, tmp_path):
    """`-a` is the whole bug: it implies the permission and ownership
    preservation the share cannot do."""
    for cmd in _captured_cmds(monkeypatch, tmp_path):
        assert "-a" not in cmd, cmd


def test_permission_and_ownership_preservation_are_disabled(monkeypatch, tmp_path):
    for cmd in _captured_cmds(monkeypatch, tmp_path):
        for flag in ("--no-perms", "--no-owner", "--no-group"):
            assert flag in cmd, (flag, cmd)


def test_inplace_is_not_used(monkeypatch, tmp_path):
    """Atomicity matters more than avoiding the rename. Writing in place
    would let an interrupted run leave a half-written file under a valid
    snapshot name, with `--delete` having possibly pruned the last good
    copy — a backup that looks restorable and is not. Disabling permission
    preservation is what actually fixes the abort; verified against a real
    CIFS share that `-rlt --no-perms` completes without it."""
    for cmd in _captured_cmds(monkeypatch, tmp_path):
        assert "--inplace" not in cmd, cmd


def test_recursion_links_and_times_are_still_preserved(monkeypatch, tmp_path):
    """Dropping `-a` must not quietly drop the behavior a mirror needs."""
    for cmd in _captured_cmds(monkeypatch, tmp_path):
        assert "-rlt" in cmd, cmd


def test_delete_is_retained_so_the_mirror_tracks_pruning(monkeypatch, tmp_path):
    """Local retention pruning has to propagate, or the share grows forever."""
    for cmd in _captured_cmds(monkeypatch, tmp_path):
        assert "--delete" in cmd, cmd


def test_source_is_passed_with_a_trailing_slash(monkeypatch, tmp_path):
    """Without it rsync nests the directory inside itself on every run."""
    for cmd in _captured_cmds(monkeypatch, tmp_path):
        assert cmd[-2].endswith("/"), cmd


def test_a_missing_source_directory_is_skipped_not_mirrored(monkeypatch, tmp_path):
    """attachments_dir does not exist in this fixture; mirroring it with
    --delete would empty the destination copy."""
    cmds = _captured_cmds(monkeypatch, tmp_path)
    assert len(cmds) == 1, cmds
    assert "backups" in cmds[0][-2]
