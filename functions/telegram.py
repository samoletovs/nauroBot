"""Minimal async Telegram Bot API client for the ops bot.

Only the handful of methods the feedback loop needs. Every call swallows and logs its
own errors so a Telegram hiccup never fails the webhook response (Telegram retries on
non-200, which would storm the handler).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("naurobot.telegram")

_API = "https://api.telegram.org/bot{token}/{method}"


class Telegram:
    """Thin wrapper over the Telegram Bot API bound to one bot token + HTTP client."""

    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self._token = token
        self._client = client

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = _API.format(token=self._token, method=method)
        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Acknowledge a button tap (stops the Telegram spinner, shows an optional toast)."""
        try:
            await self._call(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": text},
            )
        except Exception:
            log.exception("answerCallbackQuery failed")

    async def send_message(
        self,
        chat_id: Any,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Optional[int]:
        """Send a message. Returns the new message id, or None on failure."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        try:
            data = await self._call("sendMessage", payload)
            return data.get("result", {}).get("message_id")
        except Exception:
            log.exception("sendMessage failed")
            return None

    async def edit_reply_markup(
        self, chat_id: Any, message_id: int, reply_markup: Optional[dict[str, Any]]
    ) -> None:
        """Replace (or clear, with None) the inline keyboard on an existing message."""
        try:
            await self._call(
                "editMessageReplyMarkup",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": reply_markup or {"inline_keyboard": []},
                },
            )
        except Exception:
            log.exception("editMessageReplyMarkup failed")
