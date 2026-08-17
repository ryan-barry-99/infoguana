# /projects dashboard — table view of projects and their plan activity

- **Project:** infoguana
- **Status:** complete
- **Tags:** (none)
- **Linked commit:** [redacted]
- **Created:** 2026-04-25T03:05:06+00:00
- **Completed:** 2026-05-07T02:20:59+00:00

## Background and motivation

The user (Ryan) asked on 2026-04-25 for "a general project tracking task tracker, where you can view a table of your projects, open plans etc." Prior to this plan, infoguana's web UI had two ways to look at notes:

- `/browse` — filter a list of notes by project/type/status.
- `/search` — find notes by content.

Neither view answered the portfolio-scan question: *across every project the infoguana knows about, where is work pending, where has work shipped, and which projects have I touched recently?* `/projects` is the missing complement — a single-glance dashboard of all known projects keyed by plan activity.

## Design — v1 scope (as specified in the plan)

### Route and template

- New route: `GET /projects` → `projects.html`.
- Server-side Jinja render. No client-side JS required.
- New top-nav link "projects" in `base.html`, placed between "browse" and "search" (or adjacent to "browse").

### Columns and link targets

Per the plan body, each row in the table represents one project. Cells deep-link into the existing `/browse` page with matching filters:

| Column | Source | Link target |
|---|---|---|
| Project name | `projects.name` (and `notes.project` for synthetic rows) | `/browse?project=X` |
| Pending plans count | `COUNT(notes WHERE type='plan' AND status='pending')` | `/browse?project=X&type=plan&status=pending` |
| Shipped plans count | `COUNT(notes WHERE type='plan' AND status='complete')` | `/browse?project=X&type=plan&status=complete` |
| Total notes count | `COUNT(notes)` | `/browse?project=X` |
| Last activity | `MAX(notes.created_at)` | — (display only) |

### Aggregation strategy

The plan called out two viable options:

1. One SQL query with a `GROUP BY` over `notes`.
2. Two queries: `list_project_names()` then a `GROUP BY` aggregate.

The shipped implementation used a single CTE with `SUM(CASE WHEN ... THEN 1 ELSE 0 END)` columns per status (see the lessons-learned note). This is fast enough at single-node SQLite scale (hundreds of plans, thousands of notes) — the live query runs in under 5ms with no materialized view, no pre-aggregation table, and no caching layer.

### Default sort

`pending plans DESC, last_activity DESC`.

Rationale from the plan: projects with active work float to the top. Confirmed in the lessons-learned note as the right default — "the 'what should I work on?' view comes for free from the data — don't make the user think about ordering." No user-controlled sort toggles were added.

### Last-activity field

`notes.created_at`, not `updated_at`. Chosen for consistency with `/browse`'s ordering.

## Design decisions

### One filterable `/browse` route, not N specialized routes

The most durable architectural decision recorded in the lessons-learned note was *not* to split the deep-link targets into per-view routes (`/plans`, `/by-project`, `/pending`, etc.). Every drill-through from `/projects` instead goes to `/browse` with URL parameters:

> One filterable `/browse` route covers what would otherwise be N specific routes (`/plans`, `/by-project`, `/pending`, etc.). URL-param filters (`?project=&type=&status=`) make every combination bookmarkable. The N-specific-routes design temptation is real because each combo "feels" like its own page — but they share 90% of logic and you end up duplicating templates. Don't pre-split until at least one variant needs genuinely different UI.

### Synthetic "no project" row gated by `HAVING`

One of the open questions in the plan was where notes with `project IS NULL` should appear. The shipped answer, per the lessons-learned note: include them as a synthetic trailing row, but gate the row with `UNION ALL ... HAVING COUNT(n.id) > 0` so it disappears when no orphan notes exist:

> Synthetic 'no project' row needs `UNION ALL ... HAVING COUNT(n.id) > 0` to skip when there are no orphan notes — otherwise the table always has an empty trailing row labeled "no project" with all zeros, which is visual noise. The HAVING gate keeps it out of sight until it actually has content.

### No pre-optimization of the aggregate query

Explicitly rejected for v1 per the lessons-learned note: no materialized view, no pre-aggregation table, no caching layer. The `SUM(CASE)` CTE is the entire implementation. The note flags the revisit threshold: ~100k+ notes.

## Implementation

Landed in commit `[redacted]` on the `[redacted]` repository (recorded as the plan's only linked artifact on the plan; the working tree on this devbox is at `/root/code/infoguana`, but the deployed infoguana lives in `[redacted]`). The plan was marked `status=complete` on 2026-05-07T02:20:59+00:00.

The implementation comprises:

- The `/projects` route handler that runs the CTE described above.
- `templates/projects.html` rendering the table.
- The nav link in `templates/base.html`.

The notes do not expose review comments or per-file diffs for `[redacted]` (the linked artifact is a commit URL, not a pull request, so there is no review thread to fold in).

## Open questions from the plan and how they were resolved

The plan left three open questions. The lessons-learned note and the shipped query together resolve them as follows:

| Question from the plan | Resolution |
|---|---|
| Should projects with zero notes (exist only in the `projects` table, never captured against) appear? Probably yes but muted/greyed. | The lessons-learned note does not explicitly confirm a muted/greyed treatment for zero-note projects from the `projects` table; it only documents the `HAVING COUNT(n.id) > 0` gating used for the synthetic "no project" row. The notes do not specify the final UI treatment for zero-note registered projects. |
| Where does "no project" (project IS NULL) fit — a row at the bottom? Or hidden entirely? | Synthetic trailing row, conditionally rendered via `UNION ALL ... HAVING COUNT(n.id) > 0`. |
| Is "last activity" based on `notes.created_at` or `notes.updated_at`? | `created_at`, for consistency with `/browse` ordering. |

## Deferred work / out of scope for v1

Captured in the plan and not addressed in the shipped commit:

- Editable project descriptions / READMEs (would lean on the existing `projects` table, which stores `name, path, description`).
- Archive / pin / reorder projects.
- Activity sparklines (per-day note count over the last 30 days).
- Per-project time-to-ship stats on plans (would need plan `updated_at - created_at` when status transitions to complete).
- RSS / digest view.

These remain open follow-ups; no successor plan in the linked graph implements them as of the plan's completion.

## Operational notes

- Deployed via the `[redacted]` repo; the route is live on the devbox infoguana.
- No schema migration was required — the dashboard reads only from existing `notes` and `projects` tables.
- No background job, no caching layer, no pre-aggregation table. The CTE runs synchronously on each page load.
- Performance budget: <5ms on the live database at the time of shipping. Revisit only above 100k notes.

## References

- **Plan** — `/projects` dashboard (this document's root; status=complete).
- **Lessons-learned note** — from shipping `/projects` + `/browse` dashboards. Documents the one-filterable-route pattern, the `SUM(CASE)` aggregate, the `HAVING`-gated synthetic "no project" row, and the default sort rationale.
- **Commit:** [redacted]