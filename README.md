# nauroBot

nauroBot is the interactive backend for the nauroLabs operations bot. It is an
Azure Functions webhook that turns an authorized Telegram action into a GitHub
issue update.

## Research question

nauroBot tests the nauroLabs question **"Can a company run itself?"** It explores
whether a phone-sized human approval surface is enough to supervise an automated
idea and build loop without giving the automation authority to approve its own
work.

## What it does

Its first job is the **idea feedback loop**.
[autoRefine](https://github.com/samoletovs/autoRefine) scans a project
against its north-star fitness, proposes a functional idea, files it as a GitHub issue, and
sends a Telegram card:

```
💡 [P1] era — inline posting-rule explanations
Proposed by autoRefine for samoletovs/era
[👍 Build]   [👎 Not now]
```

- **👍 Build** → the idea issue loses `needs-approval`, gains `approved`, and the **Copilot
  coding agent is assigned** — the build starts, hands-free.
- **👎 Not now** → the issue is closed `declined`; reply with a reason and it's logged on the
  issue, where autoRefine reads it and proposes differently next time.

No database — the **GitHub issue is the shared state**. nauroBot (always-on) and autoRefine
(CI batch) both speak GitHub issues, so the loop needs no shared storage.

## How it works

```
autoRefine (CI)                 nauroBot (Function)              GitHub
──────────────                  ───────────────────              ──────
propose idea ──▶ file issue ──▶                          ──▶ issue: needs-approval
send Telegram card ──────────▶  (owner taps 👍 / 👎)
                                POST /api/telegram
                                ├─ 👍 relabel + assign Copilot ─▶ issue: approved + agent
                                └─ 👎 close + label ────────────▶ issue: declined (+ reason)
next run: read declined reasons ◀──────────────────────────────  (reason-aware ideation)
```

## Stack

Python 3.11 · Azure Functions (v2, Y1 Consumption) · httpx · Telegram Bot API · GitHub
REST + GraphQL. Managed by Bicep on the nauroLabs golden path (shared monitoring module).

## Run locally

```pwsh
cd nauroBot
python -m pip install -r functions/requirements.txt
python -m unittest discover tests
```

All tests are offline and mock Telegram and GitHub.

## Configure

All config is Function App settings (see [.env.example](.env.example)):

| Setting | Purpose |
|---|---|
| `NAURO_BOT_TOKEN` | Ops bot token (shared with autoRefine) |
| `TELEGRAM_WEBHOOK_SECRET` | Secret token verified on every request |
| `GH_ASSIGN_PAT` | User PAT (`repo` scope) — labels/closes issues + assigns Copilot |
| `NAURO_CHAT_ID` | The only chat allowed to drive the bot |
| `NAURO_GITHUB_OWNER` | `samoletovs` |

## Deploy

```pwsh
pwsh ./deploy.ps1
```

Provisions infra + publishes the code, then prints the two manual steps: set the secret app
settings, and register the Telegram webhook (`setWebhook` with `secret_token`). Once the
webhook is live, tapping a card drives the loop end-to-end.

---

## Status

**Scaffold.** The Telegram webhook, approval/decline handlers, GitHub operations,
and offline tests are implemented. Deployment still requires repository and
Function App secrets plus Telegram webhook registration.

## License

MIT
