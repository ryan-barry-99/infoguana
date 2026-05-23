import json
import logging
import re
import shutil
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Optional

from app.config import settings
from app.models import NoteType


log = logging.getLogger(__name__)

VALID_TYPES = {"idea", "memory", "feedback", "feature", "reference", "plan", "task"}

TEXT_PROMPT = """You are classifying a note captured into a personal knowledge base.

Respond with ONLY a single JSON object, no prose, no code fences, with keys:
  "type":    one of idea | memory | feedback | feature | reference | plan | task
  "tags":    array of 0-3 short lowercase tags (single words or hyphenated)
  "project": project name if one is clearly implied, else null
  "title":   a 3-8 word summary title
  "preview": 1-5 lines, max ~200 chars, plain text (no markdown headers,
             no bullet markers, no code fences). A faithful one-glance
             summary that lets a reader decide whether to fetch the full
             note. Lead with the punchline; drop ceremony/headings.

Type definitions:
- idea:      a rough thought or proposal that hasn't been fleshed out yet
- memory:    something learned or experienced, a fact worth retaining
- feedback:  a preference, rule, or correction about how work should be done
- feature:   a deliverable that has shipped (or is shipping) — name + story +
             list of PRs. Use this for "we built X" / "X now works like Y"
             notes, not for ongoing-work status.
- reference: a pointer to an external resource, link, or system
- plan:      a developed feature/change the user intends to build that will
             produce a new deliverable — goal + approach + open questions, the
             kind of intent that "graduates" into a feature once shipped.
             Classify as 'plan' only when there's enough substance to come
             back to weeks later AND the work spawns a new deliverable.
- task:      a tracked unit of work that does NOT produce a new deliverable —
             PR reviews, bug fixes, chores, refactors, follow-ups. Same
             lifecycle as a plan (not_started/pending/complete) but no
             graduation. If the note describes something to do but doesn't
             scope out a new feature, it's a 'task', not a 'plan'.

Note to classify:
<<<
{content}
>>>"""


IMAGE_PROMPT = """You are ingesting a note into a personal knowledge base. The note consists of the user's text (possibly empty) PLUS one or more attached images shown above/below this prompt. Read the image(s) carefully — including any visible text, diagrams, or UI elements — and produce a single JSON object, no prose, no code fences:

  "type":        one of idea | memory | feedback | feature | reference | plan | task
  "tags":        array of 0-4 short lowercase tags
  "project":     project name if clearly implied, else null
  "title":       a 3-8 word summary title
  "description": 1-3 sentences describing what the image shows, including any
                 notable text, UI elements, or context. Aim for searchable —
                 someone looking for this screenshot weeks from now should be
                 able to find it by describing its contents.
  "preview":     1-5 lines, max ~200 chars, plain text (no markdown). A
                 faithful one-glance summary of the note (text + image)
                 that lets a reader decide whether to fetch the full note.

Type definitions:
- idea:      a rough thought, proposal, or thing to try (not yet fleshed out)
- memory:    something worth retaining (a fact, lesson, snippet)
- feedback:  a preference or rule about how to do work
- feature:   a deliverable that has shipped (or is shipping) — name + story +
             list of PRs; use for "we built X" not ongoing status
- plan:      a developed feature/change with goal + approach that will produce
             a new deliverable. 'plan' only when substantive enough to
             implement from weeks later AND scoped to spawn a new feature.
- task:      a tracked unit of work that does NOT produce a new deliverable —
             PR reviews, bug fixes, chores, refactors, follow-ups. Same
             lifecycle as a plan (not_started/pending/complete) but no
             graduation. Things to do that don't scope out a new feature.
- reference: a pointer to an external resource (link, docs, a found gem)

User's accompanying text (may be empty):
<<<
{content}
>>>"""


class Classification:
    def __init__(self, type: NoteType, tags: list[str], project: Optional[str],
                 title: Optional[str], description: Optional[str] = None,
                 preview: Optional[str] = None):
        self.type = type
        self.tags = tags
        self.project = project
        self.title = title
        self.description = description
        self.preview = preview

    def __repr__(self) -> str:
        return (f"Classification(type={self.type!r}, tags={self.tags!r}, "
                f"project={self.project!r}, desc={bool(self.description)}, "
                f"preview={bool(self.preview)})")


PREVIEW_MAX_LINES = 5
PREVIEW_MAX_CHARS = 200


def clamp_preview(text: str) -> str:
    """Trim a preview to PREVIEW_MAX_LINES lines OR PREVIEW_MAX_CHARS chars,
    whichever bound hits first. Strips leading/trailing whitespace and any
    accidental code fences. Appends an ellipsis if truncation occurred."""
    if not text:
        return ""
    s = text.strip()
    # Strip ``` fences a hallucinating model might emit.
    if s.startswith("```"):
        s = s.lstrip("`").lstrip()
        if s.endswith("```"):
            s = s.rstrip("`").rstrip()
    truncated = False
    lines = s.splitlines()
    if len(lines) > PREVIEW_MAX_LINES:
        lines = lines[:PREVIEW_MAX_LINES]
        truncated = True
    s = "\n".join(lines).rstrip()
    if len(s) > PREVIEW_MAX_CHARS:
        s = s[:PREVIEW_MAX_CHARS].rstrip()
        truncated = True
    if truncated and not s.endswith("…"):
        s += "…"
    return s


def derive_fallback_preview(content: str) -> str:
    """Cheap, no-LLM preview: first non-empty line of content, stripped of
    leading markdown markers and clamped. Used when the haiku call is
    unavailable or skipped (e.g. on content edits to a note whose type is
    already established)."""
    for raw in (content or "").splitlines():
        line = raw.strip().lstrip("#").lstrip("-*").rstrip("*").strip()
        if line:
            return clamp_preview(line)
    return ""


def _extract_json(raw: str) -> Optional[dict]:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def _parse(raw_output: str, is_image: bool) -> Optional[Classification]:
    data = _extract_json(raw_output)
    if not data:
        log.warning("could not extract JSON from classifier output: %r", raw_output[:300])
        return None

    raw_type = str(data.get("type", "")).lower().strip()
    if raw_type not in VALID_TYPES:
        log.warning("invalid classified type %r, defaulting to 'idea'", raw_type)
        raw_type = "idea"

    tags = data.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(t).lower().strip() for t in tags if str(t).strip()][:5]

    project = data.get("project")
    if project is not None:
        project = str(project).strip() or None

    title = data.get("title")
    if title is not None:
        title = str(title).strip() or None

    description = data.get("description") if is_image else None
    if description is not None:
        description = str(description).strip() or None

    preview_raw = data.get("preview")
    preview = clamp_preview(str(preview_raw)) if preview_raw else None
    if preview == "":
        preview = None

    return Classification(type=raw_type, tags=tags, project=project,  # type: ignore[arg-type]
                          title=title, description=description, preview=preview)


def _downscaled_copy(src: Path, stack: ExitStack) -> Path:
    """Produce a temp JPEG downscaled to settings.classify_image_max_px on
    the longest side. Returns the temp path; caller's ExitStack cleans it up.
    If Pillow isn't available or the image can't be opened, returns src."""
    try:
        from PIL import Image  # lazy import
    except Exception:
        return src

    try:
        with Image.open(src) as im:
            im.load()
            w, h = im.size
            longest = max(w, h)
            if longest <= settings.classify_image_max_px:
                return src
            scale = settings.classify_image_max_px / longest
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
            im = im.resize(new_size, Image.LANCZOS)
            tmp = tempfile.NamedTemporaryFile(
                prefix="infoguana-cls-", suffix=".jpg", delete=False
            )
            stack.callback(lambda p=tmp.name: Path(p).unlink(missing_ok=True))
            im.save(tmp.name, format="JPEG", quality=82, optimize=True)
            tmp.close()
            return Path(tmp.name)
    except Exception:
        log.exception("resize failed for %s, sending original", src)
        return src


def classify(content: str, image_paths: Optional[list[Path]] = None,
             timeout: float = 120.0) -> Optional[Classification]:
    """Classify a note via `claude -p`. With image_paths, the CLI is asked to
    also produce a searchable description of the image(s). Returns None if
    the claude CLI is unavailable or the call fails."""
    bin_path = shutil.which(settings.claude_bin)
    if not bin_path:
        log.info("claude CLI not found at %r, skipping classification", settings.claude_bin)
        return None

    has_images = bool(image_paths)
    template = IMAGE_PROMPT if has_images else TEXT_PROMPT
    prompt_body = template.format(content=content.strip() or "(no text)")

    with ExitStack() as stack:
        if has_images:
            scaled = [_downscaled_copy(p, stack) for p in image_paths]  # type: ignore[union-attr]
            attachments = "\n".join(f"@{p}" for p in scaled)
            prompt = f"{attachments}\n\n{prompt_body}"
        else:
            prompt = prompt_body

        try:
            result = subprocess.run(
                [bin_path, "-p", "--model", settings.classify_model,
                 "--output-format", "text", prompt],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            log.warning("classification timed out after %ss", timeout)
            return None
        except Exception:
            log.exception("classification subprocess failed")
            return None

    if result.returncode != 0:
        log.warning("claude -p returned %d: %s", result.returncode, result.stderr[:300])
        return None

    return _parse(result.stdout, is_image=has_images)
