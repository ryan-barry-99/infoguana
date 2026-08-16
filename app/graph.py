"""Knowledge graph over infoguana:

- note nodes  (semantic similarity edges between them)
- project nodes (each note is linked to its project node)
- tag nodes   (each note is linked to each of its tags, weighted by IDF)

Plus a `build_context(project, budget_tokens)` BFS that produces a compact,
token-budgeted subgraph suitable for feeding to an MCP-consuming agent at
task start — replaces per-project CLAUDE.md dumps with *just the relevant
memories*.

Node IDs are namespaced strings: "note:42", "project:infoguana", "tag:docker".
"""
import heapq
import json
import math
import struct
from dataclasses import dataclass, field
from typing import Iterable, Optional

import igraph as ig
import numpy as np

from app import db, duedate
from app.models import Note


# Render coordinates land in roughly [-LAYOUT_SCALE, LAYOUT_SCALE]; collision
# relaxation runs in the same space so node radii (in pixels) line up with
# the post-zoom rendering. Generous so the FR layout has enough room to
# separate disks once collision relaxation runs — the client zoom-to-fits.
LAYOUT_SCALE = 750.0


_DIAMOND_ENCLOSE = 0.8 * 1.4142135623730951  # rect rotated 45°, side = 1.6r


def _layout_node_radius(n: dict) -> float:
    """Pixel radius for collision relaxation — must match graph.html's
    nodeEncloseRadius. Returns the smallest circle that contains the rendered
    shape so non-circle nodes (project + feature diamonds, plan/task
    triangles) don't visually overlap their neighbors at the corners."""
    if n["kind"] == "project":
        base = 11.0 + (max(n.get("size") or 0, 0) ** 0.5) * 1.8
        return base * _DIAMOND_ENCLOSE
    if n["kind"] == "tag":
        return 2.0 + float(n.get("weight") or 0.5) * 2.0
    base = 5.0 + (max(n.get("degree") or 0, 0) ** 0.5) * 2.0
    if n.get("type") == "feature":
        return base * _DIAMOND_ENCLOSE
    # plan/task triangles draw with vertices at distance r — base already
    # encloses them. idea/memory/feedback/reference/unsorted are circles.
    return base


def _relax_collisions(
    positions: np.ndarray,
    radii: np.ndarray,
    *,
    iterations: int = 60,
    padding: float = 4.0,
) -> np.ndarray:
    """Push overlapping disks apart until no overlaps remain (or we hit the
    iteration cap). Position-correcting Gauss-Seidel pass — the same approach
    d3.forceCollide uses, but run once at layout time instead of every tick."""
    n = positions.shape[0]
    if n == 0:
        return positions
    sum_r = radii[:, None] + radii[None, :] + padding
    for _ in range(iterations):
        delta = positions[:, None, :] - positions[None, :, :]
        dist2 = (delta * delta).sum(-1) + 1e-6
        np.fill_diagonal(dist2, 1e9)
        dist = np.sqrt(dist2)
        overlap = dist < sum_r
        if not overlap.any():
            break
        depth = np.where(overlap, (sum_r - dist) * 0.5, 0.0)
        correction = (delta * (depth / dist)[:, :, None]).sum(axis=1)
        positions = positions + correction
    return positions


# Confirmed explicit edges (from the `edges` table — implements, supersedes,
# caused_by, …) outrank semantic-similarity edges in retrieval. Anything
# > 1.0 is enough to dominate the strongest similarity edge; 1.2 gives a
# 20% boost without making explicit chains explode in score.
EXPLICIT_EDGE_WEIGHT = 1.2

# `supersedes` is the one explicit edge whose semantics are *anti-symmetric*
# for retrieval: from_id is the replacement, to_id is the stale note it
# obsoletes (new → old). Routing a reader from the stale note up to its
# replacement is exactly what the edge is for, so that direction keeps the
# premium weight. Routing the other way — replacement → stale — would hand
# the best edge in the graph to knowledge that was explicitly marked
# outdated, letting it outrank fresher notes. We keep the link reachable
# (so the `via` path still records `edge:supersedes:…` and a reader chasing
# history can follow it) but at a sub-similarity penalty so it never wins
# routing. All other edge types are navigationally symmetric and stay
# undirected at the premium weight.
SUPERSEDED_EDGE_WEIGHT = 0.5


# --- helpers ---------------------------------------------------------------


def _unpack_vec(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"{dim}f", blob))


def _approx_tokens(text: str) -> int:
    """Rough heuristic: ~4 chars/token for English. Good enough for budgeting."""
    return max(1, len(text or "") // 4)


def _note_tokens(n: Note, full: bool = False) -> int:
    """Budget estimate for a note. Default is preview-mode: the
    haiku preview + description. With `full=True`, sizes against the actual
    body — used only by the bounded `expand_top` slot, never as the default,
    so a greedy caller can't reintroduce the full-mode regression."""
    from app import classify  # avoid import cycle on module load
    if full:
        body = (n.content or "") + "\n" + (n.description or "")
    else:
        body = (n.preview or classify.derive_fallback_preview(n.content or "")) \
               + "\n" + (n.description or "")
    return _approx_tokens(body) + 20


# --- node dicts ------------------------------------------------------------


def _note_node(n: Note) -> dict:
    # Keep newlines intact so the tooltip's markdown renderer can show a
    # header / first paragraph as a real title block instead of mashing
    # everything onto one line. Truncate at ~240 chars and prefer to cut
    # at a paragraph break when one is nearby — avoids slicing mid-sentence.
    raw = (n.content or n.description or "").strip()
    if len(raw) > 240:
        head = raw[:240]
        last_break = head.rfind("\n\n")
        preview = (raw[:last_break].rstrip() + "\n\n…") if last_break >= 80 \
            else head.rstrip() + "…"
    else:
        preview = raw
    return {
        "id": f"note:{n.id}",
        "kind": "note",
        "note_id": n.id,
        "type": n.type,
        "project": n.project,
        "tags": n.tags,
        "preview": preview or "(empty note)",
        "has_image": any((a.mime_type or "").startswith("image/") for a in n.attachments),
        "created_at": n.created_at.isoformat(),
        "status": n.status,
        "linked_prs": n.linked_prs,
        "due_date": n.due_date,
    }


def _project_node(name: str, size: int) -> dict:
    return {"id": f"project:{name}", "kind": "project", "name": name, "size": size}


def _tag_node(name: str, df: int, idf: float, weight: float) -> dict:
    return {
        "id": f"tag:{name}",
        "kind": "tag",
        "name": name,
        "df": df,
        "idf": round(idf, 4),
        "weight": round(weight, 4),
    }


# --- idf over tags ---------------------------------------------------------


def _compute_tag_stats(conn) -> tuple[dict[str, int], int]:
    """Return (df_per_tag, total_notes_with_tags)."""
    df: dict[str, int] = {}
    total = 0
    for row in conn.execute("SELECT tags FROM notes").fetchall():
        try:
            tags = json.loads(row[0] or "[]")
        except Exception:
            tags = []
        if tags:
            total += 1
        for t in set(tags):
            df[t] = df.get(t, 0) + 1
    return df, total


def _tag_weights(df: dict[str, int], N: int) -> dict[str, tuple[float, float]]:
    """Return {tag: (idf, normalized_weight)}.
    Uses smoothed idf = log((1+N)/(1+df)) + 1.
    Normalizes weight to [0.15, 1.0] so very-common tags still have a faint edge.
    """
    if not df:
        return {}
    idfs = {t: math.log((1 + N) / (1 + d)) + 1.0 for t, d in df.items()}
    mx = max(idfs.values())
    mn = min(idfs.values())
    span = mx - mn or 1.0
    out: dict[str, tuple[float, float]] = {}
    for t, idf in idfs.items():
        norm = (idf - mn) / span
        out[t] = (idf, 0.15 + norm * 0.85)
    return out


# --- layout (server-side Fruchterman-Reingold) ----------------------------


# Cached layouts keyed by (params, dataset signature). Layout is the slow
# step (~0.4s for ~1000 nodes); the rest of build_graph is ~0.07s. The cache
# is process-local — single-user, single-process server, so nothing fancier
# is warranted.
_LAYOUT_CACHE: dict[tuple, dict[str, tuple[float, float]]] = {}


def _layout_signature(conn) -> tuple:
    """Coarse fingerprint of the graph-affecting state in the DB.

    Same signature ⇒ same nodes/edges ⇒ layout can be reused. Captures table
    counts plus the latest mutation timestamp from each table so any write
    invalidates the cache. Cheap (three small SELECTs).
    """
    n_notes, n_max_updated = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM notes"
    ).fetchone()
    n_edges, e_max_created = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(created_at), '') FROM edges"
    ).fetchone()
    n_links, l_max_created = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(created_at), '') FROM links"
    ).fetchone()
    return (n_notes, n_max_updated, n_edges, e_max_created, n_links, l_max_created)


def compute_layout(
    nodes: list[dict],
    links: list[dict],
    *,
    niter: int = 500,
    seed: int = 42,
) -> dict[str, tuple[float, float]]:
    """Static layout: Fruchterman-Reingold via igraph, then a collision
    relaxation pass so the rendered disks don't visually overlap. Returns
    {node_id: (x, y)} in pixel space (centered at the origin, spanning
    roughly ±LAYOUT_SCALE)."""
    if not nodes:
        return {}
    idx = {n["id"]: i for i, n in enumerate(nodes)}
    edge_pairs: list[tuple[int, int]] = []
    weights: list[float] = []
    for l in links:
        a, b = idx.get(l["source"]), idx.get(l["target"])
        if a is None or b is None or a == b:
            continue
        edge_pairs.append((a, b))
        weights.append(max(float(l.get("weight") or 0.0), 0.01))

    g = ig.Graph(n=len(nodes), edges=edge_pairs, directed=False)
    # Deterministic layout — same DB state ⇒ same coordinates, so the cache
    # signature actually means something visually.
    ig.set_random_number_generator(__import__("random").Random(seed))
    g_layout = g.layout_fruchterman_reingold(
        weights=weights or None,
        niter=niter,
    )

    coords = np.asarray(g_layout.coords, dtype=np.float64)
    # Center on origin, scale to pixel space.
    cx = (coords[:, 0].min() + coords[:, 0].max()) / 2.0
    cy = (coords[:, 1].min() + coords[:, 1].max()) / 2.0
    span = max(np.ptp(coords[:, 0]), np.ptp(coords[:, 1]), 1e-6) / 2.0
    coords = (coords - np.array([cx, cy])) / span * LAYOUT_SCALE

    # Relax overlaps so tag clusters and dense note neighborhoods don't
    # render on top of each other.
    radii = np.array([_layout_node_radius(n) for n in nodes], dtype=np.float64)
    coords = _relax_collisions(coords, radii)

    return {n["id"]: (float(coords[i, 0]), float(coords[i, 1])) for i, n in enumerate(nodes)}


# --- full graph (for /graph viz) -------------------------------------------


def build_graph(
    per_node_k: int = 3,
    distance_threshold: float = 1.2,
    project_filter: Optional[str] = None,
    include_projects: bool = True,
    include_tags: bool = True,
) -> dict:
    conn = db.get_conn()
    where = []
    params: list = []
    if project_filter:
        where.append("project = ?")
        params.append(project_filter)
    sql = "SELECT id FROM notes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    note_ids = [r[0] for r in conn.execute(sql, params).fetchall()]

    notes_by_id: dict[int, Note] = {}
    nodes: list[dict] = []
    degree: dict[str, int] = {}
    project_sizes: dict[str, int] = {}
    seen_tags: set[str] = set()

    for nid in note_ids:
        n = db.get_note(nid)
        if not n:
            continue
        notes_by_id[nid] = n
        nodes.append(_note_node(n))
        degree[f"note:{nid}"] = 0
        if n.project and include_projects:
            project_sizes[n.project] = project_sizes.get(n.project, 0) + 1
        if include_tags:
            seen_tags.update(n.tags)

    df, N = _compute_tag_stats(conn)
    tag_w = _tag_weights(df, N)

    id_set = set(note_ids)
    edges: dict[tuple[str, str], dict] = {}

    def add_edge(a: str, b: str, weight: float, kind: str, distance: Optional[float] = None):
        key = tuple(sorted((a, b)))  # type: ignore[assignment]
        existing = edges.get(key)
        if existing is None or weight > existing["weight"]:
            edges[key] = {  # type: ignore[assignment]
                "source": key[0], "target": key[1],
                "weight": round(float(weight), 4),
                "kind": kind,
                **({"distance": round(float(distance), 4)} if distance is not None else {}),
            }

    # note-to-note semantic similarity
    for nid in note_ids:
        row = conn.execute(
            "SELECT embedding FROM notes_vec WHERE note_id = ?", (nid,)
        ).fetchone()
        if not row:
            continue
        vec = _unpack_vec(row[0], db.EMBED_DIM)
        neighbors = db.vector_search(vec, limit=per_node_k + 1)
        for neighbor, dist in neighbors:
            if neighbor.id == nid or neighbor.id not in id_set:
                continue
            if dist > distance_threshold:
                continue
            weight = max(0.0, 1.0 - float(dist))
            add_edge(f"note:{nid}", f"note:{neighbor.id}", weight, "similar", distance=float(dist))

    # explicit links — legacy `links` table (untyped) and the typed `edges`
    # table that backs link / traverse. Only confirmed edges
    # contribute to retrieval-influencing weight.
    for row in conn.execute("SELECT from_id, to_id, relation FROM links").fetchall():
        if row[0] not in id_set or row[1] not in id_set:
            continue
        add_edge(f"note:{row[0]}", f"note:{row[1]}", 1.0, f"link:{row[2]}")
    for row in conn.execute(
        "SELECT from_id, to_id, edge_type FROM edges WHERE confirmed_by_user = 1"
    ).fetchall():
        if row[0] not in id_set or row[1] not in id_set:
            continue
        add_edge(f"note:{row[0]}", f"note:{row[1]}",
                 EXPLICIT_EDGE_WEIGHT, f"edge:{row[2]}")

    # project nodes + note↔project edges
    if include_projects:
        for proj, count in project_sizes.items():
            nodes.append(_project_node(proj, count))
            degree[f"project:{proj}"] = 0
        for n in notes_by_id.values():
            if n.project:
                add_edge(f"note:{n.id}", f"project:{n.project}", 1.0, "in_project")

    # Synthetic "tendons" from each global note (project=None) to every
    # project node. Globals have no in_project edge so they render as
    # orphans even though they semantically apply *everywhere*.
    # Tendons are pure rendering: they're returned separately so they
    # don't influence layout (otherwise globals collapse onto the
    # project centroid) and don't inflate the global note's `degree`
    # (which would oversize its disk for a connection that's
    # implicit, not earned).
    tendons: list[dict] = []
    if include_projects and project_sizes:
        for n in notes_by_id.values():
            if n.project:
                continue
            for proj in project_sizes:
                tendons.append({
                    "source": f"note:{n.id}",
                    "target": f"project:{proj}",
                    "kind": "global_to_project",
                })

    # tag nodes + note↔tag edges
    if include_tags:
        for t in seen_tags:
            idf, w = tag_w.get(t, (1.0, 0.5))
            nodes.append(_tag_node(t, df.get(t, 0), idf, w))
            degree[f"tag:{t}"] = 0
        for n in notes_by_id.values():
            for t in n.tags:
                _, w = tag_w.get(t, (1.0, 0.5))
                add_edge(f"note:{n.id}", f"tag:{t}", w, "has_tag")

    for key in edges.keys():
        degree[key[0]] = degree.get(key[0], 0) + 1
        degree[key[1]] = degree.get(key[1], 0) + 1

    for n in nodes:
        n["degree"] = degree.get(n["id"], 0)

    link_list = list(edges.values())

    cache_key = (
        project_filter, per_node_k, round(float(distance_threshold), 4),
        include_projects, include_tags, _layout_signature(conn),
    )
    coords = _LAYOUT_CACHE.get(cache_key)
    if coords is None:
        coords = compute_layout(nodes, link_list)
        _LAYOUT_CACHE.clear()  # one slot per server — keep memory bounded
        _LAYOUT_CACHE[cache_key] = coords
    for n in nodes:
        x, y = coords.get(n["id"], (0.0, 0.0))
        n["x"], n["y"] = x, y

    return {"nodes": nodes, "links": link_list, "tendons": tendons}


# --- BFS context retrieval -------------------------------------------------


class _Adjacency:
    """Lazy adjacency view of infoguana graph, scoped per call."""

    def __init__(self) -> None:
        conn = db.get_conn()
        self.conn = conn
        df, N = _compute_tag_stats(conn)
        self._tag_w = _tag_weights(df, N)
        self._notes_by_project: dict[str, list[int]] = {}
        self._notes_by_tag: dict[str, list[int]] = {}
        for row in conn.execute("SELECT id, project, tags FROM notes").fetchall():
            nid = row[0]
            if row[1]:
                self._notes_by_project.setdefault(row[1], []).append(nid)
            try:
                for t in json.loads(row[2] or "[]"):
                    self._notes_by_tag.setdefault(t, []).append(nid)
            except Exception:
                pass
        # Eagerly load confirmed explicit edges as adjacency. Most edge types
        # are navigationally symmetric — if A `implements` B, hopping
        # note->note in either direction should follow the link — so we add
        # both endpoints. The third tuple element records whether this entry
        # walks the edge in its stored direction (`forward=True`: we're at
        # from_id heading to to_id) so `neighbors()` can special-case the one
        # edge type whose retrieval semantics are anti-symmetric (supersedes).
        # Edge type goes into the `kind` so the via path records why we got
        # here (`edge:implements:note:42`).
        self._explicit_edges: dict[int, list[tuple[int, str, bool]]] = {}
        for row in conn.execute(
            "SELECT from_id, to_id, edge_type FROM edges "
            "WHERE confirmed_by_user = 1"
        ).fetchall():
            self._explicit_edges.setdefault(row[0], []).append((row[1], row[2], True))
            self._explicit_edges.setdefault(row[1], []).append((row[0], row[2], False))

    def neighbors(self, node_id: str, per_node_k: int = 4) -> list[tuple[str, float, str]]:
        """Return (neighbor_id, edge_weight, kind) triples."""
        out: list[tuple[str, float, str]] = []
        if node_id.startswith("note:"):
            nid = int(node_id.split(":", 1)[1])
            note = db.get_note(nid)
            if not note:
                return []
            # to project
            if note.project:
                out.append((f"project:{note.project}", 1.0, "in_project"))
            # to tags (weighted by IDF)
            for t in note.tags:
                _, w = self._tag_w.get(t, (1.0, 0.5))
                out.append((f"tag:{t}", w, "has_tag"))
            # to semantic neighbors
            row = self.conn.execute(
                "SELECT embedding FROM notes_vec WHERE note_id = ?", (nid,)
            ).fetchone()
            if row:
                vec = _unpack_vec(row[0], db.EMBED_DIM)
                for neighbor, dist in db.vector_search(vec, limit=per_node_k + 1):
                    if neighbor.id == nid:
                        continue
                    w = max(0.0, 1.0 - float(dist))
                    out.append((f"note:{neighbor.id}", w, "similar"))
            # to explicit-edge neighbors (confirmed only) — these win against
            # similarity because the user/agent deliberately wired them. The
            # exception is the replacement → stale direction of a `supersedes`
            # edge (forward = from_id→to_id, i.e. new→old): we deliberately
            # de-rank it so the walk doesn't route readers *into* superseded
            # material at premium weight. The reverse direction (stale→
            # replacement) keeps the boost — that's the edge doing its job.
            for neighbor_id, edge_type, forward in self._explicit_edges.get(nid, ()):
                if edge_type == "supersedes" and forward:
                    weight = SUPERSEDED_EDGE_WEIGHT
                else:
                    weight = EXPLICIT_EDGE_WEIGHT
                out.append((f"note:{neighbor_id}", weight, f"edge:{edge_type}"))
        elif node_id.startswith("project:"):
            name = node_id.split(":", 1)[1]
            for nid in self._notes_by_project.get(name, []):
                out.append((f"note:{nid}", 1.0, "in_project"))
        elif node_id.startswith("tag:"):
            name = node_id.split(":", 1)[1]
            _, w = self._tag_w.get(name, (1.0, 0.5))
            for nid in self._notes_by_tag.get(name, []):
                out.append((f"note:{nid}", w, "has_tag"))
        return out


@dataclass
class _ContextState:
    """Shared scratch space for the three build_context phases. Each phase
    appends to its own bucket (`rules`, `active_plans`, `selected`) and
    decrements the shared `total_tokens` / `expand_remaining` budgets."""
    project: str
    budget_tokens: int
    type_filter: Optional[set[str]]
    expand_remaining: int
    total_tokens: int = 0
    seen_note_ids: set[int] = field(default_factory=set)
    rules: list[dict] = field(default_factory=list)
    active_plans: list[dict] = field(default_factory=list)
    selected: list[dict] = field(default_factory=list)

    def can_fit(self, tokens: int) -> bool:
        return self.total_tokens + tokens <= self.budget_tokens

    def emit_body(self, n: Note, full: bool) -> dict:
        """Body fields for an emitted note. With `full=False` (default),
        returns the haiku preview (or fallback) under `content` plus
        `preview: True`. With `full=True` (used for the first `expand_top`
        slots), returns the actual content."""
        if full:
            return {"content": n.content, "description": n.description}
        from app import classify  # local to avoid module-load import cycle
        body = n.preview or classify.derive_fallback_preview(n.content or "")
        return {"content": body, "description": n.description, "preview": True}


def _pin_rules(state: _ContextState) -> None:
    """Pin `rule` notes (standing constraints) above everything else. Rules
    are the user's hard always-true instructions — emit with full bodies
    (not previews) since they're short and must be read, not triaged. Rules
    don't carry a lifecycle, so no status/due-date handling. They count
    against the budget normally so they can't crowd out everything else,
    but they're claimed first so a tight budget doesn't drop them.

    Two scopes: rules with `project=None` are *global* (apply in every
    project's context), rules with `project=<this>` are project-specific.
    Globals come first so they read like the user's overarching norms,
    then the project-specific rules layer on top."""
    if state.type_filter is not None and "rule" not in state.type_filter:
        return
    all_rules = db.list_notes(type="rule", limit=200)
    scoped_rules = [
        r for r in all_rules if r.project is None or r.project == state.project
    ]
    # Global first, then project-specific. Within each bucket, newest first.
    scoped_rules.sort(
        key=lambda r: (0 if r.project is None else 1, -r.created_at.timestamp())
    )
    for rule in scoped_rules:
        tokens = _note_tokens(rule, full=True)
        if not state.can_fit(tokens):
            break
        state.rules.append({
            "id": rule.id,
            "content": rule.content,
            "description": rule.description,
            "type": rule.type,
            "project": rule.project,
            "scope": "global" if rule.project is None else "project",
            "tags": rule.tags,
            "created_at": rule.created_at.isoformat(),
            "updated_at": rule.updated_at.isoformat(),
            "tokens_est": tokens,
        })
        state.total_tokens += tokens
        state.seen_note_ids.add(rule.id)


def _plan_entry(state: _ContextState, plan: Note, full: bool, tokens: int,
                bundled_under: Optional[int] = None) -> dict:
    entry = {
        "id": plan.id,
        **state.emit_body(plan, full=full),
        "type": plan.type,
        "project": plan.project,
        "tags": plan.tags,
        "created_at": plan.created_at.isoformat(),
        "updated_at": plan.updated_at.isoformat(),
        "status": plan.status,
        "linked_prs": plan.linked_prs,
        "tokens_est": tokens,
    }
    if plan.due_date:
        disp = duedate.display(plan.due_date)
        entry["due_date"] = plan.due_date
        if disp:
            entry["due_state"] = disp["bucket"]
            entry["due_in_days"] = disp["days_until"]
    if bundled_under is not None:
        entry["bundled_under"] = bundled_under
    return entry


def _ref_entry(sub: Note, parent_id: int) -> dict:
    """One-line reference for a not_started bundled subtask. ~20-30 tokens
    vs ~400 for a full body — the agent sees the roadmap structure without
    paying for unread detail."""
    ref_text = (sub.preview or sub.description
                or (sub.content or "").splitlines()[0:1] or [""])
    if isinstance(ref_text, list):
        ref_text = ref_text[0] if ref_text else ""
    return {
        "id": sub.id,
        "type": sub.type,
        "project": sub.project,
        "tags": sub.tags,
        "status": sub.status,
        "ref_only": True,
        "preview": True,
        "content": ref_text,
        "description": sub.description,
        "bundled_under": parent_id,
        "tokens_est": _approx_tokens(ref_text) + 10,
    }


def _pin_active_work(state: _ContextState) -> None:
    """Pin tracked work (plans + tasks) for this project to the top of the
    context. Callers coming back to work weeks later should see unfinished
    plans/tasks (and especially overdue ones) before the general BFS budget
    runs out on tangential notes. db.list_plans orders overdue first, then
    upcoming-by-due-date, then in-progress without a date.

    We pin both `pending` and `not_started` when they have a due date — an
    overdue not_started item is exactly the thing the user would want to
    see first.

    A plan's bundled tasks render inline under the parent
    instead of as separate pins. Active subtask (status='pending') gets a
    full body; future subtasks (status='not_started') render as one-line
    refs; completed subtasks are skipped."""
    tf = state.type_filter
    if tf is not None and "plan" not in tf and "task" not in tf:
        return

    today_str = duedate.today_local().isoformat()
    candidates: list[Note] = []
    # In-progress (pending) work — always pin, regardless of date.
    candidates.extend(db.list_plans(project=state.project, status="pending",
                                    today=today_str))
    # Not-started work *with a due date* — pin so overdue queued items
    # surface. Items without dates stay out to avoid firehosing the
    # context with everything not yet started.
    for p in db.list_plans(project=state.project, status="not_started",
                           today=today_str):
        if p.due_date:
            candidates.append(p)

    # Dedup + apply type filter up-front so the rollup logic below can
    # check membership without re-running the filter.
    seen: set[int] = set()
    deduped: list[Note] = []
    for p in candidates:
        if p.id in seen:
            continue
        seen.add(p.id)
        if tf is not None and p.type not in tf:
            continue
        deduped.append(p)
    pinnable_ids = {p.id for p in deduped}

    for plan in deduped:
        # A task whose parent plan will pin renders inline under the
        # parent, not as a top-level pin. Standalone tasks (no parent, or
        # parent in different project / not pinning) fall through.
        if plan.type == "task":
            parent = db.bundled_parent_of(plan.id)
            if (parent is not None
                    and parent.id in pinnable_ids
                    and parent.project == state.project):
                continue

        full = state.expand_remaining > 0
        tokens = _note_tokens(plan, full=full)
        if not state.can_fit(tokens):
            break
        state.active_plans.append(_plan_entry(state, plan, full, tokens))
        state.total_tokens += tokens
        state.seen_note_ids.add(plan.id)
        if full:
            state.expand_remaining -= 1

        if plan.type != "plan":
            continue
        # Inline rollup of bundled subtasks.
        for sub in db.bundled_tasks_of(plan.id):
            if sub.status == "complete":
                continue
            if sub.status == "pending":
                sub_tokens = _note_tokens(sub, full=True)
                if not state.can_fit(sub_tokens):
                    break
                state.active_plans.append(
                    _plan_entry(state, sub, True, sub_tokens,
                                bundled_under=plan.id)
                )
                state.total_tokens += sub_tokens
                state.seen_note_ids.add(sub.id)
            else:
                ref = _ref_entry(sub, plan.id)
                if not state.can_fit(ref["tokens_est"]):
                    break
                state.active_plans.append(ref)
                state.total_tokens += ref["tokens_est"]
                state.seen_note_ids.add(sub.id)


def _bfs_neighborhood(state: _ContextState, max_hops: int,
                      per_node_k: int) -> bool:
    """Dijkstra-ish walk outward from the project node, appending notes to
    `state.selected` until the budget is exhausted or the queue drains.
    Returns True if the walk stopped early (budget hit or queue empty)."""
    start = f"project:{state.project}"
    adj = _Adjacency()

    # Priority queue keyed by -score so we always expand the best
    # unreached node first.
    pq: list[tuple[float, int, str, tuple[str, ...]]] = [(-1.0, 0, start, ())]
    best: dict[str, float] = {start: 1.0}
    decay = 0.85  # per-hop decay to prefer closer nodes overall

    while pq and state.total_tokens < state.budget_tokens:
        neg_score, hops, node_id, path = heapq.heappop(pq)
        score = -neg_score
        if best.get(node_id, -1.0) > score + 1e-9:
            continue  # a better path already processed this node

        if node_id.startswith("note:"):
            nid = int(node_id.split(":", 1)[1])
            # Skip pinned/already-emitted notes entirely — including
            # neighbor expansion. If we re-expanded their neighbors, BFS
            # would re-traverse the pinned subgraph and starve other
            # branches of budget.
            if nid in state.seen_note_ids:
                continue
            note = db.get_note(nid)
            if note and (state.type_filter is None
                         or note.type in state.type_filter):
                full = state.expand_remaining > 0
                tokens = _note_tokens(note, full=full)
                if state.can_fit(tokens):
                    entry = {
                        "id": note.id,
                        **state.emit_body(note, full=full),
                        "type": note.type,
                        "project": note.project,
                        "tags": note.tags,
                        "created_at": note.created_at.isoformat(),
                        "status": note.status,
                        "linked_prs": note.linked_prs,
                        "reachability": round(score, 4),
                        "hops": hops,
                        "via": list(path),
                        "tokens_est": tokens,
                    }
                    if note.type in ("plan", "task") and note.due_date:
                        disp = duedate.display(note.due_date)
                        entry["due_date"] = note.due_date
                        if disp:
                            entry["due_state"] = disp["bucket"]
                            entry["due_in_days"] = disp["days_until"]
                    state.selected.append(entry)
                    state.total_tokens += tokens
                    state.seen_note_ids.add(nid)
                    if full:
                        state.expand_remaining -= 1

        if hops >= max_hops:
            continue

        for neighbor, edge_w, kind in adj.neighbors(node_id, per_node_k=per_node_k):
            new_score = score * edge_w * decay
            if new_score <= 0.2:
                continue
            if best.get(neighbor, -1.0) >= new_score - 1e-9:
                continue
            best[neighbor] = new_score
            heapq.heappush(pq, (-new_score, hops + 1, neighbor,
                                path + (f"{kind}:{node_id}",)))

    return state.total_tokens >= state.budget_tokens or not pq


def build_context(
    project: str,
    budget_tokens: int = 4000,
    max_hops: int = 4,
    per_node_k: int = 4,
    include_types: Optional[Iterable[str]] = None,
    expand_top: int = 0,
) -> dict:
    """BFS outward from a project node, returning a token-budgeted list of
    notes sorted by reachability (product of edge weights along the best path).

    Intended for an agent to call at task start: "what does infoguana know
    about <project>, within ~1500 tokens?"

    `rule` notes for the project pin to the very top with full bodies
    (always-true repo constraints — must be read, not triaged), then active
    plans/tasks, then the BFS-discovered neighborhood. Each note is emitted
    as its haiku-generated preview. Pass `expand_top=N` (capped
    by the caller — mcp_server clamps to 5) to inline full bodies for the
    first N selected notes (active-plans pin first, then by reachability).
    Sizing accounts for full-body expansion so the budget stays honest.
    Call get(id) for ad-hoc deep reads.
    """
    state = _ContextState(
        project=project,
        budget_tokens=budget_tokens,
        type_filter=set(include_types) if include_types else None,
        expand_remaining=max(0, expand_top),
    )

    _pin_rules(state)
    _pin_active_work(state)
    stopped = _bfs_neighborhood(state, max_hops, per_node_k)

    return {
        "project": project,
        "budget_tokens": budget_tokens,
        "total_tokens_est": state.total_tokens,
        "rules": state.rules,
        "active_plans": state.active_plans,
        "notes": state.selected,
        "stopped": stopped,
    }


def rank_project_notes(project: str) -> list[int]:
    """Order every note in `project` by relevance to the project node, using
    the same Dijkstra-on-edge-weights walk that powers build_context — but
    without the token budget so the caller gets a complete ordering it can
    use as a UI sort key.

    Pending plans are pinned to the top in created_at DESC order (matching
    build_context's "active plans" pin). The remainder is sorted by
    reachability score DESC, with non-reached notes (sub-threshold) falling
    back to created_at DESC so every note in the project still appears.
    """
    notes = db.list_notes(project=project, limit=10_000)
    if not notes:
        return []

    pending_plans = [
        n.id for n in notes
        if n.type in ("plan", "task") and n.status == "pending"
    ]
    placed = set(pending_plans)

    # Same Dijkstra as build_context, lower score floor (we want every
    # reachable note rather than a budgeted slice) and no token cap.
    start = f"project:{project}"
    adj = _Adjacency()
    pq: list[tuple[float, int, str]] = [(-1.0, 0, start)]
    best: dict[str, float] = {start: 1.0}
    decay = 0.85
    max_hops = 4
    score_floor = 0.05

    while pq:
        neg_score, hops, node_id = heapq.heappop(pq)
        score = -neg_score
        if best.get(node_id, -1.0) > score + 1e-9:
            continue
        if hops >= max_hops:
            continue
        for neighbor, edge_w, _kind in adj.neighbors(node_id, per_node_k=4):
            new_score = score * edge_w * decay
            if new_score <= score_floor:
                continue
            if best.get(neighbor, -1.0) >= new_score - 1e-9:
                continue
            best[neighbor] = new_score
            heapq.heappush(pq, (-new_score, hops + 1, neighbor))

    # list_notes returns created_at DESC, so the index doubles as a recency
    # tiebreaker (smaller = newer).
    recency_index = {n.id: i for i, n in enumerate(notes)}
    remaining = [n.id for n in notes if n.id not in placed]
    remaining.sort(key=lambda nid: (-best.get(f"note:{nid}", 0.0),
                                    recency_index[nid]))

    return pending_plans + remaining
