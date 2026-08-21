"""GitHub ops tests (offline; httpx MockTransport — no network)."""
import json
import os
import sys
import unittest

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "functions"))

from github_ops import GitHub  # noqa: E402


class AssignCopilotTests(unittest.IsolatedAsyncioTestCase):
    async def _run_assign(self, actor_login):
        calls = []

        def handler(request):
            query = json.loads(request.content).get("query", "")
            calls.append(query)
            if "suggestedActors" in query:
                return httpx.Response(200, json={"data": {"repository": {"suggestedActors": {
                    "nodes": [{"login": actor_login, "__typename": "Bot", "id": "BOT_ID"}]}}}})
            if "issue(number" in query:
                return httpx.Response(200, json={"data": {"repository": {"issue": {"id": "ISSUE_ID"}}}})
            return httpx.Response(200, json={"data": {"replaceActorsForAssignable": {
                "assignable": {"number": 12}}}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gh = GitHub("tok", "samoletovs", client)
            return await gh.assign_copilot("era", 12), calls

    async def test_assigns_when_agent_available(self):
        ok, calls = await self._run_assign("copilot-swe-agent")
        self.assertTrue(ok)
        self.assertEqual(len(calls), 3)  # actors → issue id → mutation

    async def test_returns_false_when_agent_absent(self):
        ok, calls = await self._run_assign("someone-else")
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)  # stops after the actors lookup


class RestTests(unittest.IsolatedAsyncioTestCase):
    async def test_remove_label_tolerates_404(self):
        def handler(request):
            return httpx.Response(404, json={"message": "Label does not exist"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gh = GitHub("tok", "samoletovs", client)
            await gh.remove_label("era", 12, "needs-approval")  # must not raise

    async def test_add_labels_posts_expected(self):
        seen = {}

        def handler(request):
            if request.url.path.endswith("/issues/12/labels"):
                seen["url"] = str(request.url)
                seen["body"] = json.loads(request.content)
            return httpx.Response(201, json=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gh = GitHub("tok", "samoletovs", client)
            await gh.add_labels("era", 12, ["approved"])
        self.assertIn("/repos/samoletovs/era/issues/12/labels", seen["url"])
        self.assertEqual(seen["body"], {"labels": ["approved"]})

    async def test_add_labels_ensures_repo_label_first(self):
        paths = []

        def handler(request):
            paths.append(request.url.path)
            # Repo-level label already exists → 422; attach succeeds.
            if request.url.path.endswith("/era/labels"):
                return httpx.Response(422, json={"message": "already_exists"})
            return httpx.Response(200, json=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gh = GitHub("tok", "samoletovs", client)
            await gh.add_labels("era", 12, ["declined"])  # must not raise on 422
        self.assertTrue(any(p.endswith("/era/labels") for p in paths))
        self.assertTrue(any(p.endswith("/issues/12/labels") for p in paths))

    async def test_close_issue_sets_not_planned(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gh = GitHub("tok", "samoletovs", client)
            await gh.close_issue("era", 12)
        self.assertEqual(seen["body"], {"state": "closed", "state_reason": "not_planned"})

    async def test_graphql_raises_on_errors(self):
        def handler(request):
            return httpx.Response(200, json={"errors": [{"message": "bad"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gh = GitHub("tok", "samoletovs", client)
            with self.assertRaises(RuntimeError):
                await gh.assign_copilot("era", 12)


class PrOpsTests(unittest.IsolatedAsyncioTestCase):
    async def test_approve_pr_posts_approve_review(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": 1})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gh = GitHub("tok", "samoletovs", client)
            await gh.approve_pr("era", 12)
        self.assertTrue(seen["path"].endswith("/repos/samoletovs/era/pulls/12/reviews"))
        self.assertEqual(seen["body"]["event"], "APPROVE")

    async def test_merge_pr_success_uses_squash(self):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"merged": True, "sha": "abc"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gh = GitHub("tok", "samoletovs", client)
            merged, detail = await gh.merge_pr("era", 12)
        self.assertTrue(merged)
        self.assertEqual(detail, "merged")
        self.assertEqual(seen["method"], "PUT")
        self.assertEqual(seen["body"]["merge_method"], "squash")

    async def test_merge_pr_reports_405(self):
        def handler(request):
            return httpx.Response(405, json={"message": "Pull Request is not mergeable"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gh = GitHub("tok", "samoletovs", client)
            merged, detail = await gh.merge_pr("era", 12)
        self.assertFalse(merged)
        self.assertIn("405", detail)
        self.assertIn("not mergeable", detail)

    async def test_close_pr_sets_closed(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gh = GitHub("tok", "samoletovs", client)
            await gh.close_pr("era", 12)
        self.assertEqual(seen["body"], {"state": "closed"})


class ChecksStateTests(unittest.IsolatedAsyncioTestCase):
    """The CI rollup must resolve to a state, and must never guess 'passing'."""

    async def _state(self, response):
        def handler(request):
            return response

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gh = GitHub("tok", "samoletovs", client)
            return await gh.checks_state("era", 12)

    def _rollup(self, state):
        return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": state}}}]}}}}})

    async def test_success_is_passing(self):
        self.assertEqual((await self._state(self._rollup("SUCCESS")))[0], "passing")

    async def test_failure_is_failing(self):
        self.assertEqual((await self._state(self._rollup("FAILURE")))[0], "failing")

    async def test_error_is_failing(self):
        self.assertEqual((await self._state(self._rollup("ERROR")))[0], "failing")

    async def test_pending_is_pending(self):
        self.assertEqual((await self._state(self._rollup("PENDING")))[0], "pending")

    async def test_absent_rollup_is_none_not_passing(self):
        resp = httpx.Response(200, json={"data": {"repository": {"pullRequest": {
            "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]}}}}})
        self.assertEqual((await self._state(resp))[0], "none")

    async def test_graphql_errors_are_unknown(self):
        resp = httpx.Response(200, json={"errors": [{"message": "Bad credentials"}]})
        state, detail = await self._state(resp)
        self.assertEqual(state, "unknown")
        self.assertIn("Bad credentials", detail)

    async def test_http_error_is_unknown_not_passing(self):
        self.assertEqual((await self._state(httpx.Response(500, text="boom")))[0], "unknown")

    async def test_missing_pull_request_is_unknown(self):
        # PR deleted, repo renamed, or the PAT lost access: `data` is present, PR is null.
        resp = httpx.Response(200, json={"data": {"repository": {"pullRequest": None}}})
        self.assertEqual((await self._state(resp))[0], "unknown")

    async def test_empty_commit_list_is_unknown(self):
        resp = httpx.Response(200, json={"data": {"repository": {"pullRequest": {
            "commits": {"nodes": []}}}}})
        self.assertEqual((await self._state(resp))[0], "unknown")

    async def test_missing_state_key_is_unknown(self):
        resp = httpx.Response(200, json={"data": {"repository": {"pullRequest": {
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {}}}]}}}}})
        self.assertEqual((await self._state(resp))[0], "unknown")

    async def test_non_object_payload_is_unknown_and_does_not_raise(self):
        # Valid JSON that isn't an object. checks_state must *return* a state, never raise:
        # an escaping exception is caught by the webhook's catch-all, so the human's tap
        # would silently do nothing instead of showing a refusal they can act on.
        self.assertEqual((await self._state(httpx.Response(200, json=["nope"])))[0], "unknown")

    async def test_no_payload_shape_can_raise_or_read_green(self):
        """checks_state must *return* a verdict for any body, never raise.

        An exception here does not reach the user: the webhook's catch-all swallows it,
        so the human's tap looks like it did nothing and they tap again. Both properties
        are asserted together because a payload that raises is also a payload that was
        never judged not-green.
        """
        hostile = [
            {"errors": ["a bare string, not an object"]},
            {"errors": [{"no_message_key": 1}]},
            {"errors": "not even a list"},
            {"data": None},
            {"data": {"repository": None}},
            {"data": {"repository": {"pullRequest": {"commits": None}}}},
            {"data": {"repository": {"pullRequest": {
                "commits": {"nodes": [{"commit": {"statusCheckRollup": "SUCCESS"}}]}}}}},
            {"data": {"repository": {"pullRequest": {
                "commits": {"nodes": [{"commit": None}]}}}}},
            {},
        ]
        for body in hostile:
            with self.subTest(body=body):
                verdict, detail = await self._state(httpx.Response(200, json=body))
                self.assertNotEqual(verdict, "passing")
                self.assertTrue(detail)

    async def test_only_success_is_ever_passing(self):
        """The whole guarantee in one test: nothing but an explicit SUCCESS reads green.

        This asserts the requirement, not the implementation — it fails if anyone widens
        the state table, including for a GitHub enum member that does not exist yet. The
        last five are CheckConclusionState values that should never surface as a rollup
        state; if GitHub ever leaked one through, the gate must still refuse.
        """
        never_green = [
            "FAILURE", "ERROR", "PENDING", "EXPECTED", "", None, "SOMETHING_GITHUB_ADDS",
            "NEUTRAL", "SKIPPED", "CANCELLED", "ACTION_REQUIRED", "TIMED_OUT",
        ]
        for rollup_state in never_green:
            with self.subTest(rollup=rollup_state):
                verdict, _ = await self._state(self._rollup(rollup_state))
                self.assertNotEqual(verdict, "passing")


if __name__ == "__main__":
    unittest.main()
