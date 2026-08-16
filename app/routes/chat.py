"""Chat router: multi-turn conversations backed by the `claude -p` CLI with
infoguana MCP server wired in.

The agent run is decoupled from the HTTP connection so navigating away (or
restarting infoguana) doesn't kill it. POST /chats/{id}/send creates the
assistant message row, kicks off a background task that streams events from
`claude -p` into the `message_events` table, and returns the message id.
Clients then subscribe via GET /messages/{id}/events (SSE), which tails the
event log — they can disconnect, reattach from another tab, or replay from
any seq."""
import asyncio
import base64
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.datastructures import UploadFile
from typing import AsyncIterator

from app import db, embed, graph, onboard, titler
from app.config import settings
from app.mcp_server import CHAT_ALLOWED_TOOLS
from app.models import Message, MessageAttachment
from app.templating import templates


# Built once: "WebSearch,WebFetch,mcp__infoguana__search,..." for `claude -p
# --allowedTools`. Web tools come from the CLI itself; the rest is the MCP
# allowlist defined in mcp_server.py.
_ALLOWED_TOOLS_ARG = ",".join(
    ["WebSearch", "WebFetch"]
    + [f"mcp__infoguana__{name}" for name in CHAT_ALLOWED_TOOLS]
)


log = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


# Per-message pub/sub so SSE readers wake up immediately when the background
# runner appends a new event, instead of polling the DB.
# message_id -> set of asyncio.Events; each Event is signaled by _notify on
# every append, then cleared by the waiter before re-checking.
_subscribers: dict[int, set[asyncio.Event]] = {}


def _subscribe(message_id: int) -> asyncio.Event:
    ev = asyncio.Event()
    _subscribers.setdefault(message_id, set()).add(ev)
    return ev


def _unsubscribe(message_id: int, ev: asyncio.Event) -> None:
    bucket = _subscribers.get(message_id)
    if not bucket:
        return
    bucket.discard(ev)
    if not bucket:
        _subscribers.pop(message_id, None)


def _notify(message_id: int) -> None:
    for ev in _subscribers.get(message_id, ()):
        ev.set()


# Track in-flight background tasks so they aren't garbage-collected mid-run.
# (asyncio.create_task only holds a weak reference; without this, GC could
# kill a long synthesis.)
_tasks: set[asyncio.Task] = set()

# Assistant-message-id -> the task currently driving its run, so /stop can
# look up by message id and cancel. Populated by _run_assistant on entry,
# popped on exit (success, error, or cancellation). Distinct from _tasks
# (which also holds title-generation tasks and exists purely for GC).
_runs: dict[int, asyncio.Task] = {}


SUGGESTED_MODELS = [
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "opus",
    "sonnet",
    "haiku",
]
DEFAULT_MODEL = "claude-sonnet-4-6"


def _clean_model(m: str) -> Optional[str]:
    m = (m or "").strip()
    if not m or len(m) > 80:
        return None
    # Claude CLI accepts aliases (opus/sonnet/haiku) and full ids
    # (claude-sonnet-4-6, etc.). Restrict to the character set those use so
    # we can't inject shell metacharacters as a model arg.
    if not all(c.isalnum() or c in "-._" for c in m):
        return None
    return m


def _clean_project(p: Optional[str]) -> Optional[str]:
    if p is None:
        return None
    p = p.strip()
    if not p or p.lower() in {"none", "auto", "(auto)"}:
        return None
    if len(p) > 80:
        return None
    return p

SYSTEM_PROMPT = """You are the user's assistant running inside the `infoguana` \
personal knowledge base.

Tools available:
- search / similar: look up past notes from any project
- context: pull a token-budgeted subgraph of memories for a project
- recent / get: browse recent notes
- add: save a new note
- update: refine or correct an existing note (full replacement of the \
fields you pass; prefer this over creating a near-duplicate when a note is \
stale, incomplete, or wrong)
- delete: remove an obsolete or duplicate note — use sparingly, and \
only after the user confirms
- link / unlink: create or remove a typed edge between two notes \
(implements, caused_by, supersedes, references, bundled_with, prerequisite_for)
- traverse: walk the explicit link graph from a note out/in/both \
directions to surface connected notes
- plan_complete: mark a plan complete; attaches PR URLs and optionally \
spawns a linked lessons-learned memory note. Pass `pr_urls=[]` for non-code \
plans (writing, research, personal projects) that don't ship via GitHub.
- gh_issue_get / gh_issue_comments / gh_issue_list: read \
GitHub issues and their comments with the user's personal PAT
- gh_pr_get / gh_pr_comments: read pull requests and their \
conversation + inline review comments
- gh_issue_comment_post: post a comment on a GitHub issue AS the \
chat's bot identity (see rules below)
- gh_issue_create: open a new GitHub issue AS the chat's bot identity \
(same rules as posting a comment)
- WebSearch: search the web for current information
- WebFetch: read a specific URL

Editing infoguana: if the user tells you a saved note is wrong, out of date, \
or missing context, update it in place via update rather than adding a \
new one. Deletions should be explicitly user-requested or obvious-duplicate \
cleanups — when in doubt, ask.

Linking notes: after you save a note (or surface a clear connection mid-\
conversation), scan for relationships to existing notes — `#NNN` mentions, \
"this supersedes the old plan", "the bug was caused by X", "this implements \
idea Y". For each one you can justify in a sentence, propose a link \
with the matching edge_type ("Link this to #142 as supersedes?") and wait \
for yes/no — same pattern as confirming saves. Edge types: implements \
(plan→idea/spec), caused_by (incident→root cause), supersedes (new→old \
decision), references (cites another note), bundled_with (shipped together), \
prerequisite_for (dependency). Only propose what you can justify; speculative \
links pollute the graph more than they help.

GitHub writes: gh_issue_comment_post always takes a `bot_project` \
argument. Set it to the project this chat is scoped to (visible in the \
first-turn seed). The comment posts as that project's bot GitHub identity, \
NOT as the user — so if you want the user to be notified, include \
`@<their-handle>` in the body (ask the user for their handle if it isn't \
already established in this conversation or the project context). \
Before calling this tool, show the user the exact comment body and wait \
for explicit confirmation; it's a write on shared state.

Retrieval hierarchy: before answering, decide whether the question is about
(a) the user's own past work, ideas, or decisions — call search /
context first; infoguana is authoritative and things from training \
may be out of date; (b) current external facts or research — call \
WebSearch, then WebFetch on the most promising link for details; (c) general \
reasoning — answer directly. Combine as needed.

Previews are for triage, not citation. search / similar / \
context return 1-5 line haiku previews marked `preview: True`. They \
help you decide which notes to read; they are NOT safe to cite. Before \
stating a fact, decision, recommendation, or design point that's anchored \
on a preview, fetch the full body via get(id), get_many(ids=\
[...]), or expand_top=N on the next search call. Cite from verified \
content, not from a hand-sized summary.

Saving memories: do NOT save web findings automatically. When you return \
research or a fact that seems worth remembering, ask the user if they want \
to save it — e.g., "want me to remember that?". Only call add after \
the user agrees. When you do save, write the HOW and the reasoning, not just \
the category — this is a cross-project infoguana so bare references like \
"foo.py:42" or a bare URL rot. Summarize the substance inline and include \
the source URL so it's traceable.

Reply to the user in plain prose. Be concise."""


def _mcp_config_path() -> Path:
    return (settings.db_path.parent / "mcp-config.json").resolve()


# Mime types we accept on chat uploads. Keep aligned with what the model can
# actually consume (image blocks + PDF document blocks).
_ATTACH_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}
# Long-edge cap before we re-encode. Anthropic recommends ~1568px on the long
# edge for images; bigger files just get re-encoded server-side.
_IMAGE_LONG_EDGE_MAX = 1568


def _resolve_mime(upload: UploadFile, data: bytes) -> str:
    raw = (upload.content_type or "").lower().split(";", 1)[0].strip()
    if raw in _ATTACH_EXT:
        return raw
    # Some browsers send 'image/jpg'; normalize.
    if raw == "image/jpg":
        return "image/jpeg"
    # Last resort: filename extension.
    guess = mimetypes.guess_type(upload.filename or "")[0] or ""
    guess = guess.lower()
    if guess == "image/jpg":
        guess = "image/jpeg"
    return guess


def _maybe_downscale_image(data: bytes, mime: str) -> tuple[bytes, str]:
    """Downscale large images to <=_IMAGE_LONG_EDGE_MAX on the long edge.
    Returns (data, mime). Falls back to the original on any Pillow error."""
    if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        return data, mime
    try:
        from PIL import Image  # lazy import — heavy
    except Exception:
        return data, mime
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            longest = max(w, h)
            if longest <= _IMAGE_LONG_EDGE_MAX:
                return data, mime
            scale = _IMAGE_LONG_EDGE_MAX / longest
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            # GIFs collapse to a single frame after thumbnail; if that's a
            # problem we can keep the original instead.
            im2 = im.convert("RGB") if mime == "image/jpeg" else im.copy()
            im2.thumbnail(new_size, Image.Resampling.LANCZOS)
            out = io.BytesIO()
            if mime == "image/jpeg":
                im2.save(out, format="JPEG", quality=85, optimize=True)
            elif mime == "image/png":
                im2.save(out, format="PNG", optimize=True)
            elif mime == "image/webp":
                im2.save(out, format="WEBP", quality=85)
            else:  # gif
                im2.save(out, format="GIF")
            return out.getvalue(), mime
    except Exception:
        log.exception("image downscale failed; passing through original")
        return data, mime


def _save_message_upload(message_id: int, upload: UploadFile) -> MessageAttachment:
    """Read the upload, validate mime + size, optionally downscale, and write
    to attachments_dir/chat-messages/{message_id}/{digest}{ext}. Records the
    attachment in the DB and returns it."""
    data = upload.file.read()
    if not data:
        raise HTTPException(400, "empty upload")
    if len(data) > settings.attachment_max_bytes:
        raise HTTPException(
            413, f"upload exceeds {settings.attachment_max_bytes} bytes",
        )
    mime = _resolve_mime(upload, data)
    if mime not in _ATTACH_EXT:
        raise HTTPException(415, f"unsupported file type: {mime or 'unknown'}")

    data, mime = _maybe_downscale_image(data, mime)
    ext = _ATTACH_EXT[mime]
    digest = hashlib.sha256(data).hexdigest()[:16]
    rel = f"chat-messages/{message_id}/{digest}{ext}"
    dest = settings.attachments_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return db.add_message_attachment(
        message_id, rel, mime, len(data), upload.filename or None,
    )


def _read_attachment_bytes(att: MessageAttachment) -> bytes:
    return (settings.attachments_dir / att.path).read_bytes()


def write_mcp_config() -> None:
    """Called on app startup. Emits a loopback MCP config the spawned
    `claude -p` uses to talk back to this same process."""
    cfg = {
        "mcpServers": {
            "infoguana": {
                "type": "http",
                "url": f"http://127.0.0.1:{settings.port}/mcp/",
                "headers": {"Authorization": f"Bearer {settings.mcp_secret}"},
            }
        }
    }
    path = _mcp_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2))


def _attachment_marker(atts: list[MessageAttachment]) -> str:
    """Plain-text reference to attachments included in a prior user turn.
    The model only gets bytes for the *current* turn's images — earlier
    images degrade to this marker so it at least knows something was there."""
    if not atts:
        return ""
    names = [a.original_name or Path(a.path).name for a in atts]
    return f" [attached: {', '.join(names)}]"


def _render_history(messages: list[Message]) -> str:
    """Rebuild prior turns as a plain-text transcript. v1 doesn't replay tool
    calls — Claude can re-query infoguana if it needs to."""
    lines: list[str] = []
    for m in messages:
        if m.role == "user":
            lines.append(f"User: {m.content}{_attachment_marker(m.attachments)}")
        elif m.role == "assistant":
            lines.append(f"Assistant: {m.content}")
    return "\n\n".join(lines)


def _seed_context(query: str, project: Optional[str] = None,
                        limit: int = 5) -> str:
    """Build a first-turn prompt preamble so infoguana is always consulted on
    turn one rather than trusting the model to remember to call the tool.

    When `project` is set, render via the same `onboard.build` that produces
    the SessionStart preamble for project-local Claude Code sessions. This
    keeps the chat agent's view shape-identical to Claude Code's — same
    protocol intro, same `preview: True`-shaped note formatting — so rules
    like "previews are for triage, not citation" fire here too.
    Otherwise fall back to a hybrid search seeded by the user's first
    message.
    """
    if project:
        try:
            return onboard.build(project=project, budget_tokens=4000)
        except Exception:
            log.exception("seed context failed")
            return ""

    try:
        qv = embed.engine().embed(query)
    except Exception:
        qv = None
    try:
        hits = db.hybrid_search(query, query_vec=qv, limit=limit)
    except Exception:
        log.exception("seed search failed")
        return ""
    if not hits:
        return ""
    lines = [
        "Relevant notes from infoguana (auto-fetched at conversation start):",
        "",
    ]
    for note, score in hits:
        header = f"- [{note.type} · {note.project or 'unscoped'} · score={score:.3f}]"
        lines.append(header)
        body = note.content.strip()
        if len(body) > 800:
            body = body[:800].rstrip() + "…"
        for bl in body.splitlines():
            lines.append(f"  {bl}")
        lines.append("")
    lines.append(
        "Use these if relevant; call search / context for more."
    )
    return "\n".join(lines)


def _build_prompt(history: list[Message], user_msg: str, seed: str = "") -> str:
    if not history:
        if seed:
            return f"{seed}\n\n---\nUser: {user_msg}"
        return user_msg
    transcript = _render_history(history)
    return (
        f"Previous turns in this conversation:\n\n{transcript}\n\n"
        f"---\nUser's new message: {user_msg}"
    )


def _attachment_blocks(atts: list[MessageAttachment]) -> list[dict]:
    """Encode message attachments as Anthropic content blocks. Images become
    image blocks; PDFs become document blocks. Anything we don't know how to
    inline is silently skipped (the user already saw a text marker)."""
    blocks: list[dict] = []
    for a in atts:
        try:
            raw = _read_attachment_bytes(a)
        except FileNotFoundError:
            log.warning("attachment %s missing on disk: %s", a.id, a.path)
            continue
        b64 = base64.b64encode(raw).decode("ascii")
        mime = a.mime_type or "application/octet-stream"
        if mime.startswith("image/"):
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            })
        elif mime == "application/pdf":
            blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            })
    return blocks


async def _stream_claude(prompt: str, model: str,
                         attachments: Optional[list[MessageAttachment]] = None,
                         ) -> AsyncIterator[dict]:
    """Spawn `claude -p` with --include-partial-messages and yield parsed
    events as they arrive. Each event is a dict with a 'type' key:
      {type: "text", delta: "..."}            text token(s)
      {type: "tool_use", id, name, args}      tool call (args finalized)
      {type: "tool_result", id, result}       mcp returned
      {type: "final", text: "..."}            canonical final text (DB persist)
      {type: "error", message}                fatal error

    When `attachments` is non-empty we switch to --input-format stream-json
    and pipe the user message as a content-block array so images/PDFs ride
    along as base64-encoded blocks. Otherwise we keep the cheaper text-arg
    fast path.
    """
    bin_path = shutil.which(settings.claude_bin)
    if not bin_path:
        yield {"type": "error", "message": "claude CLI not found"}
        return

    use_stream_input = bool(attachments)
    extra_blocks = _attachment_blocks(attachments or [])

    # Node's stdout is block-buffered when piped (≈4KB chunks), so claude -p
    # would deliver an entire response in one burst at the end despite
    # --include-partial-messages. Wrap with `stdbuf -oL` to force line-
    # buffered output so events stream as they're emitted. Falls back to a
    # bare invocation if stdbuf isn't available (non-GNU systems).
    stdbuf = shutil.which("stdbuf")
    cmd = ([stdbuf, "-oL"] if stdbuf else []) + [
        bin_path, "-p",
        "--model", model,
        "--mcp-config", str(_mcp_config_path()),
        "--strict-mcp-config",
        "--tools", "WebSearch,WebFetch",
        "--allowedTools", _ALLOWED_TOOLS_ARG,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--append-system-prompt", SYSTEM_PROMPT,
    ]
    if use_stream_input:
        cmd += ["--input-format", "stream-json"]
    else:
        cmd.append(prompt)
    # INFOGUANA_HOOK_DISABLE: tell the user-level SessionStart hook
    # (infoguana-onboard-chunk.sh) to no-op for this subprocess.
    # _seed_context already prepended a project-scoped seed via
    # graph.build_context(chat.project), so running the hook on top
    # would (a) duplicate the seed and (b) always scope it to
    # `infoguana` because the hook derives PROJECT from cwd, which is
    # the service's WorkingDirectory.
    env = {**os.environ, "IS_SANDBOX": "1", "INFOGUANA_HOOK_DISABLE": "1"}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if use_stream_input else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env, limit=1024 * 1024 * 8,
    )

    if use_stream_input and proc.stdin is not None:
        msg = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}, *extra_blocks],
            },
        }
        try:
            proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception:
            log.exception("failed to feed stream-json input to claude -p")

    # Per-block scratch: content_block_start gives us id/name; input_json_delta
    # appends partial JSON; content_block_stop finalizes and we emit tool_use.
    tool_blocks: dict[int, dict] = {}   # content-block index -> {id, name, args_buf}
    assert proc.stdout is not None

    try:
        while True:
            try:
                line_b = await asyncio.wait_for(proc.stdout.readline(), timeout=300)
            except asyncio.TimeoutError:
                yield {"type": "error", "message": "claude timed out after 5m"}
                return
            if not line_b:
                break
            line = line_b.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = evt.get("type")
            if etype == "stream_event":
                inner = evt.get("event", {}) or {}
                it = inner.get("type")
                idx = inner.get("index")
                if it == "content_block_start":
                    block = inner.get("content_block", {}) or {}
                    if block.get("type") == "tool_use":
                        tool_blocks[idx] = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "args_buf": "",
                        }
                        yield {"type": "tool_use_start",
                               "id": block.get("id", ""),
                               "name": block.get("name", "")}
                elif it == "content_block_delta":
                    delta = inner.get("delta", {}) or {}
                    dt = delta.get("type")
                    if dt == "text_delta":
                        text = delta.get("text") or ""
                        if text:
                            yield {"type": "text", "delta": text}
                    elif dt == "input_json_delta" and idx in tool_blocks:
                        tool_blocks[idx]["args_buf"] += delta.get("partial_json", "") or ""
                elif it == "content_block_stop":
                    if idx in tool_blocks:
                        tb = tool_blocks.pop(idx)
                        try:
                            args = json.loads(tb["args_buf"]) if tb["args_buf"] else {}
                        except json.JSONDecodeError:
                            args = {}
                        yield {"type": "tool_use", "id": tb["id"],
                               "name": tb["name"], "args": args}
            elif etype == "user":
                # tool_result blocks arrive as synthetic user messages after the MCP call returns
                for block in evt.get("message", {}).get("content", []) or []:
                    if block.get("type") == "tool_result":
                        tuid = block.get("tool_use_id")
                        content = block.get("content")
                        if isinstance(content, list):
                            content = "\n".join(
                                c.get("text", "") for c in content if isinstance(c, dict)
                            )
                        elif not isinstance(content, str):
                            content = json.dumps(content)
                        if tuid:
                            yield {"type": "tool_result", "id": tuid,
                                   "result": content or ""}
            elif etype == "result":
                yield {"type": "final", "text": evt.get("result") or ""}

        stderr_b = await proc.stderr.read() if proc.stderr else b""
        rc = await proc.wait()
        if rc != 0:
            err = stderr_b.decode("utf-8", "replace")[:500]
            log.warning("claude -p returned %d: %s", rc, err)
            yield {"type": "error", "message": f"claude exited {rc}: {err}"}
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request, chat_id: Optional[int] = None) -> HTMLResponse:
    chats = db.list_chats()
    current = db.get_chat(chat_id) if chat_id else None
    if chat_id and not current:
        raise HTTPException(404, "chat not found")
    messages = db.list_messages(current.id) if current else []
    return templates.TemplateResponse(
        request, "chat.html",
        {
            "chats": chats,
            "current": current,
            "messages": messages,
            "models": SUGGESTED_MODELS,
            "projects": db.list_project_names(),
        },
    )


@router.post("/chats", response_class=HTMLResponse)
def create_chat(request: Request,
                model: str = Form(DEFAULT_MODEL),
                project: Optional[str] = Form(None)) -> HTMLResponse:
    clean = _clean_model(model) or DEFAULT_MODEL
    clean_proj = _clean_project(project)
    chat = db.create_chat(model=clean, project=clean_proj)
    dest = f"/chat?chat_id={chat.id}"
    # HTMX requests: return 200 with HX-Redirect; browser fetch auto-follows
    # 3xx + Location, which swallows HX-Redirect. Non-HTMX requests get a
    # normal redirect.
    if request.headers.get("HX-Request"):
        return HTMLResponse("", status_code=200, headers={"HX-Redirect": dest})
    return RedirectResponse(url=dest, status_code=303)


@router.delete("/chats/{chat_id}")
def delete_chat(chat_id: int) -> HTMLResponse:
    if not db.delete_chat(chat_id):
        raise HTTPException(404, "not found")
    return HTMLResponse("", status_code=200, headers={"HX-Redirect": "/chat"})


@router.delete("/messages/{message_id}")
def delete_message(message_id: int) -> dict:
    if not db.delete_message(message_id):
        raise HTTPException(404, "not found")
    return {"deleted": message_id}


@router.post("/messages/{message_id}/stop")
def stop_message(message_id: int) -> dict:
    """Cancel the in-flight agent run for an assistant message. The runner
    catches CancelledError, persists whatever partial output it has, and
    pushes a `done` event — so the SSE stream wraps up cleanly for any
    attached client. Idempotent: stopping a finished message is a no-op."""
    task = _runs.get(message_id)
    if task is None or task.done():
        return {"stopped": False, "reason": "not running"}
    task.cancel()
    return {"stopped": True}


@router.post("/messages/{message_id}/edit")
async def edit_message(message_id: int,
                       content: str = Form(...)) -> JSONResponse:
    """Rewrite a user message and restart the conversation from that turn.
    Updates the message's content, deletes every later message in the chat,
    creates a fresh assistant placeholder, and kicks off a new run. Refuses
    while any message in the chat is still running — caller should stop it
    first."""
    msg = db.get_message(message_id)
    if not msg:
        raise HTTPException(404, "message not found")
    if msg.role != "user":
        raise HTTPException(400, "can only edit user messages")
    new_text = content.strip()
    if not new_text and not msg.attachments:
        raise HTTPException(400, "empty message")

    chat = db.get_chat(msg.chat_id)
    if not chat:
        raise HTTPException(404, "chat not found")
    if db.chat_has_running_message(msg.chat_id):
        raise HTTPException(409, "chat has a running message; stop it first")

    db.update_message_content(message_id, new_text)
    db.delete_messages_after(msg.chat_id, message_id)

    # Rebuild prompt history from everything up to (but not including) the
    # edited message, then add the edited content as the new user turn.
    all_msgs = db.list_messages(msg.chat_id)
    prior = [m for m in all_msgs if m.id < message_id]
    edited = db.get_message(message_id) or msg

    seed = ""
    if not prior:
        seed = await asyncio.to_thread(
            _seed_context, new_text or "image upload", chat.project,
        )
    prompt_text = new_text or "(see attached image)"
    prompt = _build_prompt(prior, prompt_text, seed=seed)

    assistant_msg = db.add_message(
        msg.chat_id, "assistant", "", tool_calls=None, run_status="running",
    )

    task = asyncio.create_task(
        _run_assistant(assistant_msg.id, msg.chat_id, prompt, chat.model,
                       attachments=edited.attachments),
    )
    _tasks.add(task)
    _runs[assistant_msg.id] = task
    task.add_done_callback(_tasks.discard)

    return JSONResponse({
        "user_msg_id": message_id,
        "assistant_msg_id": assistant_msg.id,
    })


def _render_chat_markdown(chat, messages: list[Message]) -> str:
    """Plain-text markdown transcript of a chat. Tool calls render as
    GitHub-flavored <details> blocks so they collapse in renderers that
    support it but stay readable in plain text."""
    lines: list[str] = []
    lines.append(f"# {chat.title}")
    lines.append("")
    lines.append(f"- chat id: {chat.id}")
    lines.append(f"- model: {chat.model}")
    if chat.project:
        lines.append(f"- project: {chat.project}")
    lines.append(f"- created: {chat.created_at.isoformat()}")
    lines.append(f"- updated: {chat.updated_at.isoformat()}")
    lines.append(f"- messages: {len(messages)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for m in messages:
        role = "User" if m.role == "user" else "Assistant"
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append(f"## {role} · {ts}")
        lines.append("")

        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                name = tc.get("name") or "tool"
                args = tc.get("args") or {}
                result = tc.get("result") or ""
                lines.append(f"<details><summary><code>{name}</code></summary>")
                lines.append("")
                lines.append("**args**")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(args, indent=2, ensure_ascii=False))
                lines.append("```")
                if result:
                    lines.append("")
                    lines.append("**result**")
                    lines.append("")
                    lines.append("```")
                    lines.append(result)
                    lines.append("```")
                lines.append("")
                lines.append("</details>")
                lines.append("")

        if m.content:
            lines.append(m.content.rstrip())
            lines.append("")

        if m.role == "assistant" and m.run_status in ("interrupted", "error"):
            lines.append(f"_(run {m.run_status})_")
            lines.append("")

    return "\n".join(lines)


def _slug_for_filename(text: str, max_len: int = 60) -> str:
    s = (text or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if not s:
        s = "chat"
    return s[:max_len].rstrip("-") or "chat"


@router.get("/chats/{chat_id}/export.md")
def export_chat(chat_id: int) -> Response:
    """Download the chat as a markdown transcript."""
    chat = db.get_chat(chat_id)
    if not chat:
        raise HTTPException(404, "chat not found")
    messages = db.list_messages(chat_id)
    body = _render_chat_markdown(chat, messages)
    filename = f"chat-{chat.id}-{_slug_for_filename(chat.title)}.md"
    return Response(
        content=body.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/chats/{chat_id}/fork")
def fork(chat_id: int, from_message_id: int = Form(...)) -> dict:
    new = db.fork_chat(chat_id, from_message_id)
    if not new:
        raise HTTPException(404, "source chat or message not found")
    return {"chat_id": new.id, "redirect": f"/chat?chat_id={new.id}"}


@router.post("/chats/{chat_id}/model")
def set_model(chat_id: int, model: str = Form(...)) -> dict:
    clean = _clean_model(model)
    if not clean:
        raise HTTPException(400, "invalid model")
    chat = db.update_chat(chat_id, model=clean)
    if not chat:
        raise HTTPException(404, "not found")
    return {"model": chat.model}


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


async def _generate_chat_title(chat_id: int, assistant_msg_id: int,
                               first_message: str) -> None:
    """Background task: ask haiku to produce a title for a fresh chat, then
    push it through the assistant message's event stream so the user's open
    sidebar updates without a reload. Falls back silently on failure (the
    placeholder set in send() stays)."""
    try:
        title = await asyncio.to_thread(titler.generate_title, first_message)
        if not title:
            return
        db.update_chat(chat_id, title=title)
        # Piggyback on the open SSE stream — readers see the new title and
        # update the sidebar entry for chat_id. Persisted in the event log
        # so a page reload during the run still picks it up on replay.
        try:
            db.append_message_event(
                assistant_msg_id, "chat_title",
                {"chat_id": chat_id, "title": title},
            )
            _notify(assistant_msg_id)
        except Exception:
            log.exception("append chat_title event failed (msg=%s)",
                          assistant_msg_id)
    except Exception:
        log.exception("title generation failed (chat=%s)", chat_id)


async def _run_assistant(message_id: int, chat_id: int,
                         prompt: str, model: str,
                         attachments: Optional[list[MessageAttachment]] = None,
                         ) -> None:
    """Drive `claude -p` to completion, streaming events into the DB. Runs as
    an unattached background task so it survives client disconnects and
    navigation. The SSE endpoint reads the resulting event log."""
    text_parts: list[str] = []
    final_text = ""
    tool_uses: dict[str, dict] = {}
    tool_results: dict[str, str] = {}

    def _append(event_type: str, payload: dict) -> None:
        try:
            db.append_message_event(message_id, event_type, payload)
            _notify(message_id)
        except Exception:
            log.exception("append_message_event failed (msg=%s)", message_id)

    def _calls_so_far() -> list[dict]:
        return [
            {"name": u["name"], "args": u["args"],
             "result": tool_results.get(tuid, "")}
            for tuid, u in tool_uses.items()
        ]

    def _finalize(body: str, calls: list[dict], status: str,
                  error_msg: Optional[str], cleanup_label: str) -> None:
        try:
            db.finalize_message(message_id, body, calls, status)
            if error_msg is not None:
                _append("error", {"message": error_msg})
            _append("done", {"id": message_id})
        except Exception:
            log.exception("%s cleanup failed (msg=%s)",
                          cleanup_label, message_id)

    try:
        async for evt in _stream_claude(prompt, model, attachments=attachments):
            t = evt["type"]
            if t == "text":
                text_parts.append(evt["delta"])
                _append("text", {"delta": evt["delta"]})
            elif t == "tool_use_start":
                _append("tool_use_start",
                        {"id": evt["id"], "name": evt["name"]})
            elif t == "tool_use":
                tool_uses[evt["id"]] = {"name": evt["name"], "args": evt["args"]}
                _append("tool_use",
                        {"id": evt["id"], "name": evt["name"],
                         "args": evt["args"]})
            elif t == "tool_result":
                tool_results[evt["id"]] = evt["result"]
                _append("tool_result",
                        {"id": evt["id"], "result": evt["result"]})
            elif t == "final":
                final_text = evt["text"]
            elif t == "error":
                _append("error", {"message": evt["message"]})

        body = final_text or "".join(text_parts) or "(no response)"
        _finalize(body, _calls_so_far(), "complete", None, "complete")
    except asyncio.CancelledError:
        # User clicked stop, or the process is shutting down. Persist whatever
        # partial output we have so the message isn't a blank bubble.
        _finalize(
            "".join(text_parts) or "(stopped)", _calls_so_far(),
            "interrupted", "agent run cancelled", "cancel",
        )
        raise
    except Exception as e:
        log.exception("assistant run failed (msg=%s)", message_id)
        _finalize(
            "".join(text_parts) or f"(error: {e})",
            [], "error", str(e), "error",
        )
    finally:
        _runs.pop(message_id, None)


@router.post("/chats/{chat_id}/send")
async def send(chat_id: int, request: Request) -> JSONResponse:
    """Save the user's message, create a placeholder assistant message in
    'running' state, and kick off the background agent task. Returns the
    two message ids; the client then opens GET /messages/{assistant_id}/events
    to stream the run.

    Multipart form: `content` (text) plus zero or more `image` files (any
    accepted mime — images and PDFs). At least one of content/attachments
    must be present. With url-encoded form, only `content` is read."""
    chat = db.get_chat(chat_id)
    if not chat:
        raise HTTPException(404, "chat not found")

    form = await request.form()
    user_text = str(form.get("content") or "").strip()
    uploads = [
        u for u in form.getlist("image")
        if isinstance(u, UploadFile) and u.filename
    ]
    if not user_text and not uploads:
        raise HTTPException(400, "empty message")

    prior = db.list_messages(chat_id)
    user_msg = db.add_message(chat_id, "user", user_text)

    saved_attachments: list[MessageAttachment] = []
    for upload in uploads:
        try:
            saved_attachments.append(_save_message_upload(user_msg.id, upload))
        except HTTPException:
            # Roll back the user message so the chat stays consistent — the
            # client will surface the error and the user can retry.
            db.delete_message(user_msg.id)
            raise

    # Refetch to pick up the persisted attachments (the model needs them on
    # the live Message object).
    user_msg = db.get_message(user_msg.id) or user_msg

    is_first_turn = not prior and chat.title == "new chat"
    if is_first_turn:
        # Truncated placeholder so the sidebar shows something immediately;
        # haiku will replace it once the assistant message exists (so the
        # title-generated event can ride the open SSE stream).
        title_seed = user_text[:60] if user_text else (
            saved_attachments[0].original_name
            or Path(saved_attachments[0].path).name
        )
        db.update_chat(chat_id, title=title_seed)

    seed = ""
    if not prior:
        seed_query = user_text or "image upload"
        seed = await asyncio.to_thread(
            _seed_context, seed_query, chat.project,
        )
    # If the user only sent images, give the model a placeholder text block
    # so the content array has both text and image parts (some renderers
    # need it). For the rebuilt history transcript, attachments-only turns
    # appear as just the marker.
    prompt_text = user_text or "(see attached image)"
    prompt = _build_prompt(prior, prompt_text, seed=seed)

    assistant_msg = db.add_message(
        chat_id, "assistant", "", tool_calls=None, run_status="running",
    )

    task = asyncio.create_task(
        _run_assistant(assistant_msg.id, chat_id, prompt, chat.model,
                       attachments=saved_attachments),
    )
    _tasks.add(task)
    _runs[assistant_msg.id] = task
    task.add_done_callback(_tasks.discard)

    if is_first_turn:
        title_task = asyncio.create_task(
            _generate_chat_title(chat_id, assistant_msg.id,
                                 user_text or "[image upload]"),
        )
        _tasks.add(title_task)
        title_task.add_done_callback(_tasks.discard)

    return JSONResponse({
        "user_msg_id": user_msg.id,
        "assistant_msg_id": assistant_msg.id,
        "attachments": [
            {
                "id": a.id,
                "url": f"/chat-attachments/{a.message_id}/{Path(a.path).name}",
                "mime_type": a.mime_type,
                "original_name": a.original_name,
            }
            for a in saved_attachments
        ],
    })


@router.get("/messages/{message_id}/events")
async def message_events(message_id: int, after: int = -1) -> StreamingResponse:
    """Tail the event log for an assistant message as SSE. `after` is the
    last seq the client has already seen (-1 means "from the start"). Closes
    when a 'done' event is observed or the message run_status is terminal
    and no further events remain."""
    msg = db.get_message(message_id)
    if not msg:
        raise HTTPException(404, "message not found")
    if msg.role != "assistant":
        raise HTTPException(400, "events only available for assistant messages")

    async def generator() -> AsyncIterator[bytes]:
        last_seq = after
        # Subscribe before draining so we don't miss events written between
        # the drain and the wait.
        sub = _subscribe(message_id)
        try:
            while True:
                events = db.list_message_events(message_id, last_seq)
                saw_done = False
                for ev in events:
                    last_seq = ev["seq"]
                    yield _sse(ev["event_type"], ev["payload"])
                    if ev["event_type"] == "done":
                        saw_done = True
                if saw_done:
                    return

                # No more events buffered. If the run is terminal, we're done.
                current = db.get_message(message_id)
                if current and current.run_status not in (None, "running"):
                    # Terminal but no 'done' event was logged (legacy or race).
                    # Synthesize one so the client can close cleanly.
                    yield _sse("done", {"id": message_id})
                    return

                # Wait for the next append, with a long safety timeout that
                # also serves as a heartbeat so dead connections get cleaned up.
                sub.clear()
                try:
                    await asyncio.wait_for(sub.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
        finally:
            _unsubscribe(message_id, sub)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/chat-attachments/{message_id}/{rest:path}")
def chat_attachment(message_id: int, rest: str) -> FileResponse:
    """Serve a chat message attachment by relative path under
    chat-messages/{message_id}/. Path is sanitized against the
    attachments_dir root."""
    rel = f"chat-messages/{message_id}/{rest}"
    base = settings.attachments_dir.resolve()
    safe = (settings.attachments_dir / rel).resolve()
    if base not in safe.parents:
        raise HTTPException(404, "not found")
    if not safe.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(safe)
