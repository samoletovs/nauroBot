"""Route Telegram updates for the NauroLabs idea feedback loop.

autoRefine proposes a functional idea, files it as a ``needs-approval`` issue, and sends a
Telegram card with ``[👍 Build] [👎 Not now]``. The button carries ``arf:<repo>:<num>:<y|n>``
and the card text carries the same ``arf:<repo>:<num>`` token so a text *reply* to the card
can be attributed back to the issue.

- 👍 → drop ``needs-approval``, add ``approved``, assign the Copilot agent → build starts.
- 🅿️ → drop ``needs-approval``, add ``parked``, keep the issue open as a stepping-stone.
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

from config import LABEL_APPROVED, LABEL_DECLINED, LABEL_NEEDS_APPROVAL, LABEL_PARKED
from github_ops import GitHub
from telegram import Telegram

log = logging.getLogger("naurobot.handlers")

# arf:<repo>:<issue-number> — matched in both callback_data and card text.
_ARF = re.compile(r"arf:([A-Za-z0-9_.-]+):(\d+)")


def _parse_callback(data: str) -> Optional[tuple[str, int, str]]:
    """Parse ``arf:<repo>:<num>:<y|p|n>`` → ``(repo, num, verdict)`` or None if malformed."""
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "arf":
        return None
    _, repo, raw_num, verdict = parts
    if verdict not in ("y", "p", "n") or not raw_num.isdigit():
        return None
    return repo, int(raw_num), verdict


# arfpr:<repo>:<pr-number> — the PR-approval card. A distinct namespace from arf: so the
# idea and PR flows never collide ("arf:" never matches "arfpr:" and vice-versa).
_ARFPR = re.compile(r"arfpr:([A-Za-z0-9_.-]+):(\d+)")


def _parse_pr_callback(data: str) -> Optional[tuple[str, int, str]]:
    """Parse ``arfpr:<repo>:<num>:<y|n>`` → ``(repo, num, verdict)`` or None if malformed."""
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "arfpr":
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


async def _park(
    gh: GitHub, tg: Telegram, repo: str, num: int, callback_id: str, chat_id: Any,
    message_id: Optional[int],
) -> dict[str, Any]:
    """🅿️ on an idea card: keep it, but not now.

    Drop ``needs-approval``, add ``parked``, and leave the issue OPEN as a stepping-stone
    (EVOLUTION.md §2 archive) — no Copilot, no close. A later harvest or scan can revive it.
    """
    await gh.remove_label(repo, num, LABEL_NEEDS_APPROVAL)
    await gh.add_labels(repo, num, [LABEL_PARKED])
    await gh.comment(repo, num, "Parked via Telegram — kept as a stepping-stone (still open).")
    await tg.answer_callback(callback_id, "Parked 🅿️")
    if message_id is not None:
        await tg.edit_reply_markup(chat_id, message_id, None)
    return {"ok": True, "action": "parked", "repo": repo, "num": num}


async def _approve_pr(
    gh: GitHub, tg: Telegram, repo: str, num: int, callback_id: str, chat_id: Any,
    message_id: Optional[int],
) -> dict[str, Any]:
    """👍 on a PR card: verify CI is green, then approve (as the PAT user) and squash-merge.

    The CI check is done here, client-side, before any write. It is not redundant with
    GitHub's 405: that only refuses a red PR when the repo has branch protection with
    required status checks, and audited 2026-08-21 **no NauroLabs repo has any** — so
    without this gate a single Telegram tap squash-merges a failing PR into the default
    branch and GitHub returns 200.

    Anything that is not positively green (failing, pending, no checks, or an unresolvable
    rollup) refuses the merge and tells the human why. The card keeps its buttons so they
    can tap again once CI settles.
    """
    state, detail = await gh.checks_state(repo, num)
    if state != "passing":
        # Each case gets its own next step. A gate whose only advice is "tap again"
        # is a dead end when the PR has no CI at all, and a gate people can't satisfy
        # is a gate people route around.
        guidance = {
            "failing": (
                "CI is red",
                "Fix the failure and push; the card stays live. Tap ✅ again once it is green.",
            ),
            "pending": (
                "CI is still running",
                "Give it a minute, then tap ✅ again.",
            ),
            "none": (
                "this PR has no CI checks at all",
                "Nothing here can turn green, so I will not merge it unattended. "
                "Review and merge it on GitHub if you intend to ship it.",
            ),
            "unknown": (
                "I could not read the CI status",
                "Treating that as not-green on purpose. Check the PR on GitHub.",
            ),
        }
        reason, next_step = guidance.get(state, ("CI is not green", "Check the PR on GitHub."))
        await tg.answer_callback(callback_id, f"Not merged — {reason}")
        await tg.send_message(
            chat_id,
            f"🛑 Refused to merge {repo}#{num}: {reason}.\n{detail}\n\n"
            f"Nothing was approved or merged.\n{next_step}\n\n"
            f"arfpr:{repo}:{num}",
            reply_to_message_id=message_id,
        )
        return {
            "ok": True, "action": "merge_refused", "repo": repo, "num": num,
            "checks": state, "detail": detail,
        }

    try:
        await gh.approve_pr(repo, num)
    except Exception:  # noqa: BLE001 — a review hiccup must not 500 the webhook
        log.exception("approve_pr failed for %s#%s", repo, num)
    merged, detail = await gh.merge_pr(repo, num)
    if merged:
        await gh.comment(repo, num, "Approved + squash-merged via Telegram. 🚢")
        await tg.answer_callback(callback_id, "Merged 🚢")
        if message_id is not None:
            await tg.edit_reply_markup(chat_id, message_id, None)
        return {"ok": True, "action": "merged", "repo": repo, "num": num}
    await tg.answer_callback(callback_id, "Approved — not mergeable yet")
    await tg.send_message(
        chat_id,
        f"👍 Approved {repo}#{num}, but GitHub won't merge it yet:\n{detail}\n\n"
        f"Tap ✅ again once CI is green.\n\narfpr:{repo}:{num}",
        reply_to_message_id=message_id,
    )
    return {"ok": True, "action": "approved_unmerged", "repo": repo, "num": num, "detail": detail}


async def _decline_pr(
    gh: GitHub, tg: Telegram, repo: str, num: int, callback_id: str, chat_id: Any,
    message_id: Optional[int],
) -> dict[str, Any]:
    """👎 on a PR card: close the PR without merging."""
    await gh.comment(repo, num, "Closed via Telegram (not merged).")
    await gh.close_pr(repo, num)
    await tg.answer_callback(callback_id, "Closed 👎")
    if message_id is not None:
        await tg.edit_reply_markup(chat_id, message_id, None)
    return {"ok": True, "action": "pr_closed", "repo": repo, "num": num}


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
    data = cq.get("data") or ""
    # PR-approval card (arfpr:) is a distinct namespace from the idea card (arf:).
    pr_parsed = _parse_pr_callback(data)
    if pr_parsed is not None:
        repo, num, verdict = pr_parsed
        if verdict == "y":
            return await _approve_pr(gh, tg, repo, num, callback_id, chat_id, message_id)
        return await _decline_pr(gh, tg, repo, num, callback_id, chat_id, message_id)
    parsed = _parse_callback(data)
    if parsed is None:
        await tg.answer_callback(callback_id)
        return {"ok": True, "action": "ignored"}
    repo, num, verdict = parsed
    if verdict == "y":
        return await _approve(gh, tg, repo, num, callback_id, chat_id, message_id)
    if verdict == "p":
        return await _park(gh, tg, repo, num, callback_id, chat_id, message_id)
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
