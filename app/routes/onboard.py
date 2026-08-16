"""SessionStart onboarding endpoints.

Claude Code caps each `additionalContext` hook output at ~2KB inline, but
the cap is *per-hook* — register N hooks and each gets its own ~2KB inline
window with no truncation. We exploit that here:
`/onboard/<project>/chunk/<i>?of=<n>` slices the full onboard blob into
N line-aligned pieces; the installer wires up N hook entries that each
fetch one slice. All N slices land inline; the agent sees the full
~22KB blob at session start with no Read-tool round-trip.

`/onboard/<project>` (no chunking) is preserved for non-hook callers
(web UI / debugging)."""
import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import PlainTextResponse

from app import db, onboard
from app.config import settings


router = APIRouter(tags=["onboard"])
log = logging.getLogger(__name__)


# Target inline-safe chunk size. The harness's additionalContext cap is
# empirically ~2KB inline; we leave headroom for the JSON wrapper and any
# unicode-multibyte characters in note content.
CHUNK_TARGET_BYTES = 1700

# Upper bound on hook entries the chunk route will serve. Not a harness
# limit — a sanity bound on how many subprocesses a session start should
# spawn. Raised 64 -> 128 once the largest project needed 74: the old
# value had drifted from "far above anything real" to "exactly the
# requirement", which is how a sanity bound turns into silent truncation.
# A surplus entry is a no-op (empty slice, hook emits nothing), so the
# cost of headroom is one wasted subprocess; the cost of shortfall is
# lost rules. Re-derive with the installer as the corpus grows.
MAX_CHUNKS = 128


def _bearer_auth(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {settings.mcp_secret}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})


def _break_candidates(text: str) -> tuple[list[int], set[int]]:
    """Line-start offsets where a split won't sever a markdown unit, plus
    the subset that are also paragraph boundaries (preceded by a blank
    line) and therefore preferable.

    Two placements produce broken output and are excluded:

    - **Directly after a heading.** The heading lands at the tail of one
      slice and its body at the head of the next. Since the harness
      assembles slices as independent blocks and not necessarily in
      registration order, the reader sees an empty section and its
      content detached somewhere else. That is exactly how a
      session came to report "a `## skills available`
      heading ... which came through empty" alongside "a separate skills
      preamble further down" — one heading, orphaned, read as two things.
    - **Inside a fenced code block.** Splitting between ``` fences leaves
      an unterminated fence in one slice and a stray closer in the other,
      so following prose renders as code.
    """
    safe: list[int] = []
    paragraph: set[int] = set()
    in_fence = False
    prev_meaningful = ""
    prev_blank = False
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not in_fence and not prev_meaningful.startswith("#"):
            safe.append(pos)
            if prev_blank:
                paragraph.add(pos)
        if stripped.startswith("```"):
            in_fence = not in_fence
        if stripped:
            prev_meaningful = stripped
        prev_blank = not stripped
        pos += len(line)
    return safe, paragraph


def _line_aligned_chunks(text: str, n: int) -> list[str]:
    """Split `text` into exactly `n` chunks, breaking only at offsets that
    keep markdown units intact (see `_break_candidates`).

    Boundaries start as n+1 evenly-spaced offsets, then each interior one
    snaps *backward* to the best available break: a paragraph boundary if
    one is in reach, otherwise any safe line start, otherwise the raw
    line start it sits in. Even spacing avoids tail-end accumulation —
    the previous close-when-over-target approach produced ~n+1 chunks and
    ballooned the last slot ~2x when surplus was merged.

    Snapping backward pushes bytes forward into the following slice, so a
    slice can exceed total/n; `_chunks_fitting` measures the real split
    rather than assuming the average. Empty input yields n empty strings;
    very short blobs may produce empty leading/trailing chunks (the
    agent's hook emits nothing for those — a no-op). Operates on
    character offsets, which equals byte offsets for ASCII content (the
    onboard blob is mostly ASCII)."""
    if n <= 0:
        return []
    if not text:
        return [""] * n
    total = len(text)
    safe, paragraph = _break_candidates(text)

    def _snap(ideal: int) -> int:
        # Nearest safe break at or before `ideal`; prefer a paragraph
        # boundary when one sits within the same slice's worth of text,
        # so sections stay whole rather than merely lines.
        window = max(1, total // n)
        best_line = 0
        best_para = -1
        for off in safe:
            if off > ideal:
                break
            best_line = off
            if off in paragraph:
                best_para = off
        if best_para >= 0 and ideal - best_para <= window:
            return best_para
        if best_line:
            return best_line
        nl = text.rfind("\n", 0, ideal)
        return nl + 1 if nl != -1 else 0

    snapped = [0]
    for i in range(1, n):
        snapped.append(_snap((i * total) // n))
    snapped.append(total)
    for i in range(1, len(snapped)):
        if snapped[i] < snapped[i - 1]:
            snapped[i] = snapped[i - 1]
    return [text[snapped[i]:snapped[i + 1]] for i in range(n)]


def chunks_needed(blob: str) -> int:
    """Smallest chunk count (1..MAX_CHUNKS) at which no line-aligned slice of
    `blob` exceeds CHUNK_TARGET_BYTES.

    Measures the real split rather than predicting it from the total.
    `ceil(total / target)` bounds only the *average* slice: interior
    boundaries snap backward to a line start, which pushes those bytes
    forward into the following slice, so one long line can push a single
    slice past the even split by its own length. Concretely,
    one 77,679 B blob at the ceil-derived 46 still
    produced a 2,288 B worst slice against a ~2,048 B cap — the average
    was fine and one slice was over anyway.

    Returns MAX_CHUNKS (the route's ceiling) if even that can't satisfy the
    target; callers report the shortfall rather than silently accepting
    over-cap slices.
    """
    return _chunks_fitting([blob])


def _widest_slice(blob: str, n: int) -> int:
    """Byte length of the largest of `n` line-aligned slices of `blob`."""
    return max(
        (len(s.encode("utf-8")) for s in _line_aligned_chunks(blob, n)),
        default=0,
    )


def _chunks_fitting(blobs: list[str]) -> int:
    """Smallest chunk count (1..MAX_CHUNKS) at which *every* blob in `blobs` splits
    with no slice over CHUNK_TARGET_BYTES.

    One hook count serves every project, so the count has to be validated
    against all of them together. Taking each project's own minimum and
    then the max across projects is not equivalent and not safe: whether a
    slice fits depends on where line boundaries happen to fall, so the
    property is NOT monotonic in n. One real blob fits at n=49
    and then *regresses* to an 1,845 B worst slice at n=62 — a count
    derived as "the largest per-project minimum" therefore silently broke
    a project that had already been fine at a smaller count.

    Returns MAX_CHUNKS if even the ceiling can't satisfy every blob; callers
    report the shortfall rather than accept over-cap slices quietly.
    """
    real = [b for b in blobs if b]
    if not real:
        return 1
    floor = max(
        1,
        max(-(-len(b.encode("utf-8")) // CHUNK_TARGET_BYTES) for b in real),
    )
    for n in range(floor, MAX_CHUNKS + 1):
        if all(_widest_slice(b, n) <= CHUNK_TARGET_BYTES for b in real):
            return n
    return MAX_CHUNKS


@router.get("/onboard/sizing", dependencies=[Depends(_bearer_auth)])
def onboard_sizing(budget_tokens: int = 4000) -> dict:
    """Per-project onboard blob sizes plus the chunk count needed to
    deliver the largest one without any slice exceeding the inline cap.

    Called by scripts/install-infoguana-hooks.py at install time. Blobs
    are built through `build_cached`, so the 25-odd projects cost one
    pass and re-serve from cache.

    Each project reports its own `needed` count (measured, see
    chunks_needed) alongside `bytes`; `recommended_chunks` is the max
    across all of them, since one hook count serves every project. The
    installer prints any project whose `needed` exceeds what it can
    register, so an undersized split is visible at install time rather
    than inferred from garbled context weeks later.
    """
    blobs: dict[str, str] = {
        name: onboard.build_cached(project=name, budget_tokens=budget_tokens)
        for name in db.list_project_names()
    }
    recommended = _chunks_fitting(list(blobs.values()))
    sizes = [
        {
            "project": name,
            "bytes": len(blob.encode("utf-8")),
            "needed": chunks_needed(blob),
            # Worst slice this project actually gets at the recommended
            # count — the number that decides whether it fits, since the
            # installed count is global and each project's own minimum
            # says nothing about how it splits at someone else's.
            "widest_at_recommended": _widest_slice(blob, recommended),
        }
        for name, blob in blobs.items()
    ]
    sizes.sort(key=lambda s: -s["widest_at_recommended"])
    return {
        "chunk_target_bytes": CHUNK_TARGET_BYTES,
        "budget_tokens": budget_tokens,
        "max_bytes": max((s["bytes"] for s in sizes), default=0),
        "recommended_chunks": recommended,
        "fits_all": all(
            s["widest_at_recommended"] <= CHUNK_TARGET_BYTES for s in sizes
        ),
        "projects": sizes,
    }


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
    from N independent hook responses.

    An over-cap split does not fail — the harness truncates each slice, so
    content vanishes mid-line with nothing saying so. That is how a
    16-chunk split kept serving a blob that had grown to need 35, dropping
    roughly the back half of every slice for weeks. When it happens now,
    the mechanism detail goes to the server log (operators can act on it)
    and the agent gets one sentence saying its brief may be short and how
    to fetch the rest.

    Chunking is transport and stays out of the agent's view: no chunk
    indices, no counts, no installer commands in anything returned here.
    Agent-visible text that describes the delivery mechanism is noise the
    agent cannot act on, and it invites reasoning about slices instead of
    about memory."""
    if of <= 0 or of > MAX_CHUNKS:
        raise HTTPException(400, f"of must be 1..{MAX_CHUNKS}")
    if index < 0 or index >= of:
        raise HTTPException(400, f"index must be 0..{of - 1}")
    blob = onboard.build_cached(project=project, budget_tokens=budget_tokens)
    chunks = _line_aligned_chunks(blob, of)
    body = chunks[index]
    if index == 0:
        # Measure the split actually being served, not the smallest count
        # that would work. Fit is not monotonic in n (see _chunks_fitting):
        # boundaries snap backward to a line start, so a blob that fits at
        # 35 can regress at 36, and `needed <= of` therefore does not mean
        # this split fits. Every project in the current corpus has some
        # `of` at or above its own `needed` at which slices run 1.7-2.4 KB
        # against the cap — which is to say the old guard was silent for
        # all of them.
        widest = max((len(c.encode("utf-8")) for c in chunks), default=0)
        if widest > CHUNK_TARGET_BYTES:
            needed = chunks_needed(blob)
            log.warning(
                "onboard delivery undersized for %s: %d chunks registered, "
                "%d needed for %d B — widest slice is %d B against a %d B "
                "cap, so slices are being truncated. Re-run "
                "scripts/install-infoguana-hooks.py to re-derive the count.",
                project, of, needed, len(blob.encode("utf-8")),
                widest, CHUNK_TARGET_BYTES,
            )
            body = (
                "_Some of this project's memory may be missing from this "
                "brief. Call `context(project=...)` for the full set before "
                "relying on rules or plans._\n\n"
            ) + body
    if not body:
        return ""
    return body if body.endswith("\n") else body + "\n"


@router.get("/onboard/{project}", response_class=PlainTextResponse,
            dependencies=[Depends(_bearer_auth)])
def onboard_project(project: str, budget_tokens: int = 4000) -> str:
    """Full onboard blob (no chunking). Preserved for web UI / debugging.
    Hook callers should use /onboard/<project>/chunk/<i>?of=<n>."""
    return onboard.build(project=project, budget_tokens=budget_tokens)
