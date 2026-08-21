"""GitHub issue operations for the idea feedback loop (REST + GraphQL).

Uses a **user PAT** (``repo`` scope). Labelling, commenting and closing are plain REST;
assigning the Copilot coding agent is the GraphQL ``replaceActorsForAssignable`` dance,
ported from the mindVault ``newsletter-assign`` workflow. The Actions installation token
cannot assign the agent — a user token can, which is exactly why nauroBot holds one.
"""
from __future__ import annotations

import logging
from typing import Any, NamedTuple, Optional

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

# GitHub's StatusState enum → our verdict. **Anything absent from this table is
# `unknown`, and `unknown` is never green.** That default is the security property:
# if GitHub adds an enum member, or returns one we misread, the merge gate refuses
# rather than guessing. Widening this table is a security change — the
# `test_only_success_is_ever_passing` test exists to make that deliberate.
#
# Matched exactly, not case-folded: GraphQL enum members are always upper-case, so
# normalising a lower-case "success" to green would only ever accept a response
# GitHub does not send.
_ROLLUP_STATES = {
    "SUCCESS": "passing",
    "EXPECTED": "pending",
    "PENDING": "pending",
    "FAILURE": "failing",
    "ERROR": "failing",
}


class ChecksState(NamedTuple):
    """A CI verdict, and the exact commit it was read from.

    ``sha`` is what makes the verdict actionable rather than merely informative: it is
    handed to ``merge_pr`` so GitHub refuses (409) if the head moved between the check
    and the merge. A verdict without a ``sha`` can never be ``passing`` — see
    ``checks_state``.
    """

    state: str
    detail: str
    sha: str = ""


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

    async def checks_state(self, repo: str, num: int) -> ChecksState:
        """Resolve the CI rollup for a PR's head commit.

        ``state`` is one of ``passing`` / ``failing`` / ``pending`` / ``none`` / ``unknown``.

        This is a **client-side** gate and it is not redundant with GitHub's own 405.
        Relying on the 405 assumes the target repo has branch protection with required
        status checks; audited 2026-08-21, **no NauroLabs repo actually requires checks**
        (12 of 17 have no protection at all, the rest declare no contexts). With no
        required check, GitHub merges a red-CI PR and returns 200 — so the 405 path can
        never fire for the case it was trusted to catch.

        ``none`` (a PR with no checks at all) is deliberately *not* treated as passing;
        the caller decides, so a repo without CI cannot silently look green.

        **What ``passing`` does and does not prove.** It proves GitHub's own aggregate for
        the head commit is green — the same thing a human sees before clicking Merge. It
        does *not* prove that any particular workflow ran: GitHub folds ``SKIPPED`` and
        ``NEUTRAL`` conclusions into a ``SUCCESS`` rollup, so a PR whose every check was
        skipped reads green here exactly as it does on the PR page. Closing that gap needs
        a per-repo list of required contexts to verify by name; tracked separately, and
        deliberately not smuggled in as a default here.
        """
        query = """
        query($o:String!,$n:String!,$num:Int!){
          repository(owner:$o,name:$n){
            pullRequest(number:$num){
              commits(last:1){ nodes{ commit{
                oid
                statusCheckRollup{ state }
              } } }
            }
          }
        }
        """
        try:
            resp = await self._client.post(
                _GRAPHQL,
                headers=self._headers,
                json={
                    "query": query,
                    "variables": {"o": self._owner, "n": repo, "num": num},
                },
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Fail closed at the call site: an unresolvable rollup must not read as green.
            log.warning("checks_state failed for %s#%s: %s", repo, num, exc)
            return ChecksState("unknown", str(exc))

        if not isinstance(payload, dict):
            # Valid JSON that isn't an object (a proxy or captive-portal error page).
            # This method's contract is that it always *returns* a state and never raises:
            # an escaping exception is swallowed by the webhook's catch-all, so the human
            # would see the tap do nothing at all rather than a refusal they can act on.
            return ChecksState("unknown", f"unexpected response body ({type(payload).__name__})")

        try:
            # Checked before `data`: GraphQL can return partial data *alongside* errors,
            # and a green-looking rollup next to an error is not a verdict we can trust.
            if payload.get("errors"):
                detail = "; ".join(str(e.get("message", e)) for e in payload["errors"])
                log.warning("checks_state GraphQL errors for %s#%s: %s", repo, num, detail)
                return ChecksState("unknown", detail)

            nodes = payload["data"]["repository"]["pullRequest"]["commits"]["nodes"]
            commit = nodes[0]["commit"]
            sha = commit["oid"]
            rollup = commit["statusCheckRollup"]
            if rollup is None:
                return ChecksState("none", "no checks configured on the head commit", sha)
            state = rollup["state"]
        except (AttributeError, KeyError, IndexError, TypeError) as exc:
            # Every unexpected shape lands here, and every one of them is `unknown`.
            # This is why the whole parse sits inside the try rather than only the
            # subscript chain: a malformed `errors` entry or a non-object rollup would
            # otherwise raise past the guard, and a raised exception is not a refusal.
            return ChecksState("unknown", f"unreadable rollup response ({type(exc).__name__})")

        verdict = _ROLLUP_STATES.get(state, "unknown") if isinstance(state, str) else "unknown"
        if verdict == "passing" and not isinstance(sha, str):
            # Green but unpinnable: we could not name the commit we just judged, so the
            # merge could not be pinned to it. Refuse rather than merge an unnamed head.
            return ChecksState("unknown", "rollup was green but the head commit had no oid")
        return ChecksState(verdict, str(state) if state else "empty state", sha)

    async def merge_pr(
        self, repo: str, num: int, method: str = "squash", sha: str = ""
    ) -> tuple[bool, str]:
        """Squash-merge a PR. Returns ``(merged, detail)``; a non-200 is reported, not raised.

        GitHub returns 405 when the PR isn't mergeable (CI still running, a conflict, …).

        ``sha`` pins the merge to one commit: GitHub returns **409** if the head has moved
        since, instead of merging whatever arrived in the meantime. Without it there is no
        head-moved protection at all — the endpoint simply merges the current head — so the
        caller passes the exact commit whose checks it verified. That closes the window
        between reading the CI rollup and merging, which matters here because the PRs this
        bot merges belong to a coding agent that may still be pushing to them.
        """
        body: dict[str, Any] = {"merge_method": method}
        if sha:
            body["sha"] = sha
        resp = await self._client.put(
            f"{_REST}/repos/{self._owner}/{repo}/pulls/{num}/merge",
            headers=self._headers,
            json=body,
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
