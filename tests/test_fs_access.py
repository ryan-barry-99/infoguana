"""Coverage for the read-only filesystem boundary and the MCP host allowlist.

`app/fs_access.py` is the one module where a bug hands an MCP caller
somebody's private files, and it had no tests at all. Two groups here:

  * the allowlist/denylist decisions, which are pure functions of
    `settings` plus a path — no database, no server, no fixture;
  * the two settings that now default to closed, `fs_allowlist` (empty,
    so the filesystem tools are off) and `mcp_allowed_hosts` (empty, so
    the SDK's Host/Origin checks stay off).

`_audit` writes to the `fs_reads` table and swallows every exception, so
the read paths below run without a database. It is monkeypatched anyway
where a test asserts on it — a silent no-op is exactly the failure mode
worth pinning.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import fs_access
from app.config import Settings, settings
from app.fs_access import FSAccessError


@pytest.fixture
def allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the allowlist at a scratch root and silence the audit table."""
    monkeypatch.setattr(settings, "fs_allowlist", [tmp_path])
    monkeypatch.setattr(fs_access, "_audit", lambda *a, **k: None)
    return tmp_path


@pytest.fixture
def disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fs_allowlist", [])


# --- defaults --------------------------------------------------------------


def test_fs_allowlist_defaults_to_empty() -> None:
    """A fresh install exposes no filesystem roots.

    Shipping a default root would mean the first operator to set a bearer
    token also, silently, published whatever lives under it.
    """
    assert Settings().fs_allowlist == []


def test_mcp_allowed_hosts_defaults_to_empty() -> None:
    assert Settings().mcp_allowed_hosts == []


# --- disabled-by-default behavior ------------------------------------------


@pytest.mark.usefixtures("disabled")
@pytest.mark.parametrize("call", [
    lambda: fs_access.resolve("/etc/passwd"),
    lambda: fs_access.read_file("/etc/passwd"),
    lambda: fs_access.list_dir("/etc"),
    lambda: fs_access.grep("root", "/etc"),
])
def test_every_entry_point_refuses_with_no_roots(call) -> None:
    with pytest.raises(FSAccessError):
        call()


@pytest.mark.usefixtures("disabled")
def test_empty_allowlist_says_the_feature_is_off() -> None:
    """Not 'outside the configured allowlist ()'.

    That message sends the reader hunting for a path bug when nothing is
    wrong with the path — the tools are simply not enabled.
    """
    with pytest.raises(FSAccessError) as exc:
        fs_access.resolve("/etc/passwd")
    message = exc.value.message
    assert "disabled" in message
    assert "INFOGUANA_FS_ALLOWLIST" in message
    assert "allowlist ()" not in message


# --- allowlist -------------------------------------------------------------


def test_path_under_a_root_resolves(allowed: Path) -> None:
    (allowed / "a.py").write_text("x = 1\n")
    assert fs_access.resolve(str(allowed / "a.py")) == allowed / "a.py"


def test_path_outside_every_root_is_refused(allowed: Path, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside") / "b.py"
    outside.write_text("x = 1\n")
    with pytest.raises(FSAccessError) as exc:
        fs_access.resolve(str(outside))
    assert "outside the configured allowlist" in exc.value.message
    # The configured roots are named, so the operator can see what was expected.
    assert str(allowed) in exc.value.message


def test_traversal_out_of_a_root_is_refused(allowed: Path) -> None:
    with pytest.raises(FSAccessError):
        fs_access.resolve(str(allowed / ".." / ".." / "etc" / "passwd"))


def test_symlink_pointing_out_of_a_root_is_refused(
    allowed: Path, tmp_path_factory
) -> None:
    """The check is on the *resolved* path, so a symlink cannot tunnel out."""
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("secret\n")
    link = allowed / "innocent.txt"
    link.symlink_to(outside)
    with pytest.raises(FSAccessError) as exc:
        fs_access.resolve(str(link))
    assert "outside the configured allowlist" in exc.value.message


def test_a_second_root_is_honored(tmp_path_factory, monkeypatch) -> None:
    first = tmp_path_factory.mktemp("first")
    second = tmp_path_factory.mktemp("second")
    monkeypatch.setattr(settings, "fs_allowlist", [first, second])
    (second / "c.py").write_text("x = 1\n")
    assert fs_access.resolve(str(second / "c.py")) == second / "c.py"


def test_resolve_does_not_require_existence(allowed: Path) -> None:
    """`list_dir` probes paths that may not exist; the gate is location only."""
    assert fs_access.resolve(str(allowed / "nope")) == allowed / "nope"


# --- denylist --------------------------------------------------------------


@pytest.mark.parametrize("name", [
    ".env", ".env.local", "prod.env",
    "id_rsa", "id_ed25519.pub", "server.pem", "tls.key",
    "credentials", "secrets.yaml",
    "notes.db", "store.sqlite", "store.sqlite-wal",
])
def test_denylisted_filenames_are_refused(allowed: Path, name: str) -> None:
    (allowed / name).write_text("x\n")
    with pytest.raises(FSAccessError) as exc:
        fs_access.resolve(str(allowed / name))
    assert "denylist" in exc.value.message


@pytest.mark.parametrize("parent", [".git", ".ssh", ".aws", ".gnupg"])
def test_denylist_matches_any_component_not_just_the_basename(
    allowed: Path, parent: str
) -> None:
    d = allowed / parent
    d.mkdir()
    (d / "config").write_text("x\n")
    with pytest.raises(FSAccessError):
        fs_access.resolve(str(d / "config"))


def test_denylist_is_case_insensitive(allowed: Path) -> None:
    (allowed / "ID_RSA").write_text("x\n")
    with pytest.raises(FSAccessError):
        fs_access.resolve(str(allowed / "ID_RSA"))


def test_docker_config_is_refused_but_a_plain_config_json_is_not(
    allowed: Path
) -> None:
    """`config.json` alone is too common to block outright.

    Every npm project has one; only the Docker auth file carries a
    registry token, so that one is matched on the full path suffix.
    """
    docker = allowed / ".docker"
    docker.mkdir()
    (docker / "config.json").write_text("{}\n")
    with pytest.raises(FSAccessError):
        fs_access.resolve(str(docker / "config.json"))

    plain = allowed / "pkg"
    plain.mkdir()
    (plain / "config.json").write_text("{}\n")
    assert fs_access.resolve(str(plain / "config.json")).name == "config.json"


# --- reading ---------------------------------------------------------------


def test_read_file_numbers_its_lines(allowed: Path) -> None:
    (allowed / "a.py").write_text("one\ntwo\nthree\n")
    r = fs_access.read_file(str(allowed / "a.py"))
    assert r.content.splitlines() == ["1\tone", "2\ttwo", "3\tthree"]
    assert (r.total_lines, r.lines_returned, r.truncated) == (3, 3, False)


def test_read_file_paginates_and_still_reports_the_total(allowed: Path) -> None:
    (allowed / "a.py").write_text("".join(f"line{i}\n" for i in range(1, 11)))
    r = fs_access.read_file(str(allowed / "a.py"), offset=3, limit=2)
    assert r.content.splitlines() == ["3\tline3", "4\tline4"]
    assert r.truncated is True
    # The counter is drained past the limit so the agent knows what it missed.
    assert r.total_lines == 10


def test_read_file_refuses_a_binary_file(allowed: Path) -> None:
    (allowed / "blob.bin").write_bytes(b"MZ\x00\x00payload")
    with pytest.raises(FSAccessError) as exc:
        fs_access.read_file(str(allowed / "blob.bin"))
    assert "binary" in exc.value.message


def test_read_file_over_the_cap_demands_pagination(
    allowed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "fs_read_max_bytes", 64)
    (allowed / "big.py").write_text("".join(f"line{i}\n" for i in range(200)))
    with pytest.raises(FSAccessError) as exc:
        fs_access.read_file(str(allowed / "big.py"))
    assert "offset" in exc.value.message

    # …and the paginated re-request is served, truncated at the cap.
    r = fs_access.read_file(str(allowed / "big.py"), offset=1, limit=500)
    assert r.truncated is True
    assert r.bytes_read <= 64 + len("line199\n")


def test_read_file_rejects_a_directory(allowed: Path) -> None:
    with pytest.raises(FSAccessError) as exc:
        fs_access.read_file(str(allowed))
    assert "not a regular file" in exc.value.message


def test_read_file_audits_the_read(
    allowed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(fs_access, "_audit",
                        lambda tool, path, n, result: calls.append((tool, result)))
    (allowed / "a.py").write_text("x = 1\n")
    fs_access.read_file(str(allowed / "a.py"))
    assert calls == [("read_file", "ok")]


# --- listing ---------------------------------------------------------------


def test_list_dir_flags_denied_entries_without_hiding_them(allowed: Path) -> None:
    (allowed / "a.py").write_text("x\n")
    (allowed / ".env").write_text("SECRET=1\n")
    by_name = {e.name: e for e in fs_access.list_dir(str(allowed))}
    assert by_name["a.py"].denied is False
    # Surfaced so the agent knows the file exists and is off limits, rather
    # than concluding the directory doesn't contain it.
    assert by_name[".env"].denied is True
    assert by_name[".env"].hidden is True


def test_list_dir_caps_the_entry_count(allowed: Path) -> None:
    for i in range(10):
        (allowed / f"f{i}.py").write_text("x\n")
    assert len(fs_access.list_dir(str(allowed), max_entries=4)) == 4


# --- grep ------------------------------------------------------------------


def test_grep_finds_matches_and_skips_denylisted_files(allowed: Path) -> None:
    (allowed / "a.py").write_text("needle here\n")
    (allowed / ".env").write_text("needle here\n")
    hits = fs_access.grep("needle", str(allowed))
    assert [Path(h.path).name for h in hits] == ["a.py"]
    assert hits[0].line == 1


def test_grep_skips_dependency_directories(allowed: Path) -> None:
    vendor = allowed / "node_modules"
    vendor.mkdir()
    (vendor / "dep.js").write_text("needle here\n")
    (allowed / "a.py").write_text("needle here\n")
    hits = fs_access.grep("needle", str(allowed))
    assert [Path(h.path).name for h in hits] == ["a.py"]


def test_grep_rg_rechecks_the_boundary_on_every_hit(
    allowed: Path, tmp_path_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ripgrep's own output is not trusted.

    `grep` dispatches to ripgrep when it is installed, which it is not in
    the shipped image — so this path is only ever exercised on a bare-metal
    install and would otherwise go untested. rg can follow symlinks and
    ignore rules we do not control, so each reported path is re-run through
    the denylist and the allowlist before it reaches the caller.
    """
    import subprocess

    outside = tmp_path_factory.mktemp("outside")
    stdout = "".join([
        f"{allowed / 'a.py'}:1:needle here\n",
        f"{allowed / '.env'}:1:needle here\n",     # denylisted
        f"{outside / 'b.py'}:7:needle here\n",     # outside every root
        "not-a-parseable-line\n",
    ])
    monkeypatch.setattr(fs_access, "_RG", "/usr/bin/rg")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        args=a, returncode=0, stdout=stdout, stderr=""))

    hits = fs_access.grep("needle", str(allowed))
    assert [(Path(h.path).name, h.line) for h in hits] == [("a.py", 1)]


def test_grep_rg_surfaces_a_ripgrep_error_but_not_an_empty_result(
    allowed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rg exits 1 for "no matches" and 2 for a real failure."""
    import subprocess

    monkeypatch.setattr(fs_access, "_RG", "/usr/bin/rg")

    def _run(code: int, err: str):
        return lambda *a, **k: subprocess.CompletedProcess(
            args=a, returncode=code, stdout="", stderr=err)

    monkeypatch.setattr(subprocess, "run", _run(1, ""))
    assert fs_access.grep("needle", str(allowed)) == []

    monkeypatch.setattr(subprocess, "run", _run(2, "regex parse error"))
    with pytest.raises(FSAccessError) as exc:
        fs_access.grep("needle", str(allowed))
    assert "regex parse error" in exc.value.message


def test_grep_py_rejects_an_invalid_regex(allowed: Path, monkeypatch) -> None:
    monkeypatch.setattr(fs_access, "_RG", None)
    with pytest.raises(FSAccessError) as exc:
        fs_access.grep("(unclosed", str(allowed))
    assert "invalid regex" in exc.value.message


# --- audit -----------------------------------------------------------------


def test_audit_failure_does_not_break_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit row is best-effort; losing it must not lose the answer.

    Deliberately not using the `allowed` fixture — that one stubs `_audit`
    out, and the real one is what is under test here.
    """
    from app import db

    def _boom():
        raise RuntimeError("no database")

    monkeypatch.setattr(settings, "fs_allowlist", [tmp_path])
    monkeypatch.setattr(db, "tx", _boom)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert fs_access.read_file(str(tmp_path / "a.py")).lines_returned == 1


# --- serializers -----------------------------------------------------------


def test_serializers_round_trip_the_dataclasses(allowed: Path) -> None:
    """The MCP layer returns dicts; these are the only place that conversion
    happens, so a renamed field would otherwise surface as a silent None."""
    (allowed / "a.py").write_text("needle\n")

    read = fs_access.read_result_to_dict(fs_access.read_file(str(allowed / "a.py")))
    assert read["path"].endswith("a.py")
    assert read["content"] == "1\tneedle"
    assert read["truncated"] is False

    listed = fs_access.entries_to_list(fs_access.list_dir(str(allowed)))
    assert listed[0]["name"] == "a.py"
    assert listed[0]["is_dir"] is False and listed[0]["denied"] is False

    hits = fs_access.hits_to_list(fs_access.grep("needle", str(allowed)))
    assert hits == [{"path": str(allowed / "a.py"), "line": 1, "text": "needle"}]


# --- settings parsing ------------------------------------------------------


def test_fs_allowlist_splits_on_colons_from_env(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INFOGUANA_FS_ALLOWLIST", "/root/code:/root/docs")
    assert Settings().fs_allowlist == [Path("/root/code"), Path("/root/docs")]


def test_mcp_allowed_hosts_splits_on_commas_not_colons(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """`:` is taken by `host:port`, so this list splits differently."""
    monkeypatch.setenv("INFOGUANA_MCP_ALLOWED_HOSTS", "10.0.0.5:*, box.ts.net")
    assert Settings().mcp_allowed_hosts == ["10.0.0.5:*", "box.ts.net"]


@pytest.mark.parametrize("field,env,empty", [
    ("fs_allowlist", "INFOGUANA_FS_ALLOWLIST", ""),
    ("mcp_allowed_hosts", "INFOGUANA_MCP_ALLOWED_HOSTS", ""),
    ("fs_allowlist", "INFOGUANA_FS_ALLOWLIST", ":"),
    ("mcp_allowed_hosts", "INFOGUANA_MCP_ALLOWED_HOSTS", ","),
])
def test_empty_env_value_parses_as_no_entries(
    monkeypatch: pytest.MonkeyPatch, field: str, env: str, empty: str
) -> None:
    """docker-compose passes these as "" when the operator hasn't set them."""
    monkeypatch.setenv(env, empty)
    assert getattr(Settings(), field) == []


# --- MCP transport security ------------------------------------------------


def test_transport_security_is_off_when_no_hosts_are_configured(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """None, not a loopback-only settings object.

    Loopback-only would enable rebinding protection and lock out every
    client reaching the server by LAN or tailnet name — the common
    deployment — which is a worse default than no checks at all.
    """
    from app import mcp_server

    monkeypatch.setattr(settings, "mcp_allowed_hosts", [])
    assert mcp_server._transport_security() is None


def test_transport_security_keeps_loopback_alongside_configured_hosts(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import mcp_server

    monkeypatch.setattr(settings, "mcp_allowed_hosts", ["10.0.0.5:*"])
    ts = mcp_server._transport_security()
    assert ts is not None
    assert ts.enable_dns_rebinding_protection is True
    assert "10.0.0.5:*" in ts.allowed_hosts
    # Configuring a LAN host must not cost the operator localhost access.
    for host in mcp_server.LOOPBACK_HOSTS:
        assert host in ts.allowed_hosts
    assert "http://10.0.0.5:*" in ts.allowed_origins
