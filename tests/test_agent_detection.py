"""Tests for host-agent detection and the memory-system override text.

`detect_agent` decides which built-in memory store the override tells the
agent to leave alone, so getting it wrong points an agent away from a
store it does not have while leaving its real one unmentioned. It is a
pure function of four environment variables — no filesystem, no network —
so there is no reason for it to be untested.

The precedence case is the one that matters most and the one a reader is
most likely to "clean up": CLAUDECODE must be checked before CODEX_HOME,
because CODEX_HOME is exported into the whole IDE environment wherever
the Codex extension is installed and is therefore not evidence that Codex
is the host. Swapping those two branches passes every other test here.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

_AGENT_VARS = ("INFOGUANA_AGENT", "CLAUDECODE", "CLAUDE_PROJECT_DIR",
               "CODEX_HOME", "CODEX_SANDBOX")


def _load():
    """Import the helper by path — it lives in scripts/, which is not a
    package, and it is imported there by filename."""
    spec = importlib.util.spec_from_file_location(
        "_infoguana_agent", REPO / "scripts" / "_infoguana_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent(monkeypatch):
    """The module with every agent-identifying variable cleared.

    The test process inherits whatever agent is running the suite, so
    without this the results depend on who invoked pytest.
    """
    for var in _AGENT_VARS:
        monkeypatch.delenv(var, raising=False)
    return _load()


def test_nothing_set_is_generic(agent):
    assert agent.detect_agent() == "generic"


@pytest.mark.parametrize("var", ["CLAUDECODE", "CLAUDE_PROJECT_DIR"])
def test_claude_probes(agent, monkeypatch, var):
    monkeypatch.setenv(var, "1")
    assert agent.detect_agent() == "claude"


@pytest.mark.parametrize("var", ["CODEX_HOME", "CODEX_SANDBOX"])
def test_codex_probes(agent, monkeypatch, var):
    monkeypatch.setenv(var, "1")
    assert agent.detect_agent() == "codex"


def test_claude_wins_when_both_probes_are_present(agent, monkeypatch):
    """The documented hazard: CODEX_HOME leaks into any session started
    from an IDE that has the Codex extension installed, so it must not
    outrank a positive Claude Code signal."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CODEX_HOME", "/home/u/.codex")
    assert agent.detect_agent() == "claude"


@pytest.mark.parametrize("value", ["codex", "CODEX", "  Codex  "])
def test_explicit_override_outranks_the_probes(agent, monkeypatch, value):
    """INFOGUANA_AGENT is the documented escape hatch for exactly the
    case above, so it has to beat a positive probe, and it is written by
    hand into ~/.infoguana.env — hence the case and whitespace folding."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("INFOGUANA_AGENT", value)
    assert agent.detect_agent() == "codex"


def test_an_unrecognized_override_falls_through_to_detection(agent, monkeypatch):
    """An unknown value must not be echoed back as an agent name, or the
    override text would silently lose its agent-specific clause."""
    monkeypatch.setenv("INFOGUANA_AGENT", "gemini")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert agent.detect_agent() == "claude"


def test_an_empty_override_falls_through_to_detection(agent, monkeypatch):
    monkeypatch.setenv("INFOGUANA_AGENT", "")
    monkeypatch.setenv("CODEX_HOME", "/home/u/.codex")
    assert agent.detect_agent() == "codex"


def test_each_agent_is_told_about_its_own_store_and_not_the_others(agent,
                                                                  monkeypatch):
    """The failure this guards is a store named at the wrong agent, which
    reads as authoritative and sends it to edit a path it does not use."""
    monkeypatch.setenv("INFOGUANA_AGENT", "claude")
    claude = agent.memory_override()
    assert "~/.claude/projects" in claude
    assert "~/.codex/" not in claude

    monkeypatch.setenv("INFOGUANA_AGENT", "codex")
    codex = agent.memory_override()
    assert "~/.codex/" in codex
    assert "~/.claude/projects" not in codex


def test_the_generic_override_names_no_store(agent):
    """A host we cannot identify gets the shared directive only — naming
    a store here would be a guess presented as an instruction."""
    generic = agent.memory_override()
    assert "authoritative persistent memory" in generic
    assert "~/.claude/projects" not in generic
    assert "~/.codex/" not in generic
