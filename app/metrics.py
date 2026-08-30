"""Rollup metrics and the human-readable report."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from .store import NON_TERMINAL_OUTCOME, Store

OUTCOMES = ["succeeded", "declined", "failed", "blocked_on_budget", "timed_out", "errored"]


def _elapsed(record: dict[str, Any]) -> float | None:
    end = record.get("terminal_at")
    if not end:
        return None
    return round(float(end) - float(record["created_at"]), 1)


def build_metrics(store: Store) -> dict[str, Any]:
    # A record without a session id is either an in-flight claim (not addressed yet)
    # or a creation failure — the latter must stay in the errored count and in the
    # success-rate denominator.
    records = [
        r
        for r in store.all_records()
        if r.get("devin_id") or r.get("outcome") != NON_TERMINAL_OUTCOME
    ]
    counters = store.counters()

    breakdown = {name: 0 for name in OUTCOMES}
    breakdown["in_progress"] = 0
    for record in records:
        breakdown[record["outcome"]] = breakdown.get(record["outcome"], 0) + 1

    addressed = len(records)
    succeeded = breakdown["succeeded"]
    pr_urls = [r["pr_url"] for r in records if r.get("pr_url")]
    total_acus = round(sum(float(r.get("acus_consumed") or 0) for r in records), 2)
    elapsed = [e for e in (_elapsed(r) for r in records) if e is not None]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "issues_addressed": addressed,
        "outcomes": breakdown,
        "success_rate_pct": round(100.0 * succeeded / addressed, 1) if addressed else 0.0,
        "prs_opened": len(pr_urls),
        "pr_urls": pr_urls,
        "total_acus": total_acus,
        "acus_per_session": {
            r["devin_id"]: round(float(r.get("acus_consumed") or 0), 2) for r in records
        },
        "cost_per_fix_acus": round(total_acus / len(pr_urls), 2) if pr_urls else None,
        "elapsed_seconds": {
            "per_issue": {str(r["issue_number"]): _elapsed(r) for r in records},
            "mean": round(sum(elapsed) / len(elapsed), 1) if elapsed else None,
            "max": max(elapsed) if elapsed else None,
        },
        "dedup_skips": counters.get("dedup_skips", 0),
        "redeliveries_ignored": counters.get("redeliveries_ignored", 0),
        "webhooks_received": counters.get("webhooks_received", 0),
        "webhooks_ignored": counters.get("webhooks_ignored", 0),
        "sessions_reused_409": counters.get("sessions_reused_409", 0),
        "records": [
            {
                "issue_number": r["issue_number"],
                "finding_key": r["finding_key"],
                "rule": r["rule"],
                "file": r["file"],
                "devin_id": r["devin_id"],
                "session_id": r["session_id"],
                "tags": r["tags"],
                "status": r["status"],
                "status_detail": r["status_detail"],
                "outcome": r["outcome"],
                "pr_url": r["pr_url"],
                "acus_consumed": r["acus_consumed"],
                "elapsed_seconds": _elapsed(r),
            }
            for r in records
        ],
    }


def render_report(metrics: dict[str, Any]) -> str:
    o = metrics["outcomes"]
    cost = metrics["cost_per_fix_acus"]
    lines = [
        "# SonarQube auto-remediation run report",
        "",
        f"_Generated {metrics['generated_at']}_",
        "",
        "## Headline",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Issues addressed (sessions created) | {metrics['issues_addressed']} |",
        f"| Success rate | {metrics['success_rate_pct']}% |",
        f"| PRs opened | {metrics['prs_opened']} |",
        f"| Total ACUs consumed | {metrics['total_acus']} |",
        f"| **Cost per fix (ACU/PR)** | {cost if cost is not None else 'n/a'} |",
        f"| Duplicate webhooks/findings ignored | {metrics['dedup_skips']} |",
        f"| ...of which GitHub webhook redeliveries | {metrics['redeliveries_ignored']} |",
        f"| Sessions reused via 409 (idempotent create) | {metrics['sessions_reused_409']} |",
        "",
        "## Outcome breakdown",
        "",
        "| Outcome | Count |",
        "| --- | --- |",
        f"| succeeded | {o.get('succeeded', 0)} |",
        f"| declined (judgment: false positive) | {o.get('declined', 0)} |",
        f"| failed | {o.get('failed', 0)} |",
        f"| blocked_on_budget | {o.get('blocked_on_budget', 0)} |",
        f"| timed_out | {o.get('timed_out', 0)} |",
        f"| errored (orchestrator-side) | {o.get('errored', 0)} |",
        f"| in_progress | {o.get('in_progress', 0)} |",
        "",
        "## Pull requests",
        "",
    ]
    lines += [f"- {url}" for url in metrics["pr_urls"]] or ["_none yet_"]
    lines += [
        "",
        "## Throughput",
        "",
        f"- Mean time to terminal: {metrics['elapsed_seconds']['mean']}s",
        f"- Slowest issue: {metrics['elapsed_seconds']['max']}s",
        "",
        "## Per-issue detail",
        "",
        "| Issue | Rule | File | Session | Outcome | PR | ACUs | Elapsed (s) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in metrics["records"]:
        lines.append(
            f"| #{r['issue_number']} | {r['rule']} | `{r['file']}` | {r['devin_id']} |"
            f" {r['outcome']} | {r['pr_url'] or '-'} | {r['acus_consumed']} |"
            f" {r['elapsed_seconds'] if r['elapsed_seconds'] is not None else '-'} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_report(store: Store, path: str) -> str:
    metrics = build_metrics(store)
    content = render_report(metrics)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return content
