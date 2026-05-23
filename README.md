<div align="center">
  <img src="app/static/infoguana.png" alt="infoguana" width="240">
</div>

Iguanas are ectotherms that depend on external heat to function.
Infoguana — *info* + *iguana* — gives LLM agents the same kind of
external lifeline: a cross-project second brain with typed-graph notes,
hybrid retrieval, and MCP integration. FastAPI + SQLite (with
sqlite-vec + FTS5) + HTMX UI + MCP. Captures notes from phone/laptop,
classifies them via Claude Code CLI, and serves them to project-local
Claude agents over MCP so each agent is backed by a bigger shared memory.

## How it works

Infoguana is a typed graph of short notes with hybrid retrieval, designed to
feed LLM agents only the slice of memory relevant to the current turn.

**Notes have types and tags.** Every note is one of `memory`, `feedback`,
`project`, `reference`, `plan`, `task`, `feature`, or `idea` — the type
changes how it's surfaced (e.g. `feedback` is sticky guidance the agent
should re-read, `plan` and `task` enter a lifecycle, `reference` is a
pointer). Tags are agent-curated at write time; `tag_suggest` ranks existing
vocabulary first so tags don't drift into singletons.

**Retrieval is hybrid and budgeted.** `search` fuses BM25 (FTS5) and cosine
similarity (sqlite-vec) over a single ranking. Hits come back as
**previews** — haiku-sized 1–5 line summaries generated at write time — so
an agent can triage 20 results for a few hundred tokens and only pull full
bodies (`get` / `get_many` / `expand_top=N`) for the ones worth quoting.

**Notes form a typed graph.** Edges carry meaning: `implements`,
`supersedes`, `references`, `caused_by`, `bundled_with`, `prerequisite_for`.
`traverse(start_id, edge_type)` walks design provenance; `search(...,
include_edges=True)` attaches neighbors inline so plan/decision lookups land
in one call. `context(project)` pre-walks an IDF-weighted BFS from pinned
anchors and packs the result into a token budget — that pack is what the
SessionStart hook hands the agent on turn one.

**Plans and tasks are first-class.** `plan` and `task` notes share a
lifecycle (`not_started` → `pending` → `complete`) — plans are deliberate
units of work you want context on across sessions, tasks are smaller scoped
items. Both surface together: pending items for the current project are
pinned to the top of every `context()` call so outstanding work never gets
lost. `plan_complete(id, pr_urls=[...])` retires an item, attaches the PRs
that landed it, and can synthesize a `feature` note carrying lessons-learned.

**Updates are non-destructive.** Every `update` snapshots prior state and
bumps a version; `history(id)` returns diffs. Edges and audit rows survive
parent deletes via tombstones.

**Design history exports as an engineering notebook.** Once a feature lands,
`export(start_id)` walks the typed-edge graph from a root plan in both
directions (depth-bounded, `confirmed_only` by default so speculative agent
edges don't pollute the record), pulls in every linked PR, and spawns a
Claude synthesis pass to render the whole arc — original idea, the plan
that implemented it, the decisions it superseded, the bugs it caused, the
lessons-learned, the PRs that shipped — as one comprehensive markdown doc.
Output lives in `./data/exports/` and survives outside infoguana as a lab
notebook, audit trail, or onboarding handoff.

The net effect: an agent dropped into any project gets the right few
thousand tokens of context on its first turn (pinned plans + ranked
previews), can drill into the graph for design intent, can capture what it
learned without inventing new vocabulary, and the next session — possibly
in a different repo — sees that knowledge surface again.

## Endpoints

- `GET /` — web capture UI (mobile-friendly)
- `GET /search/ui?q=` — hybrid BM25 + semantic search UI
- `POST /notes`, `GET /notes/{id}`, `PATCH /notes/{id}`, `DELETE /notes/{id}` — REST
- `GET /search?q=` — hybrid search JSON
- `POST /mcp/` — MCP Streamable HTTP (Bearer `$INFOGUANA_MCP_SECRET`)

## MCP tools

**Notes**
- `search(query, ...)` — hybrid BM25 + semantic search
- `similar(text, ...)` — pure semantic nearest-neighbor
- `recent(project?, limit?)` — latest notes
- `get(id)` / `get_many(ids)` — fetch by id
- `context(project, budget_tokens?)` — preview pack for an agent's first turn
- `history(id)` — version history (diffs across updates)
- `add(content, project?, type?, tags?, ...)` — save a note (auto-classified)
- `update(id, ...)` / `delete(id)` — edit / remove (snapshots prior state)
- `tag_suggest(text)` — rank existing tags for a note before write

**Plans**
- `plans(project?, status?)` — list tracked work (pending / in-progress / done)
- `plan_complete(id, pr_urls?)` — retire a plan; optionally synthesize lessons

**Graph**
- `link(from_id, to_id, edge_type)` / `unlink(...)` — typed edges
  (`implements`, `caused_by`, `supersedes`, `references`, `bundled_with`,
  `prerequisite_for`)
- `traverse(start_id, edge_type?, ...)` — multi-hop walk from a note
- `infer_edges(project?)` — suggest edges from co-citations / similarity
- `export(start_id, ...)` — render a note + linked design provenance as
  a markdown engineering notebook

**GitHub (read)**
- `gh_issue_get` / `gh_issue_list` / `gh_issue_comments`
- `gh_pr_get` / `gh_pr_comments`

**GitHub (write)**
- `gh_issue_create` / `gh_issue_comment_post` — gated by per-project bot PAT
  (`INFOGUANA_GITHUB_BOT_TOKENS`); refuses if no PAT configured for the
  project

**Filesystem (read-only, allowlisted)**
- `read_file(path, ...)` / `list_dir(path)` / `grep(pattern, ...)` — scoped
  to `INFOGUANA_FS_ALLOWLIST`; see [DEPLOY.md](DEPLOY.md) for details

## Quick start (Docker)

Requires Docker (with Compose v2) and Python 3.10+ on the host. Works on
Linux, macOS, and Windows.

```bash
git clone <this repo> infoguana && cd infoguana
docker compose up -d --build
python scripts/install-infoguana-mcp.py      # wires it into ~/.claude.json
docker compose exec infoguana claude /login  # optional: enables auto-classification
```

On Linux/macOS, use `python3` if `python` isn't on your PATH.

No `.env` editing required — the container generates an MCP bearer on
first start, persists it under `./data/.mcp_secret`, and writes a
ready-to-paste `./data/mcp.json`. The installer merges that snippet into
`~/.claude.json` (idempotent; preserves your other `mcpServers` entries;
safe to re-run after a secret rotation).

If you're running infoguana on a different host than your Claude Code
machine, point the generated snippet at the right hostname:

```bash
INFOGUANA_PUBLIC_HOST=infoguana.example.com docker compose up -d --build
python scripts/install-infoguana-mcp.py
```

After install, restart any open Claude Code sessions and run `/mcp list`
to confirm `infoguana` is connected.

See [DEPLOY.md](DEPLOY.md) for backups, updating, and optional knobs.

## Local dev

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m app.main
```

Then http://localhost:8789/. All settings have defaults; drop a `.env` in
the repo root to override (see `.env.example`).

## Auto-inject project context on first prompt

Claude Code's MCP gives the agent the infoguana on demand, but it has to
remember to call `context` itself. To skip the cold-start step, install
the `SessionStart` hooks that ship with this repo — they pack the
project's preview-mode infoguana context (~70 short note previews)
directly into the agent's first turn, so architecture / open work / hard
rules are visible inline before answering anything.

**Step 1.** Create `~/.infoguana.env` (in your home directory) with:

```
INFOGUANA_URL=http://localhost:8789
INFOGUANA_TOKEN=<paste from ./data/.mcp_secret>
# INFOGUANA_ONBOARD_BUDGET=4000   # optional, default 4000 tokens of previews
```

**Step 2.** Register the hooks (idempotent — adds entries to
`~/.claude/settings.json`):

```bash
python scripts/install-infoguana-hooks.py
```

That's it. Open Claude Code in any project and the first user message
auto-loads the infoguana's project context.
