"""Tests for scripts/_infoguana_setup.py — the shared installer helpers.

Everything here either touches files the user owns and this project
cannot regenerate (`~/.infoguana.env` holds the bearer token), or builds
a request that carries that token. Both are worth pinning.
"""
from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def setup_mod():
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "_infoguana_setup", REPO / "scripts" / "_infoguana_setup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------
# atomic_write
# --------------------------------------------------------------------

def test_atomic_write_creates_the_file(setup_mod, tmp_path):
    target = tmp_path / "new.txt"
    setup_mod.atomic_write(target, "hello\n")
    assert target.read_text() == "hello\n"


def test_atomic_write_replaces_existing_content(setup_mod, tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("old content\n")
    setup_mod.atomic_write(target, "new content\n")
    assert target.read_text() == "new content\n"


def test_atomic_write_applies_mode_before_the_rename(setup_mod, tmp_path):
    """The 0600 must be in force the moment the file appears at its final
    name — chmod-after-write leaves a window where a token-bearing file
    exists at the umask default."""
    target = tmp_path / "secret.env"
    setup_mod.atomic_write(target, "TOKEN=abc\n", mode=0o600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_failed_write_leaves_the_original_intact(setup_mod, tmp_path, monkeypatch):
    """The whole point: `Path.write_text` truncates to zero before writing
    a byte, so an interrupt leaves the target empty. Here the write blows
    up mid-flight and the original must be untouched."""
    target = tmp_path / "settings.json"
    original = '{"permissions": ["everything the user configured"]}\n'
    target.write_text(original)

    real_fdopen = os.fdopen

    class Boom(Exception):
        pass

    def exploding_fdopen(fd, *a, **kw):
        fh = real_fdopen(fd, *a, **kw)
        orig_write = fh.write

        def write(data):
            orig_write(data[:5])
            raise Boom("interrupted mid-write")

        fh.write = write
        return fh

    monkeypatch.setattr(setup_mod.os, "fdopen", exploding_fdopen)
    with pytest.raises(Boom):
        setup_mod.atomic_write(target, "replacement content\n")

    assert target.read_text() == original, "original must survive a failed write"


def test_a_failed_write_leaves_no_temp_files_behind(setup_mod, tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    target.write_text("original\n")

    def exploding_fdopen(fd, *a, **kw):
        os.close(fd)
        raise OSError("ENOSPC")

    monkeypatch.setattr(setup_mod.os, "fdopen", exploding_fdopen)
    with pytest.raises(OSError):
        setup_mod.atomic_write(target, "x")

    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_atomic_write_creates_missing_parent_directories(setup_mod, tmp_path):
    target = tmp_path / "deep" / "nested" / "file.txt"
    setup_mod.atomic_write(target, "ok\n")
    assert target.read_text() == "ok\n"


# --------------------------------------------------------------------
# parse_env_file / load_env_file
# --------------------------------------------------------------------

def test_parse_env_file_handles_export_quotes_and_comments(setup_mod, tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# a comment\n"
        "\n"
        "PLAIN=value\n"
        "export EXPORTED=value2\n"
        'DOUBLE="quoted value"\n'
        "SINGLE='quoted value'\n"
    )
    assert setup_mod.parse_env_file(f) == {
        "PLAIN": "value", "EXPORTED": "value2",
        "DOUBLE": "quoted value", "SINGLE": "quoted value",
    }


def test_parse_env_file_of_a_missing_file_is_empty(setup_mod, tmp_path):
    assert setup_mod.parse_env_file(tmp_path / "nope.env") == {}


def test_load_env_file_does_not_override_existing_vars(setup_mod, tmp_path,
                                                       monkeypatch):
    f = tmp_path / ".env"
    f.write_text("ALREADY_SET=from_file\nNOT_SET=from_file\n")
    monkeypatch.setenv("ALREADY_SET", "from_environment")
    monkeypatch.delenv("NOT_SET", raising=False)
    setup_mod.load_env_file(f)
    assert os.environ["ALREADY_SET"] == "from_environment"
    assert os.environ["NOT_SET"] == "from_file"


# --------------------------------------------------------------------
# ensure_infoguana_env
# --------------------------------------------------------------------

@pytest.fixture
def env_file(setup_mod, tmp_path, monkeypatch):
    """Point ENV_FILE at tmp_path so no test can touch the real one."""
    path = tmp_path / ".infoguana.env"
    monkeypatch.setattr(setup_mod, "ENV_FILE", path)
    return path


def test_ensure_env_writes_url_and_token(setup_mod, env_file):
    setup_mod.ensure_infoguana_env("tok123", "http://localhost:8789")
    parsed = setup_mod.parse_env_file(env_file)
    assert parsed["INFOGUANA_TOKEN"] == "tok123"
    assert parsed["INFOGUANA_URL"] == "http://localhost:8789"


def test_ensure_env_file_is_owner_only(setup_mod, env_file):
    setup_mod.ensure_infoguana_env("tok123", "http://localhost:8789")
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_ensure_env_preserves_unrelated_lines(setup_mod, env_file):
    env_file.write_text("# my comment\nINFOGUANA_AGENT=codex\nOTHER=keep\n")
    setup_mod.ensure_infoguana_env("tok", "http://localhost:8789")
    parsed = setup_mod.parse_env_file(env_file)
    assert "# my comment" in env_file.read_text()
    assert parsed["INFOGUANA_AGENT"] == "codex"
    assert parsed["OTHER"] == "keep"


def test_ensure_env_is_idempotent_and_says_so(setup_mod, env_file):
    """Regression: quoting on write without quoting the comparison would
    make every run report 'refreshed' and rewrite the file."""
    first = setup_mod.ensure_infoguana_env("tok", "http://localhost:8789")
    before = env_file.read_text()
    second = setup_mod.ensure_infoguana_env("tok", "http://localhost:8789")
    assert "created" in first
    assert "up-to-date" in second, second
    assert env_file.read_text() == before


@pytest.mark.parametrize("token", [
    "s3cr3t; touch /tmp/pwned",
    "has a space",
    "has#hash",
    "has'quote",
    "has$dollar",
    "back`tick`",
])
def test_a_shell_unsafe_token_round_trips_intact(setup_mod, env_file, token):
    """The file is `source`d by shell in several places, so an unquoted
    value is not merely mangled on read — it executes."""
    setup_mod.ensure_infoguana_env(token, "http://localhost:8789")
    assert setup_mod.parse_env_file(env_file)["INFOGUANA_TOKEN"] == token


def test_a_shell_unsafe_token_does_not_execute_when_sourced(setup_mod, env_file,
                                                            tmp_path):
    """The concrete attack, run for real: source the file in a shell and
    check the side effect did not happen and the value survived whole."""
    canary = tmp_path / "PWNED"
    setup_mod.ensure_infoguana_env(f"s3cr3t; touch {canary}",
                                   "http://localhost:8789")
    out = subprocess.run(
        ["sh", "-c", f'. "{env_file}"; printf %s "$INFOGUANA_TOKEN"'],
        capture_output=True, text=True, check=True)
    assert not canary.exists(), "sourcing the env file executed the token"
    assert out.stdout == f"s3cr3t; touch {canary}"


# --------------------------------------------------------------------
# resolve_credentials
# --------------------------------------------------------------------

def test_resolve_reads_the_docker_secret_and_mcp_json(setup_mod, tmp_path,
                                                      monkeypatch):
    monkeypatch.delenv("INFOGUANA_URL", raising=False)
    data = tmp_path / "data"
    data.mkdir()
    (data / ".mcp_secret").write_text("tok-from-docker\n")
    (data / "mcp.json").write_text(
        '{"mcpServers": {"infoguana": {"url": "http://box:9999/mcp/"}}}')
    assert setup_mod.resolve_credentials(tmp_path) == (
        "tok-from-docker", "http://box:9999")


def test_a_truncated_mcp_json_falls_back_instead_of_raising(setup_mod, tmp_path,
                                                            monkeypatch, capsys):
    """The entrypoint rewrites mcp.json on every container start, so a
    container killed mid-write leaves it truncated. That used to surface
    as a JSONDecodeError traceback naming json/decoder.py."""
    monkeypatch.delenv("INFOGUANA_URL", raising=False)
    data = tmp_path / "data"
    data.mkdir()
    (data / ".mcp_secret").write_text("tok\n")
    (data / "mcp.json").write_text('{"mcpServers": {"infogua')

    token, url = setup_mod.resolve_credentials(tmp_path)
    assert token == "tok"
    assert url == setup_mod.DEFAULT_URL
    assert "unreadable or incomplete" in capsys.readouterr().out


def test_mcp_json_missing_the_expected_keys_also_falls_back(setup_mod, tmp_path,
                                                            monkeypatch):
    monkeypatch.delenv("INFOGUANA_URL", raising=False)
    data = tmp_path / "data"
    data.mkdir()
    (data / ".mcp_secret").write_text("tok\n")
    (data / "mcp.json").write_text('{"mcpServers": {}}')
    assert setup_mod.resolve_credentials(tmp_path)[1] == setup_mod.DEFAULT_URL


def test_explicit_url_override_wins(setup_mod, tmp_path, monkeypatch):
    monkeypatch.setenv("INFOGUANA_URL", "http://host.docker.internal:8789")
    data = tmp_path / "data"
    data.mkdir()
    (data / ".mcp_secret").write_text("tok\n")
    (data / "mcp.json").write_text(
        '{"mcpServers": {"infoguana": {"url": "http://localhost:8789/mcp/"}}}')
    assert setup_mod.resolve_credentials(tmp_path)[1] == (
        "http://host.docker.internal:8789")


def test_falls_back_to_repo_env(setup_mod, tmp_path, monkeypatch):
    monkeypatch.delenv("INFOGUANA_URL", raising=False)
    monkeypatch.setattr(setup_mod, "ENV_FILE", tmp_path / "nonexistent.env")
    (tmp_path / ".env").write_text("INFOGUANA_MCP_SECRET=from-repo-env\n"
                                   "INFOGUANA_PORT=9001\n")
    assert setup_mod.resolve_credentials(tmp_path) == (
        "from-repo-env", "http://localhost:9001")


def test_no_credentials_anywhere_raises_an_actionable_error(setup_mod, tmp_path,
                                                            monkeypatch):
    monkeypatch.delenv("INFOGUANA_URL", raising=False)
    monkeypatch.setattr(setup_mod, "ENV_FILE", tmp_path / "nonexistent.env")
    with pytest.raises(RuntimeError,
                       match="could not find the infoguana bearer token"):
        setup_mod.resolve_credentials(tmp_path)


# --------------------------------------------------------------------
# authed_request — the token must not survive a redirect
# --------------------------------------------------------------------

class _Recorder(BaseHTTPRequestHandler):
    seen: dict = {}

    def do_GET(self):
        _Recorder.seen["auth"] = self.headers.get("Authorization")
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


@pytest.fixture
def redirect_pair():
    """Server B records what it receives; server A 302s to B."""
    _Recorder.seen = {}
    target = HTTPServer(("127.0.0.1", 0), _Recorder)
    target_port = target.server_port

    class Redirector(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target_port}/")
            self.end_headers()

        def log_message(self, *a):
            pass

    source = HTTPServer(("127.0.0.1", 0), Redirector)
    for srv in (target, source):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{source.server_port}/", _Recorder
    finally:
        for srv in (target, source):
            srv.shutdown()


def test_token_is_not_replayed_onto_a_redirect_target(setup_mod, redirect_pair):
    """urlopen follows 30x by default and redirect_request copies every
    header except Content-Length/Content-Type — Authorization included.
    This token grants full read/write over the whole corpus."""
    url, recorder = redirect_pair
    req = setup_mod.authed_request(url, "SUPERSECRET")
    urllib.request.urlopen(req, timeout=5).read()
    assert recorder.seen["auth"] is None


def test_the_plain_header_form_does_leak_which_is_why_the_helper_exists(
        setup_mod, redirect_pair):
    """Pins the mechanism, so a refactor back to a plain headers dict
    fails here rather than silently in production."""
    url, recorder = redirect_pair
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer SUPERSECRET"})
    urllib.request.urlopen(req, timeout=5).read()
    assert recorder.seen["auth"] == "Bearer SUPERSECRET"


def test_the_token_is_sent_on_the_first_hop(setup_mod):
    req = setup_mod.authed_request("http://example.invalid/", "tok")
    assert req.get_header("Authorization") == "Bearer tok"


def test_no_authorization_header_when_there_is_no_token(setup_mod):
    req = setup_mod.authed_request("http://example.invalid/", "")
    assert req.get_header("Authorization") is None
