"""Plan-specific web routes. Today this only serves the chat-driven
completion flow; general plan CRUD still lives under /notes."""
from urllib.parse import quote

from fastapi import APIRouter, HTTPException

from app import db


router = APIRouter(prefix="/plans", tags=["plans"])


def _already_graduated(plan_id: int) -> bool:
    """True if `plan_id` already has an outgoing `implements` edge to a
    feature-typed note — i.e. it's been graduated once already."""
    edges = db.list_edges_for(plan_id, direction="out", edge_types=["implements"])
    for e in edges:
        target = db.get_note(e.to_id)
        if target and target.type == "feature":
            return True
    return False


def _first_heading_or_line(content: str, max_len: int = 80) -> str:
    for raw in content.splitlines():
        s = raw.strip()
        if not s:
            continue
        cleaned = s.lstrip("#").strip()
        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len].rstrip() + "…"
        return cleaned
    return "(untitled)"


@router.post("/{plan_id}/complete-chat")
def complete_via_chat(plan_id: int) -> dict:
    """Create a chat seeded with an initial user message framing this as a
    completion conversation for a tracked-work note (plan or task). Returns
    the redirect URL — the frontend navigates to it and auto-submits the
    initial message, so the user lands directly on the agent's first response.

    The chat is scoped to the note's project so context seeds
    appropriately on the first turn."""
    plan = db.get_note(plan_id)
    if not plan:
        raise HTTPException(404, "plan not found")
    if plan.type not in ("plan", "task"):
        raise HTTPException(400, f"note {plan_id} is type={plan.type!r}, not 'plan' or 'task'")

    kind = plan.type  # 'plan' or 'task'
    chat = db.create_chat(
        model="claude-sonnet-4-6",
        title=f"ship {kind} #{plan_id}",
        project=plan.project,
    )

    title = _first_heading_or_line(plan.content)
    project_phrase = f" (project: {plan.project})" if plan.project else ""
    initial_message = (
        f"I'd like to mark {kind} #{plan_id} as complete — \"{title}\"{project_phrase}.\n\n"
        f"Please read the {kind} first (via `get` if needed), then ask me for "
        "whatever you need to complete it. If this was a code change that usually "
        f"means PR URL(s); for non-code {kind}s (writing, research, personal projects) "
        "a short summary of how it landed is fine. Then ask what was worth "
        "remembering. When you have what you need, call "
        "`plan_complete(id=" + str(plan_id) + ", pr_urls=[...], lessons_learned=\"...\")` "
        f"to finalize — `pr_urls` can be an empty list for non-code {kind}s."
    )

    redirect = f"/chat?chat_id={chat.id}&initial={quote(initial_message)}"
    return {
        "chat_id": chat.id,
        "redirect": redirect,
        "initial_message": initial_message,
    }


@router.post("/{plan_id}/graduate")
def graduate(plan_id: int) -> dict:
    """Graduate a completed plan into a feature note via a synthesis chat.

    Plan #279 phase 2: the plan + its linked PRs + any lessons-learned memory
    are exactly the material a "this thing was built" doc should contain. This
    route spawns an opus chat seeded with a synthesis prompt — the agent reads
    the plan, traverses outgoing edges, and drafts a feature note for the user
    to confirm + save. The redirect URL auto-submits the initial message so
    the user lands directly on the agent's first response.

    400s if the plan isn't complete, or if it's already been graduated (has an
    outgoing `implements` edge to a feature-typed note)."""
    plan = db.get_note(plan_id)
    if not plan:
        raise HTTPException(404, "plan not found")
    if plan.type != "plan":
        raise HTTPException(400, f"note {plan_id} is type={plan.type!r}, not 'plan'")
    if plan.status != "complete":
        raise HTTPException(
            400, "graduate is only available for completed plans"
        )
    if _already_graduated(plan_id):
        raise HTTPException(409, "plan has already been graduated to a feature")

    chat = db.create_chat(
        model="claude-opus-4-8",
        title=f"graduate plan #{plan_id}",
        project=plan.project,
    )

    title = _first_heading_or_line(plan.content)
    project_phrase = f" (project: `{plan.project}`)" if plan.project else ""
    pr_phrase = (
        f" {len(plan.linked_prs)} PR(s) attached." if plan.linked_prs else ""
    )
    initial_message = (
        f"I'd like to graduate plan #{plan_id} — \"{title}\"{project_phrase} — "
        f"into a `feature` note.{pr_phrase}\n\n"
        "Please:\n"
        f"1. Read the plan via `get(id={plan_id})` to see goal + approach + linked PRs.\n"
        f"2. Walk the plan's outgoing edges via `traverse(start_id={plan_id}, "
        "depth=1, direction=\"out\")` to surface any lessons-learned memory or "
        "linked context.\n"
        "3. Draft a feature note that bundles what shipped: a one-paragraph "
        "summary of the deliverable, a bullet list of what changed (PR titles "
        "are good seeds), and any lessons distilled from the linked memory. "
        "Avoid restating the plan's goal verbatim — the feature note is the "
        "outcome, not the intent.\n"
        "4. Show me the draft. After I confirm, save it via "
        f"`add(content=..., type=\"feature\", project=\"{plan.project or ''}\""
        ", tags=[...])` (inherit the plan's tags as a starting point) and then "
        f"propose `link(from_id={plan_id}, to_id=<feature_id>, "
        "edge_type=\"implements\")` so the plan retains its provenance link to "
        "the feature."
    )

    redirect = f"/chat?chat_id={chat.id}&initial={quote(initial_message)}"
    return {
        "chat_id": chat.id,
        "redirect": redirect,
        "initial_message": initial_message,
    }
