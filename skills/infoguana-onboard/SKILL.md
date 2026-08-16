---
name: infoguana-onboard
description: Write a minimal project-level instruction file (CLAUDE.md for Claude Code, AGENTS.md for Codex) that delegates memory to the shared infoguana MCP server. Use when the user wants to onboard a project to infoguana, set up infoguana memory for a new repo, initialize a fresh project with infoguana-first memory under any agent, or slim a bloated CLAUDE.md down to a pointer. Outputs a ~12-line file so the instruction file does not tax the context window on every session.
---

# Infoguana Onboard

Creates a minimal project-level instruction file that delegates memory to the shared **infoguana** MCP server. The file is a *pointer* to infoguana protocol (which lives in the SessionStart hook preamble), not a restatement of it — keeping per-session context small.

## When to use

- User asks to "onboard this project to infoguana" / "set up infoguana for this repo" / "write an infoguana CLAUDE.md"
- A fresh project needs infoguana-first protocol wired up for whichever agent will read it
- An existing project has a bloated `CLAUDE.md` that should be slimmed to just-a-pointer

## Which agent, and where the file goes

**Ask first if it is not obvious from the session:** Claude Code, Codex, or both. The two read different filenames from different places, and writing the wrong one fails silently — the agent simply never loads it and the project looks like it has no infoguana wiring at all.

**Codex** has no per-project user-private memory directory. Its instruction file is `AGENTS.md` in the **repo root**, which means it is committed and shared with collaborators. There is no private variant to offer; if the user wants it uncommitted, that is a `.gitignore` decision, not a different path. For that case — and for a committed `CLAUDE.md` — [`scripts/init-project-infoguana.py`](../../scripts/init-project-infoguana.py) already stamps the same template with `--agent claude|codex|both`; prefer it over doing the work by hand.

**Claude Code** is the user-private path described below, and it is the rest of this skill. Steps 4-9 are Claude-Code-specific; for Codex, write the step-8 body into `<repo-root>/AGENTS.md` and skip the slug, the memory folder, and the step-6 migration (there is nothing to migrate — that directory is a Claude Code concept).

This skill writes a **user-private** `CLAUDE.md` into Claude Code's project data folder for the current cwd:

```
<claude-data-dir>/projects/<slug>/memory/CLAUDE.md
```

- Linux / macOS: `~/.claude/projects/<slug>/memory/CLAUDE.md`
- Windows: `%USERPROFILE%\.claude\projects\<slug>\memory\CLAUDE.md`

The slug is derived from the absolute cwd by Claude Code itself. On Linux/macOS the algorithm is: replace every `/` and `_` with `-` (the leading `/` becomes a leading `-`). For example: `/home/me/code/myapp` → `-home-me-code-myapp`. **For any platform, you can confirm the convention by listing the existing entries in `<claude-data-dir>/projects/`** — they'll all follow the same pattern.

If the user wants the file *committed to the repo* instead (so collaborators who also use infoguana inherit it), use [`scripts/init-project-infoguana.py`](../../scripts/init-project-infoguana.py) instead — it stamps the same template into the repo root.

## Steps

1. **Determine project name.** Default: basename of the cwd. Only ask the user if the basename looks ambiguous (e.g., `src`, `main`).

2. **Determine author.** Default: `git config user.name`. If empty, ask the user.

3. **Get a one-line description.** Preferred order:
   - Read the repo's `README.md` and propose a one-liner drawn from the first paragraph or heading
   - If no README, ask the user
   - Keep to one or two sentences max

4. **Compute the slug.** On Linux/macOS:
   ```bash
   slug=$(pwd | sed 's|/|-|g; s|_|-|g')
   ```
   On other platforms, or if uncertain: list the existing entries in `<claude-data-dir>/projects/` and follow the same naming convention for the current cwd.

5. **Check for existing `CLAUDE.md`** at `<claude-data-dir>/projects/<slug>/memory/CLAUDE.md`:
   - If present: read it, diff against the proposed new content, show the user the change, and ask before overwriting.
   - If absent: proceed.

6. **Migrate existing local memory files (opt-in).**

   List all `*.md` files in `<claude-data-dir>/projects/<slug>/memory/` *excluding* `CLAUDE.md`. If there are none, skip to step 7.

   If there are, show the user a summary — file names, sizes, one-line preview each — and ask:

   > *"I found N existing memory notes in `<full path>`. Migrate them to infoguana and archive the originals?  [yes / no / per-file]"*

   Respect the user's answer:

   - **yes** → for each file, propose `add()` args (project=`<PROJECT NAME>`, an inferred `type` and `tags`, body prefixed with a one-line provenance note like *"Migrated from local memory/<filename> on YYYY-MM-DD"*). Show the full batch of proposals as one summary, get a single confirmation, then call each `add()` in turn. After all `add`s succeed, create `<claude-data-dir>/projects/<slug>/memory.archive-infoguana-migration-<YYYY-MM-DD>/` and move every migrated file into it.
   - **no** → leave every file in place. Do *not* add the archive bullet to the generated CLAUDE.md.
   - **per-file** → for each file, show its content and ask: *"migrate to infoguana, archive only, or leave alone?"*. Apply each choice: migrate → propose `add()` args + confirm + execute + archive; archive only → skip `add()`, just move to the archive folder; leave alone → no-op.

   If any file was archived in this step, the new `CLAUDE.md` should include the "Pre-migration notes live in..." bullet (see step 7 template). If nothing was archived, omit that bullet.

7. **Create the memory folder** if missing.

8. **Write the `CLAUDE.md`** using exactly this shape. Do not add sections, do not restate infoguana-protocol rules, do not pad:

   ```markdown
   # <PROJECT NAME>

   <ONE-LINE DESCRIPTION>. Authored by <AUTHOR>.

   ## Memory

   Use the shared **infoguana** MCP for all memory in this project. Project arg is always `<PROJECT NAME>`.

   - **Reading:** SessionStart already calls `context` — read its hits before acting. On any "how does X work / state of Y / explain Z" question, `search` first before reading source or dispatching Explore agents. Only touch source to verify a specific line or fill a clear gap.
   - **Previews vs full bodies:** `search` / `similar` / `context` return haiku previews (marked `preview: True`) for triage. They tell you which notes to read; they are NOT safe to cite. Before stating a fact, decision, or recommendation anchored on a preview, fetch the full body via `get(id)` / `get_many(ids=[...])` / `expand_top=N`.
   - **Writing:** `add(project="<PROJECT NAME>", ...)` whenever you learn something worth retaining. Prefer `update` over near-duplicates.
   - **Do not write memory files alongside this one.** This pointer file is the only instruction file this project needs; everything else belongs in infoguana.

   Full infoguana-protocol guidance (good/bad memory examples, when-to-save rules) is in the SessionStart preamble — no need to restate it here.
   ```

9. **Confirm** what was written and where. If step 6 archived any files, also tell the user how many notes were migrated to infoguana and where the originals were moved. Tell the user to start a fresh Claude Code session for the new CLAUDE.md to take effect.

## Optional variants

**Pre-migration archive bullet.** When step 6 archived any files, the generated `CLAUDE.md` gets an extra bullet just after the "Do not write" line:

```
Pre-migration notes live in `memory.archive-infoguana-migration-<YYYY-MM-DD>/` (archived <date>, do not revive).
```

This is automatic — only present when an archive folder was actually created.

## Principles

- **Under 15 lines of content.** Every line loads on every session, forever. If you're tempted to add more, ask: is it universal infoguana guidance? Then it belongs in the SessionStart hook, not here. Is it project knowledge? Then it belongs in infoguana via `add`, not here.
- **Project arg must be exact.** Cross-project retrieval depends on consistent naming. Use the repo-basename form (no spaces, no title-case).
- **Never duplicate infoguana-protocol guidance per project.** It rots into drift.

## Source of truth

The canonical template lives at [`docs/CLAUDE.md.template`](../../docs/CLAUDE.md.template) in infoguana repo — it is agent-neutral despite the filename, and is stamped as `CLAUDE.md`, `AGENTS.md`, or both. The CLI equivalent of this skill is [`scripts/init-project-infoguana.py`](../../scripts/init-project-infoguana.py), which stamps it into the **repo root** (for cases where the file should be committed) and takes `--agent claude|codex|both`. If the template or that script's agent handling changes, update this skill to match.

## Related

- `context` / `search` / `similar` / `add` / `get` — infoguana MCP tools the written file points at
- `CLAUDE.md` in the `infoguana` project itself is a live reference example of the target shape
