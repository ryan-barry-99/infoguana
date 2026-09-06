"""Skill notes — portable capability documents.

A `skill` note's body is a SKILL.md file *verbatim*: YAML frontmatter plus
prose. Storing it unaltered is the whole point — a skill can be pasted out
of (or into) any harness's skills directory without translation, and any
client that can reach the MCP server gets the project's skills without
per-harness adaptation.

The frontmatter is what this module reads when it is present; `describe`
falls back to the body's first heading and first paragraph when it is
not, so a skill saved as plain prose still gets a manifest entry.
`name` is the skill's stable identity
(what the user invokes it by — stronger identity than any other note type
has) and `description` is its trigger condition. Those two fields are what
`context()` pins as a one-line manifest entry.

The body is deliberately *not* pinned: skills run 4-8KB, so three of them
would exhaust a whole 4000-token context budget before any memory loaded.
The agent reads the manifest, decides a skill applies, and calls `get(id)`
for the body — one canonical copy, loaded only in sessions that use it.

The description in the manifest is the *authored* one, never the haiku
preview: a trigger condition has to be exact, and previews are
triage-grade by design.
"""
import difflib
import re
from typing import Iterable, Optional

import yaml

from app.models import Note


_FENCE = "---"

# Manifest descriptions are clamped here rather than trusted to be short,
# because `describe` falls back to the body's first paragraph when
# frontmatter is missing and a paragraph has no length bound at all — one
# unwrapped prose note measured 874 tokens as a single manifest entry,
# against a 4000-token budget it is exempt from.
#
# Sized from the real distribution rather than a convenient sample.
# Measured over 2,740 SKILL.md files carrying a frontmatter description
# (4,278 found on disk, deduplicated by content to 2,744): median 185,
# p95 470, p99 775, max 1,461.
#
# 800 clears the p99. It does not clear the maximum and is not meant to:
# 23 descriptions (0.8%) still truncate, against 53 (1.9%) at the
# previous 600. The ones past 800 are genuine outliers running to 1,461
# chars, where truncation is the right answer for a line that has to sit
# in every session's manifest. The earlier 525 came from the maximum of
# a nine-file sample, which is not evidence about a corpus this size.
#
# Re-derive with: find / -name SKILL.md, dedupe by content hash, then
# parse_frontmatter(...)['description']. Scanning only ~/.claude and
# ~/.agents misses more than half the corpus — the marketplace and
# vendor bundles also live under ~/.gemini, ~/.codex and ~/.npm.
#
# Truncation is not free: `describe` is the single source of the manifest
# entry, the `get_skill` payload description and `preview_line`, and a
# description's tail is its trigger list. A clamped entry looks exactly
# like a short one, so the agent simply never fires the skill for the
# situations enumerated last.
MANIFEST_DESCRIPTION_CHARS = 800


def split_frontmatter(body: str) -> tuple[str, str]:
    """Split a SKILL.md body into `(frontmatter_block, prose)`.

    Both fences are required and both must be exactly `---`. An opening
    `---` with no closing one is a markdown horizontal rule, and so is
    `----` — treating either as frontmatter pulls arbitrary
    `key: value`-shaped prose into the manifest, or silently swallows the
    document's first section.

    Returns `("", body)` when there is no frontmatter, so callers can't
    disagree with each other about whether a block exists.
    """
    lines = (body or "").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if start is None or lines[start].strip() != _FENCE:
        return "", body or ""
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() == _FENCE), None)
    if end is None:
        return "", body or ""
    return ("\n".join(lines[start + 1:end]),
            "\n".join(lines[end + 1:]).lstrip("\n"))


def parse_frontmatter(body: str) -> dict[str, str]:
    """Frontmatter as flat key → string value; `{}` when there is none.

    Real SKILL.md frontmatter is YAML and uses the whole grammar: quoted
    scalars wrapping across lines, `|` / `>` blocks with chomping
    indicators, keys beyond `name` and `description`. Hand-parsing it
    truncated descriptions mid-clause on blank lines inside a block, and
    — worse — let a colon inside a quoted description silently redefine
    `name`, the field that is a skill's identity.

    Unreadable YAML degrades to "no frontmatter" rather than raising: a
    skill listed under a fallback name is recoverable, a SessionStart
    blob that 500s is not.
    """
    block, _ = split_frontmatter(body)
    if not block.strip():
        return {}
    try:
        # `safe_load` blocks arbitrary object construction but not alias
        # expansion: the shared node graph is cheap to load and ruinous to
        # flatten, and the `str(v)` below is what flattens it. A 526-byte
        # block reaches >2 GB. Real SKILL.md frontmatter never uses
        # anchors, and `yaml.parse` streams events without composing them.
        if any(isinstance(ev, yaml.AliasEvent) for ev in yaml.parse(block)):
            return {}
        data = yaml.safe_load(block)
    # RecursionError is not a YAMLError. PyYAML composes nested nodes
    # recursively, so ~500 levels of `[` — a 1 KB block, no anchors, so the
    # alias guard above never sees it — blows the stack instead of raising a
    # parse error. Unhandled, that propagates out of `describe`, and
    # `_pin_skills` calls `describe` on every skill in scope: one such note
    # saved with project=None makes `context()` raise for every project and
    # every session. The stack has unwound by the time this runs, so
    # degrading to "no frontmatter" here is safe and matches the intent
    # stated above.
    except (yaml.YAMLError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v).strip() for k, v in data.items() if v is not None}


def describe(note: Note) -> tuple[str, str]:
    """Manifest identity for a skill note: `(name, description)`.

    Falls back through the note's own fields when frontmatter is missing
    or partial, so a skill saved as plain prose still appears in the
    manifest — degraded but present, rather than invisible.
    """
    fields = parse_frontmatter(note.content or "")
    _, prose = split_frontmatter(note.content or "")
    name = fields.get("name") or _first_heading(prose) or f"skill-{note.id}"
    description = (fields.get("description") or note.description
                   or _first_paragraph(prose) or "(no description)")
    if len(description) > MANIFEST_DESCRIPTION_CHARS:
        description = description[:MANIFEST_DESCRIPTION_CHARS].rstrip() + "…"
    return name, description


_NAME_SEPARATORS = re.compile(r"[\s_]+")


def normalize_name(name: str) -> str:
    """Fold a skill name to its lookup key.

    Names are authored kebab-case, but they get *invoked* in whatever
    shape the caller has to hand: `/code-review` from a slash command,
    `Code Review` read out of prose, `code_review` typed from memory.
    All three name the same skill, so all three fold to one key.

    The fold is deliberately narrow — case, whitespace/underscore
    separators, and the leading slash of the slash-command form. Two
    genuinely different names stay different, which keeps a name lookup
    an exact match rather than a fuzzy one. Near-misses are handled by
    `suggest_names`, where the caller sees them as candidates instead of
    receiving one silently.
    """
    folded = _NAME_SEPARATORS.sub("-", (name or "").strip().lower())
    return folded.strip("/").strip("-")


def find_by_name(notes: Iterable[Note], name: str) -> list[Note]:
    """Every skill note in `notes` whose frontmatter name folds to `name`.

    A list, not a single note, because the name space is only unique per
    scope: a global skill and a project skill may share a name (that is
    how a project overrides a global), and two projects may each define
    their own `deploy`. Resolving that collision needs the caller's scope,
    which lives at the call site — so this reports the collision instead
    of picking for them.
    """
    key = normalize_name(name)
    if not key:
        return []
    return [n for n in notes if normalize_name(describe(n)[0]) == key]


def suggest_names(notes: Iterable[Note], name: str, limit: int = 5) -> list[str]:
    """Authored names close to `name`, for a lookup that found nothing.

    Matched on the folded keys so a typo doesn't compete with case and
    separator noise, but returned in authored form — the caller has to
    retry with a name, and the one they can read is the one that works.
    """
    key = normalize_name(name)
    if not key:
        return []
    by_key: dict[str, str] = {}
    for n in notes:
        authored = describe(n)[0]
        by_key.setdefault(normalize_name(authored), authored)
    close = difflib.get_close_matches(key, list(by_key), n=limit, cutoff=0.6)
    return [by_key[k] for k in close]


# A sentence break must be followed by something that actually starts a
# sentence, and must leave a summary worth reading behind. Two rules
# instead of an abbreviation wordlist: "Handles e.g. retries" fails the
# first (lowercase after the dot), "Handles e.g. Retries" fails the second
# (12 chars is not a summary).
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[\"'`])")
_MIN_SUMMARY_CHARS = 40


def _first_sentence(text: str) -> str:
    for m in _SENTENCE_END.finditer(text):
        if m.start() >= _MIN_SUMMARY_CHARS:
            return text[:m.start()]
    return text


def preview_line(note: Note) -> str:
    """The `preview` column value for a skill note — derived, never generated.

    A SKILL.md states its own name and purpose, so asking a model to
    summarize one spends a request to paraphrase a paraphrase. This reads
    the document's shape instead: `name — <first sentence of description>`.

    First *sentence*, not a truncation, because a description is a trigger
    condition and its convention is "<what it does>. Use when <every
    situation that should fire it>." The trigger list is the long part, and
    it is the part `context()` needs in full — dispatch depends on it. A
    browse card or a search hit wants the summary, and blind truncation
    gives neither: it cuts the trigger list mid-clause.

    The result is as short as the content is — no padding to the budget and
    no ellipsis in the common case. Truncating at 200 made every skill's
    preview exactly 200 characters of chopped trigger list, which told a
    reader nothing except that there was more.

    Safe on both shapes. Measured over 2,740 real SKILL.md descriptions:
    62% carry more than one sentence, and the other 38% are a single
    sentence that yields itself unchanged. Of the multi-sentence ones,
    70 (4.1%) produce a first sentence longer than the 200-char preview
    bound (`classify.PREVIEW_MAX_CHARS`), the longest running 440. So the
    caller still passes the result through `classify.clamp_preview`, and
    that is a backstop which fires in ordinary use — on well-formed
    frontmatter, not only on pathological input and the fallback paths.
    """
    name, description = describe(note)
    return f"{name} — {_first_sentence(description)}"


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


def _first_paragraph(body: str) -> str:
    """The first prose paragraph, joined to one line.

    A paragraph, not a line: the fallback exists to carry a trigger
    condition, and hand-typed markdown wraps. Returning only the first
    line dropped the entire "Use when ..." clause — the one part the
    manifest exists to carry — with no ellipsis to mark the loss.
    """
    para: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if para:
                break
            continue
        if stripped.startswith("#") or _is_rule(stripped):
            if para:
                break
            continue
        para.append(stripped)
    return " ".join(para)


def _is_rule(line: str) -> bool:
    """A markdown thematic break (`---`, `----`, `***`). Skipped when
    hunting for a description: bodies that open with one are exactly the
    case where the fence checks decided there was no frontmatter, and
    returning the break itself as the trigger condition is worse than
    reading past it."""
    bare = line.replace(" ", "")
    return len(bare) >= 3 and set(bare) in ({"-"}, {"*"}, {"_"})
