"""Background poller that drives session records to a terminal outcome.

Terminal classification is deliberately fine-grained:

  succeeded         terminal success, structured output ``fixed == true``
  declined          terminal success, structured output ``fixed == false``
  failed            API reports ``status == "error"``
  blocked_on_budget ``suspended`` for a cost/quota reason, or ACU cap hit
  timed_out         never reached a terminal state within SESSION_MAX_WAIT_SECONDS

A network error or 5xx while polling is *not* a failure: the record stays
non-terminal and is retried on the next tick.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .devin_client import DevinAPIError, DevinAuthError, DevinClient, DevinTransientError
from .logging_setup import log_event
from .store import Store

TERMINAL_SUCCESS_STATUS = "exit"
BUDGET_STATUS_DETAILS = {
    "out_of_credits",
    "out_of_quota",
    "no_quota_allocation",
    "payment_declined",
    "usage_limit_exceeded",
    "org_usage_limit_exceeded",
    "user_usage_limit_exceeded",
    "total_session_limit_exceeded",
}


def _pr_url(session: dict[str, Any], structured: dict[str, Any] | None) -> str:
    if structured and structured.get("pr_url"):
        return str(structured["pr_url"])
    for pull in session.get("pull_requests") or []:
        if isinstance(pull, dict):
            url = pull.get("url") or pull.get("html_url")
            if url:
                return str(url)
        elif isinstance(pull, str):
            return pull
    return ""


def classify(session: dict[str, Any], *, max_acu_limit: int | None = None) -> tuple[str | None, str]:
    """Map a session payload to ``(outcome, note)``; ``outcome`` is None if still running."""
    status = session.get("status")
    detail = session.get("status_detail")
    structured = session.get("structured_output") or None

    if status == "error" or detail == "error":
        return "failed", "session reported status=error"

    if status == "suspended" and detail in BUDGET_STATUS_DETAILS:
        return "blocked_on_budget", f"suspended: {detail}"

    acus = session.get("acus_consumed") or 0
    if (
        max_acu_limit is not None
        and status in {"suspended", "exit"}
        and acus >= max_acu_limit
        and not structured
    ):
        return "blocked_on_budget", f"hit max_acu_limit ({max_acu_limit} ACU)"

    if status == TERMINAL_SUCCESS_STATUS or detail == "finished":
        if structured is None:
            # Terminal without structured output: treat as declined-with-no-reason rather
            # than inventing a success we cannot evidence.
            return "declined", "terminal with no structured output"
        return ("succeeded", "fixed=true") if structured.get("fixed") else ("declined", "fixed=false")

    return None, f"status={status} detail={detail}"


class Poller:
    def __init__(
        self,
        *,
        store: Store,
        client: DevinClient,
        interval_seconds: int,
        max_wait_seconds: int,
        max_acu_limit: int,
        on_terminal: Any = None,
    ) -> None:
        self.store = store
        self.client = client
        self.interval_seconds = interval_seconds
        self.max_wait_seconds = max_wait_seconds
        self.max_acu_limit = max_acu_limit
        self.on_terminal = on_terminal
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            rehydrated = self.store.non_terminal()
            log_event("poller.started", tracking=len(rehydrated),
                      finding_keys=[r["finding_key"] for r in rehydrated])
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown path
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                log_event("poller.tick_error", level=logging.ERROR, error=str(exc))
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def tick(self) -> None:
        """One sweep over every non-terminal, session-bearing record."""
        for record in self.store.non_terminal():
            await self._poll_one(record)

    async def _poll_one(self, record: dict[str, Any]) -> None:
        devin_id = record["devin_id"]
        finding_key = record["finding_key"]
        try:
            session = await self.client.get_session(devin_id)
        except (DevinTransientError, DevinAPIError) as exc:
            if isinstance(exc, DevinAuthError):
                self._finalize(record, "errored", f"auth error while polling: {exc}", {})
                return
            # Could not reach the API: explicitly NOT a session failure.
            log_event(
                "poller.transient_poll_failure",
                level=logging.WARNING,
                devin_id=devin_id,
                issue=record["issue_number"],
                error=str(exc),
            )
            self.store.update(finding_key, status="polling", status_detail="unreachable")
            self._check_timeout(record)
            return

        structured = session.get("structured_output") or None
        outcome, note = classify(session, max_acu_limit=self.max_acu_limit)
        status = session.get("status") or ""
        detail = session.get("status_detail")

        if (status, detail) != (record.get("status"), record.get("status_detail")):
            log_event(
                "session.status_transition",
                devin_id=devin_id,
                issue=record["issue_number"],
                from_status=record.get("status"),
                to_status=status,
                status_detail=detail,
                acus=session.get("acus_consumed"),
            )

        self.store.update(
            finding_key,
            status=status,
            status_detail=detail,
            acus_consumed=session.get("acus_consumed") or 0,
            pr_url=_pr_url(session, structured) or record.get("pr_url") or "",
            structured_output=structured,
        )

        if outcome is None:
            self._check_timeout(record)
            return
        self._finalize(record, outcome, note, session)

    def _check_timeout(self, record: dict[str, Any]) -> None:
        age = time.time() - record["created_at"]
        if age > self.max_wait_seconds:
            self._finalize(
                record,
                "timed_out",
                f"no terminal state after {int(age)}s (limit {self.max_wait_seconds}s)",
                {},
            )

    def _finalize(
        self, record: dict[str, Any], outcome: str, note: str, session: dict[str, Any]
    ) -> None:
        self.store.update(record["finding_key"], outcome=outcome, status_detail=note)
        final = self.store.get(record["finding_key"]) or record
        log_event(
            "session.terminal",
            devin_id=record.get("devin_id"),
            issue=record["issue_number"],
            outcome=outcome,
            note=note,
            pr_url=final.get("pr_url") or "",
            acus=final.get("acus_consumed"),
        )
        if self.on_terminal is not None:
            self.on_terminal(final)
