"""Parsing of SonarQube finding metadata out of a GitHub issue body."""

from __future__ import annotations

import re
from dataclasses import dataclass

_FIELD_PATTERNS = {
    "rule": r"rule",
    "file": r"file",
    "lines": r"line\(s\)|lines|line",
    "message": r"sonarqube message|message",
    "issue_key": r"sonarqube issue key|issue key",
}


_EMPHASIS = re.compile(r"\*\*|__")


def _normalize(body: str) -> str:
    """Drop markdown emphasis so ``**Rule:**`` and ``Rule:`` parse identically."""
    return _EMPHASIS.sub("", body or "")


def _extract(body: str, pattern: str) -> str:
    regex = re.compile(
        rf"^\s*[-*>#\s]*(?:{pattern})\s*:\s*(.+?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    match = regex.search(body)
    if not match:
        return ""
    return match.group(1).strip().strip("`").strip()


@dataclass(frozen=True)
class Finding:
    rule: str
    file: str
    lines: str
    message: str
    issue_key: str

    @property
    def finding_key(self) -> str:
        """Stable identity of the underlying defect, independent of GitHub issue number."""
        if self.issue_key:
            return f"sonar:{self.issue_key}"
        return f"filerule:{self.file}::{self.rule}"

    @property
    def has_sonar_key(self) -> bool:
        return bool(self.issue_key)


def parse_finding(body: str) -> Finding:
    normalized = _normalize(body)
    return Finding(
        rule=_extract(normalized, _FIELD_PATTERNS["rule"]),
        file=_extract(normalized, _FIELD_PATTERNS["file"]),
        lines=_extract(normalized, _FIELD_PATTERNS["lines"]),
        message=_extract(normalized, _FIELD_PATTERNS["message"]),
        issue_key=_extract(normalized, _FIELD_PATTERNS["issue_key"]),
    )


PROMPT_TEMPLATE = """You are fixing a SonarQube finding in the repository {repo} (a fork of apache/superset).

Rule: {rule}
File: {file}
Line(s): {lines}
SonarQube message: {message}
SonarQube issue key: {issue_key}

Task:
1. Read the file and the surrounding context. Determine whether this is a genuine defect or a false positive / intentional suppression.
2. If it is a genuine defect, apply the minimal, correct fix that resolves the rule without changing intended behavior. Follow existing patterns in the repo (e.g. for reduce-guarding, mirror the sibling component). Run/inspect relevant lint or type checks for the changed file if practical.
3. If it is NOT a genuine defect (false positive or intentional), do NOT change the code.
4. If (and only if) you made a change, open a pull request against {repo} with a clear title referencing the SonarQube rule and issue #{issue_number}, and a description of the fix and why it is safe.
5. Before ending your turn, call provide_structured_output with is_final=true containing: fixed (bool), rule (string), pr_url (string, empty if none), summary (string), reason (string - required when fixed=false, explaining why you declined).
"""


def build_prompt(finding: Finding, issue_number: int, repo: str) -> str:
    return PROMPT_TEMPLATE.format(
        repo=repo,
        rule=finding.rule or "unknown",
        file=finding.file or "unknown",
        lines=finding.lines or "unknown",
        message=finding.message or "(no message provided)",
        issue_key=finding.issue_key or "(none provided)",
        issue_number=issue_number,
    )
