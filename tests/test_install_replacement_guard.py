"""Guards against an installer silently repointing an existing install.

Two failures, one cause: ownership of a registered hook was decided by the
absolute path of the checkout running the installer. Run it from a second
checkout — a worktree, a release tarball, a throwaway copy — and the
existing entries matched nothing, so they were preserved as if they were
some other tool's hooks and a full second set was appended beside them.
The config then held both, every session start ran both, and no output
said so.

The confirmation half matters because the accumulating case and the
replacing case are the same command: whichever the installer does, the
user gets a different infoguana than the one they had, and neither is a
detail an installer should settle on its own.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
OURS = REPO / "scripts"
OTHER = Path("/tmp/some-other-checkout/scripts")


@pytest.fixture(scope="module")
def setup():
    spec = importlib.util.spec_from_file_location(
        "_infoguana_setup", REPO / "scripts" / "_infoguana_setup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cmd(d: Path, *args: str) -> str:
    return f"/usr/bin/python3 {d / 'infoguana-onboard-chunk.py'} {' '.join(args)}"


# --- ownership is by script name, not by checkout path -------------------

def test_a_hook_from_another_checkout_is_recognised_as_ours(setup):
    """This is the accumulation bug: not recognising it meant preserving
    it as foreign and appending a second full registration beside it."""
    assert setup.is_infoguana_hook(_cmd(OTHER, "0", "10"))
    assert setup.is_infoguana_hook(_cmd(OURS, "0", "10"))


@pytest.mark.parametrize("name", ["infoguana-onboard-chunk.sh",
                                  "infoguana-first-turn.sh"])
def test_legacy_hook_names_are_still_recognised(setup, name):
    """Upgrades from before the chunked hook must still be cleaned up."""
    assert setup.is_infoguana_hook(f"/bin/bash /old/checkout/scripts/{name}")


def test_an_unrelated_hook_is_never_claimed(setup):
    """The guard must not be so broad that it eats a user's own hooks —
    that failure is worse than the one it fixes, and silent too."""
    assert not setup.is_infoguana_hook("/usr/bin/python3 ~/bin/my-own-hook.py")
    assert not setup.is_infoguana_hook("echo hello")


# --- relocation detection ------------------------------------------------

def test_only_other_checkouts_are_reported(setup):
    cmds = [_cmd(OURS, "0", "3"), _cmd(OTHER, "0", "3"),
            "/usr/bin/python3 ~/bin/unrelated.py"]
    assert setup.other_install_dirs(cmds, OURS) == {str(OTHER)}


def test_an_install_in_place_reports_nothing(setup):
    """The ordinary upgrade must not start prompting — a guard that fires
    on the common path gets --force'd reflexively and stops guarding."""
    assert setup.other_install_dirs([_cmd(OURS, "0", "3")], OURS) == set()


def test_a_quoted_path_is_still_matched(setup):
    """Commands are shell-quoted, so a checkout under a path with a space
    arrives wrapped in quotes."""
    cmds = [f"'/usr/bin/python3' '/tmp/a b/scripts/infoguana-onboard-chunk.py' 0 3"]
    assert setup.other_install_dirs(cmds, OURS) == {"/tmp/a b/scripts"}


# --- the confirmation ----------------------------------------------------

def test_nothing_to_replace_never_prompts(setup):
    def explode(_):
        raise AssertionError("prompted with no other install registered")

    assert setup.confirm_replacement(Path("/x"), set(), OURS, force=False,
                                     prompt=explode, out=lambda _: None)


def test_a_non_interactive_run_refuses(setup, monkeypatch):
    """The case that motivated this: installers get run from scripts, CI
    and agent sessions, where a y/N prompt nobody sees reads as consent."""
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: False)
    said = []
    ok = setup.confirm_replacement(Path("/x"), {str(OTHER)}, OURS, force=False,
                                   prompt=lambda _: "y", out=said.append)
    assert ok is False
    assert any("refusing" in s for s in said)
    assert any(str(OTHER) in s for s in said)


def test_force_replaces_without_asking(setup, monkeypatch):
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: False)

    def explode(_):
        raise AssertionError("prompted despite --force")

    assert setup.confirm_replacement(Path("/x"), {str(OTHER)}, OURS, force=True,
                                     prompt=explode, out=lambda _: None)


@pytest.mark.parametrize("answer,expected", [("y", True), ("yes", True),
                                             ("n", False), ("", False),
                                             ("Y", True)])
def test_an_interactive_answer_decides(setup, monkeypatch, answer, expected):
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: True)
    assert setup.confirm_replacement(Path("/x"), {str(OTHER)}, OURS,
                                     force=False, prompt=lambda _: answer,
                                     out=lambda _: None) is expected


# --- end to end through the Claude Code installer's strip ----------------

@pytest.fixture(scope="module")
def hooks_installer():
    spec = importlib.util.spec_from_file_location(
        "install_infoguana_hooks", REPO / "scripts" / "install-infoguana-hooks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stripping_removes_entries_from_every_checkout(hooks_installer):
    """The accumulation bug, at the layer that caused it: a settings.json
    holding another checkout's registration must come back empty of
    infoguana hooks, not carrying them forward beside the new ones."""
    entries = [
        {"hooks": [{"type": "command", "command": _cmd(OTHER, str(i), "3")}]}
        for i in range(3)
    ] + [
        {"hooks": [{"type": "command", "command": _cmd(OURS, "0", "3")}]},
        {"hooks": [{"type": "command",
                    "command": "/usr/bin/python3 ~/bin/mine.py"}]},
    ]
    kept = hooks_installer._strip_existing(entries)
    remaining = [h["command"] for e in kept for h in e["hooks"]]
    assert remaining == ["/usr/bin/python3 ~/bin/mine.py"]


def test_a_users_own_hook_sharing_an_entry_survives(hooks_installer):
    """Entry wrappers can hold several hooks. Dropping ours must not take
    a neighbour with it."""
    entries = [{"hooks": [
        {"type": "command", "command": _cmd(OTHER, "0", "3")},
        {"type": "command", "command": "/usr/bin/python3 ~/bin/mine.py"},
    ]}]
    kept = hooks_installer._strip_existing(entries)
    assert [h["command"] for e in kept for h in e["hooks"]] == [
        "/usr/bin/python3 ~/bin/mine.py"]


def test_registered_commands_walks_every_event(hooks_installer):
    hooks = {
        "SessionStart": [{"hooks": [{"command": _cmd(OTHER, "0", "3")}]}],
        "UserPromptSubmit": [{"hooks": [{"command": "echo hi"}]}],
    }
    assert set(hooks_installer._registered_commands(hooks)) == {
        _cmd(OTHER, "0", "3"), "echo hi"}
