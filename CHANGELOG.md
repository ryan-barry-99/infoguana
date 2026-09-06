# Changelog

Notable changes to infoguana. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

This file starts partway through the project's life; changes before the first
entry below are recorded only in the commit history.

## Unreleased

### Added

- **CI builds the Docker image and boots the stack on every pull request.**
  It renders the compose file, starts the container, and checks the web
  routes, the MCP endpoint's 401, and that the filesystem tools are off by
  default. Nothing previously exercised the image.
- **Skill notes work end to end.** A `skill` note stores a SKILL.md document
  verbatim, and `context` pins it as a one-line manifest entry — name plus the
  authored trigger condition — instead of its body. An agent reads the listing,
  decides a skill applies, and calls `get(id)` for the instructions.
- **`get_skill(name)` resolves a skill the way it is invoked**, for when the ids
  are gone: after a context summary, or when the user typed `/some-skill`.
  Matching is exact after folding case, separators and a leading slash; a miss
  returns near names, and an unresolvable collision returns the candidates.
- **`infoguana-onboard` now ships as a seeded skill** rather than a file to copy
  into `~/.claude/skills/`. It is inserted on first boot from
  `app/skill_seeds/`, so it reaches every client that can talk to the MCP
  server, not just the one whose skills directory you populated.
- **`add` echoes back the derived `manifest_entry`** for a skill — the name and
  description the manifest will actually carry, with a warning when they came
  from the body rather than parsed frontmatter. A frontmatter mistake is visible
  at write time instead of silently registering the wrong trigger condition.
- The web capture form has a **skill** checkbox that types the note directly.
  Skills bypass classification entirely — the classifier has no `skill` label,
  so a pasted SKILL.md would come back as a `reference` and never reach the
  manifest.
- `INFOGUANA_MCP_ALLOWED_HOSTS` turns on the MCP transport's DNS-rebinding
  protection, checking `Host` and `Origin` against loopback plus the names you
  list (comma-separated, `:*` wildcards the port). Unset — the default —
  performs no such checks; the bearer token remains the real gate either way.
- Each configured host is permitted under both `http://` and `https://`. The
  transport matches `Origin` as a whole string including scheme, so a host
  listed by an operator running behind a TLS proxy previously passed the `Host`
  check and was still refused 403.

### Changed

- **Pinned rules are exempt from `budget_tokens`, like the skill manifest.**
  Charged against it they exhausted the allowance, so every project silently
  lost half its global rules, all of its project-specific ones, and its
  memories — the sort is global-first, so the project's own rules were always
  the casualty. Rules now carry their own caps and report through
  `rules_truncated`.
- **Re-run the installer after upgrading.** Sessions now receive the rules and
  memories that were previously dropped, which roughly triples the onboard
  blob, and the SessionStart slice count is derived at install time. An install
  sized against the old blob delivers only the first third of the new one.
- **`context` reports its budget in parts.** `notes_tokens_est` is what was
  charged, `rules_tokens_est` and `skills_tokens_est` are the exempt sections,
  and `total_tokens_est` is the whole payload. The onboard header prints the
  charged figure, which previously read as a threefold overrun.
- **The onboard blob renders pinned plans and tasks.** They were already
  charged against the budget and suppressed from the memory listing, so the
  blob paid for content it did not contain. An empty memory list now also says
  whether it was crowded out rather than genuinely empty.
- **Per-note budget sizing allows 110 tokens of serialization envelope**
  instead of 20. The old figure ignored the metadata every note carries, so a
  caller asking for 4,000 tokens of notes received roughly 9,400.
- The rule pin scopes in SQL via `list_scoped_notes` rather than fetching every
  project's rules and filtering in Python. The old path applied its cap across
  the whole corpus, so a large set elsewhere could evict this project's rules.
- **The skill manifest is exempt from `budget_tokens`.** A session that can
  afford no memories still has to know which capabilities it has. It carries
  its own caps instead, and the onboard header reports the exempt cost in a
  separate clause rather than billing it to the notes allowance.
- `update` re-derives the preview when a note's type crosses the `skill`
  boundary in either direction. A skill's preview is a function of its type,
  so a pure retype previously left an unrelated summary in place.
- **Breaking:** `INFOGUANA_FS_ALLOWLIST` is now empty by default, so
  `read_file` / `list_dir` / `grep` are off until an operator names the roots
  they may read under. Installs relying on the old `/root/code` default must set
  it explicitly, or those tools will refuse every call.

### Fixed

- **`docker-compose.yml` parses under older Compose again.** `env_file` used
  the `path:`/`required:` long form, which needs Compose v2.24+ and makes
  earlier versions reject the entire file rather than ignore the key. It is
  back to the short form, so `.env` must now exist — `cp .env.example .env`.
- **Setting `INFOGUANA_PORT` no longer publishes a port with nothing behind
  it.** The variable fed both the host side of the mapping and the app's own
  listen port, while the mapping's target and the healthcheck stayed on 8789,
  so any value but the default was unreachable. It is now the host port only.
- **Clients reaching the MCP endpoint by LAN or tailnet address are no longer
  refused `421 Invalid Host header`.** The transport left its security settings
  unset to mean "no checks", but the SDK reads that as a cue to auto-enable a
  loopback-only allowlist; they are now disabled explicitly. Only installs that
  never set `INFOGUANA_MCP_ALLOWED_HOSTS` were affected.
- The `mcp` floor is now 1.10, the first release carrying
  `mcp.server.transport_security`. An environment already holding 1.9.x
  satisfied the old `>=1.0` floor, so pip left it in place and the server
  failed at boot with `ModuleNotFoundError` rather than at install.
- `config.json` is no longer denied everywhere. It sat in the basename denylist
  despite a comment saying it applied only under `.docker/`, so every project's
  own `config.json` was refused. The Docker credentials file is still blocked by
  its full path.

## v0.2.0 — 2026-08-17

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
  `/onboard/sizing`, and `INFOGUANA_HOOK_CHUNKS` is now validated against the
  route's accepted range where it previously accepted anything.
- `scripts/init-project-infoguana.py` takes `--agent claude|codex|both`, writing
  `CLAUDE.md`, `AGENTS.md`, or the pair. Codex reads a different filename, so
  without this a Codex user had no way to wire up a project.
- `budget_tokens` on the onboard routes is bounded at 64000 and rejected outside it.
  Each value minted its own build-cache entry per project, so an unbounded parameter
  was an unbounded cache.

### Fixed

- A failed context slice is now visible in the session instead of silently absent.
  The hook injects a short notice naming the failure, so a partial load stops looking
  identical to a complete one. Only the first slice emits it — an outage fails every
  slice at once, and one notice per slice cost more context than the brief it replaced.
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
- The installers size their hook count at the budget the hook will actually request,
  not the server's default. A custom `INFOGUANA_ONBOARD_BUDGET` changes how large the
  blob is, so sizing that ignored it registered too few chunks and truncated most of
  every slice.
- A first install is no longer sized as if sessions receive nothing. `/onboard/sizing`
  enumerated known projects only, so an empty corpus recommended one chunk while every
  session still got ~12KB of global rules needing nine. Sizing now floors at the blob an
  unknown project receives.
- Re-installing from a different checkout no longer doubles the registration. Hook
  ownership was keyed to the installing checkout's absolute path, so a second copy
  of the repo saw the first one's entries as a stranger's hooks, kept them, and
  appended its own beside them.
- Both installers now confirm before repointing an integration registered from
  another checkout, and refuse outright when there is no TTY. Pass `--force` to
  replace it anyway.
- The Codex installer reports a sizing shortfall instead of discarding it, and both
  installers now name the globals-only blob when that is what will not fit. A fresh
  install is exactly the case the projects list is silent about.
- The undersized-delivery notice no longer evicts the content it warns about. It rides
  whichever slice has the most room rather than always the first, and is skipped only
  when adding it would push an otherwise-fitting slice over the cap. A slice already
  over the cap still carries it, since that is the case losing the most content.

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
