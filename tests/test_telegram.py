"""Telegram client tests (offline; httpx MockTransport — no network)."""
import json
import os
import sys
import unittest

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "functions"))

from telegram import Telegram  # noqa: E402


class SendMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_returns_id(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tg = Telegram("TOK", client)
            mid = await tg.send_message(42, "hi", reply_markup={"inline_keyboard": []})
        self.assertEqual(mid, 99)
        self.assertIn("/botTOK/sendMessage", seen["url"])
        self.assertEqual(seen["body"]["chat_id"], 42)
        self.assertIn("reply_markup", seen["body"])

    async def test_send_message_swallows_errors(self):
        def handler(request):
            return httpx.Response(500, json={"ok": False})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tg = Telegram("TOK", client)
            mid = await tg.send_message(42, "hi")
        self.assertIsNone(mid)


class FailureFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_failures_notify_the_chat_once(self):
        for operation in ("send_message", "edit_reply_markup"):
            for failure in ("http", "transport", "api", "json"):
                with self.subTest(operation=operation, failure=failure):
                    requests = []

                    def handler(request):
                        requests.append(request)
                        if len(requests) > 1:
                            return httpx.Response(
                                200, json={"ok": True, "result": {"message_id": 100}}
                            )
                        if failure == "transport":
                            raise httpx.ConnectError("private detail", request=request)
                        if failure == "json":
                            return httpx.Response(200, text="not JSON")
                        return httpx.Response(
                            500 if failure == "http" else 200,
                            json={"ok": False, "description": "private detail"},
                        )

                    async with httpx.AsyncClient(
                        transport=httpx.MockTransport(handler)
                    ) as client:
                        tg = Telegram("TOK", client)
                        with self.assertLogs("naurobot.telegram", level="ERROR"):
                            if operation == "send_message":
                                result = await tg.send_message(
                                    42, "original", reply_markup={"inline_keyboard": []},
                                    reply_to_message_id=7,
                                )
                            else:
                                result = await tg.edit_reply_markup(42, 7, None)

                    self.assertIsNone(result)
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(requests[1].url.path, "/botTOK/sendMessage")
                    payload = json.loads(requests[1].content)
                    self.assertEqual(set(payload), {"chat_id", "text"})
                    self.assertEqual(payload["chat_id"], 42)
                    self.assertIn("couldn't", payload["text"])
                    self.assertIn(
                        "send" if operation == "send_message" else "update",
                        payload["text"],
                    )
                    self.assertNotIn("private detail", payload["text"])
                    self.assertNotIn("TOK", payload["text"])

    async def test_notification_failure_is_logged_without_recursion(self):
        for operation in ("send_message", "edit_reply_markup"):
            with self.subTest(operation=operation):
                requests = []

                def handler(request):
                    requests.append(request)
                    return httpx.Response(500, json={"ok": False})

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ) as client:
                    tg = Telegram("TOK", client)
                    with self.assertLogs("naurobot.telegram", level="ERROR") as captured:
                        if operation == "send_message":
                            result = await tg.send_message(42, "hi")
                        else:
                            result = await tg.edit_reply_markup(42, 7, None)

                self.assertIsNone(result)
                self.assertEqual(len(requests), 2)
                self.assertIn("failure notification failed", "\n".join(captured.output))
                self.assertNotIn("TOK", "\n".join(captured.output))

    async def test_successful_edit_does_not_send_notification(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await Telegram("TOK", client).edit_reply_markup(42, 7, None)

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/botTOK/editMessageReplyMarkup")
        self.assertEqual(json.loads(requests[0].content), {
            "chat_id": 42, "message_id": 7, "reply_markup": {"inline_keyboard": []},
        })


class TokenNeverReachesTheLogTests(unittest.IsolatedAsyncioTestCase):
    """The token lives in the request path, so any httpx error text leaks it.

    httpx.HTTPStatusError renders as "... for url 'https://api.telegram.org/bot<TOKEN>/...'".
    Handlers log these with log.exception, which wrote the live credential into Application
    Insights - 92 records before this was caught. Assert on the whole exception chain,
    because a chained __cause__ is printed by the traceback just as loudly.
    """

    SECRET = "123456:AAHsuperSecretTokenValue"

    def _chain_text(self, exc: BaseException) -> str:
        parts, seen = [], set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            parts.append(f"{exc!r} {exc}")
            exc = exc.__cause__ or exc.__context__
        return " ".join(parts)

    async def test_http_error_is_reraised_without_the_token(self):
        def handler(request):
            return httpx.Response(400, json={"ok": False, "description": "bad request"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tg = Telegram(self.SECRET, client)
            with self.assertRaises(Exception) as caught:
                await tg._call("editMessageReplyMarkup", {"chat_id": 1})

        text = self._chain_text(caught.exception)
        self.assertNotIn(self.SECRET, text)
        self.assertNotIn("api.telegram.org", text)
        self.assertIn("editMessageReplyMarkup", text)

    async def test_transport_error_is_reraised_without_the_token(self):
        def handler(request):
            raise httpx.ConnectError("boom", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tg = Telegram(self.SECRET, client)
            with self.assertRaises(Exception) as caught:
                await tg._call("sendMessage", {"chat_id": 1})

        text = self._chain_text(caught.exception)
        self.assertNotIn(self.SECRET, text)

    async def test_handler_log_output_carries_no_token(self):
        # The chain assertions above are the mechanism; this is the actual contract -
        # what a public method writes to the log when Telegram rejects the call.
        def handler(request):
            return httpx.Response(400, json={"ok": False})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tg = Telegram(self.SECRET, client)
            with self.assertLogs("naurobot.telegram", level="ERROR") as captured:
                await tg.edit_reply_markup(1, 2, None)

        self.assertNotIn(self.SECRET, "\n".join(captured.output))
        self.assertNotIn("api.telegram.org", "\n".join(captured.output))


class AnswerCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_expected_payload(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "result": True})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tg = Telegram("TOK", client)
            await tg.answer_callback("cb1", "hello")
        self.assertIn("/botTOK/answerCallbackQuery", seen["url"])
        self.assertEqual(seen["body"]["callback_query_id"], "cb1")

    async def test_swallows_errors(self):
        def handler(request):
            return httpx.Response(500, json={"ok": False})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tg = Telegram("TOK", client)
            await tg.answer_callback("cb1", "hello")  # must not raise


if __name__ == "__main__":
    unittest.main()
