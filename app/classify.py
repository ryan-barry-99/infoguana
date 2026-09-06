import json
import logging
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
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


def _classify_http(prompt: str, timeout: float) -> Optional[Classification]:
    """Classify against an OpenAI-compatible /chat/completions endpoint.

    Deliberately hand-rolled over urllib rather than pulling in an SDK: the
    surface used here is one POST, and the endpoint may be LM Studio,
    Ollama, vLLM or OpenAI itself. Returns None on any failure so the
    caller falls back exactly as it does when the CLI is missing.

    `timeout` is the caller's, not `settings.classify_timeout`. The two
    disagreed by half again (120 vs 180), and the parameter reached only
    the CLI path — so `classify(..., timeout=15)` would silently keep a
    180s bound on any machine with `INFOGUANA_CLASSIFY_BASE_URL` set,
    which is exactly where a hung request is most likely.

    Always parses as text. The HTTP path never sends images (see
    `classify`), so there is no image-mode result to parse.
    """
    url = settings.classify_base_url.rstrip("/") + "/chat/completions"  # type: ignore[union-attr]
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": settings.classify_model,
        "messages": [{"role": "user", "content": prompt}],
        # Low but non-zero: classification is near-deterministic, yet 0.0
        # makes some local models loop on repeated tokens.
        "temperature": 0.2,
        "max_tokens": 600,
    }

    def post(body: dict) -> Optional[dict]:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        if settings.classify_api_key:
            # Unredirected, so the key is not replayed onto a 30x target.
            # `urlopen` follows redirects by default and
            # `HTTPRedirectHandler.redirect_request` copies every header
            # except Content-Length/Content-Type onto the follow-up
            # request — Authorization is not excluded, so a key set in a
            # plain `headers` dict rides along cross-origin. Set here
            # rather than at the top because the Request is rebuilt per
            # attempt by the max_completion_tokens retry.
            req.add_unredirected_header(
                "Authorization", f"Bearer {settings.classify_api_key}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    try:
        try:
            body = post(payload)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            # OpenAI's newer models reject `max_tokens` and require
            # `max_completion_tokens`; older models and most local servers
            # only understand the former. Rather than maintain a model list,
            # send the common spelling and switch on being told to.
            if e.code == 400 and "max_completion_tokens" in detail:
                payload["max_completion_tokens"] = payload.pop("max_tokens")
                body = post(payload)
            else:
                log.warning("classify endpoint %s returned HTTP %s: %.300s",
                            url, e.code, detail)
                return None
        raw = body["choices"][0]["message"]["content"]  # type: ignore[index]
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("classify endpoint %s unreachable: %s", url, e)
        return None
    except (KeyError, IndexError, TypeError, ValueError):
        log.exception("classify endpoint %s returned an unexpected shape", url)
        return None

    parsed = _parse(raw, is_image=False)
    if parsed is None:
        # Small local models drop fields or wrap output in prose. Say so —
        # the note would otherwise land untagged and `unsorted` with nothing
        # in the log to explain why.
        log.warning("classify model %r returned unparseable output: %.200r",
                    settings.classify_model, raw)
    return parsed


def classify(content: str, image_paths: Optional[list[Path]] = None,
             timeout: float = 120.0) -> Optional[Classification]:
    """Classify a note.

    Uses an OpenAI-compatible HTTP endpoint when `classify_base_url` is set,
    otherwise shells out to `claude -p`. With image_paths, the model is
    asked to also produce a searchable description of the image(s). Returns
    None if no backend is available or the call fails — callers fall back to
    `derive_fallback_preview` and leave the note `unsorted`.
    """
    has_images = bool(image_paths)

    if settings.classify_base_url:
        if has_images and not content.strip():
            # Nothing to degrade to. With text alongside the image,
            # classifying the text is a defensible partial result — but an
            # image-only note has none, so the model would be classifying
            # the literal string "(no text)" and returning a type and tags
            # that describe nothing. Text-mode parsing then forces
            # `description=None`, which is the only text an image-only
            # note contributes to FTS and the embedding: the note lands
            # confidently mislabelled and effectively unsearchable.
            # Unsorted is recoverable; a confident wrong label is not.
            log.info("classify endpoint is HTTP and the note is image-only "
                     "(%d image(s), no text) — leaving it unsorted rather "
                     "than classifying an empty prompt",
                     len(image_paths or []))
            return None
        if has_images:
            # Attachments are passed to the CLI as @path references, which
            # has no equivalent here; a vision endpoint would need the
            # base64 image_url content-part form. Classify the text so the
            # note still gets a type and tags.
            log.info("classify endpoint is HTTP; skipping %d image(s), text only",
                     len(image_paths or []))
        prompt = TEXT_PROMPT.format(content=content.strip() or "(no text)")
        return _classify_http(prompt, timeout=timeout)

    bin_path = shutil.which(settings.claude_bin)
    if not bin_path:
        log.info("claude CLI not found at %r and no classify_base_url set, "
                 "skipping classification", settings.claude_bin)
        return None

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
