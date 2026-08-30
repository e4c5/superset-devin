"""Minimal GitHub client — used only to comment back on issues for visibility."""

from __future__ import annotations

import logging

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
