"""Tests for the HTTP classification backend.

The CLI path only exists on machines running Claude Code, so a headless or
Codex-only install has no classifier at all without this. Everything here
runs against a stubbed `urllib.request.urlopen` — no endpoint is contacted,
which is also why the real network failure modes are only representable as
the exceptions the code catches.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from app import classify as C


class _Resp(io.BytesIO):
    """Minimal stand-in for the object `urlopen` yields as a context manager."""
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _ok(content: str) -> _Resp:
    return _Resp(json.dumps(
        {"choices": [{"message": {"content": content}}]}).encode())


GOOD = json.dumps({
    "type": "memory", "tags": ["a", "b"], "project": "p",
    "title": "T", "description": "D",
})


@pytest.fixture
def http_backend(monkeypatch):
    """Point the classifier at an HTTP endpoint without touching the network."""
    monkeypatch.setattr(C.settings, "classify_base_url", "http://x/v1")
    monkeypatch.setattr(C.settings, "classify_api_key", None)
    monkeypatch.setattr(C.settings, "classify_model", "test-model")


# --- routing ---------------------------------------------------------------

def test_http_backend_is_used_when_a_base_url_is_set(http_backend, monkeypatch):
    """The whole point: no Claude CLI on the box, and classification still
    happens rather than every note landing `unsorted`."""
    seen = {}
    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)
        return _ok(GOOD)
    monkeypatch.setattr(C.urllib.request, "urlopen", fake)
    # which() would find a real CLI on a dev box; the HTTP path must win
    # regardless, so make the CLI "available" and assert it is not used.
    monkeypatch.setattr(C.shutil, "which", lambda *_: "/usr/bin/claude")
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **k:
                        pytest.fail("shelled out to the CLI despite a base_url"))
    got = C.classify("some note text")
    assert got is not None and got.type == "memory"
    assert seen["url"] == "http://x/v1/chat/completions"
    assert seen["body"]["model"] == "test-model"


def test_cli_is_used_when_no_base_url_is_set(monkeypatch):
    """The default path must be untouched by this change."""
    monkeypatch.setattr(C.settings, "classify_base_url", None)
    monkeypatch.setattr(C.shutil, "which", lambda *_: None)
    monkeypatch.setattr(C.urllib.request, "urlopen", lambda *a, **k:
                        pytest.fail("used HTTP with no base_url configured"))
    assert C.classify("text") is None


# --- the caller's timeout actually reaches the request ---------------------

def test_caller_timeout_is_honored(http_backend, monkeypatch):
    """`timeout` previously reached only the CLI branch, so a caller asking
    for 15s got the 180s default on exactly the machines where a hung
    request is most likely."""
    seen = {}
    def fake(req, timeout=None):
        seen["timeout"] = timeout
        return _ok(GOOD)
    monkeypatch.setattr(C.urllib.request, "urlopen", fake)
    C.classify("text", timeout=15)
    assert seen["timeout"] == 15


# --- the API key must not survive a redirect -------------------------------

def test_api_key_is_set_as_an_unredirected_header(http_backend, monkeypatch):
    """`urlopen` follows redirects and copies ordinary headers onto the
    follow-up request — Authorization included. A key set the plain way
    would be replayed to whatever host a 30x names."""
    monkeypatch.setattr(C.settings, "classify_api_key", "sk-secret")
    seen = {}
    def fake(req, timeout=None):
        seen["unredirected"] = dict(req.unredirected_hdrs)
        seen["all"] = dict(req.headers)
        return _ok(GOOD)
    monkeypatch.setattr(C.urllib.request, "urlopen", fake)
    C.classify("text")
    assert seen["unredirected"].get("Authorization") == "Bearer sk-secret"
    # Not in the redirect-surviving set.
    assert "Authorization" not in seen["all"]


def test_no_authorization_header_when_no_key_is_configured(http_backend, monkeypatch):
    seen = {}
    def fake(req, timeout=None):
        seen["u"] = dict(req.unredirected_hdrs); return _ok(GOOD)
    monkeypatch.setattr(C.urllib.request, "urlopen", fake)
    C.classify("text")
    assert "Authorization" not in seen["u"]


# --- the max_tokens / max_completion_tokens split --------------------------

def _http_error(code, body):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


def test_retries_once_with_max_completion_tokens(http_backend, monkeypatch):
    """Newer OpenAI models reject `max_tokens`; local servers only know it.
    Send the common spelling and switch on being told to, rather than
    maintaining a model list."""
    calls = []
    def fake(req, timeout=None):
        body = json.loads(req.data); calls.append(body)
        if len(calls) == 1:
            raise _http_error(400, b"Unsupported: use 'max_completion_tokens'")
        return _ok(GOOD)
    monkeypatch.setattr(C.urllib.request, "urlopen", fake)
    got = C.classify("text")
    assert got is not None
    assert "max_tokens" in calls[0]
    assert calls[1].get("max_completion_tokens") == 600
    assert "max_tokens" not in calls[1]


def test_other_400s_are_not_retried(http_backend, monkeypatch):
    calls = []
    def fake(req, timeout=None):
        calls.append(1); raise _http_error(400, b"context length exceeded")
    monkeypatch.setattr(C.urllib.request, "urlopen", fake)
    assert C.classify("text") is None
    assert len(calls) == 1


# --- failures degrade to None, never raise ---------------------------------

@pytest.mark.parametrize("boom", [
    urllib.error.URLError("connection refused"),
    TimeoutError("timed out"),
    OSError("network down"),
])
def test_transport_failures_return_none(http_backend, monkeypatch, boom):
    """A note still gets saved when the classifier is down — it lands
    `unsorted` with a fallback preview rather than failing the write."""
    def fake(req, timeout=None): raise boom
    monkeypatch.setattr(C.urllib.request, "urlopen", fake)
    assert C.classify("text") is None


def test_unexpected_response_shape_returns_none(http_backend, monkeypatch):
    monkeypatch.setattr(C.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(b'{"nope": 1}'))
    assert C.classify("text") is None


def test_unparseable_model_output_returns_none(http_backend, monkeypatch):
    """Small local models wrap output in prose or drop fields."""
    monkeypatch.setattr(C.urllib.request, "urlopen",
                        lambda req, timeout=None: _ok("Sure! Here you go: maybe a memory?"))
    assert C.classify("text") is None


# --- images: the HTTP backend cannot send them ---------------------------

def test_images_fall_through_to_the_cli_when_one_is_installed(
        http_backend, monkeypatch, tmp_path):
    """Setting a base_url is a cheaper text backend, not an instruction to
    give up image classification the machine can still do. The HTTP path
    has no way to send an attachment, so images must reach the CLI."""
    monkeypatch.setattr(C.shutil, "which", lambda *_: "/usr/bin/claude")
    monkeypatch.setattr(C.urllib.request, "urlopen", lambda *a, **k:
                        pytest.fail("sent an image note to the HTTP backend"))
    calls = []

    class _Done:
        returncode = 1
        stdout = ""
        stderr = "stub"
    monkeypatch.setattr(C.subprocess, "run",
                        lambda *a, **k: (calls.append(a), _Done())[1])
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    C.classify("text", image_paths=[img])
    assert calls, "image note never reached the CLI"


def test_image_with_text_uses_http_when_no_cli_is_available(
        http_backend, monkeypatch, tmp_path):
    """A partial result beats none: classify the text, skip the images."""
    monkeypatch.setattr(C.shutil, "which", lambda *_: None)
    seen = {}
    def fake(req, timeout=None):
        seen["body"] = json.loads(req.data); return _ok(GOOD)
    monkeypatch.setattr(C.urllib.request, "urlopen", fake)
    got = C.classify("a real sentence", image_paths=[tmp_path / "a.png"])
    assert got is not None and got.type == "memory"
    assert "a real sentence" in seen["body"]["messages"][0]["content"]


def test_image_only_note_is_left_unsorted_when_no_cli_is_available(
        http_backend, monkeypatch, tmp_path):
    """With no text and no CLI there is nothing to degrade to. The model
    would classify the literal string "(no text)" and invent tags, which
    are then the only text the note contributes to search — text-mode
    parsing drops `description`. Unsorted is recoverable; findable under
    the wrong words is not."""
    monkeypatch.setattr(C.shutil, "which", lambda *_: None)
    monkeypatch.setattr(C.urllib.request, "urlopen", lambda *a, **k:
                        pytest.fail("classified an image-only note over HTTP"))
    assert C.classify("   ", image_paths=[tmp_path / "a.png"]) is None


# --- classified project names are bounded --------------------------------

def test_an_implausible_project_name_is_discarded(http_backend, monkeypatch):
    """`project` is the one classifier field with no cap, and it is
    serialized into every preview-mode hit for the note. Discarded rather
    than truncated: a cut-off name is still a namespace matching nothing."""
    long_project = "this is a sentence about where the note belongs " * 3
    monkeypatch.setattr(C.urllib.request, "urlopen", lambda req, timeout=None:
                        _ok(json.dumps({"type": "memory", "tags": [],
                                        "project": long_project,
                                        "title": "T", "description": "D"})))
    got = C.classify("text")
    assert got is not None and got.project is None


def test_a_normal_project_name_survives(http_backend, monkeypatch):
    monkeypatch.setattr(C.urllib.request, "urlopen", lambda req, timeout=None:
                        _ok(json.dumps({"type": "memory", "tags": [],
                                        "project": "infoguana",
                                        "title": "T", "description": "D"})))
    got = C.classify("text")
    assert got is not None and got.project == "infoguana"


# --- a hostile or broken response must not escape classify() -------------

@pytest.mark.parametrize("content", [None, [{"type": "text", "text": "hi"}], 42])
def test_non_string_content_returns_none_instead_of_raising(
        http_backend, monkeypatch, content):
    """Servers return null content for reasoning-only and tool-call
    responses, and some return a list of content parts. `_parse` runs
    outside the request's try block, so an unguarded value reaches
    `.strip()` and the AttributeError escapes `classify()` — breaking its
    contract of returning None, and aborting `process_note` before the
    note is ever embedded. It then has no preview and no vector row, is
    invisible to semantic search, and nothing re-runs the pipeline."""
    monkeypatch.setattr(C.urllib.request, "urlopen", lambda req, timeout=None:
                        _Resp(json.dumps(
                            {"choices": [{"message": {"content": content}}]}).encode()))
    assert C.classify("text") is None
