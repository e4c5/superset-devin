from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.devin_client import (
    DevinAuthError,
    DevinClient,
    DevinTransientError,
    DevinValidationError,
)
from app.findings import parse_finding
from app.github_client import GitHubClient
from app.metrics import build_metrics
from app.orchestrator import Orchestrator
from app.poller import Poller, classify
from app.store import STALE_CLAIM_SECONDS, Store
from app.webhook import build_app, verify_signature
from simulator.demo_issues import DEMO_FINDINGS, issue_payload
from simulator.fake_devin import FakeDevinAPI, FakeGitHubAPI, Scenario

ORG = "org-test"
SECRET = "s3cr3t"


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        devin_token="cog_test",
        devin_org_id=ORG,
        devin_api_base="https://api.devin.ai",
        github_webhook_secret=SECRET,
        github_token="ghp_test",
        target_repo="e4c5/superset",
        max_acu_limit=10,
        poll_interval_seconds=1,
        session_max_wait_seconds=3600,
        database_path=str(tmp_path / "state.db"),
        report_path=str(tmp_path / "report.md"),
    )
    base.update(overrides)
    return Settings(**base)


def build_stack(tmp_path, scenarios=None, client_max_retries=5, **overrides):
    settings = make_settings(tmp_path, **overrides)
    fake_devin = FakeDevinAPI(org_id=ORG, scenarios=scenarios or {})
    fake_github = FakeGitHubAPI()
    store = Store(settings.database_path)
    devin = DevinClient(
        base_url=settings.devin_api_base,
        org_id=ORG,
        token=settings.devin_token,
        transport=fake_devin.transport(),
        max_retries=client_max_retries,
        backoff_base=0.001,
    )
    github = GitHubClient(token="ghp_test", repo=settings.target_repo,
                          transport=fake_github.transport())
    orch = Orchestrator(settings=settings, store=store, devin=devin, github=github)
    poller = Poller(
        store=store,
        client=devin,
        interval_seconds=1,
        max_wait_seconds=settings.session_max_wait_seconds,
        max_acu_limit=settings.max_acu_limit,
    )
    return settings, store, orch, poller, fake_devin, fake_github


# --------------------------------------------------------------------------
# signature + gating
# --------------------------------------------------------------------------
def test_signature_verification():
    body = b'{"action":"opened"}'
    good = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(SECRET, body, good)
    assert not verify_signature(SECRET, body, "sha256=deadbeef")
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body + b" ", good)
    assert not verify_signature("", body, good)


def test_webhook_endpoint_gating(tmp_path):
    settings, store, _, _, fake_devin, fake_github = build_stack(tmp_path)
    app = build_app(
        settings,
        devin_client=DevinClient(
            base_url=settings.devin_api_base, org_id=ORG, token="cog_test",
            transport=fake_devin.transport(), backoff_base=0.001,
        ),
        github_client=GitHubClient(token="ghp_test", repo=settings.target_repo,
                                   transport=fake_github.transport()),
        store=store,
        start_poller=False,
    )
    with TestClient(app) as client:
        def post(payload, event="issues", sign=True):
            body = json.dumps(payload).encode()
            headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": "d1"}
            if sign:
                headers["X-Hub-Signature-256"] = (
                    "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
                )
            return client.post("/webhook", content=body, headers=headers)

        payload = issue_payload(DEMO_FINDINGS[0])
        assert post(payload, sign=False).status_code == 401

        unlabeled = issue_payload(DEMO_FINDINGS[0], number=201, label="question")
        assert post(unlabeled).json()["status"] == "ignored"

        closed = issue_payload(DEMO_FINDINGS[0], number=202)
        closed["action"] = "closed"
        assert post(closed).json()["status"] == "ignored"

        assert post(payload, event="push").json()["status"] == "ignored"

        response = post(payload)
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"

        # background task ran: session created for the labeled issue
        assert store.get_by_issue(101)["devin_id"] == "devin-s6440-issue-101"
        assert client.get("/status").json()["issues_addressed"] == 1
        assert "SonarQube auto-remediation" in client.get("/report").text


def test_status_endpoints_require_token_when_configured(tmp_path):
    settings, store, _, _, fake_devin, fake_github = build_stack(tmp_path, status_token="tok")
    app = build_app(
        settings,
        devin_client=DevinClient(
            base_url=settings.devin_api_base, org_id=ORG, token="cog_test",
            transport=fake_devin.transport(), backoff_base=0.001,
        ),
        github_client=GitHubClient(token="ghp_test", repo=settings.target_repo,
                                   transport=fake_github.transport()),
        store=store,
        start_poller=False,
    )
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/status").status_code == 401
        assert client.get("/report").status_code == 401
        auth = {"Authorization": "Bearer tok"}
        assert client.get("/status", headers=auth).status_code == 200
        assert client.get("/report", headers=auth).status_code == 200


def test_gate_rejects_foreign_repository_and_unrelated_labels(tmp_path):
    _, _, orch, _, _, _ = build_stack(tmp_path)

    spoofed = issue_payload(DEMO_FINDINGS[0], repo="attacker/evil")
    handled, reason = orch.should_handle(spoofed)
    assert not handled and "attacker/evil" in reason

    other_label = issue_payload(DEMO_FINDINGS[0], action="labeled")
    other_label["label"] = {"name": "needs-triage"}
    handled, reason = orch.should_handle(other_label)
    assert not handled and "not the trigger label" in reason

    assert orch.should_handle(issue_payload(DEMO_FINDINGS[0], action="labeled"))[0]


def test_abandoned_claim_is_resumed_and_reclaim_clears_prior_attempt(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    assert store.claim_finding("sonar:abc", 1).scenario == "new"
    # A claim with no session recorded is a redelivery while the create is in flight...
    assert store.claim_finding("sonar:abc", 1).scenario == "redelivery"
    # ...but an abandoned one (orchestrator died mid-create) is retried.
    store._connect().execute(
        "UPDATE issues SET updated_at = ? WHERE finding_key = 'sonar:abc'",
        (time.time() - STALE_CLAIM_SECONDS - 1,),
    )
    assert store.claim_finding("sonar:abc", 1).scenario == "resumed"

    store.update(
        "sonar:abc",
        devin_id="devin-1",
        pr_url="https://example.com/pr/1",
        acus_consumed=4.0,
        outcome="failed",
        terminal_at=time.time(),
    )
    reclaimed = store.claim_finding("sonar:abc", 2, rule="r", file="f").record
    assert reclaimed["pr_url"] is None
    assert reclaimed["acus_consumed"] == 0
    assert reclaimed["terminal_at"] is None
    assert reclaimed["created_at"] == reclaimed["updated_at"]


# --------------------------------------------------------------------------
# finding parsing
# --------------------------------------------------------------------------
def test_parse_finding_from_demo_body():
    payload = issue_payload(DEMO_FINDINGS[0])
    finding = parse_finding(payload["issue"]["body"])
    assert finding.rule == "typescript:S6440"
    assert finding.file.endswith("DataTable/DataTable.tsx")
    assert finding.lines == "476,477,484,485,525"
    assert finding.issue_key == "AZk1c0f4-0001-4a11-9d10-datatable6440"
    assert finding.finding_key == "sonar:AZk1c0f4-0001-4a11-9d10-datatable6440"


def test_parse_finding_falls_back_to_file_and_rule():
    finding = parse_finding("Rule: typescript:S6440\nFile: a/b.tsx\nLine(s): 1\n")
    assert not finding.has_sonar_key
    assert finding.finding_key == "filerule:a/b.tsx::typescript:S6440"


# --------------------------------------------------------------------------
# dedup
# --------------------------------------------------------------------------
def test_store_claim_is_atomic_under_concurrency(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    results = []

    def claim(issue_number: int) -> None:
        results.append(store.claim_finding("sonar:abc", issue_number).acquired)

    import threading

    threads = [threading.Thread(target=claim, args=(n,)) for n in range(300, 310)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(results) == 1, "exactly one concurrent claim may win"
    assert store.counters()["dedup_skips"] == 9


@pytest.mark.asyncio
async def test_scenario_b_same_finding_two_issues(tmp_path):
    _, store, orch, _, fake_devin, fake_github = build_stack(tmp_path)
    first = await orch.handle_issue_event(issue_payload(DEMO_FINDINGS[0]))
    assert first["status"] == "session_created"

    second = await orch.handle_issue_event(issue_payload(DEMO_FINDINGS[0], number=106))
    assert second["status"] == "skipped_duplicate"
    assert second["existing_issue"] == 101
    assert store.counters()["dedup_skips"] == 1
    assert len([r for r in store.all_records() if r["devin_id"]]) == 1
    assert fake_github.comments[0][0] == 106
    assert "Duplicate SonarQube finding" in fake_github.comments[0][1]
    # only one create ever reached the API
    assert sum(1 for method, _ in fake_devin.requests if method == "POST") == 1


@pytest.mark.asyncio
async def test_scenario_a_redelivery_is_skipped(tmp_path):
    _, store, orch, _, _, fake_github = build_stack(tmp_path)
    await orch.handle_issue_event(issue_payload(DEMO_FINDINGS[0]))
    again = await orch.handle_issue_event(issue_payload(DEMO_FINDINGS[0]))
    assert again["status"] == "skipped_duplicate"
    assert store.counters()["redeliveries_ignored"] == 1
    # no comment spam on the original issue for a mere redelivery
    assert fake_github.comments == []


@pytest.mark.asyncio
async def test_409_from_create_is_idempotent_success(tmp_path):
    _, store_a, orch_a, _, fake_devin, fake_github = build_stack(tmp_path)
    await orch_a.handle_issue_event(issue_payload(DEMO_FINDINGS[0]))

    # A second replica with a cold dedup store races the same finding.
    settings_b = make_settings(tmp_path / "b")
    store_b = Store(str(tmp_path / "b.db"))
    devin_b = DevinClient(base_url=settings_b.devin_api_base, org_id=ORG, token="cog_test",
                          transport=fake_devin.transport(), backoff_base=0.001)
    orch_b = Orchestrator(
        settings=settings_b, store=store_b, devin=devin_b,
        github=GitHubClient(token="ghp_test", repo="e4c5/superset",
                            transport=fake_github.transport()),
    )
    result = await orch_b.handle_issue_event(issue_payload(DEMO_FINDINGS[0]))
    assert result["status"] == "session_reused"
    assert store_b.counters()["sessions_reused_409"] == 1
    assert store_b.get_by_issue(101)["outcome"] == "in_progress"


# --------------------------------------------------------------------------
# resilient client
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_client_retries_429_and_honours_retry_after():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"status": 429})
        return httpx.Response(200, json={"session_id": "devin-x", "status": "new"})

    client = DevinClient(base_url="https://api.devin.ai", org_id=ORG, token="t",
                         transport=httpx.MockTransport(handler), backoff_base=0.001)
    session = await client.get_session("devin-x")
    assert session["session_id"] == "devin-x"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_client_gives_up_on_persistent_5xx():
    client = DevinClient(
        base_url="https://api.devin.ai", org_id=ORG, token="t", max_retries=3,
        transport=httpx.MockTransport(lambda r: httpx.Response(503, json={"status": 503})),
        backoff_base=0.001,
    )
    with pytest.raises(DevinTransientError):
        await client.get_session("devin-x")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,exc", [(401, DevinAuthError), (403, DevinAuthError), (422, DevinValidationError)]
)
async def test_client_non_retryable_statuses(status, exc):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, json={"status": status, "title": "nope", "errors": []})

    client = DevinClient(base_url="https://api.devin.ai", org_id=ORG, token="t",
                         transport=httpx.MockTransport(handler), backoff_base=0.001)
    with pytest.raises(exc):
        await client.get_session("devin-x")
    assert calls["n"] == 1, "non-retryable statuses must not be retried"


@pytest.mark.asyncio
async def test_create_session_sends_expected_contract():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"session_id": "devin-s6440-issue-101", "status": "new"})

    client = DevinClient(base_url="https://api.devin.ai", org_id=ORG, token="cog_abc",
                         transport=httpx.MockTransport(handler))
    await client.create_session(
        devin_id="devin-s6440-issue-101", prompt="p", repos=["e4c5/superset"],
        tags=["devin-fix"], title="t", max_acu_limit=10,
    )
    assert f"/v3/organizations/{ORG}/sessions" in captured["url"]
    assert "devin_id=devin-s6440-issue-101" in captured["url"]
    assert captured["auth"] == "Bearer cog_abc"
    assert captured["body"]["max_acu_limit"] == 10
    assert captured["body"]["bypass_approval"] is True
    assert captured["body"]["structured_output_required"] is True
    assert captured["body"]["structured_output_schema"]["required"] == [
        "fixed", "rule", "pr_url", "summary", "reason"
    ]


@pytest.mark.asyncio
async def test_orchestrator_marks_errored_and_comments_on_auth_failure(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database_path)
    fake_github = FakeGitHubAPI()
    devin = DevinClient(
        base_url=settings.devin_api_base, org_id=ORG, token="",
        transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"status": 403})),
        backoff_base=0.001,
    )
    orch = Orchestrator(
        settings=settings, store=store, devin=devin,
        github=GitHubClient(token="ghp", repo="e4c5/superset", transport=fake_github.transport()),
    )
    result = await orch.handle_issue_event(issue_payload(DEMO_FINDINGS[0]))
    assert result["status"] == "errored"
    assert store.get_by_issue(101)["outcome"] == "errored"
    assert "could not be created" in fake_github.comments[0][1]
    # A creation failure has no devin_id but must stay visible in the rollup.
    metrics = build_metrics(store)
    assert metrics["issues_addressed"] == 1
    assert metrics["outcomes"]["errored"] == 1
    assert metrics["success_rate_pct"] == 0.0


# --------------------------------------------------------------------------
# poller classification
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "session,expected",
    [
        ({"status": "running", "status_detail": "working"}, None),
        ({"status": "new", "status_detail": None}, None),
        ({"status": "error", "status_detail": "error"}, "failed"),
        ({"status": "suspended", "status_detail": "out_of_credits"}, "blocked_on_budget"),
        ({"status": "suspended", "status_detail": "org_usage_limit_exceeded"}, "blocked_on_budget"),
        ({"status": "suspended", "status_detail": "inactivity"}, None),
        (
            {"status": "exit", "status_detail": "finished",
             "structured_output": {"fixed": True, "pr_url": "u"}},
            "succeeded",
        ),
        (
            {"status": "exit", "status_detail": "finished",
             "structured_output": {"fixed": False, "reason": "false positive"}},
            "declined",
        ),
        (
            {"status": "running", "status_detail": "finished",
             "structured_output": {"fixed": True}},
            "succeeded",
        ),
    ],
)
def test_classify(session, expected):
    assert classify(session, max_acu_limit=10)[0] == expected


def test_classify_acu_cap_is_budget_not_failure():
    session = {"status": "suspended", "status_detail": "inactivity", "acus_consumed": 10}
    assert classify(session, max_acu_limit=10)[0] == "blocked_on_budget"


@pytest.mark.asyncio
async def test_poller_transient_failure_does_not_mark_failed(tmp_path):
    scenarios = {
        "devin-s6440-issue-101": Scenario(
            ticks_to_terminal=2,
            fail_get_on_tick=1,
            structured_output={"fixed": True, "rule": "r", "pr_url": "https://pr/1",
                               "summary": "s", "reason": ""},
        )
    }
    # max_retries=1 so the client cannot absorb the 503 itself and the poller's
    # own transient-vs-terminal distinction is what is under test.
    _, store, orch, poller, _, _ = build_stack(
        tmp_path, scenarios=scenarios, client_max_retries=1
    )
    await orch.handle_issue_event(issue_payload(DEMO_FINDINGS[0]))

    await poller.tick()  # 503 on the first GET
    record = store.get_by_issue(101)
    assert record["outcome"] == "in_progress"
    assert record["status"] == "polling"

    for _ in range(3):
        await poller.tick()
    record = store.get_by_issue(101)
    assert record["outcome"] == "succeeded"
    assert record["pr_url"] == "https://pr/1"


@pytest.mark.asyncio
async def test_poller_times_out_stuck_session(tmp_path):
    scenarios = {"devin-s6440-issue-101": Scenario(ticks_to_terminal=10_000)}
    _, store, orch, poller, _, _ = build_stack(
        tmp_path, scenarios=scenarios, session_max_wait_seconds=0
    )
    await orch.handle_issue_event(issue_payload(DEMO_FINDINGS[0]))
    time.sleep(0.01)
    await poller.tick()
    assert store.get_by_issue(101)["outcome"] == "timed_out"


@pytest.mark.asyncio
async def test_poller_rehydrates_after_restart(tmp_path):
    scenarios = {
        "devin-s6440-issue-101": Scenario(
            ticks_to_terminal=1,
            structured_output={"fixed": True, "rule": "r", "pr_url": "https://pr/1",
                               "summary": "s", "reason": ""},
        )
    }
    settings, store, orch, _, fake_devin, _ = build_stack(tmp_path, scenarios=scenarios)
    await orch.handle_issue_event(issue_payload(DEMO_FINDINGS[0]))

    # Simulate a process restart: brand new Store/Poller over the same database.
    fresh_store = Store(settings.database_path)
    assert [r["devin_id"] for r in fresh_store.non_terminal()] == ["devin-s6440-issue-101"]
    fresh_poller = Poller(
        store=fresh_store,
        client=DevinClient(base_url=settings.devin_api_base, org_id=ORG, token="cog_test",
                           transport=fake_devin.transport(), backoff_base=0.001),
        interval_seconds=1, max_wait_seconds=3600, max_acu_limit=10,
    )
    await fresh_poller.tick()
    assert fresh_store.get_by_issue(101)["outcome"] == "succeeded"


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_metrics_rollup(tmp_path):
    scenarios = {
        "devin-s6440-issue-101": Scenario(
            ticks_to_terminal=1, acus=4.0,
            structured_output={"fixed": True, "rule": "typescript:S6440",
                               "pr_url": "https://pr/1", "summary": "s", "reason": ""},
        ),
        "devin-s6440-issue-105": Scenario(
            ticks_to_terminal=1, acus=2.0,
            structured_output={"fixed": False, "rule": "typescript:S6757", "pr_url": "",
                               "summary": "s", "reason": "false positive"},
        ),
    }
    _, store, orch, poller, _, _ = build_stack(tmp_path, scenarios=scenarios)
    await orch.handle_issue_event(issue_payload(DEMO_FINDINGS[0]))
    await orch.handle_issue_event(issue_payload(DEMO_FINDINGS[4]))
    await orch.handle_issue_event(issue_payload(DEMO_FINDINGS[0], number=106))
    for _ in range(3):
        await poller.tick()

    metrics = build_metrics(store)
    assert metrics["issues_addressed"] == 2
    assert metrics["outcomes"]["succeeded"] == 1
    assert metrics["outcomes"]["declined"] == 1
    assert metrics["success_rate_pct"] == 50.0
    assert metrics["prs_opened"] == 1
    assert metrics["total_acus"] == 6.0
    assert metrics["cost_per_fix_acus"] == 6.0
    assert metrics["dedup_skips"] == 1


def test_event_loop_policy_sanity():
    assert asyncio.get_event_loop_policy() is not None
