"""Generate a short chat title from the user's first message via `claude -p`.
Uses settings.classify_model (haiku by default) — same pattern as
classify.py: short, sync, returns None if anything fails so the caller can
fall back to whatever placeholder it had."""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Optional

from app.config import settings


log = logging.getLogger(__name__)


PROMPT = """Generate a short chat title (3-7 words) that captures what the \
user wants to discuss in their first message below.

Rules:
- 3 to 7 words.
- Plain text only — no quotes, no markdown, no trailing punctuation.
- Specific over generic. "Debug FastAPI route caching" beats "Help with code".
- Sentence case (capitalize the first word and proper nouns; lowercase the rest).
- Output ONLY the title text. No preamble, no explanation.

User's first message:
{content}"""


def generate_title(content: str, timeout: float = 30.0) -> Optional[str]:
    bin_path = shutil.which(settings.claude_bin)
    if not bin_path:
        log.info("claude CLI not found, skipping title generation")
        return None
    try:
        result = subprocess.run(
            [bin_path, "-p", "--model", settings.classify_model,
             "--output-format", "text",
             PROMPT.format(content=(content or "").strip() or "(empty)")],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("title generation timed out after %ss", timeout)
        return None
    except Exception:
        log.exception("title subprocess failed")
        return None
    if result.returncode != 0:
        log.warning("title claude -p exited %d: %s",
                    result.returncode, (result.stderr or "")[:200])
        return None

    title = (result.stdout or "").strip()
    # Strip surrounding quotes and trailing punctuation that the model
    # sometimes emits despite the rules.
    title = title.strip('"\'`')
    title = title.rstrip(".!?,;:")
    title = title.strip()
    # Reject empty / overlong / multi-line outputs (the model went rogue).
    if not title or "\n" in title or len(title) > 80:
        return None
    return title
