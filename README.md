# nauroBot

The interactive backend for the **NauroLabs ops bot** — an always-on Azure Functions
webhook that turns Telegram taps into lab action.

Its first job is the **idea feedback loop**. [autoRefine](../autoRefine) scans a project
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
send Telegram card ──────────▶  (Sam taps 👍 / 👎)
                                POST /api/telegram
                                ├─ 👍 relabel + assign Copilot ─▶ issue: approved + agent
                                └─ 👎 close + label ────────────▶ issue: declined (+ reason)
next run: read declined reasons ◀──────────────────────────────  (reason-aware ideation)
```

## Stack

Python 3.11 · Azure Functions (v2, Y1 Consumption) · httpx · Telegram Bot API · GitHub
REST + GraphQL. Managed by Bicep on the NauroLabs golden path (shared monitoring module).

## Configure

All config is Function App settings (see [.env.example](.env.example)):

| Setting | Purpose |
|---|---|
| `NAURO_BOT_TOKEN` | Ops bot token (shared with autoRefine) |
| `TELEGRAM_WEBHOOK_SECRET` | Secret token verified on every request |
| `GH_ASSIGN_PAT` | User PAT (`repo` scope) — labels/closes issues + assigns Copilot |
| `NAURO_CHAT_ID` | The only chat allowed to drive the bot |
| `NAURO_GITHUB_OWNER` | `samoletovs` |

## Develop

```pwsh
cd nauroBot
python -m unittest discover tests    # offline; no network, no Azure
```

## Deploy

```pwsh
pwsh ./deploy.ps1
```

Provisions infra + publishes the code, then prints the two manual steps: set the secret app
settings, and register the Telegram webhook (`setWebhook` with `secret_token`). Once the
webhook is live, tapping a card drives the loop end-to-end.

---

Part of [NauroLabs](https://naurolabs.com) · self-evolution Phase 6 (see
`.github/EVOLUTION.md`).
