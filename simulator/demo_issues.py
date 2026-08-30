"""The five demo SonarQube findings, as GitHub ``issues`` webhook payloads."""

from __future__ import annotations

from typing import Any

ISSUE_BODY_TEMPLATE = """### SonarQube finding

**Rule:** {rule}
**File:** `{file}`
**Line(s):** {lines}
**SonarQube message:** {message}
**SonarQube issue key:** `{issue_key}`

### Suggested remediation

{remediation}
"""

DEMO_FINDINGS: list[dict[str, str]] = [
    {
        "number": "101",
        "title": "SonarQube typescript:S6440 - React hooks called after early return in DataTable.tsx",
        "rule": "typescript:S6440",
        "file": "superset-frontend/plugins/plugin-chart-table/src/DataTable/DataTable.tsx",
        "lines": "476,477,484,485,525",
        "message": (
            "React Hook 'useRef'/'useEffect' is called conditionally: hooks at lines "
            "476,477,484,485,525 run after the early return at line 344."
        ),
        "issue_key": "AZk1c0f4-0001-4a11-9d10-datatable6440",
        "remediation": (
            "Move the `isMountedRef` / `rafRef` / `lastSigRef` declarations and their "
            "effects above the early return at line 344 so every render evaluates the "
            "same hook sequence."
        ),
    },
    {
        "number": "102",
        "title": "SonarQube typescript:S6959 - unguarded reduce() in TimeoutErrorMessage.tsx",
        "rule": "typescript:S6959",
        "file": "superset-frontend/src/components/ErrorMessage/TimeoutErrorMessage.tsx",
        "lines": "67",
        "message": (
            "Array.prototype.reduce() should have an initial value, or the array must be "
            "guaranteed non-empty; this call throws on an empty array."
        ),
        "issue_key": "AZk1c0f4-0002-4a11-9d10-timeouterr6959",
        "remediation": (
            "Add an `extra.issue_codes.length > 0 &&` guard, mirroring the sibling "
            "`ParameterErrorMessage.tsx:110`, or pass an initial value to `reduce()`."
        ),
    },
    {
        "number": "103",
        "title": "SonarQube typescript:S6440 - useTheme() called inside non-component function in AsyncAceEditor",
        "rule": "typescript:S6440",
        "file": "superset-frontend/packages/superset-ui-core/src/components/AsyncAceEditor/index.tsx",
        "lines": "592",
        "message": (
            "React Hook 'useTheme' is called in function 'placeholder' that is neither a "
            "React function component nor a custom React Hook function."
        ),
        "issue_key": "AZk1c0f4-0003-4a11-9d10-asyncace6440",
        "remediation": (
            "Rename `placeholder` to `Placeholder` (capitalised, so it is a component) and "
            "render it as `<Placeholder />`."
        ),
    },
    {
        "number": "104",
        "title": "SonarQube typescript:S6440 - 16 hooks after feature-flag early return in TaskList",
        "rule": "typescript:S6440",
        "file": "superset-frontend/src/pages/TaskList/index.tsx",
        "lines": "91",
        "message": (
            "React Hooks are called conditionally: 16 hooks execute only when the "
            "feature-flag early return at line 91 is not taken."
        ),
        "issue_key": "AZk1c0f4-0004-4a11-9d10-tasklist6440",
        "remediation": (
            "Move the feature-flag early return below all hook calls, or extract the body "
            "into an inner `<TaskListContent />` component rendered behind the flag."
        ),
    },
    {
        "number": "105",
        "title": "SonarQube typescript:S6757 - 'this' used in ondrag callback in EchartsTimeseries.tsx",
        "rule": "typescript:S6757",
        "file": "superset-frontend/plugins/plugin-chart-echarts/src/Timeseries/EchartsTimeseries.tsx",
        "lines": "211,214",
        "message": (
            "'this' should not be used in a function passed as a prop; prefer an arrow "
            "function or bind explicitly."
        ),
        "issue_key": "AZk1c0f4-0005-4a11-9d10-echartsts6757",
        "remediation": (
            "Expected outcome: DECLINE. The typed `this` parameter is the documented "
            "contract for an ECharts graphic-element `ondrag` callback, so this is a "
            "false positive and the code must not change."
        ),
    },
]


def issue_body(finding: dict[str, str]) -> str:
    return ISSUE_BODY_TEMPLATE.format(**finding)


def issue_payload(
    finding: dict[str, str],
    *,
    action: str = "opened",
    number: int | None = None,
    label: str = "devin-fix",
    repo: str = "e4c5/superset",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "repository": {"full_name": repo},
        "issue": {
            "number": int(number if number is not None else finding["number"]),
            "title": finding["title"],
            "body": issue_body(finding),
            "labels": [{"name": label}],
            "html_url": f"https://github.com/{repo}/issues/{number or finding['number']}",
        },
    }
    if action == "labeled":
        payload["label"] = {"name": label}
    return payload


def pull_request_payload(
    issue_number: int,
    *,
    number: int = 900,
    action: str = "opened",
    repo: str = "e4c5/superset",
    rule: str = "typescript:S6440",
) -> dict[str, Any]:
    """A ``pull_request`` webhook for the PR Devin opens against an issue."""
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {
            "number": number,
            "title": f"fix({rule}): resolve SonarQube finding from issue #{issue_number}",
            "body": f"Fixes the finding reported in issue #{issue_number}.",
            "html_url": f"https://github.com/{repo}/pull/{number}",
        },
    }
