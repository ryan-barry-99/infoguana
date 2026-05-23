"""Suggest existing tags for a piece of content at write time.

Targets compound tag drift (`#auth` vs `#authentication` vs `#oauth`): given
draft note content + optional draft tags, score the corpus's established tag
vocabulary by semantic relevance and co-occurrence with what the agent already
intends to use, then return a ranked list. The agent re-uses what fits and
mints fresh tags only when the suggestions are genuinely a poor match.

Scoring layers (per plan #165, refined by #315):
- Semantic neighborhood: top-K most-similar notes vote for their tags weighted
  by (1 - distance). Captures "tags used on notes like this."
- NPMI co-occurrence: each draft tag boosts candidates that historically
  co-occur with it. Asymmetric gate — trigger df >= 3, candidate df >= 4 —
  keeps singletons from amplifying into the suggestion pool.
- Frequency prior: small log(df) tiebreaker so established tags edge out
  rarer ones at equal semantic score.
- Project affinity: small bonus if the candidate is already in use in the
  caller's project. Cross-project tags still flow — infoguana's whole point
  is cross-pollination.

Ephemeral identifier patterns (`issue-NNN`, `pr-NNN`, `<repo>-pr-NNN`)
never surface; they are intentionally singletons.
"""
import json
import math
import re
from typing import Optional

from app import db, embed


MIN_CANDIDATE_DF = 4
MIN_TRIGGER_DF = 3
MIN_COOC = 3

NEIGHBOR_K = 30
DEFAULT_LIMIT = 12

W_SEM = 0.60
W_COOC = 0.30
W_DF = 0.05
W_PROJECT = 0.05


_EPHEMERAL_TAG = re.compile(
    r"^(?:issue|pr|gh|ticket|bug|commit)-\d+$"
    r"|^.+-pr-\d+$"
    r"|^.+-issue-\d+$",
    re.I,
)


def _is_ephemeral(tag: str) -> bool:
    return bool(_EPHEMERAL_TAG.match(tag))


def _scan_corpus() -> tuple[dict[str, int], int, dict[str, set[int]], dict[str, set[str]]]:
    """One pass over notes. Returns (df, N, by_tag, project_tags) where:
    - df: tag -> note count
    - N: total notes with at least one tag
    - by_tag: tag -> set of note ids carrying it
    - project_tags: project -> set of tags ever used in that project
    """
    conn = db.get_conn()
    rows = conn.execute("SELECT id, project, tags FROM notes").fetchall()
    df: dict[str, int] = {}
    by_tag: dict[str, set[int]] = {}
    project_tags: dict[str, set[str]] = {}
    N = 0
    for r in rows:
        try:
            tags = json.loads(r["tags"] or "[]")
        except Exception:
            tags = []
        if not tags:
            continue
        N += 1
        nid = r["id"]
        proj = r["project"]
        unique = {t for t in tags if t}
        for t in unique:
            df[t] = df.get(t, 0) + 1
            by_tag.setdefault(t, set()).add(nid)
            if proj:
                project_tags.setdefault(proj, set()).add(t)
    return df, N, by_tag, project_tags


def _npmi(cooc: int, df_a: int, df_b: int, N: int) -> float:
    """Normalized PMI in [-1, 1]. Returns 0 for any degenerate input so the
    caller can sum/max without special-casing."""
    if cooc <= 0 or df_a <= 0 or df_b <= 0 or N <= 0:
        return 0.0
    p_xy = cooc / N
    denom = -math.log(p_xy)
    if denom <= 0:
        return 0.0
    pmi = math.log(p_xy / ((df_a / N) * (df_b / N)))
    return pmi / denom


def suggest_tags(content: str,
                 project: Optional[str] = None,
                 draft_tags: Optional[list[str]] = None,
                 limit: int = DEFAULT_LIMIT) -> dict:
    """Rank existing tags by relevance to `content` (+ optional draft tags).

    Returns a dict with `suggestions` (ranked list with score breakdown),
    `candidate_pool_size`, `neighbor_count`, and the normalized `draft_tags`
    that were considered triggers.
    """
    norm_drafts = [t.lower().strip() for t in (draft_tags or []) if t and t.strip()]

    try:
        qv = embed.engine().embed(content)
    except Exception:
        qv = None
    neighbors = db.vector_search(qv, limit=NEIGHBOR_K) if qv is not None else []

    df, N, by_tag, project_tags = _scan_corpus()

    if N == 0:
        return {
            "suggestions": [],
            "candidate_pool_size": 0,
            "neighbor_count": len(neighbors),
            "draft_tags": norm_drafts,
        }

    draft_set = set(norm_drafts)
    candidates = {
        t for t, d in df.items()
        if d >= MIN_CANDIDATE_DF
        and t not in draft_set
        and not _is_ephemeral(t)
    }

    sem_raw: dict[str, float] = {}
    for note, distance in neighbors:
        if not note.tags:
            continue
        weight = max(0.0, 1.0 - float(distance))
        for t in set(note.tags):
            if t in candidates:
                sem_raw[t] = sem_raw.get(t, 0.0) + weight
    sem_max = max(sem_raw.values(), default=0.0)
    sem = {t: v / sem_max for t, v in sem_raw.items()} if sem_max > 0 else {}

    triggers = [t for t in norm_drafts
                if df.get(t, 0) >= MIN_TRIGGER_DF and t in by_tag]
    cooc: dict[str, float] = {}
    if triggers:
        trig_notes = {t: by_tag[t] for t in triggers}
        for cand in candidates:
            cand_notes = by_tag.get(cand)
            if not cand_notes:
                continue
            best = 0.0
            for trig, tnotes in trig_notes.items():
                shared = len(cand_notes & tnotes)
                if shared < MIN_COOC:
                    continue
                v = _npmi(shared, df[trig], df[cand], N)
                if v > best:
                    best = v
            if best > 0:
                cooc[cand] = best

    max_df = max(df.values()) if df else 1
    log_max = math.log(1 + max_df) or 1.0
    df_prior = {t: math.log(1 + df[t]) / log_max for t in candidates}

    project_set = project_tags.get(project, set()) if project else set()

    scored: list[tuple[str, float, dict]] = []
    for t in candidates:
        s_sem = sem.get(t, 0.0)
        s_cooc = cooc.get(t, 0.0)
        if s_sem == 0.0 and s_cooc == 0.0:
            continue
        s_df = df_prior.get(t, 0.0)
        s_proj = 1.0 if t in project_set else 0.0
        total = (W_SEM * s_sem + W_COOC * s_cooc
                 + W_DF * s_df + W_PROJECT * s_proj)
        sources = []
        if s_sem > 0:
            sources.append("semantic")
        if s_cooc > 0:
            sources.append("cooc")
        if s_proj > 0:
            sources.append("project")
        scored.append((t, total, {
            "sem": round(s_sem, 4),
            "cooc": round(s_cooc, 4),
            "df_prior": round(s_df, 4),
            "in_project": s_proj > 0,
            "from": sources,
        }))

    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:limit]

    return {
        "suggestions": [
            {"tag": t, "score": round(score, 4), "df": df[t], **breakdown}
            for t, score, breakdown in top
        ],
        "candidate_pool_size": len(candidates),
        "neighbor_count": len(neighbors),
        "draft_tags": norm_drafts,
    }
