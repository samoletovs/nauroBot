"""Route Telegram updates for the NauroLabs idea feedback loop.

autoRefine proposes a functional idea, files it as a ``needs-approval`` issue, and sends a
Telegram card with ``[👍 Build] [👎 Not now]``. The button carries ``arf:<repo>:<num>:<y|n>``
and the card text carries the same ``arf:<repo>:<num>`` token so a text *reply* to the card
can be attributed back to the issue.

- 👍 → drop ``needs-approval``, add ``approved``, assign the Copilot agent → build starts.
- 👎 → drop ``needs-approval``, add ``declined``, close the issue, invite a reason.
- reply to a card → logged as a feedback comment on the issue. autoRefine reads the reasons
  on declined ideas and feeds them back into the generator so it proposes differently.

The GitHub issue is the shared bus: both nauroBot and the (CI-only) autoRefine job speak it,
so neither needs access to the other's storage.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from config import LABEL_APPROVED, LABEL_DECLINED, LABEL_NEEDS_APPROVAL
from github_ops import GitHub
from telegram import Telegram

log = logging.getLogger("naurobot.handlers")

# arf:<repo>:<issue-number> — matched in both callback_data and card text.
_ARF = re.compile(r"arf:([A-Za-z0-9_.-]+):(\d+)")


def _parse_callback(data: str) -> Optional[tuple[str, int, str]]:
    """Parse ``arf:<repo>:<num>:<y|n>`` → ``(repo, num, verdict)`` or None if malformed."""
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "arf":
        return None
    _, repo, raw_num, verdict = parts
    if verdict not in ("y", "n") or not raw_num.isdigit():
        return None
    return repo, int(raw_num), verdict


def _authorised(chat_id: Any, allowed_chat: str) -> bool:
    """True when no allow-list is configured, or the chat matches it."""
    return not allowed_chat or str(chat_id) == str(allowed_chat)


async def _approve(
    gh: GitHub, tg: Telegram, repo: str, num: int, callback_id: str, chat_id: Any,
    message_id: Optional[int],
) -> dict[str, Any]:
    await gh.remove_label(repo, num, LABEL_NEEDS_APPROVAL)
    await gh.add_labels(repo, num, [LABEL_APPROVED])
    assigned = await gh.assign_copilot(repo, num)
    note = "🛠️ Building — Copilot assigned." if assigned else "Approved (assign Copilot manually)."
    await gh.comment(repo, num, f"Approved via Telegram. {note}")
    await tg.answer_callback(callback_id, "Building ✅" if assigned else "Approved ✅")
    if message_id is not None:
        await tg.edit_reply_markup(chat_id, message_id, None)
    return {"ok": True, "action": "approved", "repo": repo, "num": num, "assigned": assigned}


async def _decline(
    gh: GitHub, tg: Telegram, repo: str, num: int, callback_id: str, chat_id: Any,
    message_id: Optional[int],
) -> dict[str, Any]:
    await gh.remove_label(repo, num, LABEL_NEEDS_APPROVAL)
    await gh.add_labels(repo, num, [LABEL_DECLINED])
    await gh.close_issue(repo, num, state_reason="not_planned")
    await gh.comment(repo, num, "Declined via Telegram.")
    await tg.answer_callback(callback_id, "Declined 👎")
    if message_id is not None:
        await tg.edit_reply_markup(chat_id, message_id, None)
    # Invite an optional reason. The message carries the arf token so a reply to it is
    # attributed back to the issue by _handle_reply and fed to the generator.
    await tg.send_message(
        chat_id,
        f"Noted — declined. Reply to this message with a reason and I'll teach the idea "
        f"generator to avoid it.\n\narf:{repo}:{num}",
        reply_to_message_id=message_id,
    )
    return {"ok": True, "action": "declined", "repo": repo, "num": num}


async def _handle_callback(
    cq: dict[str, Any], tg: Telegram, gh: GitHub, allowed_chat: str
) -> dict[str, Any]:
    callback_id = cq.get("id", "")
    message = cq.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    if not _authorised(chat_id, allowed_chat):
        await tg.answer_callback(callback_id, "Not authorised")
        return {"ok": False, "error": "unauthorised"}
    parsed = _parse_callback(cq.get("data") or "")
    if parsed is None:
        await tg.answer_callback(callback_id)
        return {"ok": True, "action": "ignored"}
    repo, num, verdict = parsed
    if verdict == "y":
        return await _approve(gh, tg, repo, num, callback_id, chat_id, message_id)
    return await _decline(gh, tg, repo, num, callback_id, chat_id, message_id)


async def _handle_reply(
    message: dict[str, Any], tg: Telegram, gh: GitHub, allowed_chat: str
) -> dict[str, Any]:
    chat_id = (message.get("chat") or {}).get("id")
    if not _authorised(chat_id, allowed_chat):
        return {"ok": False, "error": "unauthorised"}
    reason = (message.get("text") or "").strip()
    parent_text = (message.get("reply_to_message") or {}).get("text") or ""
    match = _ARF.search(parent_text)
    if not reason or match is None:
        return {"ok": True, "action": "ignored"}
    repo, num = match.group(1), int(match.group(2))
    await gh.comment(repo, num, f"Feedback from Telegram: {reason}")
    return {"ok": True, "action": "reason", "repo": repo, "num": num}


async def handle_update(
    update: dict[str, Any], tg: Telegram, gh: GitHub, allowed_chat: str = ""
) -> dict[str, Any]:
    """Route one Telegram update to the idea feedback handlers."""
    if "callback_query" in update:
        return await _handle_callback(update["callback_query"], tg, gh, allowed_chat)
    message = update.get("message") or {}
    if message.get("reply_to_message"):
        return await _handle_reply(message, tg, gh, allowed_chat)
    return {"ok": True, "action": "ignored"}
