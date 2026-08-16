"""Synthesize a plan's edge subgraph into one comprehensive markdown doc by
spawning `claude -p` over the hydrated notes + linked PR context.

Replaces the per-note file dump — users wanted "one doc
to read" rather than a folder of fragments to parse manually. The agent is
called with no tools: we pre-traverse the subgraph, pre-fetch the PR data,
and hand the synthesis a self-contained prompt.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app import db, github
from app.config import settings
from app.models import Note


log = logging.getLogger(__name__)

DEFAULT_EXPORT_ROOT = Path("./data/exports")
DEFAULT_EXPORT_MODEL = "claude-sonnet-4-6"
SUGGESTED_EXPORT_MODELS = [
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "opus",
    "sonnet",
    "haiku",
]
EXPORT_TIMEOUT_SECONDS = 600


def _clean_model(m: Optional[str]) -> Optional[str]:
    """Same character allowlist as chat.py — keeps shell-metachar injection
    out of the `claude --model <m>` arg. Returns None for invalid input."""
    if not m:
        return None
    m = m.strip()
    if not m or len(m) > 80:
        return None
    if not all(c.isalnum() or c in "-._" for c in m):
        return None
    return m

# Caps to keep the synthesis prompt bounded when a plan has lots of PR chatter.
MAX_PR_CONVERSATION_COMMENTS = 30
MAX_PR_REVIEW_COMMENTS = 30
MAX_COMMENT_BODY_CHARS = 4000
MAX_PR_BODY_CHARS = 8000


SYNTHESIS_PROMPT = """You are producing real engineering documentation for a \
plan in the user's personal knowledge graph — a permanent reference doc, not \
a summary or executive overview.

You will receive: the root plan, connected notes reached via typed edges \
(implements / caused_by / supersedes / references / bundled_with / \
prerequisite_for), and for each linked PR the title, body, merge state, and \
review comments.

Goal: produce ONE comprehensive markdown document that a future engineer \
(possibly the author after months away, possibly a teammate who's never seen \
the project) can rely on as the authoritative record. They should NOT need \
to open the source notes after reading this. The doc should be longer and \
more detailed than the sum of the inputs, not shorter.

## Hard rules

- **Preserve technical substance verbatim.** Wire formats, byte layouts, \
field tables, enums, file paths with line numbers, function names, commit \
hashes, branch names, error codes, configuration keys, command syntax, \
stash identifiers — keep these exact. Do not paraphrase them.
- **Notes with `type=reference` are spec/protocol/table material.** \
Reproduce their structure (tables stay tables, field listings stay field \
listings) rather than narrating them.
- **Code blocks and command-line snippets stay as code blocks.** Do not \
inline them into prose.
- **Synthesis means organizing and reconciling — not compressing.** Merge \
duplicate facts, sequence events chronologically, resolve contradictions \
between notes (and call out which won and why if the notes disagree). Do \
not drop details to make the doc shorter.
- **Cite sources inline.** When a fact comes from a specific note, write \
`(see #149)` or similar so the reader can trace it. Same for PRs: cite by \
number (`PR #144`).

## Structure (use what fits, omit empty sections, add others as needed)

  # <plan title>
  Top: project, status, branch, tags, linked PRs (as a list of links).

  ## Background and motivation
  Why this work exists. Problem, prior state, constraints.

  ## Specification / wire format / data model
  If any input note describes a spec, protocol, schema, or data layout, \
this section reproduces it. Keep tables tabular, keep enums enumerated.

  ## Design decisions
  Each non-trivial decision as its own subsection: what was chosen, what \
the alternatives were, why this won. If a `supersedes` edge points at an \
older decision, document the supersession explicitly.

  ## Implementation
  What was built, by component / file / submodule. Cite PRs and commits. \
If review comments revealed course-corrections, surface them with the \
reviewer's concern and the resolution.

  ## Bugs encountered and resolutions
  For each `caused_by` edge or bug-fix note: symptom, root cause, fix, and \
any prevention measures (e.g. compile-time guards, asserts) added.

  ## Deferred work / open follow-ups
  Anything explicitly deferred, with the reasoning and any stubs left in \
the codebase (file:line). Open design questions go here too.

  ## Operational notes
  Stash identifiers, branch state, restore commands, environment quirks — \
anything an engineer picking this up tomorrow needs to actually run it.

  ## References
  All linked notes (with #id and one-line description) and PR URLs.

## Style

- Length follows content. A plan with three notes might be a page; a plan \
with twenty notes and a 200-comment PR review will be many pages. That's \
fine — completeness over brevity.
- Do not invent facts not present in the inputs. If something is unclear or \
absent, say so explicitly ("the notes do not specify X").
- Do not editorialize ("this elegant solution…"). Engineering docs are \
neutral.

Output ONLY the markdown body. No preamble like "Here is the document"."""


def _slug(text: str, max_len: int = 50) -> str:
    line = ""
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#"):
            line = s.lstrip("#").strip()
            break
        line = s
        break
    line = line.lower()
    line = re.sub(r"[^a-z0-9]+", "-", line).strip("-")
    if not line:
        line = "untitled"
    if len(line) > max_len:
        line = line[:max_len].rstrip("-")
    return line


def _ts_compact(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y%m%d-%H%M%S")


def _truncate(s: str, n: int) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n].rstrip() + "\n[...truncated]"


_PR_URL_RE = re.compile(r"https?://github\.com/([^/]+/[^/]+)/pull/(\d+)")


def _parse_pr_url(url: str) -> Optional[tuple[str, int]]:
    m = _PR_URL_RE.match(url.strip())
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _fetch_pr_payload(url: str) -> Optional[dict]:
    parsed = _parse_pr_url(url)
    if not parsed:
        log.warning("export: cannot parse PR url %r", url)
        return None
    repo, num = parsed
    try:
        pr = github.get_pr(repo, num)
        comments = github.list_pr_comments(
            repo, num,
            limit=max(MAX_PR_CONVERSATION_COMMENTS, MAX_PR_REVIEW_COMMENTS),
        )
    except Exception:
        log.exception("export: failed to fetch PR %s", url)
        return None
    return {
        "url": url,
        "pr": pr,
        "conversation": (comments.get("conversation") or [])[:MAX_PR_CONVERSATION_COMMENTS],
        "review": (comments.get("review") or [])[:MAX_PR_REVIEW_COMMENTS],
    }


def _build_prompt(
    root: Note,
    entries: list[dict],
    edges: list[dict],
    pr_payloads: list[dict],
) -> str:
    lines: list[str] = []
    lines.append(f"# Root plan: #{root.id}")
    lines.append("")
    lines.append(f"- type: {root.type}")
    lines.append(f"- project: {root.project or '(unscoped)'}")
    if root.type == "plan":
        lines.append(f"- status: {root.status or 'unknown'}")
    lines.append(f"- tags: {', '.join(root.tags) if root.tags else '(none)'}")
    if root.linked_prs:
        lines.append(f"- linked PRs: {', '.join(root.linked_prs)}")
    lines.append(f"- created_at: {root.created_at.isoformat()}")
    lines.append(f"- updated_at: {root.updated_at.isoformat()}")
    lines.append("")
    lines.append("## Root content")
    lines.append("")
    lines.append(root.content.rstrip())
    lines.append("")

    non_root = [e for e in entries if not e["is_root"]]
    if non_root:
        lines.append("## Connected notes")
        lines.append("")
        for e in non_root:
            n: Note = e["note"]
            tag_str = f", tags={','.join(n.tags)}" if n.tags else ""
            status_str = f", status={n.status}" if n.type == "plan" and n.status else ""
            lines.append(
                f"### #{n.id} ({n.type}, hops={e['hops']}{status_str}{tag_str})"
            )
            lines.append("")
            lines.append(n.content.rstrip())
            lines.append("")

    if edges:
        lines.append("## Edges in subgraph")
        lines.append("")
        for ed in edges:
            lines.append(
                f"- #{ed['from_id']} —[{ed['edge_type']}]→ #{ed['to_id']}"
            )
        lines.append("")

    if pr_payloads:
        lines.append("## Linked pull requests")
        lines.append("")
        for p in pr_payloads:
            pr = p["pr"]
            lines.append(f"### {p['url']}")
            lines.append(f"- title: {pr.get('title')}")
            lines.append(
                f"- state: {pr.get('state')}, merged: {pr.get('merged')}, "
                f"author: @{pr.get('author')}"
            )
            if pr.get("body"):
                lines.append("")
                lines.append("Body:")
                lines.append("")
                lines.append(_truncate(pr["body"], MAX_PR_BODY_CHARS))
            conv = p.get("conversation") or []
            if conv:
                lines.append("")
                lines.append("Conversation comments:")
                for c in conv:
                    body = _truncate(c.get("body", ""), MAX_COMMENT_BODY_CHARS)
                    lines.append(f"- @{c.get('author')}: {body}")
            rev = p.get("review") or []
            if rev:
                lines.append("")
                lines.append("Review comments:")
                for c in rev:
                    where = ""
                    if c.get("path"):
                        where = f" ({c['path']}:{c.get('line')})"
                    body = _truncate(c.get("body", ""), MAX_COMMENT_BODY_CHARS)
                    lines.append(f"- @{c.get('author')}{where}: {body}")
            lines.append("")

    return "\n".join(lines)


def _spawn_claude(prompt: str, model: str, timeout: int) -> str:
    bin_path = shutil.which(settings.claude_bin)
    if not bin_path:
        raise RuntimeError(
            f"claude CLI '{settings.claude_bin}' not found on PATH"
        )
    cmd = [
        bin_path, "-p",
        "--model", model,
        "--output-format", "json",
        "--tools", "",
        "--allowedTools", "",
        "--append-system-prompt", SYNTHESIS_PROMPT,
        prompt,
    ]
    env = {**os.environ, "IS_SANDBOX": "1"}
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env=env, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude -p timed out after {timeout}s")
    if proc.returncode != 0:
        err = (proc.stderr or "")[:500]
        raise RuntimeError(f"claude -p exited {proc.returncode}: {err}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"failed to parse claude json: {e}: {proc.stdout[:500]}"
        )
    text = (payload.get("result") or "").strip()
    if not text:
        raise RuntimeError("claude returned an empty doc")
    return text


def export_subgraph(
    start_id: int,
    edge_types: Optional[list[str]] = None,
    depth: int = 3,
    direction: str = "both",
    confirmed_only: bool = True,
    out_dir: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Walk the explicit edges from `start_id`, fetch any linked PRs, and
    spawn `claude -p` to synthesize a single comprehensive markdown doc.

    `model` selects which Claude model does the synthesis (e.g. opus for
    more thorough writing, haiku for speed). Defaults to
    DEFAULT_EXPORT_MODEL. Validated against the same character allowlist as
    the chat feature.

    Writes the doc to ./data/exports/<type>-<id>-<slug>-<UTC-stamp>.md (or
    under `out_dir`). Returns:
      {path, filename, model, node_count, edge_count, pr_count,
       skipped_unconfirmed}.

    Raises ValueError on bad input (unknown start_id, bad direction, bad
    model). Raises RuntimeError if the agent call fails.
    """
    if direction not in {"out", "in", "both"}:
        raise ValueError(f"direction must be out|in|both, got {direction!r}")
    if depth < 0:
        raise ValueError(f"depth must be >= 0, got {depth}")
    chosen_model = _clean_model(model) if model else DEFAULT_EXPORT_MODEL
    if chosen_model is None:
        raise ValueError(f"invalid model {model!r}")

    root = db.get_note(start_id)
    if root is None:
        raise ValueError(f"start_id {start_id} not found")

    sub = db.traverse_edges(
        start_id, depth=depth, direction=direction, edge_types=edge_types,
    )
    raw_edges: list[dict] = sub["edges"]
    nodes: list[dict] = sub["nodes"]

    skipped = 0
    if confirmed_only:
        kept: list[dict] = []
        for e in raw_edges:
            edges_for = db.list_edges_for(
                e["from_id"], direction="out", edge_types=[e["edge_type"]],
            )
            match = next(
                (x for x in edges_for
                 if x.to_id == e["to_id"] and x.edge_type == e["edge_type"]),
                None,
            )
            if match is None or not match.confirmed_by_user:
                skipped += 1
                continue
            kept.append(e)
        if kept != raw_edges:
            reachable = {start_id}
            changed = True
            while changed:
                changed = False
                for e in kept:
                    if e["from_id"] in reachable and e["to_id"] not in reachable:
                        reachable.add(e["to_id"])
                        changed = True
                    if e["to_id"] in reachable and e["from_id"] not in reachable:
                        reachable.add(e["from_id"])
                        changed = True
            nodes = [n for n in nodes if n["note_id"] in reachable]
        raw_edges = kept

    entries: list[dict] = []
    for n in nodes:
        note = db.get_note(n["note_id"])
        if note is None:
            continue
        entries.append({
            "note": note,
            "hops": n["hops"],
            "is_root": note.id == start_id,
        })

    pr_payloads: list[dict] = []
    seen_urls: set[str] = set()
    for entry in entries:
        n = entry["note"]
        if n.type != "plan" or not n.linked_prs:
            continue
        for url in n.linked_prs:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            payload = _fetch_pr_payload(url)
            if payload is not None:
                pr_payloads.append(payload)

    prompt = _build_prompt(root, entries, raw_edges, pr_payloads)
    doc_text = _spawn_claude(
        prompt, model=chosen_model, timeout=EXPORT_TIMEOUT_SECONDS,
    )

    target_dir = Path(out_dir) if out_dir else DEFAULT_EXPORT_ROOT
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = (
        f"{root.type}-{root.id}-{_slug(root.content)}-{_ts_compact()}.md"
    )
    target = target_dir / filename
    target.write_text(doc_text)

    return {
        "path": str(target),
        "filename": filename,
        "model": chosen_model,
        "node_count": len(entries),
        "edge_count": len(raw_edges),
        "pr_count": len(pr_payloads),
        "skipped_unconfirmed": skipped,
    }
