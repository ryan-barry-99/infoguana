# Changelog

Notable changes to infoguana. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

This file starts partway through the project's life; changes before the first
entry below are recorded only in the commit history.

## Unreleased

### Added

- Codex is a supported client alongside Claude Code. `scripts/install-infoguana-codex.py`
  writes a managed block into `~/.codex/config.toml` registering the MCP server and the
  SessionStart hooks, reusing the same `~/.infoguana.env` the Claude Code installer
  creates. Everything outside the marker comments is preserved across re-runs.
- `scripts/_infoguana_setup.py`, shared installer helpers with atomic writes and
  `.env` parsing, so a failed install cannot leave a half-written config or a stray
  temp file behind.
- `GET /onboard/sizing` reports each project's blob size and the chunk count needed
  to deliver the largest without a slice exceeding the inline cap. Both installers
  call it, so the hook count is measured rather than guessed.

### Changed

- The SessionStart hook serves either agent unmodified, adapting only the text that
  names which built-in memory store to leave alone. Set `INFOGUANA_AGENT=claude|codex`
  when autodetection guesses wrong.
- The onboard protocol no longer opens by telling the agent it is Claude Code. Note
  that the protocol row is seeded once and then owned by the user, so existing
  installs keep the old wording until edited in the web UI.
- Chunk boundaries are chosen to avoid splitting a heading from its body or breaking
  a rule mid-sentence, instead of snapping to evenly-spaced line starts. The slice
  ceiling rose from 64 to 128.
- The installers no longer register a fixed 16 hooks; the count comes from
  `/onboard/sizing`, and `INFOGUANA_HOOK_CHUNKS` now validates a 1..64 range that
  previously accepted anything.
- `scripts/init-project-infoguana.py` takes `--agent claude|codex|both`, writing
  `CLAUDE.md`, `AGENTS.md`, or the pair. Codex reads a different filename, so
  without this a Codex user had no way to wire up a project.
- Chunk-count resolution moved into `scripts/_infoguana_setup.py`, shared by both
  installers. `INFOGUANA_HOOK_CHUNKS` is validated before any credential or network
  work, and its upper bound now tracks the route's own ceiling of 128 rather than a
  hardcoded 64 that had gone stale.

### Fixed

- A failed context slice is now visible in the session instead of silently absent.
  The hook injects a short notice naming the failure, so a partial load stops looking
  identical to a complete one.
- `INFOGUANA_HOOK_DISABLE=1` now suppresses the memory-override slice too, not only
  the context slices. The web UI's chat seeds its own context and sets this, so it
  was receiving roughly 1KB of override text on every turn.
- A server without `/onboard/sizing` is reported as such instead of as unreachable.
  The request falls through to `/onboard/{project}` and returns 200, so the old
  message blamed the network on a server that was plainly answering.
- `scripts/install-infoguana-mcp.py` writes `~/.claude.json` atomically. It holds
  every project's Claude Code history and MCP config, and a truncating write left it
  empty if interrupted.
- Re-running an installer re-applies mode 600 to `~/.infoguana.env` even when the
  contents need no change, so a file whose permissions drifted stops being reported
  as fine while holding a readable bearer token.
- An invalid `INFOGUANA_HOOK_CHUNKS` prints a one-line error from the Codex installer
  instead of an uncaught traceback.

## v0.1.0 — 2026-08-16

### Added

- `skill` is a first-class note type, accepted by `NoteType`, the MCP write
  and read paths, the web edit form and browse filter, and the graph legend.
  It is authored by hand: the classifier never assigns it, by design.
- `READABLE_TYPES`, the set of types a caller may filter reads by. It is a
  superset of the write set, because `unsorted` is a real state worth listing
  but not one to assign by hand.

### Changed

- **Breaking for callers passing a bad type.** An unrecognized note type is now
  an error from `search`, `context`, `add` and `update`, naming the valid set.
  A stale or misspelled type that used to return plausible-looking results now
  fails instead.
- Reads no longer widen when the filter is unrecognized. An unknown type used
  to coerce to "no filter" rather than "no matches", so the caller received a
  full unfiltered payload it had every reason to believe was narrowed.
- Writes no longer fall through to the classifier on an unrecognized type. It
  cannot produce `rule` or `skill` at all, so a typo landed the note under a
  type nobody asked for and no later session could tell.
- `provenance_note` is clamped to 200 characters in preview-mode hits from
  `search` and `similar`. It was the one serialized field with no length
  discipline, and on the largest notes it cost several times what a preview
  is budgeted for. Full text still comes back from `expand_top`, `get` and
  `get_many`.
- Edge inference recognizes `feature`, `task`, `rule` and `skill` as type
  hints before a `#N` reference. Without them such a reference degraded to a
  *bare* proposal, which `infer_edges` discards unless asked for it, so the
  edge silently never formed.

### Fixed

- A database containing `skill` notes could not be read at all. `NoteType`
  lacked the literal, so pydantic raised `literal_error` on every row it
  materialized, taking down `/browse`, `get_note`, `hybrid_search` and
  `build_context`.
- Saving a note from the edit card no longer silently retypes it. A type
  missing from the form's whitelist left the dropdown with no matching
  option, so the browser posted the empty "auto (reclassify)" value and the
  save reset the note to `unsorted`.
