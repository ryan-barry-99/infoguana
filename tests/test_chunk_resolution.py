"""Tests for the shared chunk-count resolution.

Both installers register N SessionStart hooks and ask the server what N
should be. Getting N too low is the failure this machinery exists to
prevent, and it is silent: the surplus content is simply absent from the
session, which is indistinguishable from a project having nothing to say.

`parse_chunk_override` is deliberately pure and runs before any network
or credential work, so it is testable without a server or a fixture —
which is the reason it sits where it does in both installers.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def setup():
    spec = importlib.util.spec_from_file_location(
        "_infoguana_setup", REPO / "scripts" / "_infoguana_setup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_unset_override_means_measure(setup):
    assert setup.parse_chunk_override(None) is None


@pytest.mark.parametrize("raw,expected", [("1", 1), ("17", 17), ("128", 128)])
def test_valid_overrides_pass_through(setup, raw, expected):
    assert setup.parse_chunk_override(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "", "3.5"])
def test_a_non_integer_is_rejected(setup, raw):
    with pytest.raises(ValueError, match="must be an integer"):
        setup.parse_chunk_override(raw)


@pytest.mark.parametrize("raw", ["0", "-1", "129"])
def test_out_of_range_is_rejected(setup, raw):
    """The upper bound must track the chunk route's own ceiling. Both
    installers previously hardcoded 64 and silently went stale when the
    route raised its limit, rejecting counts the server would accept."""
    with pytest.raises(ValueError, match=r"must be 1\.\.128"):
        setup.parse_chunk_override(raw)


def test_the_bound_matches_the_route(setup):
    """Reads the route's constant directly — a drift check, since the two
    numbers live in different files and nothing else couples them."""
    route = (REPO / "app" / "routes" / "onboard.py").read_text()
    assert f"MAX_CHUNKS = {setup.MAX_CHUNKS}" in route


def test_an_override_skips_the_network_entirely(setup):
    """With an explicit count there is nothing to measure, so a down
    server must not turn into a failed install."""
    def explode(*a, **k):
        raise AssertionError("network touched despite an explicit override")

    n, sizing = setup.resolve_chunks("http://unused", "tok", 12, explode)
    assert (n, sizing) == (12, {})


def test_a_non_json_200_reports_a_missing_endpoint_not_a_dead_server(
        setup, monkeypatch):
    """`/onboard/sizing` matches the older `/onboard/{project}` route, so a
    server predating the endpoint answers 200 with a plain-text blob for a
    phantom project named "sizing" rather than 404. Reported as a
    transport error it reads as "server down" while the server is plainly
    answering, and re-running never clears it."""
    _patch_urlopen(setup, monkeypatch, "not json at all")
    with pytest.raises(setup.SizingUnavailable, match="predates the endpoint"):
        setup.fetch_sizing("http://x", "tok")


def test_a_measured_count_is_used(setup, monkeypatch):
    _patch_urlopen(setup, monkeypatch, json.dumps({"recommended_chunks": 17}))
    warned = []
    n, sizing = setup.resolve_chunks("http://x", "tok", None, warned.append)
    assert n == 17 and sizing["recommended_chunks"] == 17
    assert warned == []


def test_an_unavailable_server_falls_back_and_says_so(setup, monkeypatch):
    """The fallback is over-provisioned on purpose, but it must announce
    itself — an unmeasured count that looks measured is how a shortfall
    goes unnoticed."""
    _patch_urlopen(setup, monkeypatch, None)
    warned = []
    n, sizing = setup.resolve_chunks("http://x", "tok", None, warned.append)
    assert n == setup.FALLBACK_CHUNKS and sizing == {}
    assert len(warned) == 1 and "could not reach" in warned[0]


def _patch_urlopen(setup, monkeypatch, body: str | None):
    """Replace only urlopen, leaving the real Request construction alone.

    Swapping the whole urllib module out instead makes authed_request
    fail while building its Request — which fetch_sizing then reports as
    an unreachable server, so every test passes for the wrong reason.
    """
    class _Resp:
        def read(self): return body.encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _urlopen(req, timeout=None):
        if body is None:
            raise OSError("connection refused")
        return _Resp()

    monkeypatch.setattr(setup.urllib.request, "urlopen", _urlopen)
