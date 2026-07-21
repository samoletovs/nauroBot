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
