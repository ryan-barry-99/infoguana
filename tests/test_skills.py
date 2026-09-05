"""Tests for app/skills.py.

This module is the one that most needs them: every function is pure over
strings, and every failure is *quiet*. A skill whose frontmatter doesn't
parse still gets stored, still appears in the manifest, and still reads
as a complete entry — it just carries the wrong text, so the agent never
fires the skill and nothing raises anywhere. There is no error to notice.

The cases below are the ones the module's own docstrings argue about,
plus regression tests for three defects found in review.
"""
from __future__ import annotations

import pytest
import yaml

from app import skills
from tests.conftest import make_note


# --------------------------------------------------------------------
# split_frontmatter — fence handling
# --------------------------------------------------------------------

def test_splits_on_exact_fences():
    block, prose = skills.split_frontmatter(
        "---\nname: deploy\n---\n# Heading\n\nBody text.\n")
    assert block == "name: deploy"
    assert prose == "# Heading\n\nBody text."


def test_four_dash_fence_is_a_horizontal_rule_not_frontmatter():
    """`----` is a thematic break. Treating it as a fence would pull
    arbitrary `key: value`-shaped prose into the manifest."""
    body = "----\nname: not-really\n----\nBody.\n"
    block, prose = skills.split_frontmatter(body)
    assert block == ""
    assert prose == body


def test_unclosed_fence_yields_no_frontmatter():
    """An opening `---` with no closer is a horizontal rule. Without this,
    the whole document would be swallowed as frontmatter."""
    block, prose = skills.split_frontmatter("---\nname: deploy\n\nBody.\n")
    assert block == ""
    assert prose == "---\nname: deploy\n\nBody.\n"


def test_leading_blank_lines_before_the_fence_are_tolerated():
    block, _ = skills.split_frontmatter("\n\n---\nname: deploy\n---\nBody.\n")
    assert block == "name: deploy"


def test_empty_body_is_not_an_error():
    assert skills.split_frontmatter("") == ("", "")


# --------------------------------------------------------------------
# parse_frontmatter — YAML grammar, and the failure modes it must absorb
# --------------------------------------------------------------------

def test_quoted_colon_does_not_redefine_name():
    """The failure that motivated using a real YAML parser: a colon
    inside a quoted description silently redefining `name`, which is a
    skill's identity."""
    fields = skills.parse_frontmatter(
        '---\nname: deploy\ndescription: "Deploy the service: staging first"\n---\n')
    assert fields["name"] == "deploy"
    assert fields["description"] == "Deploy the service: staging first"


def test_folded_block_description_joins_to_one_line():
    fields = skills.parse_frontmatter(
        "---\nname: review\ndescription: >\n  Run a review.\n  Use when asked.\n---\n")
    assert fields["description"] == "Run a review. Use when asked."


def test_unparseable_yaml_degrades_to_empty_not_an_exception():
    """A SessionStart blob that 500s is worse than a skill listed under a
    fallback name."""
    assert skills.parse_frontmatter("---\nname: [unclosed\n---\n") == {}


def test_non_mapping_frontmatter_degrades_to_empty():
    assert skills.parse_frontmatter("---\n- just\n- a list\n---\n") == {}


def test_none_valued_keys_are_dropped():
    fields = skills.parse_frontmatter("---\nname: deploy\ndescription:\n---\n")
    assert fields == {"name": "deploy"}


def test_yaml_alias_bomb_is_rejected_cheaply():
    """Regression: a 526-byte anchored frontmatter block expanded past
    2 GB when `str(v)` flattened the shared node graph, killing a
    root-running server via an unauthenticated form. safe_load is not the
    guard — it shares anchored nodes by reference, so the load is cheap
    and only the flatten is ruinous.

    Asserting on the result rather than on elapsed time: this must return
    `{}` because the guard fired, and if it ever regresses the test dies
    by OOM rather than by assertion, which is loud enough.

    Each alias level multiplies by ten: measured against the pre-fix
    parser, this 291-byte payload flattened to 49,135,790 chars, and two
    more lines would take it past 4 GB.
    """
    lines = ["---", "name: &a " + "x" * 40]
    prev = "a"
    for i in range(6):
        cur = chr(ord("b") + i)
        lines.append(f"{cur}: &{cur} [" + ",".join(f"*{prev}" for _ in range(10)) + "]")
        prev = cur
    bomb = "\n".join(lines + ["---", "body", ""])

    assert len(bomb.encode()) < 600, "the point is that it is tiny"
    assert skills.parse_frontmatter(bomb) == {}


def test_alias_guard_does_not_reject_ordinary_frontmatter():
    """The guard must be free for real files — every real SKILL.md must
    still parse, or the fix is worse than the bug."""
    fields = skills.parse_frontmatter(
        "---\nname: brain-review\ndescription: Run a review.\n"
        "allowed-tools: Read, Grep\n---\n")
    assert fields["name"] == "brain-review"
    assert fields["allowed-tools"] == "Read, Grep"


def test_anchor_without_alias_is_still_parsed():
    """Only *expansion* is dangerous. An anchor nothing references cannot
    multiply, and rejecting it would be a stricter rule than the threat."""
    fields = skills.parse_frontmatter("---\nname: &n deploy\n---\n")
    assert fields["name"] == "deploy"


# --------------------------------------------------------------------
# describe — the manifest entry, including every fallback
# --------------------------------------------------------------------

def test_describe_prefers_frontmatter():
    note = make_note("---\nname: deploy\ndescription: Ship it.\n---\nBody.\n",
                     description="haiku preview text")
    assert skills.describe(note) == ("deploy", "Ship it.")


def test_describe_falls_back_through_heading_and_note_description():
    note = make_note("# My Skill\n\nBody paragraph.\n",
                     description="the stored description")
    name, description = skills.describe(note)
    assert name == "My Skill"
    assert description == "the stored description"


def test_describe_falls_back_to_first_paragraph_when_all_else_missing():
    note = make_note("# My Skill\n\nDoes a thing.\nUse when asked.\n")
    assert skills.describe(note) == ("My Skill", "Does a thing. Use when asked.")


def test_describe_falls_back_to_note_id_when_there_is_no_name():
    note = make_note("Just prose, no heading.\n", id=42)
    assert skills.describe(note)[0] == "skill-42"


def test_describe_never_returns_an_empty_description():
    assert skills.describe(make_note(""))[1] == "(no description)"


def test_describe_clamps_a_runaway_description():
    note = make_note("---\nname: x\ndescription: " + "y" * 5000 + "\n---\n")
    _, description = skills.describe(note)
    assert len(description) == skills.MANIFEST_DESCRIPTION_CHARS + 1  # + ellipsis
    assert description.endswith("…")


def test_describe_does_not_clamp_a_description_at_the_bound():
    note = make_note(
        "---\nname: x\ndescription: " + "y" * skills.MANIFEST_DESCRIPTION_CHARS
        + "\n---\n")
    assert not skills.describe(note)[1].endswith("…")


# --------------------------------------------------------------------
# _first_paragraph — the fallback that carries the trigger condition
# --------------------------------------------------------------------

def test_first_paragraph_joins_wrapped_lines():
    """Regression: this returned the first *line*, so a wrapped
    description lost its entire "Use when ..." clause — the one part the
    manifest exists to carry — with no ellipsis marking the loss."""
    body = ("Apply pending Alembic migrations against a local database.\n"
            "Use when the user asks to migrate, or mentions schema drift.\n")
    assert skills._first_paragraph(body) == (
        "Apply pending Alembic migrations against a local database. "
        "Use when the user asks to migrate, or mentions schema drift.")


def test_first_paragraph_stops_at_the_blank_line():
    assert skills._first_paragraph("First para.\nStill first.\n\nSecond.\n") == (
        "First para. Still first.")


def test_first_paragraph_skips_headings_and_rules_before_the_prose():
    assert skills._first_paragraph("# Title\n\n---\n\nThe prose.\n") == "The prose."


def test_first_paragraph_stops_at_a_heading_that_follows_prose():
    assert skills._first_paragraph("Prose here.\n# Next Section\nMore.\n") == (
        "Prose here.")


def test_first_paragraph_of_nothing_is_empty():
    assert skills._first_paragraph("") == ""
    assert skills._first_paragraph("# Only a heading\n") == ""


# --------------------------------------------------------------------
# normalize_name / find_by_name / suggest_names
# --------------------------------------------------------------------

@pytest.mark.parametrize("given", [
    "brain-review", "Brain Review", "brain_review", "/brain-review",
    "/Brain_Review", "  BRAIN-REVIEW  ",
])
def test_names_fold_to_one_key(given):
    assert skills.normalize_name(given) == "brain-review"


def test_fold_is_narrow_enough_to_keep_distinct_names_distinct():
    assert skills.normalize_name("deploy") != skills.normalize_name("deploys")
    assert skills.normalize_name("brain-review") != skills.normalize_name("brainreview")


def test_empty_name_folds_to_empty_and_matches_nothing():
    assert skills.normalize_name("") == ""
    assert skills.normalize_name("///") == ""
    assert skills.find_by_name([make_note("---\nname: x\n---\n")], "") == []


def test_find_by_name_returns_every_scope_that_matches():
    """A list, not one note: a global and a project skill may share a name
    (that is how a project overrides a global), and resolving that needs
    the caller's scope."""
    glob = make_note("---\nname: deploy\n---\n", id=1, project=None)
    proj = make_note("---\nname: deploy\n---\n", id=2, project="thing")
    other = make_note("---\nname: build\n---\n", id=3)
    found = skills.find_by_name([glob, proj, other], "/Deploy")
    assert [n.id for n in found] == [1, 2]


def test_suggest_names_returns_authored_form_not_the_fold():
    notes = [make_note("---\nname: brain-review\n---\n"),
             make_note("---\nname: brain-pr\n---\n", id=2)]
    assert "brain-review" in skills.suggest_names(notes, "brain-reviwe")


def test_suggest_names_is_empty_for_nothing_close():
    notes = [make_note("---\nname: brain-review\n---\n")]
    assert skills.suggest_names(notes, "wholly-unrelated-thing") == []


# --------------------------------------------------------------------
# _first_sentence / preview_line
# --------------------------------------------------------------------

def test_first_sentence_splits_on_a_real_sentence_break():
    assert skills._first_sentence(
        "Deploy the service to staging and then to production. "
        "Use when asked to ship."
    ) == "Deploy the service to staging and then to production."


def test_first_sentence_keeps_a_summary_too_short_to_stand_alone():
    """The break is real, but everything before it is under
    `_MIN_SUMMARY_CHARS`, and a 17-char preview tells a reader nothing.
    Below the floor the whole description is the better preview."""
    text = "Ship the service. Use when the user asks to deploy or release."
    assert len("Ship the service.") < skills._MIN_SUMMARY_CHARS
    assert skills._first_sentence(text) == text


def test_first_sentence_ignores_a_break_too_early_to_be_a_summary():
    """"Handles e.g. Retries" — 12 chars is not a summary, so the break
    is not a sentence end."""
    text = "Runs it. Then does the much longer second part of the work."
    assert skills._first_sentence(text) == text


def test_first_sentence_ignores_a_lowercase_continuation():
    text = "Handles e.g. retries and backoff across the whole request path."
    assert skills._first_sentence(text) == text


def test_first_sentence_of_a_single_sentence_is_itself():
    text = "Just the one sentence with no break in it at all."
    assert skills._first_sentence(text) == text


def test_preview_line_is_name_plus_first_sentence():
    note = make_note(
        "---\nname: deploy\ndescription: Ship the service to staging and "
        "then to production. Use when the user asks to deploy.\n---\n")
    assert skills.preview_line(note) == (
        "deploy — Ship the service to staging and then to production.")


def test_preview_line_survives_a_note_with_no_frontmatter():
    note = make_note("# Fallback\n\nDoes a thing when asked.\n", id=7)
    assert skills.preview_line(note) == "Fallback — Does a thing when asked."


# --------------------------------------------------------------------
# _is_rule
# --------------------------------------------------------------------

@pytest.mark.parametrize("line", ["---", "----", "***", "___", "- - -", "* * *"])
def test_thematic_breaks_are_recognized(line):
    assert skills._is_rule(line)


@pytest.mark.parametrize("line", ["--", "**", "-*-", "text", "-- text"])
def test_non_breaks_are_not(line):
    assert not skills._is_rule(line)


# --------------------------------------------------------------------
# Cross-checks against the real corpus shape
# --------------------------------------------------------------------

def test_a_realistic_skill_document_round_trips():
    """The shape this module exists for, end to end."""
    body = (
        "---\n"
        "name: brain-review\n"
        "description: Run a project-aware code review of a branch or PR. "
        "Use when the user invokes /brain-review or asks for one.\n"
        "---\n\n"
        "# brain-review\n\n"
        "Long instructions the manifest must never carry.\n"
    )
    note = make_note(body, id=778)
    name, description = skills.describe(note)
    assert name == "brain-review"
    assert description.startswith("Run a project-aware code review")
    assert "Use when the user invokes" in description, (
        "the trigger list is the part the manifest exists to carry")
    assert "Long instructions" not in description
    # The preview is the summary half only; the manifest keeps the whole
    # description, trigger list included.
    assert skills.preview_line(note) == (
        "brain-review — Run a project-aware code review of a branch or PR.")
    assert skills.find_by_name([note], "/brain-review") == [note]


def test_yaml_is_the_parser_not_a_hand_rolled_one():
    """Guards the choice, not the behavior: if someone swaps in a
    hand-parser, the multi-line + quoted-colon cases above go with it."""
    assert yaml.safe_load("a: 1") == {"a": 1}


def test_deeply_nested_frontmatter_degrades_instead_of_raising():
    """PyYAML composes nested nodes recursively, so ~500 levels of `[` in a
    1 KB block raises RecursionError — which is not a YAMLError and so
    escaped the degrade-to-{} guard. `_pin_skills` calls describe() on
    every skill in scope, so one such note saved globally made context()
    raise for every project and every session."""
    body = "---\na: " + "[" * 500 + "]" * 500 + "\n---\n\n# T\n\nprose\n"
    assert skills.parse_frontmatter(body) == {}
    # describe() must stay usable — that is the call the context pin makes.
    name, description = skills.describe(make_note(body))
    assert name and description


def test_skill_lookup_covers_the_manifest():
    """`get_skill` must be able to resolve anything `context` advertises.

    The manifest is where an agent learns a skill's name, so a lookup
    bound below the manifest's fetch bound would list skills that then
    come back "not found" — the two constants live in different modules
    and nothing but this test couples them.
    """
    from app import graph, mcp_server
    assert mcp_server.SKILL_LOOKUP_LIMIT >= graph.SKILLS_FETCH_LIMIT
