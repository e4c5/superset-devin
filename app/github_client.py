"""Minimal GitHub client — used only to comment back on issues for visibility."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .logging_setup import log_event


class GitHubClient:
    def __init__(
        self,
        *,
        token: str,
        repo: str,
        base_url: str = "https://api.github.com",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.repo = repo
        self.enabled = bool(token)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def issue_state(self, issue_number: int) -> str | None:
        """``"open"``/``"closed"``, or None when GitHub could not be asked."""
        payload = await self._get(f"/repos/{self.repo}/issues/{issue_number}")
        state = payload.get("state") if payload else None
        return str(state) if state else None

    async def pull_request_state(self, pr_url: str) -> tuple[str, bool] | None:
        """``(state, merged)`` for an ``.../pull/<n>`` URL, or None if unreadable."""
        number = pr_url.rstrip("/").rsplit("/", 1)[-1]
        if not number.isdigit():
            return None
        payload = await self._get(f"/repos/{self.repo}/pulls/{number}")
        if not payload or not payload.get("state"):
            return None
        return str(payload["state"]), bool(payload.get("merged") or payload.get("merged_at"))

    async def _get(self, path: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            response = await self._client.get(path)
        except httpx.HTTPError as exc:
            log_event("github.get_failed", level=logging.WARNING, path=path, error=str(exc))
            return None
        if response.status_code >= 400:
            log_event(
                "github.get_failed", level=logging.WARNING, path=path, status=response.status_code
            )
            return None
        payload = response.json()
        return payload if isinstance(payload, dict) else None

    async def comment(self, issue_number: int, body: str) -> bool:
        """Best-effort issue comment. Never raises — visibility must not break the pipeline."""
        if not self.enabled:
            log_event(
                "github.comment_skipped",
                level=logging.WARNING,
                issue=issue_number,
                reason="GITHUB_TOKEN not set",
            )
            return False
        try:
            response = await self._client.post(
                f"/repos/{self.repo}/issues/{issue_number}/comments", json={"body": body}
            )
        except httpx.HTTPError as exc:
            log_event("github.comment_failed", level=logging.WARNING, issue=issue_number, error=str(exc))
            return False
        if response.status_code >= 400:
            log_event(
                "github.comment_failed",
                level=logging.WARNING,
                issue=issue_number,
                status=response.status_code,
                body=response.text[:300],
            )
            return False
        log_event("github.comment_posted", issue=issue_number)
        return True
