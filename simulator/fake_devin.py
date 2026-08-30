"""An in-process fake of the Devin API v3 (and of GitHub's issue-comment API).

Used by ``scripts/simulate.py`` and the tests so the whole pipeline —
webhook, dedup, retry/backoff, 409 idempotency, poller classification and the
report — can be exercised without network access or a real token.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class Scenario:
    """How a fake session should behave over successive polls."""

    #: number of GETs before the session reaches its terminal state
    ticks_to_terminal: int = 2
    terminal_status: str = "exit"
    terminal_status_detail: str | None = "finished"
    structured_output: dict[str, Any] | None = None
    acus: float = 2.0
    #: emit one 503 on the Nth GET (1-based) to exercise transient-poll handling
    fail_get_on_tick: int | None = None
    #: emit one 429 (with Retry-After) before honouring the create
    rate_limit_creates: int = 0


DEFAULT_SCENARIO = Scenario(
    structured_output={
        "fixed": True,
        "rule": "typescript:S6440",
        "pr_url": "https://github.com/e4c5/superset/pull/999",
        "summary": "Applied the minimal fix.",
        "reason": "",
    }
)


@dataclass
class FakeDevinAPI:
    org_id: str
    scenarios: dict[str, Scenario] = field(default_factory=dict)
    default_scenario: Scenario = field(default_factory=lambda: DEFAULT_SCENARIO)
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    polls: dict[str, int] = field(default_factory=dict)
    create_attempts: dict[str, int] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)

    def scenario_for(self, devin_id: str) -> Scenario:
        return self.scenarios.get(devin_id, self.default_scenario)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # -- routing ---------------------------------------------------------
    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, str(request.url)))
        prefix = f"/v3/organizations/{self.org_id}/sessions"
        path = request.url.path
        if request.headers.get("Authorization", "") in ("", "Bearer "):
            return self._problem(401, "Unauthorized", "Missing service user token")
        if request.method == "POST" and path == prefix:
            return self._create(request)
        if request.method == "GET" and path.startswith(prefix + "/"):
            return self._get(path.rsplit("/", 1)[-1])
        return self._problem(404, "Not Found", f"No route for {request.method} {path}")

    # -- handlers --------------------------------------------------------
    def _create(self, request: httpx.Request) -> httpx.Response:
        devin_id = request.url.params.get("devin_id")
        if not devin_id:
            return self._problem(422, "Unprocessable Content", "devin_id is required by this fake",
                                 errors=[{"loc": ["query", "devin_id"], "msg": "field required"}])
        body = json.loads(request.content or b"{}")
        if not body.get("prompt"):
            return self._problem(422, "Unprocessable Content", "prompt is required",
                                 errors=[{"loc": ["body", "prompt"], "msg": "field required"}])

        scenario = self.scenario_for(devin_id)
        attempts = self.create_attempts.get(devin_id, 0)
        if attempts < scenario.rate_limit_creates:
            self.create_attempts[devin_id] = attempts + 1
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json=self._problem_body(429, "Too Many Requests", "Slow down"),
            )
        self.create_attempts[devin_id] = attempts + 1

        if devin_id in self.sessions:
            # Scenario A: GitHub redelivered the same webhook.
            return self._problem(409, "Conflict", f"Session {devin_id} already exists")

        now = int(time.time())
        self.sessions[devin_id] = {
            "session_id": devin_id,
            "url": f"https://app.devin.ai/sessions/{devin_id.removeprefix('devin-')}",
            "status": "new",
            "status_detail": None,
            "tags": body.get("tags") or [],
            "title": body.get("title"),
            "org_id": self.org_id,
            "created_at": now,
            "updated_at": now,
            "acus_consumed": 0.0,
            "pull_requests": [],
            "structured_output": None,
        }
        return httpx.Response(200, json=self.sessions[devin_id])

    def _get(self, devin_id: str) -> httpx.Response:
        session = self.sessions.get(devin_id)
        if session is None:
            return self._problem(404, "Not Found", f"No session {devin_id}")

        scenario = self.scenario_for(devin_id)
        tick = self.polls.get(devin_id, 0) + 1
        self.polls[devin_id] = tick

        if scenario.fail_get_on_tick == tick:
            return httpx.Response(
                503, json=self._problem_body(503, "Service Unavailable", "upstream hiccup")
            )

        session["updated_at"] = int(time.time())
        if tick < scenario.ticks_to_terminal:
            session["status"] = "running"
            session["status_detail"] = "working"
            session["acus_consumed"] = round(scenario.acus * tick / scenario.ticks_to_terminal, 2)
        else:
            session["status"] = scenario.terminal_status
            session["status_detail"] = scenario.terminal_status_detail
            session["acus_consumed"] = scenario.acus
            session["structured_output"] = scenario.structured_output
            pr_url = (scenario.structured_output or {}).get("pr_url")
            session["pull_requests"] = [{"url": pr_url}] if pr_url else []
        return httpx.Response(200, json=session)

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _problem_body(status: int, title: str, detail: str, errors: Any = None) -> dict[str, Any]:
        return {
            "status": status,
            "title": title,
            "detail": detail,
            "errors": errors,
            "type": "about:blank",
        }

    def _problem(self, status: int, title: str, detail: str, errors: Any = None) -> httpx.Response:
        return httpx.Response(status, json=self._problem_body(status, title, detail, errors))


@dataclass
class FakeGitHubAPI:
    """Captures issue comments so the demo can show dedup feedback."""

    comments: list[tuple[int, str]] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            parts = request.url.path.strip("/").split("/")
            issue_number = int(parts[parts.index("issues") + 1])
            body = json.loads(request.content or b"{}").get("body", "")
            self.comments.append((issue_number, body))
            return httpx.Response(201, json={"id": len(self.comments)})

        return httpx.MockTransport(handle)
