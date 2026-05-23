---
name: infoguana-onboard
description: Write a minimal project-level CLAUDE.md that delegates memory to the shared infoguana MCP server. Use when the user wants to onboard a project to infoguana, set up infoguana memory for a new repo, initialize a fresh Claude Code project with infoguana-first memory, or slim a bloated CLAUDE.md down to a pointer. Outputs a ~12-line file so CLAUDE.md does not tax the context window on every session.
risk: safe
source: community
---

# Infoguana Onboard

Creates a minimal project-level `CLAUDE.md` that delegates memory to the shared **infoguana** MCP server. The file is a *pointer* to the infoguana protocol (which lives in the SessionStart hook preamble), not a restatement of it — keeping per-session context small.

## When to use

- User asks to "onboard this project to infoguana" / "set up infoguana for this repo" / "write an infoguana CLAUDE.md"
- A fresh project needs its Claude Code memory folder initialized with the infoguana-first protocol
- An existing project has a bloated `CLAUDE.md` that should be slimmed to just-a-pointer

## Where the file goes

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

6. **Create the memory folder** if missing.

7. **Write the `CLAUDE.md`** using exactly this shape. Do not add sections, do not restate infoguana-protocol rules, do not pad:

   ```markdown
   # <PROJECT NAME>

   <ONE-LINE DESCRIPTION>. Authored by <AUTHOR>.

   ## Memory

   Use the shared **infoguana** MCP for all memory in this project. Project arg is always `<PROJECT NAME>`.

   - **Reading:** SessionStart already calls `context` — read its hits before acting. On any "how does X work / state of Y / explain Z" question, `search` first before reading source or dispatching Explore agents. Only touch source to verify a specific line or fill a clear gap.
   - **Previews vs full bodies:** `search` / `similar` / `context` return haiku previews (marked `preview: True`) for triage. They tell you which notes to read; they are NOT safe to cite. Before stating a fact, decision, or recommendation anchored on a preview, fetch the full body via `get(id)` / `get_many(ids=[...])` / `expand_top=N`.
   - **Writing:** `add(project="<PROJECT NAME>", ...)` whenever you learn something worth retaining. Prefer `update` over near-duplicates.
   - **Do not write to the local `memory/` dir.** This `CLAUDE.md` is the only file that belongs in `memory/`.

   Full infoguana-protocol guidance (good/bad memory examples, when-to-save rules) is in the SessionStart preamble — no need to restate it here.
   ```

8. **Confirm** what was written and where. Tell the user to start a fresh Claude Code session for it to take effect.

## Optional variants

**Project migrating off local memory.** If the project had a pre-infoguana `memory/` folder with notes that were archived (not deleted), add a bullet just after the "Do not write" line:

```
Pre-migration notes live in `memory.archive-infoguana-migration-<YYYY-MM-DD>/` (archived <date>, do not revive).
```

Only add this if there is actually an archive folder — otherwise skip.

## Principles

- **Under 15 lines of content.** Every line loads on every session, forever. If you're tempted to add more, ask: is it universal infoguana guidance? Then it belongs in the SessionStart hook, not here. Is it project knowledge? Then it belongs in infoguana via `add`, not here.
- **Project arg must be exact.** Cross-project retrieval depends on consistent naming. Use the repo-basename form (no spaces, no title-case).
- **Never duplicate infoguana-protocol guidance per project.** It rots into drift.

## Source of truth

The canonical template lives at [`docs/CLAUDE.md.template`](../../docs/CLAUDE.md.template) in the infoguana repo. The CLI equivalent of this skill is [`scripts/init-project-infoguana.py`](../../scripts/init-project-infoguana.py), which stamps the same template into the **repo root** (for cases where the file should be committed). If the template changes, update this skill to match.

## Related

- `context` / `search` / `similar` / `add` / `get` — the infoguana MCP tools the written file points at
- `CLAUDE.md` in the `infoguana` project itself is a live reference example of the target shape
