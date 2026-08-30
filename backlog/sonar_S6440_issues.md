# Demo backlog — SonarQube react-hooks findings on `e4c5/superset`

Five real findings from the SonarQube scan of the Superset frontend. During the
demo they are filed **one at a time, on camera**, using the playbook in
[`playbooks/create-sonar-issue.md`](../playbooks/create-sonar-issue.md) — never
by a batch script. Each `issues` webhook is what drives the automation.

Every ticket must carry the `devin-fix` label (the orchestrator's gate) and the
five body fields below. **`SonarQube issue key` is mandatory** — it is the
finding-level dedup key that lets the orchestrator recognise the same defect
filed under two different issue numbers.

Body template:

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

---

## 1. `DataTable.tsx` — hooks after early return (HERO: real crash)

- **Title:** SonarQube typescript:S6440 - React hooks called after early return in DataTable.tsx
- **Rule:** `typescript:S6440`
- **File:** `superset-frontend/plugins/plugin-chart-table/src/DataTable/DataTable.tsx`
- **Line(s):** 476,477,484,485,525
- **SonarQube message:** React Hook 'useRef'/'useEffect' is called conditionally: hooks at lines 476,477,484,485,525 run after the early return at line 344.
- **SonarQube issue key:** `AZk1c0f4-0001-4a11-9d10-datatable6440`
- **Expected fix:** move the `isMountedRef` / `rafRef` / `lastSigRef` declarations and their effects above the early return at line 344 so every render evaluates the same hook sequence.
- **Expected outcome:** `fixed: true`, PR opened.

## 2. `TimeoutErrorMessage.tsx` — unguarded `reduce()`

- **Title:** SonarQube typescript:S6959 - unguarded reduce() in TimeoutErrorMessage.tsx
- **Rule:** `typescript:S6959`
- **File:** `superset-frontend/src/components/ErrorMessage/TimeoutErrorMessage.tsx`
- **Line(s):** 67
- **SonarQube message:** Array.prototype.reduce() should have an initial value, or the array must be guaranteed non-empty; this call throws on an empty array.
- **SonarQube issue key:** `AZk1c0f4-0002-4a11-9d10-timeouterr6959`
- **Expected fix:** add an `extra.issue_codes.length > 0 &&` guard, mirroring the sibling `ParameterErrorMessage.tsx:110`, or pass an initial value to `reduce()`.
- **Expected outcome:** `fixed: true`, PR opened.

## 3. `AsyncAceEditor/index.tsx` — `useTheme()` in a lowercase function

- **Title:** SonarQube typescript:S6440 - useTheme() called inside non-component function in AsyncAceEditor
- **Rule:** `typescript:S6440`
- **File:** `superset-frontend/packages/superset-ui-core/src/components/AsyncAceEditor/index.tsx`
- **Line(s):** 592
- **SonarQube message:** React Hook 'useTheme' is called in function 'placeholder' that is neither a React function component nor a custom React Hook function.
- **SonarQube issue key:** `AZk1c0f4-0003-4a11-9d10-asyncace6440`
- **Expected fix:** rename `placeholder` to `Placeholder` (capitalised, so it is a component) and render it as `<Placeholder />`.
- **Expected outcome:** `fixed: true`, PR opened.

## 4. `TaskList/index.tsx` — 16 hooks after a feature-flag early return

- **Title:** SonarQube typescript:S6440 - 16 hooks after feature-flag early return in TaskList
- **Rule:** `typescript:S6440`
- **File:** `superset-frontend/src/pages/TaskList/index.tsx`
- **Line(s):** 91
- **SonarQube message:** React Hooks are called conditionally: 16 hooks execute only when the feature-flag early return at line 91 is not taken.
- **SonarQube issue key:** `AZk1c0f4-0004-4a11-9d10-tasklist6440`
- **Expected fix:** move the feature-flag early return below all hook calls, or extract the body into an inner `<TaskListContent />` component rendered behind the flag.
- **Expected outcome:** `fixed: true`, PR opened. This is the largest of the five, so it is also the most likely to hit the `MAX_ACU_LIMIT=10` cap — in which case the run reports `blocked_on_budget`, a cost-cap stop, not a failure.

## 5. `EchartsTimeseries.tsx` — `this` in `ondrag` (FALSE POSITIVE, judgment case)

- **Title:** SonarQube typescript:S6757 - 'this' used in ondrag callback in EchartsTimeseries.tsx
- **Rule:** `typescript:S6757`
- **File:** `superset-frontend/plugins/plugin-chart-echarts/src/Timeseries/EchartsTimeseries.tsx`
- **Line(s):** 211,214
- **SonarQube message:** 'this' should not be used in a function passed as a prop; prefer an arrow function or bind explicitly.
- **SonarQube issue key:** `AZk1c0f4-0005-4a11-9d10-echartsts6757`
- **Expected fix:** none. The typed `this` parameter is the documented contract for an ECharts graphic-element `ondrag` callback.
- **Expected outcome:** `fixed: false`, no PR, `reason: "typed \`this\` param is valid for ECharts ondrag callback; not a defect"`.

---

## 6 (optional, on camera). Duplicate of #1 — dedup demo

File a **second** ticket for finding 1, with the **same** `SonarQube issue key`
(`AZk1c0f4-0001-4a11-9d10-datatable6440`) and a different title, e.g.
"DataTable crashes on filter change". The orchestrator must:

- create **no** new Devin session,
- comment on the new issue linking to the existing session/PR,
- increment the dedup/skip counter in `/status` and `report.md`.

## Suggested demo order

1. #1 DataTable (hero) — watch the session open and the PR land.
2. #5 EchartsTimeseries — while #1 runs, show the decline/judgment path.
3. #2 TimeoutErrorMessage and #3 AsyncAceEditor — throughput.
4. Duplicate of #1 — dedup counter increments, no second session.
5. #4 TaskList — largest scope; shows the ACU cap / `blocked_on_budget` bucket.
6. `GET /status` and `report.md` — outcome breakdown, success rate, cost per fix.
