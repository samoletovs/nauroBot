"""Handler routing tests (offline; mocked Telegram + GitHub clients)."""
import os
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "functions"))

from github_ops import GitHub  # noqa: E402
from handlers import _parse_callback, _parse_pr_callback, handle_update  # noqa: E402
from telegram import Telegram  # noqa: E402


def _callback_update(data, chat_id=42, message_id=7, text="idea\narf:era:12"):
    return {
        "callback_query": {
            "id": "cb1",
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id}, "text": text},
        }
    }


class ParseCallbackTests(unittest.TestCase):
    def test_valid_yes(self):
        self.assertEqual(_parse_callback("arf:era:12:y"), ("era", 12, "y"))

    def test_valid_no(self):
        self.assertEqual(_parse_callback("arf:amberRepublic:3:n"), ("amberRepublic", 3, "n"))

    def test_wrong_prefix_rejected(self):
        self.assertIsNone(_parse_callback("hs:1:era:12"))

    def test_bad_verdict_rejected(self):
        self.assertIsNone(_parse_callback("arf:era:12:x"))

    def test_non_numeric_rejected(self):
        self.assertIsNone(_parse_callback("arf:era:xx:y"))


class HandleCallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tg = AsyncMock(spec=Telegram)
        self.gh = AsyncMock(spec=GitHub)
        self.gh.assign_copilot.return_value = True

    async def test_approve_relabels_and_assigns(self):
        result = await handle_update(_callback_update("arf:era:12:y"), self.tg, self.gh, "42")
        self.assertEqual(result["action"], "approved")
        self.assertTrue(result["assigned"])
        self.gh.remove_label.assert_awaited_once_with("era", 12, "needs-approval")
        self.gh.add_labels.assert_awaited_once_with("era", 12, ["approved"])
        self.gh.assign_copilot.assert_awaited_once_with("era", 12)
        self.gh.close_issue.assert_not_awaited()
        self.tg.answer_callback.assert_awaited()
        self.tg.edit_reply_markup.assert_awaited_once()

    async def test_approve_without_agent_notes_manual(self):
        self.gh.assign_copilot.return_value = False
        result = await handle_update(_callback_update("arf:era:12:y"), self.tg, self.gh, "42")
        self.assertFalse(result["assigned"])

    async def test_decline_closes_and_labels(self):
        result = await handle_update(_callback_update("arf:era:12:n"), self.tg, self.gh, "42")
        self.assertEqual(result["action"], "declined")
        self.gh.add_labels.assert_awaited_once_with("era", 12, ["declined"])
        self.gh.close_issue.assert_awaited_once()
        self.gh.assign_copilot.assert_not_awaited()
        self.tg.send_message.assert_awaited_once()  # invites a reason

    async def test_unauthorised_chat_rejected(self):
        result = await handle_update(
            _callback_update("arf:era:12:y", chat_id=999), self.tg, self.gh, "42"
        )
        self.assertFalse(result["ok"])
        self.gh.remove_label.assert_not_awaited()
        self.gh.assign_copilot.assert_not_awaited()

    async def test_unparseable_callback_ignored(self):
        result = await handle_update(_callback_update("garbage"), self.tg, self.gh, "42")
        self.assertEqual(result["action"], "ignored")
        self.gh.add_labels.assert_not_awaited()

    async def test_any_chat_allowed_when_no_allowlist(self):
        result = await handle_update(
            _callback_update("arf:era:12:y", chat_id=123), self.tg, self.gh, ""
        )
        self.assertEqual(result["action"], "approved")


class HandleReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tg = AsyncMock(spec=Telegram)
        self.gh = AsyncMock(spec=GitHub)

    def _reply(self, reason, parent_text="idea\narf:era:12", chat_id=42):
        return {
            "message": {
                "chat": {"id": chat_id},
                "text": reason,
                "reply_to_message": {"text": parent_text},
            }
        }

    async def test_reply_logged_as_comment(self):
        result = await handle_update(self._reply("too complex"), self.tg, self.gh, "42")
        self.assertEqual(result["action"], "reason")
        self.gh.comment.assert_awaited_once()
        repo, num, body = self.gh.comment.await_args.args
        self.assertEqual((repo, num), ("era", 12))
        self.assertIn("too complex", body)

    async def test_reply_without_token_ignored(self):
        result = await handle_update(
            self._reply("hi", parent_text="no token here"), self.tg, self.gh, "42"
        )
        self.assertEqual(result["action"], "ignored")
        self.gh.comment.assert_not_awaited()

    async def test_empty_reply_ignored(self):
        result = await handle_update(self._reply(""), self.tg, self.gh, "42")
        self.assertEqual(result["action"], "ignored")
        self.gh.comment.assert_not_awaited()

    async def test_plain_message_ignored(self):
        result = await handle_update(
            {"message": {"chat": {"id": 42}, "text": "hello"}}, self.tg, self.gh, "42"
        )
        self.assertEqual(result["action"], "ignored")


class ParsePrCallbackTests(unittest.TestCase):
    def test_valid_yes(self):
        self.assertEqual(_parse_pr_callback("arfpr:era:12:y"), ("era", 12, "y"))

    def test_valid_no(self):
        self.assertEqual(_parse_pr_callback("arfpr:turgo:5:n"), ("turgo", 5, "n"))

    def test_idea_callback_is_not_a_pr_callback(self):
        self.assertIsNone(_parse_pr_callback("arf:era:12:y"))

    def test_bad_verdict_rejected(self):
        self.assertIsNone(_parse_pr_callback("arfpr:era:12:x"))

    def test_idea_parser_rejects_pr_callback(self):
        # No namespace collision: the arf: parser must not match an arfpr: payload.
        self.assertIsNone(_parse_callback("arfpr:era:12:y"))


class HandlePrCallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tg = AsyncMock(spec=Telegram)
        self.gh = AsyncMock(spec=GitHub)
        self.gh.merge_pr.return_value = (True, "merged")

    def _pr_update(self, data, chat_id=42, message_id=7):
        return {
            "callback_query": {
                "id": "cb1",
                "data": data,
                "message": {
                    "message_id": message_id,
                    "chat": {"id": chat_id},
                    "text": "PR ready\narfpr:era:12",
                },
            }
        }

    async def test_approve_merges(self):
        result = await handle_update(self._pr_update("arfpr:era:12:y"), self.tg, self.gh, "42")
        self.assertEqual(result["action"], "merged")
        self.gh.approve_pr.assert_awaited_once_with("era", 12)
        self.gh.merge_pr.assert_awaited_once_with("era", 12)
        self.gh.comment.assert_awaited_once()
        self.tg.edit_reply_markup.assert_awaited_once()
        # A PR tap must never touch the idea-issue lifecycle.
        self.gh.close_issue.assert_not_awaited()
        self.gh.assign_copilot.assert_not_awaited()

    async def test_approve_reports_when_not_mergeable(self):
        self.gh.merge_pr.return_value = (False, "405: Base branch was modified")
        result = await handle_update(self._pr_update("arfpr:era:12:y"), self.tg, self.gh, "42")
        self.assertEqual(result["action"], "approved_unmerged")
        self.gh.approve_pr.assert_awaited_once()
        self.tg.send_message.assert_awaited_once()  # tells the human why it won't merge
        self.tg.edit_reply_markup.assert_not_awaited()  # keep the buttons for a retry

    async def test_decline_closes_pr(self):
        result = await handle_update(self._pr_update("arfpr:era:12:n"), self.tg, self.gh, "42")
        self.assertEqual(result["action"], "pr_closed")
        self.gh.close_pr.assert_awaited_once_with("era", 12)
        self.gh.comment.assert_awaited_once()
        self.gh.merge_pr.assert_not_awaited()

    async def test_unauthorised_pr_rejected(self):
        result = await handle_update(
            self._pr_update("arfpr:era:12:y", chat_id=999), self.tg, self.gh, "42"
        )
        self.assertFalse(result["ok"])
        self.gh.approve_pr.assert_not_awaited()
        self.gh.merge_pr.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
