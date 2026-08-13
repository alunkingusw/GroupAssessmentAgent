# Roadmap

Planned work beyond the current MVP. Nothing here is scheduled or committed to a version — it's
a backlog of known next steps, ordered roughly by dependency.

## 1. Real source clients for `assess_query`

`assess_query` (see README "Design decisions") currently only reports which of conversation
transcripts / GitHub / Trello it *would* check — it doesn't check them. Before it can answer a
question for real, each source needs an actual client:

- **Transcript search**: query already-submitted `.vtt` transcripts (via the backend, or local
  storage) for content matching `transcript_focus`.
- **GitHub**: a read-only client (REST or GraphQL API) scoped to each group's repo — issues,
  PRs, commit activity. Needs per-group repo configuration (likely alongside `group_owners` in
  `config.yaml`) and a token with minimal read scopes.
- **Trello**: a read-only client (Trello REST API) scoped to each group's board — cards, lists,
  due dates. Needs per-group board configuration and an API key/token.

Once these exist, `assess_query`'s handler moves from "describe the plan" to "run the plan and
compile the results" — the sources selected by the LLM get queried independently, and their
results are compiled into a single reply. The routing decision (which this phase's dry run
validates) stays the trusted, LLM-driven part; the queries themselves stay deterministic
API calls, same trust-boundary pattern as everything else in this codebase.

## 2. Scheduled weekly per-group update

A proactive report, generated automatically overnight once a week per group, rather than
triggered by an inbound email. Requires:

- **A third background thread/loop** (alongside the existing mail-poll and job-worker loops in
  `app/main.py`) on a weekly schedule, or an external cron invoking a one-shot command — either
  fits the existing SQLite-backed, thread-per-concern architecture.
- **Iterating known groups** — `authorisation.group_owners` (or a dedicated groups config) as the
  list to run the update for.
- **Reusing the source clients from item 1** to compile each group's update (recent transcripts,
  GitHub activity, Trello board state) rather than routing a single ad-hoc question.
- **Delivery**: who receives it (group owner(s) only, or a wider list?) and what happens on a
  partial failure (e.g. GitHub reachable but Trello down for one group) — should the update still
  send with a "couldn't reach X" note, matching the "be conservative, never guess" pattern used
  elsewhere, rather than silently omitting a section.
- **Idempotency/retry**: what happens if the process restarts mid-run — needs the same
  crash-safe, resumable design as the existing job store/outbox, not a fire-and-forget loop.

This depends on item 1 existing first (there's nothing to compile weekly until the sources are
real), but the scheduling/delivery mechanics are independent and could be prototyped earlier
against stubbed data if useful.
