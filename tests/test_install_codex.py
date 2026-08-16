"""Tests for the Codex installer's config.toml handling.

Two things make this worth testing rather than eyeballing. The file is
the user's own `~/.codex/config.toml`, so a merge bug destroys settings
this project never created and cannot regenerate. And the block is
generated text parsed by a real TOML parser, so "it looks right" is not
the standard — `tomllib.loads` is.
"""
from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load():
    """Import the installer by path — it is a script, not a module, and
    its filename is not a valid identifier."""
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "install_infoguana_codex", REPO / "scripts" / "install-infoguana-codex.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ic():
    return _load()


def _parse(block: str, mod) -> dict:
    """Parse a managed block, markers stripped (they are comments, but
    tomllib is happy either way — this keeps failures readable)."""
    return tomllib.loads(block.replace(mod.BEGIN, "").replace(mod.END, ""))


# --------------------------------------------------------------------
# render_block — the generated text must be valid TOML
# --------------------------------------------------------------------

@pytest.mark.parametrize("executable,hook", [
    ("/usr/bin/python3", "/opt/infoguana/scripts/infoguana-onboard-chunk.py"),
    # A space closed the TOML string early: shell quoting emits bare `"`.
    ("/home/my user/venv/bin/python", "/home/my user/code/hook.py"),
    # `\U` in a Windows path reads as a unicode escape.
    (r"C:\Python312\python.exe", r"C:\Users\someone\infoguana\hook.py"),
    # An apostrophe is legal in a POSIX path and legal in TOML — but only
    # if whatever emits the string escapes for TOML rather than for sh.
    ("/home/o'brien/bin/python", "/home/o'brien/hook.py"),
])
def test_generated_block_is_valid_toml(ic, monkeypatch, executable, hook):
    monkeypatch.setattr(ic.sys, "executable", executable)
    monkeypatch.setattr(ic, "HOOK", Path(hook))
    parsed = _parse(ic.render_block("http://localhost:8789", 2), ic)
    # 2 slices + 1 override entry.
    assert len(parsed["hooks"]["SessionStart"]) == 3


def test_generated_block_registers_the_requested_number_of_slices(ic):
    parsed = _parse(ic.render_block("http://localhost:8789", 5), ic)
    entries = parsed["hooks"]["SessionStart"]
    assert len(entries) == 6
    commands = [e["hooks"][0]["command"] for e in entries]
    assert sum("--override" in c for c in commands) == 1
    for i in range(5):
        assert any(c.endswith(f" {i} 5") for c in commands), f"slice {i} missing"


def test_mcp_server_url_and_token_var_survive_the_round_trip(ic):
    parsed = _parse(ic.render_block("http://example.invalid:9000", 1), ic)
    assert parsed["mcp_servers"]["infoguana"]["url"] == "http://example.invalid:9000/mcp/"
    assert parsed["mcp_servers"]["infoguana"]["bearer_token_env_var"] == ic.TOKEN_ENV_VAR


# --------------------------------------------------------------------
# _merge — what must survive a regeneration
# --------------------------------------------------------------------

def test_a_user_hook_is_not_deleted_by_regeneration(ic):
    """Regression: `[[hooks.SessionStart.hooks]]` was claimed by header,
    so a user's own hook had both keys filtered as ours, `kept` came back
    empty, and the entry was never re-emitted — it vanished after one
    re-run."""
    old = ic.render_block("http://localhost:8789", 1).replace(
        ic.END,
        '[[hooks.SessionStart]]\n'
        '[[hooks.SessionStart.hooks]]\n'
        'type = "command"\n'
        'command = "/usr/local/bin/user-hook.sh"\n'
        + ic.END,
    )
    merged = ic._merge(old, ic.render_block("http://localhost:8789", 1))
    assert "/usr/local/bin/user-hook.sh" in merged
    parsed = _parse(merged, ic)
    commands = [h["command"] for e in parsed["hooks"]["SessionStart"]
                for h in e["hooks"]]
    assert "/usr/local/bin/user-hook.sh" in commands
    # And ours are still there: 1 slice + 1 override.
    assert sum("infoguana-onboard-chunk.py" in c for c in commands) == 2


def test_per_hook_keys_stay_with_their_own_hook(ic):
    """Regression: `extra_keys[header]` overwrote on each of the N+1
    identical headers, so N-1 trust hashes were lost and the survivor was
    re-attached to the first hook regardless of which it came from."""
    block = ic.render_block("http://localhost:8789", 3)
    # Give each hook table a distinct Codex-written key.
    lines, n = [], 0
    for line in block.splitlines():
        lines.append(line)
        if line.strip() == "[[hooks.SessionStart.hooks]]":
            lines.append(f'trust_hash = "h{n}"')
            n += 1
    old = "\n".join(lines) + "\n"
    assert n == 4, "3 slices + override"

    merged = ic._merge(old, ic.render_block("http://localhost:8789", 3))
    parsed = _parse(merged, ic)

    by_command = {h["command"]: h.get("trust_hash")
                  for e in parsed["hooks"]["SessionStart"] for h in e["hooks"]}
    assert sorted(v for v in by_command.values() if v) == ["h0", "h1", "h2", "h3"], (
        "every hash must survive, exactly once")

    # And each must sit beside the command it was recorded against.
    old_parsed = _parse(old, ic)
    old_by_command = {h["command"]: h.get("trust_hash")
                      for e in old_parsed["hooks"]["SessionStart"] for h in e["hooks"]}
    assert by_command == old_by_command


def test_unowned_keys_in_our_mcp_table_survive(ic):
    old = ic.render_block("http://localhost:8789", 1).replace(
        'bearer_token_env_var = "INFOGUANA_MCP_SECRET"',
        'bearer_token_env_var = "INFOGUANA_MCP_SECRET"\n'
        'default_tools_approval_mode = "never"')
    merged = ic._merge(old, ic.render_block("http://localhost:8789", 1))
    parsed = _parse(merged, ic)
    assert parsed["mcp_servers"]["infoguana"]["default_tools_approval_mode"] == "never"


def test_a_foreign_table_survives(ic):
    old = ic.render_block("http://localhost:8789", 1).replace(
        ic.END, '[hooks.state]\nsomething = "codex wrote this"\n' + ic.END)
    merged = ic._merge(old, ic.render_block("http://localhost:8789", 1))
    assert _parse(merged, ic)["hooks"]["state"]["something"] == "codex wrote this"


def test_our_own_keys_are_not_duplicated_across_regenerations(ic):
    """Idempotence: merging a block into itself repeatedly must converge,
    not accumulate."""
    block = ic.render_block("http://localhost:8789", 2)
    once = ic._merge(block, block)
    twice = ic._merge(once, block)
    assert once == twice
    parsed = _parse(twice, ic)
    assert len(parsed["hooks"]["SessionStart"]) == 3
    assert len(parsed["mcp_servers"]["infoguana"]) == 2


def test_merge_output_is_parseable_with_everything_at_once(ic):
    """The combination, since each preservation path edits the same text."""
    old = ic.render_block("http://localhost:8789", 2).replace(
        ic.END,
        '[[hooks.SessionStart]]\n'
        '[[hooks.SessionStart.hooks]]\n'
        'type = "command"\n'
        'command = "/usr/local/bin/user-hook.sh"\n'
        'trust_hash = "user"\n'
        '[hooks.state]\n'
        'approved = true\n' + ic.END,
    )
    merged = ic._merge(old, ic.render_block("http://localhost:8789", 2))
    parsed = _parse(merged, ic)
    commands = [h["command"] for e in parsed["hooks"]["SessionStart"]
                for h in e["hooks"]]
    assert "/usr/local/bin/user-hook.sh" in commands
    assert parsed["hooks"]["state"]["approved"] is True


# --------------------------------------------------------------------
# splice
# --------------------------------------------------------------------

def test_splice_preserves_content_outside_the_markers(ic):
    existing = ('# my own config\nmodel = "gpt-5"\n\n'
                + ic.render_block("http://localhost:8789", 1)
                + '\n[unrelated]\nkeep = true\n')
    out = ic.splice(existing, ic.render_block("http://localhost:9999", 1))
    assert 'model = "gpt-5"' in out
    assert "[unrelated]" in out
    assert "localhost:9999" in out
    assert "localhost:8789" not in out


def test_splice_appends_when_there_is_no_existing_block(ic):
    out = ic.splice('model = "gpt-5"\n', ic.render_block("http://localhost:8789", 1))
    assert 'model = "gpt-5"' in out
    assert ic.BEGIN in out and ic.END in out
    assert tomllib.loads(out.replace(ic.BEGIN, "").replace(ic.END, ""))
