"""Handler routing tests (offline; mocked Telegram + GitHub clients)."""
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "functions"))

from github_ops import ChecksState, GitHub  # noqa: E402
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

    def test_valid_park(self):
        self.assertEqual(_parse_callback("arf:era:12:p"), ("era", 12, "p"))

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

    async def test_park_relabels_keeps_open(self):
        result = await handle_update(_callback_update("arf:era:12:p"), self.tg, self.gh, "42")
        self.assertEqual(result["action"], "parked")
        self.gh.add_labels.assert_awaited_once_with("era", 12, ["parked"])
        self.gh.remove_label.assert_awaited_once_with("era", 12, "needs-approval")
        self.gh.close_issue.assert_not_awaited()
        self.gh.assign_copilot.assert_not_awaited()
        self.tg.edit_reply_markup.assert_awaited_once()

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
        self.gh.checks_state.return_value = ChecksState("passing", "SUCCESS", "c0ffee")

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
        # Pinned to the commit whose checks were read, not to "whatever the head is now".
        self.gh.merge_pr.assert_awaited_once_with("era", 12, sha="c0ffee")
        self.gh.comment.assert_awaited_once()
        self.tg.edit_reply_markup.assert_awaited_once()
        # A PR tap must never touch the idea-issue lifecycle.
        self.gh.close_issue.assert_not_awaited()
        self.gh.assign_copilot.assert_not_awaited()

    async def test_merge_is_pinned_to_the_verified_commit(self):
        # The requirement: whatever commit was judged is the commit that gets merged.
        # If these ever drift apart the bot merges code it never checked.
        self.gh.checks_state.return_value = ChecksState("passing", "SUCCESS", "abc123")
        await handle_update(self._pr_update("arfpr:era:12:y"), self.tg, self.gh, "42")
        self.assertEqual(self.gh.merge_pr.await_args.kwargs["sha"], "abc123")

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

    # ── CI gate ──────────────────────────────────────────────────────────────
    # These assert the *requirement* (a PR that is not green is never merged), so they
    # fail if the gate is removed — not merely if the code is reshaped. Without the gate
    # every one of these cases merges, because no NauroLabs repo requires status checks
    # and GitHub therefore returns 200 rather than the 405 the old path relied on.

    async def test_red_ci_is_never_merged_or_approved(self):
        self.gh.checks_state.return_value = ChecksState("failing", "FAILURE", "c0ffee")
        result = await handle_update(self._pr_update("arfpr:era:12:y"), self.tg, self.gh, "42")
        self.assertEqual(result["action"], "merge_refused")
        self.gh.merge_pr.assert_not_awaited()
        self.gh.approve_pr.assert_not_awaited()
        self.gh.comment.assert_not_awaited()
        self.tg.send_message.assert_awaited_once()
        self.tg.edit_reply_markup.assert_not_awaited()  # keep buttons for a retry

    async def test_pending_ci_is_not_merged(self):
        self.gh.checks_state.return_value = ChecksState("pending", "PENDING", "c0ffee")
        result = await handle_update(self._pr_update("arfpr:era:12:y"), self.tg, self.gh, "42")
        self.assertEqual(result["action"], "merge_refused")
        self.gh.merge_pr.assert_not_awaited()

    async def test_pr_without_checks_is_not_merged(self):
        # A repo with no CI must not read as green — that is the silent-failure case.
        self.gh.checks_state.return_value = ChecksState("none", "no checks configured", "c0ffee")
        result = await handle_update(self._pr_update("arfpr:era:12:y"), self.tg, self.gh, "42")
        self.assertEqual(result["action"], "merge_refused")
        self.gh.merge_pr.assert_not_awaited()

    async def test_unreadable_ci_fails_closed(self):
        self.gh.checks_state.return_value = ChecksState("unknown", "network error")
        result = await handle_update(self._pr_update("arfpr:era:12:y"), self.tg, self.gh, "42")
        self.assertEqual(result["action"], "merge_refused")
        self.gh.merge_pr.assert_not_awaited()


class PrMergeWireTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end over the wire: a real GitHub client, only the HTTP layer faked.

    The tests above mock `checks_state` wholesale, so they prove the *handler* honours a
    verdict — not that a red PR produces one. These drive the real `checks_state` from a
    real GraphQL body and assert that no merge request is ever *issued*. That is the
    requirement stated at the only layer where it cannot be faked: the network.
    """

    def _update(self):
        return {"callback_query": {
            "id": "cb1", "data": "arfpr:era:12",
            "message": {"message_id": 7, "chat": {"id": 42}, "text": "PR\narfpr:era:12"},
        }}

    async def _tap_approve(self, rollup_state):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path == "/graphql":
                return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
                    "commits": {"nodes": [{"commit": {
                        "oid": "c0ffee",
                        "statusCheckRollup": (
                            None if rollup_state is None else {"state": rollup_state}
                        ),
                    }}]}}}}})
            return httpx.Response(200, json={"merged": True, "id": 1})

        update = self._update()
        update["callback_query"]["data"] = "arfpr:era:12:y"
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gh = GitHub("tok", "samoletovs", client)
            tg = AsyncMock(spec=Telegram)
            result = await handle_update(update, tg, gh, "42")
        return result, requests

    async def test_red_rollup_issues_no_merge_request(self):
        result, requests = await self._tap_approve("FAILURE")
        self.assertEqual(result["action"], "merge_refused")
        self.assertEqual([r for r in requests if r.method == "PUT"], [])
        self.assertEqual([r for r in requests if "/reviews" in r.url.path], [])

    async def test_missing_rollup_issues_no_merge_request(self):
        result, requests = await self._tap_approve(None)
        self.assertEqual(result["action"], "merge_refused")
        self.assertEqual([r for r in requests if r.method == "PUT"], [])

    async def test_green_rollup_merges_pinned_to_the_checked_commit(self):
        result, requests = await self._tap_approve("SUCCESS")
        self.assertEqual(result["action"], "merged")
        merges = [r for r in requests if r.method == "PUT"]
        self.assertEqual(len(merges), 1)
        self.assertEqual(json.loads(merges[0].content)["sha"], "c0ffee")


if __name__ == "__main__":
    unittest.main()
