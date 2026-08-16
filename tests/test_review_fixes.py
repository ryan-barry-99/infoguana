"""Guards for the failure modes found reviewing the Codex-client change.

Each test here fails against the pre-fix code. They cluster around one
theme: a delivery path that is wrong but silent. Sizing measured at the
wrong budget, a shell-quoted path the shell rewrites, a warning banner
that evicts the content it warns about — none of them raise, and all of
them present as a project that simply has less memory than it should.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
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


# --- sizing must measure the blob delivery will fetch --------------------

def _capture_sizing_url(setup, monkeypatch, body: dict) -> str:
    seen = {}

    class _Resp:
        def read(self): return json.dumps(body).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(setup.urllib.request, "urlopen", _urlopen)
    setup.resolve_chunks("http://x", "tok", None, lambda m: None)
    return seen["url"]


def test_sizing_is_requested_at_the_hooks_budget(setup, monkeypatch, tmp_path):
    """A custom INFOGUANA_ONBOARD_BUDGET changes the blob, so sizing that
    ignores it registers a count derived from a different document. The
    same corpus needs 17 chunks at 4000 and 71 at 16000; installing 17 for
    a 71-chunk split truncates most of every slice at each session start.
    """
    monkeypatch.setenv("INFOGUANA_ONBOARD_BUDGET", "16000")
    url = _capture_sizing_url(setup, monkeypatch, {"recommended_chunks": 71})
    assert "budget_tokens=16000" in url


def test_sizing_falls_back_to_the_env_file_then_the_default(
        setup, monkeypatch, tmp_path):
    """Resolution order must mirror the hook's exactly — process env wins,
    then ~/.infoguana.env, then the shared default — or the two disagree
    for a user who set the budget in only one of them."""
    monkeypatch.delenv("INFOGUANA_ONBOARD_BUDGET", raising=False)
    env_file = tmp_path / ".infoguana.env"
    env_file.write_text("INFOGUANA_ONBOARD_BUDGET=8000\n")
    monkeypatch.setattr(setup, "ENV_FILE", env_file)
    assert setup.resolve_onboard_budget() == "8000"

    env_file.write_text("")
    assert setup.resolve_onboard_budget() == setup.DEFAULT_ONBOARD_BUDGET


def test_the_default_budget_matches_the_hook_and_the_route(setup):
    """Drift check: three files hardcode this number and nothing else
    couples them. If they disagree, sizing and delivery measure different
    blobs even when the user sets nothing at all."""
    hook = (REPO / "scripts" / "infoguana-onboard-chunk.py").read_text()
    route = (REPO / "app" / "routes" / "onboard.py").read_text()
    n = setup.DEFAULT_ONBOARD_BUDGET
    assert f'"INFOGUANA_ONBOARD_BUDGET", "{n}"' in hook
    assert f"budget_tokens: int = {n}" in route


# --- shell quoting -------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX quoting rules")
@pytest.mark.parametrize("path", [
    "/home/u/my$repo/hook.py",       # expands to /home/u/my/hook.py unquoted
    "/home/u/a b/`id`/hook.py",      # substitutes inside double quotes
    "/home/u/back\\slash/hook.py",
    "/home/u/plain/hook.py",
])
def test_quoted_paths_survive_the_shell_verbatim(setup, path):
    """The registered hook command is handed to a shell. A path the shell
    rewrites points the hook at somewhere that does not exist, and every
    slice then emits nothing — no error, just a session with no memory."""
    r = subprocess.run(["bash", "-c", f"printf %s {setup.quote(path)}"],
                       capture_output=True, text=True, timeout=30)
    assert r.stdout == path


# --- the undersized-delivery notice must pay for its own bytes -----------

@pytest.fixture
def route(monkeypatch):
    """The chunk route with its one DB call stubbed out.

    `build_cached` is the only thing between this endpoint and a database,
    so replacing it makes the slicing and notice logic testable at import
    level — no fixture, no server, no copy of the corpus.
    """
    sys.path.insert(0, str(REPO))
    from app.routes import onboard as mod
    return mod


def _serve(route, monkeypatch, blob: str, of: int) -> list[str]:
    monkeypatch.setattr(route.onboard, "build_cached",
                        lambda project, budget_tokens: blob)
    return [route.onboard_chunk(project="t", index=i, of=of,
                                budget_tokens=4000)
            for i in range(of)]


def test_the_notice_never_pushes_its_slice_over_the_cap(route, monkeypatch):
    """The banner warning that content may be missing must not cause
    content to go missing. Prepending it to slice 0 unconditionally did:
    slice 0 is frequently under the cap on its own but not by the notice's
    length, and its tail is where DEFAULT_PROTOCOL lives.
    """
    cap = route.CHUNK_TARGET_BYTES
    # One over-long line makes the split undersized at every `of`, so the
    # notice path is reached; the short lines give slices that sit just
    # under the cap, which is the case that used to overflow.
    blob = ("z" * (cap * 3) + "\n") + "".join("y" * 60 + "\n" for _ in range(120))
    for of in range(2, 16):
        served = _serve(route, monkeypatch, blob, of)
        carriers = [s for s in served if "may be missing" in s]
        assert len(carriers) <= 1, f"notice duplicated across slices at of={of}"
        for i, s in enumerate(served):
            if "may be missing" in s:
                assert len(s.encode("utf-8")) <= cap, (
                    f"notice pushed slice {i} to {len(s.encode())} B "
                    f"against a {cap} B cap at of={of}")


def test_the_notice_still_reaches_the_agent_when_it_fits(route, monkeypatch):
    """The fit check must not be so eager that the warning disappears —
    otherwise the test above passes against a route that never warns."""
    cap = route.CHUNK_TARGET_BYTES
    blob = ("z" * (cap * 3) + "\n") + "".join("y" * 60 + "\n" for _ in range(120))
    served = _serve(route, monkeypatch, blob, 8)
    assert any("may be missing" in s for s in served)


def test_a_fitting_split_gets_no_notice_at_all(route, monkeypatch):
    """The notice is a symptom of an undersized registration; a split that
    fits must stay clean."""
    blob = "".join("y" * 60 + "\n" for _ in range(40))
    served = _serve(route, monkeypatch, blob, 8)
    assert not any("may be missing" in s for s in served)


# --- an outage must not multiply its notice by the chunk count -----------

def _run_hook(args: list[str], **env_overrides) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("INFOGUANA_AGENT", "CLAUDECODE", "CLAUDE_PROJECT_DIR",
                        "CODEX_HOME", "CODEX_SANDBOX", "INFOGUANA_HOOK_DISABLE")}
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "infoguana-onboard-chunk.py"),
         *args],
        capture_output=True, text=True, timeout=30, env=env)


@pytest.mark.parametrize("index,expect_notice", [(0, True), (1, False),
                                                 (9, False)])
def test_only_slice_zero_announces_an_outage(index, expect_notice):
    """Every slice fails together in an outage, so a per-slice notice
    multiplies one sentence by the registered count — ~9 KB at 71 chunks,
    inside a ~1.7 KB per-hook budget. stderr still reports every slice."""
    r = _run_hook([str(index), "12"],
                  INFOGUANA_URL="http://127.0.0.1:9",  # discard port
                  INFOGUANA_TOKEN="tok")
    assert r.returncode == 0
    assert ("failed to load" in r.stdout) is expect_notice
    assert "infoguana: chunk" in r.stderr


# --- init-project argument parsing ---------------------------------------

@pytest.mark.parametrize("flag", ["--agent codex", "--agent=codex"])
def test_both_agent_flag_spellings_write_agents_md(flag, tmp_path):
    """`--agent=codex` used to fall through as the positional target dir,
    so the script errored on a directory named `--agent=codex` while
    silently defaulting the agent back to claude."""
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "init-project-infoguana.py"),
         "demo", str(tmp_path), *flag.split()],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "AGENTS.md").is_file()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_the_stamped_template_names_no_claude_only_paths(tmp_path):
    """A Codex user's AGENTS.md must not instruct them about `CLAUDE.md`
    or the `memory/` dir — neither exists for them, and it is the one line
    telling the agent what *not* to do."""
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "init-project-infoguana.py"),
         "demo", str(tmp_path), "--agent", "codex"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    body = (tmp_path / "AGENTS.md").read_text()
    assert "CLAUDE.md" not in body
    assert "memory/" not in body


# --- sizing must cover a project the corpus has never seen ---------------

def test_sizing_floors_at_the_unknown_project_blob(route, monkeypatch):
    """A fresh install has no projects, so enumerating them recommends 1
    chunk — while every session still receives the globals, ~12 KB needing
    9. Sizing was therefore most wrong at a first install, where every
    session is an unknown project."""
    monkeypatch.setattr(route.db, "list_project_names", lambda: [])
    big = "".join("y" * 60 + "\n" for _ in range(200))   # ~12 KB, like the globals
    monkeypatch.setattr(route.onboard, "build_cached",
                        lambda project, budget_tokens: big)
    out = route.onboard_sizing(budget_tokens=4000)
    assert out["projects"] == []
    assert out["baseline_needed"] > 1
    assert out["recommended_chunks"] == out["baseline_needed"]


def test_a_large_project_still_wins_over_the_baseline(route, monkeypatch):
    """The baseline is a floor, not a ceiling — a project bigger than the
    globals must still drive the recommendation up."""
    small = "".join("y" * 60 + "\n" for _ in range(20))
    big = "".join("y" * 60 + "\n" for _ in range(400))
    monkeypatch.setattr(route.db, "list_project_names", lambda: ["big"])
    monkeypatch.setattr(route.onboard, "build_cached",
                        lambda project, budget_tokens:
                        small if project == route._BASELINE_PROJECT else big)
    out = route.onboard_sizing(budget_tokens=4000)
    assert out["recommended_chunks"] > out["baseline_needed"]
