"""Resilient client for the Devin API v3.

Endpoints (verified against https://docs.devin.ai/api-reference/v3):
  POST /v3/organizations/{org_id}/sessions?devin_id=<deterministic id>
  GET  /v3/organizations/{org_id}/sessions/{devin_id}

Status-code policy:
  429 / 5xx  -> transient, exponential backoff (honours ``Retry-After``)
  401 / 403  -> auth/permission, non-retryable, logged loudly
  409        -> duplicate, idempotent success: fetch and return existing session
  422        -> malformed request, non-retryable, logs ``ProblemDetail.errors``
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any

import httpx

from .logging_setup import log_event

STRUCTURED_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fixed": {"type": "boolean", "description": "True only if code was changed."},
        "rule": {"type": "string", "description": "The SonarQube rule key."},
        "pr_url": {"type": "string", "description": "PR URL, empty string if none."},
        "summary": {"type": "string", "description": "What was done."},
        "reason": {
            "type": "string",
            "description": "Why the fix was declined. Required when fixed is false.",
        },
    },
    "required": ["fixed", "rule", "pr_url", "summary", "reason"],
    "additionalProperties": False,
}


class DevinAPIError(Exception):
    def __init__(self, message: str, *, status: int | None = None, problem: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.problem = problem


class DevinAuthError(DevinAPIError):
    """401/403 — token missing UseDevinSessions/ViewOrgSessions, or wrong org."""


class DevinValidationError(DevinAPIError):
    """422 — request body rejected."""


class DevinTransientError(DevinAPIError):
    """429/5xx/network — retryable, exhausted."""


@dataclass
class CreateResult:
    session: dict[str, Any]
    reused: bool  # True when the API answered 409 and we fetched the existing session


def _problem(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"detail": response.text[:500]}


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class DevinClient:
    def __init__(
        self,
        *,
        base_url: str,
        org_id: str,
        token: str,
        max_retries: int = 5,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
    ) -> None:
        self.org_id = org_id
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "superset-devin-orchestrator/1.0",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> DevinClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    @property
    def _sessions_path(self) -> str:
        return f"/v3/organizations/{self.org_id}/sessions"

    async def _sleep_backoff(self, attempt: int, retry_after: float | None) -> None:
        if retry_after is not None:
            delay = retry_after
        else:
            delay = min(self.backoff_cap, self.backoff_base * (2**attempt))
            delay += random.uniform(0, delay * 0.25)  # jitter
        await asyncio.sleep(delay)

    async def _request(
        self, method: str, path: str, *, json_body: Any = None, params: Any = None
    ) -> httpx.Response:
        """Issue a request, retrying only genuinely transient failures."""
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = await self._client.request(
                    method, path, json=json_body, params=params
                )
            except httpx.HTTPError as exc:
                last_error = exc
                log_event(
                    "devin_api.network_error",
                    level=logging.WARNING,
                    method=method,
                    path=path,
                    attempt=attempt + 1,
                    error=str(exc),
                )
                if attempt == self.max_retries - 1:
                    break
                await self._sleep_backoff(attempt, None)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                problem = _problem(response)
                log_event(
                    "devin_api.transient_error",
                    level=logging.WARNING,
                    method=method,
                    path=path,
                    status=response.status_code,
                    attempt=attempt + 1,
                    retry_after=response.headers.get("Retry-After"),
                    problem=problem,
                )
                last_error = DevinTransientError(
                    f"{method} {path} -> {response.status_code}",
                    status=response.status_code,
                    problem=problem,
                )
                if attempt == self.max_retries - 1:
                    break
                await self._sleep_backoff(attempt, _retry_after(response))
                continue

            return response

        raise DevinTransientError(
            f"{method} {path} failed after {self.max_retries} attempts: {last_error}"
        )

    @staticmethod
    def _raise_for_terminal_status(response: httpx.Response, context: dict[str, Any]) -> None:
        status = response.status_code
        if status < 400:
            return
        problem = _problem(response)
        if status in (401, 403):
            log_event(
                "devin_api.auth_error",
                level=logging.ERROR,
                status=status,
                problem=problem,
                hint="service-user token needs UseDevinSessions/ViewOrgSessions on this org_id",
                **context,
            )
            raise DevinAuthError("Devin API rejected credentials", status=status, problem=problem)
        if status == 422:
            log_event(
                "devin_api.validation_error",
                level=logging.ERROR,
                status=status,
                errors=(problem or {}).get("errors"),
                problem=problem,
                **context,
            )
            raise DevinValidationError("Devin API rejected the request body", status=status, problem=problem)
        log_event(
            "devin_api.error", level=logging.ERROR, status=status, problem=problem, **context
        )
        raise DevinAPIError(f"Devin API error {status}", status=status, problem=problem)

    async def create_session(
        self,
        *,
        devin_id: str,
        prompt: str,
        repos: list[str],
        tags: list[str],
        title: str,
        max_acu_limit: int,
    ) -> CreateResult:
        body = {
            "prompt": prompt,
            "repos": repos,
            "tags": tags,
            "title": title,
            "max_acu_limit": max_acu_limit,
            "bypass_approval": True,
            "structured_output_required": True,
            "structured_output_schema": STRUCTURED_OUTPUT_SCHEMA,
        }
        response = await self._request(
            "POST", self._sessions_path, json_body=body, params={"devin_id": devin_id}
        )
        if response.status_code == 409:
            log_event(
                "devin_api.session_conflict_reused",
                devin_id=devin_id,
                detail=(_problem(response) or {}).get("detail"),
            )
            return CreateResult(session=await self.get_session(devin_id), reused=True)

        self._raise_for_terminal_status(response, {"devin_id": devin_id, "op": "create_session"})
        return CreateResult(session=response.json(), reused=False)

    async def get_session(self, devin_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"{self._sessions_path}/{devin_id}")
        self._raise_for_terminal_status(response, {"devin_id": devin_id, "op": "get_session"})
        return response.json()
