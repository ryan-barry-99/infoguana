"""SessionStart onboarding endpoints.

Per note #374: Claude Code caps each `additionalContext` hook output at
~2KB inline, but the cap is *per-hook* — register N hooks and each gets
its own ~2KB inline window with no truncation. We exploit that here:
`/onboard/<project>/chunk/<i>?of=<n>` slices the full onboard blob into
N line-aligned pieces; the installer wires up N hook entries that each
fetch one slice. All N slices land inline; the agent sees the full
~22KB blob at session start with no Read-tool round-trip.

`/onboard/<project>` (no chunking) is preserved for non-hook callers
(web UI / debugging)."""
import hmac
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import PlainTextResponse

from app import onboard
from app.config import settings


router = APIRouter(tags=["onboard"])


# Target inline-safe chunk size. The harness's additionalContext cap is
# empirically ~2KB inline; we leave headroom for the JSON wrapper and any
# unicode-multibyte characters in note content.
CHUNK_TARGET_BYTES = 1700


def _bearer_auth(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {settings.mcp_secret}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})


def _line_aligned_chunks(text: str, n: int) -> list[str]:
    """Split `text` into exactly `n` line-aligned chunks by computing
    n+1 evenly-spaced offsets and snapping each interior offset back to
    the start of its containing line. Yields chunks of comparable size
    with no tail-end accumulation — the previous close-when-over-target
    approach produced ~n+1 chunks and ballooned the last slot by
    ~2x when surplus was merged. Empty input yields n empty strings;
    very short blobs may produce empty leading/trailing chunks (the
    agent's hook emits nothing for those — a no-op). Pathological case
    (a single line longer than total/n) puts that whole line in one
    chunk that may spill, but the others still land inline. Operates
    on character offsets, which equals byte offsets for ASCII content
    (the onboard blob is mostly ASCII)."""
    if n <= 0:
        return []
    if not text:
        return [""] * n
    total = len(text)
    boundaries = [(i * total) // n for i in range(n + 1)]
    snapped = [0]
    for b in boundaries[1:-1]:
        nl = text.rfind("\n", 0, b)
        snapped.append(nl + 1 if nl != -1 else 0)
    snapped.append(total)
    for i in range(1, len(snapped)):
        if snapped[i] < snapped[i - 1]:
            snapped[i] = snapped[i - 1]
    return [text[snapped[i]:snapped[i + 1]] for i in range(n)]


@router.get("/onboard/{project}/chunk/{index}", response_class=PlainTextResponse,
            dependencies=[Depends(_bearer_auth)])
def onboard_chunk(project: str, index: int, of: int = 16,
                  budget_tokens: int = 4000) -> str:
    """Return chunk `index` (0-based) of `of` deterministic chunks of the
    project's onboard blob. Each chunk is line-aligned and sized to fit
    inside the harness's per-hook ~2KB cap. See module docstring.

    The body is emitted unwrapped — chunks concatenate transparently into
    the agent's context as one combined blob. Hooks fire in registration
    order, and line-aligned slicing keeps splits at line boundaries, so
    the stitched output reads naturally even though the harness joins it
    from N independent hook responses."""
    if of <= 0 or of > 64:
        raise HTTPException(400, "of must be 1..64")
    if index < 0 or index >= of:
        raise HTTPException(400, f"index must be 0..{of - 1}")
    blob = onboard.build_cached(project=project, budget_tokens=budget_tokens)
    chunks = _line_aligned_chunks(blob, of)
    body = chunks[index]
    if not body:
        return ""
    return body if body.endswith("\n") else body + "\n"


@router.get("/onboard/{project}", response_class=PlainTextResponse,
            dependencies=[Depends(_bearer_auth)])
def onboard_project(project: str, budget_tokens: int = 4000) -> str:
    """Full onboard blob (no chunking). Preserved for web UI / debugging.
    Hook callers should use /onboard/<project>/chunk/<i>?of=<n>."""
    return onboard.build(project=project, budget_tokens=budget_tokens)
