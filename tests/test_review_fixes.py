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

# Kept in step with the notice in app/routes/onboard.py. Only the length
# matters here: the tests need to subtract it to reason about what a
# slice weighed before the notice rode along.
_NOTICE_BYTES = len(
    ("_Some of this project's memory may be missing from this brief. Call "
     "`context(project=...)` for the full set before relying on rules or "
     "plans._\n\n").encode("utf-8"))


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
                # The invariant is that the notice never turns a fitting
                # slice into a non-fitting one — not that a carrier is
                # always under the cap. A slice that was already over is
                # allowed to carry it (see the test below), because its
                # tail is being truncated either way.
                without = len(s.encode("utf-8")) - _NOTICE_BYTES
                assert len(s.encode("utf-8")) <= cap or without > cap, (
                    f"notice pushed fitting slice {i} to {len(s.encode())} B "
                    f"against a {cap} B cap at of={of}")


def test_a_slice_already_over_the_cap_still_carries_the_notice(route, monkeypatch):
    """The severe-undersize case, which used to warn least while losing
    most. The notice was attached only if the carrier plus the notice fit
    under the cap; when every slice is over cap — the regime the module's
    own history describes, and one a raised INFOGUANA_ONBOARD_BUDGET
    reaches — the guard failed closed and the agent got no warning at all,
    which is indistinguishable from a complete brief.
    """
    cap = route.CHUNK_TARGET_BYTES
    blob = "".join("z" * (cap * 2) + "\n" for _ in range(10))
    served = _serve(route, monkeypatch, blob, 5)
    assert all(len(s.encode("utf-8")) > cap for s in served), (
        "test blob is not in the every-slice-over-cap regime")
    assert sum("may be missing" in s for s in served) == 1


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
    chunk — while every session still receives the globals and the
    seeded protocol, ~12 KB needing 9 at the 4000-token default. Sizing
    was therefore most wrong at a first install, where every session is
    an unknown project."""
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


# --- markdown units must survive slicing ---------------------------------

def test_a_heading_is_never_severed_from_its_body(route):
    """The reason `_break_candidates` exists. A split directly after a
    heading orphans it: the harness assembles slices as independent
    blocks, so the reader gets an empty section and its content detached
    somewhere else — observed once as "a `## skills available` heading
    which came through empty" beside "a separate skills preamble further
    down". Pre-fix, splitting snapped to any line start, so the offset
    right after a heading was a legal break."""
    blob = "".join(
        f"## section {i}\n" + "body line for section\n" * 4 + "\n"
        for i in range(12)
    )
    for n in range(2, 13):
        chunks = route._line_aligned_chunks(blob, n)
        assert "".join(chunks) == blob
        for c in chunks:
            assert not c.rstrip("\n").endswith(f"## section {c.count('#')}") \
                or c.count("\n") > 1
        # No slice may end on a heading line — that is the orphaning.
        for c in chunks:
            lines = c.rstrip("\n").splitlines()
            if lines:
                assert not lines[-1].startswith("## "), f"orphaned at n={n}"


def test_a_code_fence_is_never_split_across_slices(route):
    """An unterminated ``` in one slice and a stray closer in the next
    renders every following paragraph as code. Pre-fix, fences were
    invisible to the splitter."""
    blob = "".join(
        f"Prose paragraph {i}.\n\n```\ncode line one\ncode line two\n```\n\n"
        for i in range(10)
    )
    for n in range(2, 15):
        chunks = route._line_aligned_chunks(blob, n)
        assert "".join(chunks) == blob
        for c in chunks:
            assert c.count("```") % 2 == 0, f"unbalanced fence at n={n}"


def test_a_paragraph_boundary_is_preferred_when_one_is_in_reach(route):
    """Sections stay whole rather than merely lines: a blank-line boundary
    within a slice's worth of text wins over the nearest line start.

    The paragraphs are numbered and multi-line so the assertion can tell
    a real paragraph start from any old line start — pre-fix, splitting
    snapped to whichever line boundary was nearest, which lands mid-
    paragraph for all but a lucky blob.

    The paragraphs are long enough that each evenly-spaced boundary falls
    well inside one. Short paragraphs make the boundaries land on
    paragraph starts by arithmetic, and the test then passes against a
    splitter with no notion of a paragraph at all — which is the thing it
    is supposed to detect. Against the pre-fix splitter this blob breaks
    at "continuation line" every time."""
    blob = "".join(
        f"Paragraph {i} opens here.\n" + "continuation line\n" * 9 + "\n"
        for i in range(9)
    )
    chunks = route._line_aligned_chunks(blob, 4)
    assert "".join(chunks) == blob
    for c in chunks[1:]:
        assert c.startswith("Paragraph "), "slice began mid-paragraph"


# --- the chunk ceiling is a route behavior, not a constant ---------------

def test_the_route_serves_the_full_chunk_ceiling(route, monkeypatch):
    """The installer registers up to MAX_CHUNKS entries, so the route has
    to answer at that count. A bound that regressed to 64 would 400 every
    slice above it and silently drop the tail of every brief — which the
    source-text drift check in test_chunk_resolution.py cannot see,
    because it never calls the endpoint.

    The count is written out rather than read from `route.MAX_CHUNKS`: a
    test that takes its expectation from the constant it is checking
    follows that constant wherever it goes, which is the drift check
    again in a costume."""
    monkeypatch.setattr(route.onboard, "build_cached",
                        lambda project, budget_tokens: "line\n" * 500)
    out = route.onboard_chunk(project="p", index=127, of=128)
    assert isinstance(out, str)


def test_the_route_refuses_a_count_above_the_ceiling(route, monkeypatch):
    """The other half — the bound still has to be a bound. Without this,
    raising MAX_CHUNKS to silence the test above would pass."""
    monkeypatch.setattr(route.onboard, "build_cached",
                        lambda project, budget_tokens: "line\n" * 500)
    with pytest.raises(route.HTTPException) as e:
        route.onboard_chunk(project="p", index=0, of=129)
    assert e.value.status_code == 400


# --- budget_tokens bounds the build cache's key space --------------------

def test_an_absurd_budget_is_refused_before_any_build(route, monkeypatch):
    """Each distinct budget mints its own cache entry per project, and
    nothing evicts on write, so an unbounded parameter is an unbounded
    cache. Refused ahead of the DB work, which is also what keeps this
    testable without a fixture."""
    called = []
    monkeypatch.setattr(route.onboard, "build_cached",
                        lambda project, budget_tokens: called.append(1) or "x")
    for bad in (0, -1, route.MAX_BUDGET_TOKENS + 1):
        with pytest.raises(route.HTTPException) as e:
            route.onboard_chunk(project="p", index=0, of=4, budget_tokens=bad)
        assert e.value.status_code == 400
    assert called == [], "a rejected budget still reached the builder"


# --- a shortfall must be named, including the one with no project ---------

def _shortfall(setup, sizing, n=9) -> list[str]:
    out: list[str] = []
    setup.report_shortfall(sizing, n, out.append)
    return out


def _sizing(projects, *, baseline_needed=7, fits_all=True, target=1700):
    return {"chunk_target_bytes": target, "projects": projects,
            "baseline_bytes": 11980, "baseline_needed": baseline_needed,
            "fits_all": fits_all}


def test_a_project_that_will_not_fit_is_named(setup):
    lines = _shortfall(setup, _sizing(
        [{"project": "big", "bytes": 90000, "widest_at_recommended": 4200}],
        fits_all=False))
    assert any("still split over" in ln for ln in lines)
    assert any("big" in ln for ln in lines)


def test_the_globals_only_blob_is_reported_when_no_project_is_over(setup):
    """The baseline is deliberately absent from `projects` — it floors the
    recommendation without being one — so a loop over `projects` alone is
    silent about a fresh install, which is the case the baseline exists
    for. Pre-fix that silence was the whole bug: `fits_all` was computed
    by the endpoint and read by neither installer."""
    lines = _shortfall(setup, _sizing(
        [{"project": "small", "bytes": 900, "widest_at_recommended": 200}],
        fits_all=False))
    assert any("globals every session receives" in ln for ln in lines)
    assert any("11980" in ln for ln in lines)


def test_a_corpus_that_fits_says_nothing_alarming(setup):
    lines = _shortfall(setup, _sizing(
        [{"project": "ok", "bytes": 900, "widest_at_recommended": 200}]))
    assert not any("warning" in ln for ln in lines)


def test_an_empty_sizing_report_is_silent(setup):
    """resolve_chunks returns {} when the count was overridden or guessed;
    reporting on it would describe a measurement that never happened."""
    assert _shortfall(setup, {}) == []
