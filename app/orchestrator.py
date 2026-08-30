"""Wires webhook events to Devin sessions, dedup, persistence and reporting."""

from __future__ import annotations

import logging
import re
from typing import Any

from .config import Settings
from .devin_client import DevinAPIError, DevinClient
from .findings import build_prompt, parse_finding
from .github_client import GitHubClient
from .logging_setup import log_event
from .metrics import write_report
from .poller import Poller
from .store import TERMINAL_OUTCOMES, Claim, Store, tags_for

TRIGGER_ACTIONS = {"opened", "labeled"}
#: A pull request is only a completion signal when it appears (or reappears).
PR_TRIGGER_ACTIONS = {"opened", "reopened", "ready_for_review"}
_ISSUE_REFERENCE = re.compile(r"#(\d+)")


class Orchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        devin: DevinClient,
        github: GitHubClient,
        poller: Poller | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.devin = devin
        self.github = github
        self.poller = poller

    # -- helpers ---------------------------------------------------------
    def devin_id_for(self, issue_number: int) -> str:
        return f"{self.settings.devin_id_prefix}{issue_number}"

    def should_handle(self, payload: dict[str, Any]) -> tuple[bool, str]:
        repo = (payload.get("repository") or {}).get("full_name")
        if repo != self.settings.target_repo:
            # A secret shared with another repo's webhook must not be able to aim
            # a session at the target repo.
            return False, f"repository={repo!r} is not {self.settings.target_repo!r}"
        action = payload.get("action")
        if action not in TRIGGER_ACTIONS:
            return False, f"action={action} not in {sorted(TRIGGER_ACTIONS)}"
        issue = payload.get("issue") or {}
        if action == "labeled":
            added = payload.get("label") or {}
            added_name = added.get("name") if isinstance(added, dict) else added
            if added_name != self.settings.trigger_label:
                return False, f"labeled with {added_name!r}, not the trigger label"
        labels = {
            (label.get("name") if isinstance(label, dict) else label)
            for label in issue.get("labels") or []
        }
        if self.settings.trigger_label not in labels:
            return False, f"missing label {self.settings.trigger_label!r}"
        return True, "ok"

    # -- pull requests ---------------------------------------------------
    def referenced_issue(self, payload: dict[str, Any]) -> int | None:
        """The tracked issue a pull request refers to, if any.

        Devin is prompted to reference ``issue #<n>`` in the pull request it opens
        (see ``findings.PROMPT_TEMPLATE``), so the first ``#<n>`` in the title or
        body that we actually have a record for identifies the session.
        """
        pull = payload.get("pull_request") or {}
        text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
        for match in _ISSUE_REFERENCE.finditer(text):
            issue_number = int(match.group(1))
            if self.store.get_by_issue(issue_number) is not None:
                return issue_number
        return None

    def should_handle_pull_request(self, payload: dict[str, Any]) -> tuple[bool, str]:
        """Gate for ``pull_request`` events; deliberately separate from ``should_handle``.

        The two payload shapes share nothing but ``repository``, and a pull request
        carries no ``devin-fix`` label.
        """
        repo = (payload.get("repository") or {}).get("full_name")
        if repo != self.settings.target_repo:
            return False, f"repository={repo!r} is not {self.settings.target_repo!r}"
        action = payload.get("action")
        if action not in PR_TRIGGER_ACTIONS:
            return False, f"action={action} not in {sorted(PR_TRIGGER_ACTIONS)}"
        issue_number = self.referenced_issue(payload)
        if issue_number is None:
            return False, "pull request references no tracked issue"
        record = self.store.get_by_issue(issue_number)
        if record and record.get("outcome") in TERMINAL_OUTCOMES:
            return False, f"issue #{issue_number} already {record['outcome']}"
        return True, "ok"

    async def handle_pull_request_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Resolve a session immediately from its pull request instead of waiting for a poll.

        The check runs through the poller's own poll path, so ACUs, structured
        output, ``pr_url`` and the terminal report are written exactly once and
        identically to the polled path.
        """
        pull = payload.get("pull_request") or {}
        issue_number = self.referenced_issue(payload)
        if issue_number is None:
            return {"status": "ignored", "reason": "no tracked issue referenced"}
        record = self.store.get_by_issue(issue_number)
        if record is None:
            return {"status": "ignored", "reason": f"no record for issue #{issue_number}"}

        log_event(
            "webhook.pull_request_accepted",
            issue=issue_number,
            action=payload.get("action"),
            pr_url=pull.get("html_url"),
            devin_id=record.get("devin_id"),
        )

        if record.get("outcome") in TERMINAL_OUTCOMES:
            return {"status": "already_terminal", "issue": issue_number,
                    "outcome": record["outcome"]}
        if self.poller is None:
            return {"status": "ignored", "reason": "no poller configured"}

        polled = await self.poller.poll_now(record)
        if not polled:
            return {"status": "already_terminal", "issue": issue_number}
        settled = self.store.get(record["finding_key"]) or {}
        return {
            "status": "checked",
            "issue": issue_number,
            "outcome": settled.get("outcome"),
        }

    # -- main entry point ------------------------------------------------
    async def handle_issue_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        issue = payload.get("issue") or {}
        issue_number = int(issue.get("number") or 0)
        title = issue.get("title") or ""
        finding = parse_finding(issue.get("body") or "")

        log_event(
            "webhook.accepted",
            issue=issue_number,
            action=payload.get("action"),
            title=title,
            rule=finding.rule,
            file=finding.file,
            finding_key=finding.finding_key,
            dedup_source="sonar_issue_key" if finding.has_sonar_key else "file_rule_fallback",
        )

        claim = self.store.claim_finding(
            finding.finding_key,
            issue_number,
            rule=finding.rule,
            file=finding.file,
            lines=finding.lines,
            message=finding.message,
        )
        if not claim.acquired and claim.scenario == "duplicate_finding":
            claim = await self._maybe_reclaim(claim, issue_number, finding)
        if not claim.acquired:
            return await self._skip_duplicate(issue_number, claim.record, finding.finding_key)

        log_event(
            "dedup.claimed",
            issue=issue_number,
            finding_key=finding.finding_key,
            scenario=claim.scenario,
        )
        return await self._create_session(issue_number, finding)

    async def _maybe_reclaim(self, claim: Claim, issue_number: int, finding: Any) -> Claim:
        """Re-open a finding whose earlier attempt never landed.

        A prior `succeeded` record only blocks a refile while the fix is still
        live: an issue closed without a merged PR, or a PR closed unmerged, means
        the defect is still in the tree and the finding deserves another session.
        """
        previous = claim.record
        reason = await self._abandoned_reason(previous)
        if reason is None:
            return claim
        reclaimed = self.store.reclaim_finding(
            finding.finding_key,
            issue_number,
            expected_updated_at=float(previous["updated_at"]),
            rule=finding.rule,
            file=finding.file,
            lines=finding.lines,
            message=finding.message,
        )
        log_event(
            "dedup.reclaim" if reclaimed.acquired else "dedup.reclaim_lost",
            issue=issue_number,
            finding_key=finding.finding_key,
            previous_issue=previous.get("issue_number"),
            previous_outcome=previous.get("outcome"),
            previous_pr=previous.get("pr_url"),
            reason=reason,
        )
        return reclaimed

    async def _abandoned_reason(self, previous: dict[str, Any]) -> str | None:
        """Why the previous attempt should not block a refile, or None if it should."""
        if previous.get("outcome") not in TERMINAL_OUTCOMES:
            return None  # a session is still working on it
        pr_url = previous.get("pr_url") or ""
        if pr_url:
            state = await self.github.pull_request_state(pr_url)
            if state is None:
                return None  # cannot verify — stay conservative and dedup
            pr_state, merged = state
            if merged:
                return None
            if pr_state == "closed":
                return "previous pull request closed without merging"
        previous_issue = previous.get("issue_number")
        if isinstance(previous_issue, int) and await self.github.issue_state(previous_issue) == "closed":
            return "previous issue closed without a merged fix"
        if not pr_url and previous.get("outcome") == "succeeded":
            return "previous session succeeded without opening a pull request"
        return None

    async def _skip_duplicate(
        self, issue_number: int, existing: dict[str, Any], finding_key: str
    ) -> dict[str, Any]:
        log_event(
            "dedup.skipped",
            issue=issue_number,
            finding_key=finding_key,
            existing_issue=existing.get("issue_number"),
            existing_session=existing.get("devin_id"),
            existing_outcome=existing.get("outcome"),
            pr_url=existing.get("pr_url"),
        )
        if existing.get("issue_number") != issue_number:
            session_url = (
                f"https://app.devin.ai/sessions/{(existing.get('devin_id') or '').removeprefix('devin-')}"
                if existing.get("devin_id")
                else "(session not yet created)"
            )
            pr_line = f"\n- Pull request: {existing['pr_url']}" if existing.get("pr_url") else ""
            await self.github.comment(
                issue_number,
                "**Duplicate SonarQube finding — no new Devin session created.**\n\n"
                f"This finding (`{finding_key}`) is already being handled by "
                f"issue #{existing.get('issue_number')}.\n"
                f"- Devin session: {session_url}\n"
                f"- Current outcome: `{existing.get('outcome')}`"
                f"{pr_line}",
            )
        return {
            "status": "skipped_duplicate",
            "finding_key": finding_key,
            "existing_issue": existing.get("issue_number"),
        }

    async def _create_session(self, issue_number: int, finding: Any) -> dict[str, Any]:
        devin_id = self.devin_id_for(issue_number)
        tags = list(tags_for(issue_number, finding.rule or "unknown"))
        prompt = build_prompt(finding, issue_number, self.settings.target_repo)
        title = f"Fix SonarQube {finding.rule or 'finding'} in issue #{issue_number}"

        if self.settings.dry_run:
            # Exercise signature check → gate → parse → dedup against real GitHub
            # traffic without spending ACUs. The record keeps no devin_id, so the
            # poller ignores it and it stays out of the metrics denominator.
            self.store.update(
                finding.finding_key,
                tags=tags,
                status="dry_run",
                status_detail="DRY_RUN: no Devin session created",
            )
            log_event(
                "session.dry_run",
                issue=issue_number,
                devin_id=devin_id,
                title=title,
                tags=tags,
                prompt_chars=len(prompt),
            )
            return {"status": "dry_run", "devin_id": devin_id}

        try:
            result = await self.devin.create_session(
                devin_id=devin_id,
                prompt=prompt,
                repos=[self.settings.target_repo],
                tags=tags,
                title=title,
                max_acu_limit=self.settings.max_acu_limit,
            )
        except DevinAPIError as exc:
            self.store.update(
                finding.finding_key,
                outcome="errored",
                status="errored",
                status_detail=f"{type(exc).__name__}: {exc}",
            )
            log_event(
                "session.create_failed",
                level=logging.ERROR,
                issue=issue_number,
                devin_id=devin_id,
                error=str(exc),
                problem=getattr(exc, "problem", None),
            )
            await self.github.comment(
                issue_number,
                "**Devin session could not be created.**\n\n"
                f"- Error: `{type(exc).__name__}: {exc}`\n"
                f"- Devin ID attempted: `{devin_id}`\n\n"
                "The orchestrator has recorded this issue as `errored`.",
            )
            return {"status": "errored", "error": str(exc)}

        session = result.session
        if result.reused:
            self.store.increment("sessions_reused_409")
        self.store.update(
            finding.finding_key,
            session_id=session.get("session_id"),
            devin_id=session.get("session_id") or devin_id,
            tags=tags,
            status=session.get("status") or "new",
            status_detail=session.get("status_detail"),
            acus_consumed=session.get("acus_consumed") or 0,
            outcome="in_progress",
        )
        log_event(
            "session.created",
            issue=issue_number,
            devin_id=session.get("session_id") or devin_id,
            reused_409=result.reused,
            url=session.get("url"),
            tags=tags,
        )
        if self.poller is not None:
            self.poller.start()
        return {
            "status": "session_reused" if result.reused else "session_created",
            "devin_id": session.get("session_id") or devin_id,
            "url": session.get("url"),
        }

    def write_report(self) -> str:
        return write_report(self.store, self.settings.report_path)
