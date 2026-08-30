#!/usr/bin/env python3
"""Run the full pipeline end-to-end against an in-process fake Devin API.

No token, no network and no GitHub repo required — this exercises the real
webhook handler, the real dedup store, the real resilient client (including its
backoff and 409 path) and the real poller, then prints the run report.

    python -m scripts.simulate            # or: make simulate
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.devin_client import DevinClient  # noqa: E402
from app.github_client import GitHubClient  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402
from app.metrics import build_metrics, render_report  # noqa: E402
from app.orchestrator import Orchestrator  # noqa: E402
from app.poller import Poller  # noqa: E402
from app.store import Store  # noqa: E402
from simulator.demo_issues import (  # noqa: E402
    DEMO_FINDINGS,
    issue_payload,
    pull_request_payload,
)
from simulator.fake_devin import FakeDevinAPI, FakeGitHubAPI, Scenario  # noqa: E402

SECRET = "simulated-webhook-secret"
ORG_ID = "org-d2a9a68f5bab400597bcc8c7e6e387a1"


def pr(rule: str, number: int) -> dict[str, object]:
    return {
        "fixed": True,
        "rule": rule,
        "pr_url": f"https://github.com/e4c5/superset/pull/{number}",
        "summary": "Applied the minimal fix and verified lint on the changed file.",
        "reason": "",
    }


SCENARIOS = {
    # 1. Hero fix, but the create is rate-limited once so backoff is visible.
    "devin-s6440-issue-101": Scenario(
        ticks_to_terminal=2,
        structured_output=pr("typescript:S6440", 3101),
        acus=3.4,
        rate_limit_creates=1,
    ),
    # 2. Straightforward fix, with one transient 503 while polling.
    "devin-s6440-issue-102": Scenario(
        ticks_to_terminal=3,
        structured_output=pr("typescript:S6959", 3102),
        acus=1.8,
        fail_get_on_tick=2,
    ),
    # 3. Straightforward fix, settled by its pull_request webhook rather than a poll.
    "devin-s6440-issue-103": Scenario(
        ticks_to_terminal=1, structured_output=pr("typescript:S6440", 3103), acus=2.1
    ),
    # 4. Cost cap reached: blocked_on_budget, NOT a Devin defect.
    "devin-s6440-issue-104": Scenario(
        ticks_to_terminal=2,
        terminal_status="suspended",
        terminal_status_detail="usage_limit_exceeded",
        structured_output=None,
        acus=10.0,
    ),
    # 5. Judgment call: Devin declines the false positive.
    "devin-s6440-issue-105": Scenario(
        ticks_to_terminal=2,
        structured_output={
            "fixed": False,
            "rule": "typescript:S6757",
            "pr_url": "",
            "summary": "Reviewed the ondrag callback; left the code unchanged.",
            "reason": "typed `this` param is valid for ECharts ondrag callback; not a defect",
        },
        acus=1.2,
    ),
}


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


async def main() -> int:
    configure_logging()
    workdir = tempfile.mkdtemp(prefix="superset-devin-sim-")
    settings = Settings(
        devin_token="cog_simulated",
        devin_org_id=ORG_ID,
        devin_api_base="https://api.devin.ai",
        github_webhook_secret=SECRET,
        github_token="ghp_simulated",
        target_repo="e4c5/superset",
        max_acu_limit=10,
        poll_interval_seconds=1,
        session_max_wait_seconds=3600,
        database_path=os.path.join(workdir, "state.db"),
        report_path=os.path.join(workdir, "report.md"),
        # Compressed backoff so a run that would take minutes of wall clock in
        # production finishes in a second here; the schedule itself is the same.
        poll_backoff_base_seconds=0,
        poll_backoff_cap_seconds=0,
    )

    fake_devin = FakeDevinAPI(org_id=ORG_ID, scenarios=SCENARIOS)
    fake_github = FakeGitHubAPI()
    store = Store(settings.database_path)
    devin = DevinClient(
        base_url=settings.devin_api_base,
        org_id=ORG_ID,
        token=settings.devin_token,
        transport=fake_devin.transport(),
        backoff_base=0.01,
    )
    github = GitHubClient(
        token=settings.github_token,
        repo=settings.target_repo,
        transport=fake_github.transport(),
    )
    poller = Poller(
        store=store,
        client=devin,
        interval_seconds=settings.poll_interval_seconds,
        max_wait_seconds=settings.session_max_wait_seconds,
        max_acu_limit=settings.max_acu_limit,
        backoff_base_seconds=settings.poll_backoff_base_seconds,
        backoff_cap_seconds=settings.poll_backoff_cap_seconds,
    )
    orchestrator = Orchestrator(
        settings=settings, store=store, devin=devin, github=github, poller=None
    )

    from app.webhook import verify_signature

    async def deliver(payload: dict[str, object], note: str) -> None:
        body = json.dumps(payload).encode()
        assert verify_signature(SECRET, body, sign(body)), "signature check must pass"
        store.increment("webhooks_received")
        handle, reason = orchestrator.should_handle(payload)
        print(f"\n>>> {note} -> {'accepted' if handle else 'ignored: ' + reason}")
        if handle:
            await orchestrator.handle_issue_event(payload)

    # --- the demo: five findings filed one at a time -----------------------
    for finding in DEMO_FINDINGS:
        await deliver(issue_payload(finding), f"issue #{finding['number']} opened")

    # --- Scenario A: GitHub redelivers the webhook for issue #101 ----------
    await deliver(issue_payload(DEMO_FINDINGS[0]), "issue #101 webhook REDELIVERED")

    # --- Scenario B: the same finding filed again as issue #106 ------------
    await deliver(
        issue_payload(DEMO_FINDINGS[0], number=106, action="labeled"),
        "issue #106 opened (same SonarQube key as #101)",
    )

    # --- Scenario A backstop: a second replica with a cold store ----------
    # Its dedup table knows nothing about issue #103, so it tries to create the
    # session again; the deterministic devin_id makes the API answer 409 and the
    # client treats that as an idempotent success rather than a failure.
    cold_store = Store(os.path.join(workdir, "replica.db"))
    replica = Orchestrator(
        settings=settings, store=cold_store, devin=devin, github=github, poller=None
    )
    print("\n>>> second replica (cold dedup store) replays issue #103")
    result = await replica.handle_issue_event(issue_payload(DEMO_FINDINGS[2]))
    print(f"    replica result: {result['status']} (409 reuses: "
          f"{cold_store.counters().get('sessions_reused_409', 0)})")
    store.increment("sessions_reused_409", cold_store.counters().get("sessions_reused_409", 0))

    # --- Noise the gate must ignore ---------------------------------------
    await deliver(
        issue_payload(DEMO_FINDINGS[1], number=107, label="question"),
        "issue #107 opened without the devin-fix label",
    )
    closed = issue_payload(DEMO_FINDINGS[1], number=108)
    closed["action"] = "closed"
    await deliver(closed, "issue #108 closed event")

    # --- the happy path resolves on the pull_request webhook, not on a poll --
    # (the poller is attached only here so the simulation drives every tick itself)
    orchestrator.poller = poller
    pr_payload = pull_request_payload(103, number=3103)
    handle, reason = orchestrator.should_handle_pull_request(pr_payload)
    print(f"\n>>> pull_request opened for issue #103 -> "
          f"{'accepted' if handle else 'ignored: ' + reason}")
    if handle:
        result = await orchestrator.handle_pull_request_event(pr_payload)
        print(f"    immediate check: {result}")

    # --- poll to completion ------------------------------------------------
    print("\n>>> polling sessions to terminal state")
    for _ in range(10):
        if not store.non_terminal():
            break
        await poller.tick()
        await asyncio.sleep(0.05)

    print("\n>>> GitHub comments posted by the orchestrator")
    for number, body in fake_github.comments:
        print(f"  - issue #{number}: {body.splitlines()[0]}")

    report = render_report(build_metrics(store))
    with open(settings.report_path, "w", encoding="utf-8") as handle:
        handle.write(report)
    print("\n" + report)
    print(f"(report written to {settings.report_path})")

    await devin.aclose()
    await github.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
