"""GitHub issue operations for the idea feedback loop (REST + GraphQL).

Uses a **user PAT** (``repo`` scope). Labelling, commenting and closing are plain REST;
assigning the Copilot coding agent is the GraphQL ``replaceActorsForAssignable`` dance,
ported from the mindVault ``newsletter-assign`` workflow. The Actions installation token
cannot assign the agent — a user token can, which is exactly why nauroBot holds one.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("naurobot.github")

_REST = "https://api.github.com"
_GRAPHQL = "https://api.github.com/graphql"
# The login the Copilot coding agent presents as an assignable actor.
_COPILOT_LOGIN = "copilot-swe-agent"

_SUGGESTED_ACTORS = """
query($o:String!,$n:String!){
  repository(owner:$o,name:$n){
    suggestedActors(capabilities:[CAN_BE_ASSIGNED],first:50){
      nodes{ login __typename ... on Bot{ id } }
    }
  }
}
"""

_ISSUE_ID = """
query($o:String!,$n:String!,$num:Int!){
  repository(owner:$o,name:$n){ issue(number:$num){ id } }
}
"""

_REPLACE_ACTORS = """
mutation($a:ID!,$b:ID!){
  replaceActorsForAssignable(input:{assignableId:$a, actorIds:[$b]}){
    assignable{ ... on Issue{ number } }
  }
}
"""


class GitHub:
    """Issue operations against ``{owner}/{repo}`` using a user PAT + shared HTTP client."""

    def __init__(self, token: str, owner: str, client: httpx.AsyncClient) -> None:
        self._owner = owner
        self._client = client
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def add_labels(self, repo: str, num: int, labels: list[str]) -> None:
        # `approved` / `declined` won't pre-exist in a project repo, and the add-labels
        # endpoint rejects unknown labels — so ensure each exists first (idempotent).
        for label in labels:
            await self._ensure_label(repo, label)
        resp = await self._client.post(
            f"{_REST}/repos/{self._owner}/{repo}/issues/{num}/labels",
            headers=self._headers,
            json={"labels": labels},
        )
        resp.raise_for_status()

    async def _ensure_label(self, repo: str, name: str) -> None:
        """Create a repo label if missing. Idempotent — tolerates 'already exists' (422)."""
        resp = await self._client.post(
            f"{_REST}/repos/{self._owner}/{repo}/labels",
            headers=self._headers,
            json={"name": name, "color": "ededed"},
        )
        # 201 = created; 422 = already exists — both are fine.
        if resp.status_code not in (201, 422):
            resp.raise_for_status()

    async def remove_label(self, repo: str, num: int, label: str) -> None:
        resp = await self._client.delete(
            f"{_REST}/repos/{self._owner}/{repo}/issues/{num}/labels/{label}",
            headers=self._headers,
        )
        # 404 = the label wasn't on the issue; that's a no-op, not an error.
        if resp.status_code not in (200, 404):
            resp.raise_for_status()

    async def comment(self, repo: str, num: int, body: str) -> None:
        resp = await self._client.post(
            f"{_REST}/repos/{self._owner}/{repo}/issues/{num}/comments",
            headers=self._headers,
            json={"body": body},
        )
        resp.raise_for_status()

    async def close_issue(self, repo: str, num: int, state_reason: str = "not_planned") -> None:
        resp = await self._client.patch(
            f"{_REST}/repos/{self._owner}/{repo}/issues/{num}",
            headers=self._headers,
            json={"state": "closed", "state_reason": state_reason},
        )
        resp.raise_for_status()

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.post(
            _GRAPHQL, headers=self._headers, json={"query": query, "variables": variables}
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"GraphQL error: {data['errors']}")
        return data["data"]

    async def assign_copilot(self, repo: str, num: int) -> bool:
        """Assign the Copilot coding agent to an issue. Returns True on success.

        Returns False (and logs) when the agent isn't an assignable actor on the repo —
        e.g. Copilot coding agent isn't enabled there — so the caller can fall back to
        asking the human to assign manually.
        """
        actors = await self._graphql(_SUGGESTED_ACTORS, {"o": self._owner, "n": repo})
        bot_id: Optional[str] = None
        for node in actors["repository"]["suggestedActors"]["nodes"]:
            if node.get("login") == _COPILOT_LOGIN:
                bot_id = node.get("id")
                break
        if not bot_id:
            log.warning("Copilot agent not assignable on %s/%s", self._owner, repo)
            return False
        issue = await self._graphql(_ISSUE_ID, {"o": self._owner, "n": repo, "num": num})
        issue_id = issue["repository"]["issue"]["id"]
        await self._graphql(_REPLACE_ACTORS, {"a": issue_id, "b": bot_id})
        return True

    # ── Pull-request approval loop ─────────────────────────────────────────
    # autoRefine cards a ready + CI-green Copilot PR to Telegram; a 👍 approves and
    # squash-merges it, a 👎 closes it. Approving as the PAT user (who is not the PR
    # author) is what unblocks the merge; squash matches the lab merge convention.

    async def approve_pr(self, repo: str, num: int, body: str = "") -> None:
        """Submit an APPROVE review on a PR (as the PAT user, who is not the author)."""
        resp = await self._client.post(
            f"{_REST}/repos/{self._owner}/{repo}/pulls/{num}/reviews",
            headers=self._headers,
            json={"event": "APPROVE", "body": body or "Approved via Telegram."},
        )
        resp.raise_for_status()

    async def merge_pr(self, repo: str, num: int, method: str = "squash") -> tuple[bool, str]:
        """Squash-merge a PR. Returns ``(merged, detail)``; a non-200 is reported, not raised.

        GitHub returns 405 when the PR isn't mergeable (CI still running, a conflict, …)
        and 409 when the head SHA moved — surface the message so the caller can tell the
        human instead of failing silently.
        """
        resp = await self._client.put(
            f"{_REST}/repos/{self._owner}/{repo}/pulls/{num}/merge",
            headers=self._headers,
            json={"merge_method": method},
        )
        if resp.status_code == 200:
            return True, "merged"
        try:
            detail = resp.json().get("message", resp.text)
        except (ValueError, KeyError):
            detail = resp.text
        return False, f"{resp.status_code}: {detail}"

    async def close_pr(self, repo: str, num: int) -> None:
        """Close a PR without merging (the 👎 path)."""
        resp = await self._client.patch(
            f"{_REST}/repos/{self._owner}/{repo}/pulls/{num}",
            headers=self._headers,
            json={"state": "closed"},
        )
        resp.raise_for_status()
