# Playbook — File a SonarQube finding as a `devin-fix` issue

**When to use:** the presenter wants to open one ticket on `e4c5/superset` for a
single SonarQube finding, so that the orchestrator's webhook fires. One
invocation = one issue. Never batch-create; the demo depends on the tickets
arriving one at a time.

**Do not** clone the repo, scan the code, or fix anything in this playbook. Its
only job is to create a well-formed GitHub issue.

## Inputs

| Input | Required | Notes |
| --- | --- | --- |
| `rule` | yes | e.g. `typescript:S6440` |
| `file` | yes | repo-relative path |
| `lines` | yes | comma-separated line numbers |
| `message` | yes | the SonarQube message, verbatim |
| `issue_key` | yes | the SonarQube issue UUID — **the dedup key** |
| `remediation` | no | one-paragraph suggestion |
| `title` | no | defaults to `SonarQube <rule> in <basename(file)>` |

If any required input is missing, ask for it — do not invent a value. In
particular, never fabricate an `issue_key`: without it the orchestrator falls
back to `(file, rule)` dedup, which is weaker.

The five demo findings are recorded in
[`backlog/sonar_S6440_issues.md`](../backlog/sonar_S6440_issues.md); take the
inputs from there.

## Steps

1. Confirm the `devin-fix` label exists on `e4c5/superset`. If it does not,
   create it (colour `#0e8a16`, description "Auto-remediated by Devin").
2. Check the repo's open issues for the same `issue_key`. If one already exists,
   stop and report it — unless the presenter explicitly asked for the duplicate
   ticket that demonstrates finding-level dedup.
3. Create the issue on `e4c5/superset` with the `devin-fix` label and exactly
   this body:

   ```markdown
   ### SonarQube finding

   **Rule:** {rule}
   **File:** `{file}`
   **Line(s):** {lines}
   **SonarQube message:** {message}
   **SonarQube issue key:** `{issue_key}`

   ### Suggested remediation

   {remediation}
   ```

4. Report back the issue number and URL.

## Rules

- The five bold field labels must appear verbatim — the orchestrator parses them
  (markdown emphasis is stripped, but the label names and the colon matter).
- The `devin-fix` label must be present at creation time. Applying it later also
  works (the orchestrator handles `action: labeled`), but on-camera it is
  cleaner to have it from the start.
- Do not open a pull request, and do not modify any file in `e4c5/superset`.
  That fork stays vanilla except for the PRs Devin's own sessions open.

## Verification

- The issue is visible at `https://github.com/e4c5/superset/issues/<n>` and
  carries the `devin-fix` label.
- Within ~2 s the orchestrator logs `webhook.received` → `dedup.claimed` →
  `session.created` (or `dedup.skipped` for the duplicate ticket).
