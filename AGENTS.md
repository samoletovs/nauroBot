# nauroBot — Agent Instructions

> Project-specific instructions for AI coding agents (Copilot, Codex, Claude Code).

## Project

nauroBot is the **interactive backend for the NauroLabs ops bot** — an always-on Azure
Functions (Python v2) webhook that receives Telegram updates and acts on them. Its first job
is the **idea feedback loop**: autoRefine proposes a functional idea, files it as a GitHub
issue, and sends a Telegram card with `[👍 Build] [👎 Not now]`; nauroBot handles the tap.

## North star

Turn a phone tap into lab action, and turn a decline into learning.

- **👍 Build** → drop the `needs-approval` label, add `approved`, and **assign the Copilot
  coding agent** to the issue (via GraphQL `replaceActorsForAssignable`). The build starts.
- **👎 Not now** → drop `needs-approval`, add `declined`, close the issue, and invite a
  one-line reason.
- **Reply to a card** → logged as a feedback comment on the issue. autoRefine reads the
  reasons on declined ideas and feeds them into its generator so it proposes differently.

## Architecture

- `functions/function_app.py` — the single `POST /api/telegram` webhook. Verifies the
  Telegram secret-token header, then routes to `handlers.handle_update`.
- `functions/handlers.py` — routing + business logic (approve / decline / reason).
- `functions/github_ops.py` — REST labelling/closing/commenting + the GraphQL Copilot assign.
- `functions/telegram.py` — minimal async Telegram Bot API client.
- `functions/config.py` — env-driven config (all secrets are Function App settings).

**The GitHub issue is the shared bus.** nauroBot and the CI-only autoRefine job both speak
GitHub issues, so neither needs access to the other's storage. There is no database.

## Conventions

- Python: type hints, `logging` (never `print`), imports ordered stdlib → third-party → local.
- The webhook always returns **200** — a non-200 makes Telegram retry-storm on a handler bug.
- Auth is the **secret-token header** (Telegram can't send a Function key). Only the
  allow-listed `NAURO_CHAT_ID` is honoured.
- Assigning the Copilot agent needs a **user PAT** (`GH_ASSIGN_PAT`), not the Actions
  installation token — the latter cannot add the Copilot bot as an actor.

## Adding a scenario

New capabilities (approve a deploy, status query, trigger a plan run) are new branches in
`handle_update` + new methods on the clients. Keep each handler small and covered by a test
in `tests/` (offline — mock the Telegram/GitHub clients or use `httpx.MockTransport`).

## Deploy

`pwsh ./deploy.ps1` provisions infra + publishes, then prints the two manual steps: set the
secret app settings, and register the Telegram webhook. See [README.md](README.md).
