"""Environment configuration for the nauroBot webhook.

All values come from Function App settings. Secrets (bot token, GitHub PAT,
webhook secret) are set after the first deploy — never committed. See ``deploy.ps1``.
"""
from __future__ import annotations

import os

# The NauroLabs ops bot token. Shared with autoRefine, which sends the idea cards;
# nauroBot receives the taps and replies on the same bot.
BOT_TOKEN: str = os.getenv("NAURO_BOT_TOKEN", "")

# Secret token registered with Telegram's setWebhook. Telegram echoes it back in the
# ``X-Telegram-Bot-Api-Secret-Token`` header, which we verify on every request.
WEBHOOK_SECRET: str = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

# Only updates originating from this chat are honoured (Sam's ops chat). Empty = allow any.
ALLOWED_CHAT_ID: str = os.getenv("NAURO_CHAT_ID", "")

# User PAT (classic, ``repo`` scope) used to relabel/close/comment issues and assign the
# Copilot coding agent cross-repo. A user token is required — the Actions installation
# token cannot add the Copilot bot via ``replaceActorsForAssignable``.
GITHUB_TOKEN: str = os.getenv("GH_ASSIGN_PAT", "")

# Owner for all NauroLabs project repos.
GITHUB_OWNER: str = os.getenv("NAURO_GITHUB_OWNER", "samoletovs")

# Labels that model the idea lifecycle. autoRefine files ideas as ``needs-approval``;
# a 👍 moves them to ``approved`` (and assigns Copilot), a 👎 to ``declined``.
LABEL_NEEDS_APPROVAL: str = "needs-approval"
LABEL_APPROVED: str = "approved"
LABEL_DECLINED: str = "declined"
