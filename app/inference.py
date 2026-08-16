"""Phase-3 backfill: scan existing notes for textual cross-references and
propose typed edges for the explicit link graph.

The scan is conservative: strong verb patterns ("supersedes #42", "caused by
#17", "depends on #88") classify precisely; everything else falls back to a
plain `references` edge. False-positive prefixes like `PR #42` and
`issue #42` are skipped so GitHub-style references don't pollute the graph.

The tool is read-only — proposals are returned to the caller, who confirms
each with `link`. That matches the existing consent-gated capture
pattern and lets `create_edge`'s upsert handle idempotency for re-runs.
"""
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from app import db
from app.models import EdgeType


# Verb → edge_type patterns, ordered strongest-first. Each captures the note
# id in group 1. The optional `(?:plan|idea|…)\s+` segment absorbs type hints
# so "supersedes plan #42" and "supersedes #42" both match.
_VERB_PATTERNS: list[tuple[EdgeType, re.Pattern]] = [
    (
        "supersedes",
        re.compile(
            r"\b(?:supersed(?:es?|ing)|replac(?:es?|ing)|obsoletes?)\s+"
            r"(?:(?:plan|idea|memory|note|decision)\s+)?#(\d{1,6})\b",
            re.I,
        ),
    ),
    (
        "implements",
        re.compile(
            r"\b(?:implements?|fulfill(?:s|ed|ing)?)\s+"
            r"(?:(?:plan|idea|spec|memory|note)\s+)?#(\d{1,6})\b",
            re.I,
        ),
    ),
    (
        "caused_by",
        re.compile(
            r"\b(?:caused[- ]by|root[- ]caused[- ]by|root[- ]cause:?\s*)"
            r"\s*(?:(?:memory|note|idea|incident|bug)\s+)?#(\d{1,6})\b",
            re.I,
        ),
    ),
    (
        "bundled_with",
        re.compile(
            r"\b(?:bundled with|shipped with|shipped alongside|bundled_with)\s+"
            r"(?:(?:plan|memory|note)\s+)?#(\d{1,6})\b",
            re.I,
        ),
    ),
    (
        "prerequisite_for",
        re.compile(
            r"\b(?:requires?|depends on|blocked by|prerequisite:?\s*|"
            r"prerequisite_for|builds on)"
            r"\s*(?:(?:plan|memory|note|idea)\s+)?#(\d{1,6})\b",
            re.I,
        ),
    ),
]

# Bare `#N` / `plan #N` references — default to `references`. Group 2 is the id.
_BARE_RE = re.compile(
    r"(?:(?<=^)|(?<=[^\w/#]))"
    # Prose prefixes, not the `NoteType` enum — `note` and `project` are how
    # people write, and both predate the type they resemble. Missing entries
    # here degrade a reference to a *bare* proposal, which `infer_edges`
    # discards unless `include_bare=True`, so the edge silently never forms.
    r"(?:(plan|idea|memory|note|feedback|reference|project"
    r"|feature|task|rule|skill)\s+)?"
    r"#(\d{1,6})\b",
    re.I,
)

# Skip when the match's immediate leading context ends in a GitHub-ish word
# (PR / issue / ticket / bug / commit / pull request / gh). The trailing `\b`
# keeps "pr" from matching inside "pry"/"prom"; the `s?` catches the very
# common "PRs #123, #124" plural list form.
_SKIP_BEFORE_RE = re.compile(
    r"\b(?:prs?|pull\s*requests?|issues?|tickets?|gh|github|bugs?|commits?)\b"
    r"\s*$",
    re.I,
)

# A full PR-list span: "PRs #115, #119, #123" or "PR #131-#157". Any `#N`
# caught inside such a span is treated as a GitHub ref and skipped, so the
# list's tail items aren't misclassified as infoguana note references just
# because the "PRs" cue was far to the left.
_SKIP_SPAN_RE = re.compile(
    r"\b(?:prs?|pull\s*requests?|issues?|tickets?|gh|github|bugs?|commits?)\b"
    r"\s*#\d+"
    r"(?:\s*[,–\-]\s*#?\d+)*",
    re.I,
)


@dataclass
class ProposedEdge:
    from_id: int
    to_id: int
    edge_type: EdgeType
    evidence: str
    source_field: str  # "content" or "description"
    # "verb" (e.g. 'supersedes #42'), "type_hint" ('plan #42'), or "bare"
    # ('#42' with no local cue). Bare matches are the bulk of false positives
    # — PR-list tables etc. — so callers can default to verb+type_hint only.
    signal: str = "bare"

    def key(self) -> tuple[int, int, str]:
        return (self.from_id, self.to_id, self.edge_type)


def _snippet(text: str, start: int, end: int, pad: int = 32) -> str:
    a = max(0, start - pad)
    b = min(len(text), end + pad)
    s = text[a:b].replace("\n", " ").strip()
    if a > 0:
        s = "…" + s
    if b < len(text):
        s = s + "…"
    return s


def _scan_text(
    from_id: int,
    text: str,
    valid_ids: set[int],
    source_field: str,
) -> list[ProposedEdge]:
    if not text:
        return []
    out: list[ProposedEdge] = []
    claimed: list[tuple[int, int]] = []
    skip_spans: list[tuple[int, int]] = [
        (m.start(), m.end()) for m in _SKIP_SPAN_RE.finditer(text)
    ]

    def _in_skip_span(a: int, b: int) -> bool:
        return any(sa <= a and b <= sb for (sa, sb) in skip_spans)

    for edge_type, pat in _VERB_PATTERNS:
        for m in pat.finditer(text):
            if _in_skip_span(m.start(), m.end()):
                continue
            to_id = int(m.group(1))
            if to_id not in valid_ids:
                continue
            # `prerequisite_for` reads "current note X depends on #N" — #N is
            # the prerequisite for X, so flip direction: from_id=N, to_id=X.
            if edge_type == "prerequisite_for":
                src, dst = to_id, from_id
            else:
                src, dst = from_id, to_id
            if src == dst:
                continue
            out.append(
                ProposedEdge(
                    from_id=src,
                    to_id=dst,
                    edge_type=edge_type,
                    evidence=_snippet(text, m.start(), m.end()),
                    source_field=source_field,
                    signal="verb",
                )
            )
            claimed.append((m.start(), m.end()))

    def _already_claimed(a: int, b: int) -> bool:
        return any(not (b <= ca or a >= cb) for (ca, cb) in claimed)

    for m in _BARE_RE.finditer(text):
        if _already_claimed(m.start(), m.end()):
            continue
        if _in_skip_span(m.start(), m.end()):
            continue
        left = text[max(0, m.start() - 20):m.start()]
        if _SKIP_BEFORE_RE.search(left):
            continue
        to_id = int(m.group(2))
        if to_id == from_id or to_id not in valid_ids:
            continue
        signal = "type_hint" if m.group(1) else "bare"
        out.append(
            ProposedEdge(
                from_id=from_id,
                to_id=to_id,
                edge_type="references",
                evidence=_snippet(text, m.start(), m.end()),
                source_field=source_field,
                signal=signal,
            )
        )
    return out


def _existing_edge_keys() -> set[tuple[int, int, str]]:
    return {
        (row[0], row[1], row[2])
        for row in db.get_conn()
        .execute("SELECT from_id, to_id, edge_type FROM edges")
        .fetchall()
    }


def _existing_pairs() -> set[tuple[int, int]]:
    """Directed (from_id, to_id) pairs that already have *any* edge. Used to
    suppress weak `references` proposals once a stronger edge type has been
    recorded for the same pair."""
    return {
        (row[0], row[1])
        for row in db.get_conn()
        .execute("SELECT DISTINCT from_id, to_id FROM edges")
        .fetchall()
    }


def infer_edges(
    project: Optional[str] = None,
    limit: Optional[int] = None,
    note_ids: Optional[Iterable[int]] = None,
    include_bare: bool = False,
) -> list[ProposedEdge]:
    """Scan notes (all, or scoped by project / id list) and return edge
    proposals that aren't already in the graph.

    Proposals are deduped on (from_id, to_id, edge_type). When the same pair
    is matched by both a verb pattern and a bare reference, the verb wins;
    when two verbs match the same span, the first-listed (strongest) wins.

    By default (`include_bare=False`) only high-confidence proposals are
    returned — those with a verb cue ("supersedes #42") or a type hint
    ("plan #42"). Bare `#N` matches are the biggest source of false positives
    (PR changelog tables, issue lists in memory notes), so they're opt-in.
    """
    conn = db.get_conn()
    all_ids = {row[0] for row in conn.execute("SELECT id FROM notes").fetchall()}

    params: list = []
    clauses: list[str] = []
    if project:
        clauses.append("project = ?")
        params.append(project)
    if note_ids is not None:
        ids = list(note_ids)
        if not ids:
            return []
        clauses.append(f"id IN ({','.join('?' * len(ids))})")
        params.extend(ids)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT id, content, description FROM notes{where} ORDER BY id",
        params,
    ).fetchall()

    existing = _existing_edge_keys()
    existing_pairs = _existing_pairs()
    seen: set[tuple[int, int, str]] = set()
    out: list[ProposedEdge] = []
    for row in rows:
        nid = row[0]
        for field, text in (("content", row[1]), ("description", row[2])):
            for p in _scan_text(nid, text or "", all_ids, field):
                if p.signal == "bare" and not include_bare:
                    continue
                k = p.key()
                if k in existing or k in seen:
                    continue
                # Suppress weak `references` proposals when any edge already
                # exists between the same directed pair — a stronger
                # (implements/supersedes/…) edge subsumes the reference.
                if (p.edge_type == "references"
                        and (p.from_id, p.to_id) in existing_pairs):
                    continue
                seen.add(k)
                out.append(p)
                if limit is not None and len(out) >= limit:
                    return out
    return out
