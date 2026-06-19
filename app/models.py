from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field


NoteType = Literal["idea", "memory", "feedback", "feature", "reference", "plan", "task", "rule", "unsorted"]
PlanStatus = Literal["not_started", "pending", "complete"]
# Plan #167. Trust dimension on captured claims. The agent fills this at
# write time; 'unspecified' is the legacy/skipped default and is meant to
# look ugly so it's not the path of least resistance.
Confidence = Literal["stated", "inferred", "speculative", "unspecified"]
EdgeType = Literal[
    "implements",
    "caused_by",
    "supersedes",
    "references",
    "bundled_with",
    "prerequisite_for",
]


class NoteCreate(BaseModel):
    content: str = ""
    type: Optional[NoteType] = None
    project: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    source: str = "web"
    status: Optional[PlanStatus] = None
    linked_prs: list[str] = Field(default_factory=list)
    # ISO 'YYYY-MM-DD' for plan/task due dates. Stored without time/TZ;
    # interpreted in the user's local TZ at read time.
    due_date: Optional[str] = None
    confidence: Confidence = "unspecified"
    provenance_note: Optional[str] = None


class NoteUpdate(BaseModel):
    content: Optional[str] = None
    description: Optional[str] = None
    preview: Optional[str] = None
    type: Optional[NoteType] = None
    project: Optional[str] = None
    tags: Optional[list[str]] = None
    status: Optional[PlanStatus] = None
    linked_prs: Optional[list[str]] = None
    # None means "leave alone"; "" means "clear the due date"; a 'YYYY-MM-DD'
    # string sets it. (No native date type — sqlite stores it as TEXT.)
    due_date: Optional[str] = None
    confidence: Optional[Confidence] = None
    # None = leave alone; "" = clear; any other string sets the free-text detail.
    provenance_note: Optional[str] = None


class Attachment(BaseModel):
    id: int
    note_id: int
    path: str
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    created_at: datetime


class MessageAttachment(BaseModel):
    id: int
    message_id: int
    path: str               # relative to settings.attachments_dir
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    original_name: Optional[str] = None
    created_at: datetime


class EdgeView(BaseModel):
    """View-model for a single edge as seen from a focal note. `direction`
    is relative to the focal note: 'out' means focal -> target; 'in' means
    target -> focal. The verb in the UI is rendered with the focal note as
    subject in both cases (outgoing → "supersedes #X"; incoming → "← #X
    supersedes"). Populated by db.attach_links — not persisted."""
    direction: Literal["out", "in"]
    edge_type: EdgeType
    target_id: int
    target_preview: str
    target_type: NoteType
    target_status: Optional[PlanStatus] = None


class Note(BaseModel):
    id: int
    content: str
    description: Optional[str] = None
    preview: Optional[str] = None
    type: NoteType
    project: Optional[str]
    tags: list[str]
    source: str
    created_at: datetime
    updated_at: datetime
    status: Optional[PlanStatus] = None
    linked_prs: list[str] = Field(default_factory=list)
    due_date: Optional[str] = None
    version: int = 1
    confidence: Confidence = "unspecified"
    provenance_note: Optional[str] = None
    # True when this Note instance is reconstructed from a tombstone row in
    # note_versions — the underlying note has been deleted. Set only by
    # rendering paths that surface deleted notes (e.g. /browse search). Not
    # persisted; live db.get_note results always have tombstoned=False.
    tombstoned: bool = False
    attachments: list[Attachment] = Field(default_factory=list)
    # Populated only when a render path explicitly calls db.attach_links.
    # Otherwise empty — API consumers that want links should walk the edges
    # endpoints / traverse instead.
    links: list[EdgeView] = Field(default_factory=list)


class Edge(BaseModel):
    from_id: int
    to_id: int
    edge_type: EdgeType
    created_by_agent: bool = False
    confirmed_by_user: bool = False
    created_at: datetime


ChatModel = Literal["claude-sonnet-4-6", "claude-opus-4-8"]


class Chat(BaseModel):
    id: int
    title: str
    model: str
    project: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ToolCall(BaseModel):
    name: str
    args: dict = Field(default_factory=dict)
    result: str = ""


class Message(BaseModel):
    id: int
    chat_id: int
    role: str  # user | assistant | system
    content: str
    tool_calls: list[dict] = Field(default_factory=list)
    created_at: datetime
    # Assistant only. NULL/None = legacy or no run tracked. Live values:
    # running | complete | error | interrupted.
    run_status: Optional[str] = None
    attachments: list["MessageAttachment"] = Field(default_factory=list)
