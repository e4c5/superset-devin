# superset-devin — event-driven SonarQube remediation on the Devin API v3

A small orchestrator that turns a SonarQube finding filed as a GitHub issue into
a Devin session that fixes it — or reasons about it and declines — and then
reports the business numbers: success rate, PRs opened, ACUs burned, cost per
fix.

```
GitHub issue (label: devin-fix)
        │  issues webhook  (opened | labeled)
        ▼
   localtunnel  ──►  POST /webhook   (HMAC verified, 200 in <50ms)
                          │ background task
                          ▼
                    dedup (atomic, SQLite)
                     │            └── duplicate ─► comment on issue + skip counter
                     ▼
        POST /v3/organizations/{org}/sessions   (devin_id=devin-s6440-issue-<n>)
                     │            └── 409 ─► fetch existing session, continue
                     ▼
                  poller  ──►  GET .../sessions/{devin_id}  every POLL_INTERVAL_SECONDS
                     │
                     ▼
   succeeded / declined / failed / blocked_on_budget / timed_out
                     │
                     ▼
        structured logs + GET /status + report.md
```

There is **no batch scan step**. The only way work starts is an `issues`
webhook, so the demo is driven entirely by filing tickets on camera.

## What is where

| Path | Purpose |
| --- | --- |
| `app/webhook.py` | FastAPI service: `POST /webhook`, `GET /status`, `GET /report`, `GET /healthz` |
| `app/orchestrator.py` | Label gate, dedup claim, prompt build, session create |
| `app/findings.py` | Parses the issue body into a `Finding`; builds the per-issue prompt |
| `app/devin_client.py` | Devin v3 client: backoff on 429/5xx, 409-as-idempotent, 401/403/422 handling |
| `app/store.py` | SQLite state + **atomic** finding-level dedup |
| `app/poller.py` | Non-terminal session polling, outcome classification, timeout, restart rehydration |
| `app/metrics.py` | `/status` payload and `report.md` |
| `backlog/sonar_S6440_issues.md` | The 5 demo findings, ready to file |
| `playbooks/create-sonar-issue.md` | Playbook that opens one ticket per finding |
| `simulator/`, `scripts/simulate.py` | Offline end-to-end run — no GitHub, no Devin, no tokens |

## Run it for real

### 1. Configure

```bash
cp .env.example .env
$EDITOR .env      # DEVIN_SERVICE_USER_TOKEN, GITHUB_WEBHOOK_SECRET, GITHUB_TOKEN
```

The Devin service-user token (`cog_…`) needs **`UseDevinSessions`** to create
sessions and **`ViewOrgSessions`** to poll them. Missing either shows up as a
loud, non-retryable `401`/`403` that marks the issue `errored` and comments on
it — it never silently stalls.

### 2. Start the service

```bash
docker compose up --build      # serves on :8080, state in the orchestrator-data volume
```

Or without Docker:

```bash
make install
make run                       # uvicorn --env-file .env, port 8080
curl -s localhost:8080/healthz
```

### 3. Expose it with a tunnel

```bash
npm install -g localtunnel
lt --port 8080 --subdomain superset-devin
# → https://superset-devin.loca.lt
```

> **loca.lt can silently break the demo.** Its anti-abuse layer sometimes answers
> a first request from a new client IP with an HTML interstitial instead of
> proxying it. GitHub does not click through it: the delivery is recorded as a
> non-2xx response and the session is never created, with nothing wrong in our
> logs because the request never reached us. Check *Recent Deliveries* on the
> webhook if an issue produces no `webhook.received` line. For anything you care
> about, prefer a tunnel with no interstitial:
>
> ```bash
> cloudflared tunnel --url http://localhost:8080     # free, no account needed
> ngrok http 8080                                    # paid tier for a stable domain
> ```

### 4. Point GitHub at it

You need **admin on `e4c5/superset`** for this step (a PAT needs `admin:repo_hook`
as well as `repo`) — webhooks can only be created by a repo admin. Without it,
ask an admin to add the webhook; everything else here works with plain push
access.

On `e4c5/superset` → Settings → Webhooks → Add webhook:

| Field | Value |
| --- | --- |
| Payload URL | `https://superset-devin.loca.lt/webhook` |
| Content type | `application/json` |
| Secret | the same string as `GITHUB_WEBHOOK_SECRET` |
| Events | *Let me select individual events* → **Issues** only |

Then create the `devin-fix` label on the repo. Issues without that label are
acknowledged with 200 and ignored — the gate is enforced server-side, not by
the webhook config.

### 5. File tickets, one at a time

Use [`playbooks/create-sonar-issue.md`](playbooks/create-sonar-issue.md) with
the findings in [`backlog/sonar_S6440_issues.md`](backlog/sonar_S6440_issues.md)
(that file also has the suggested on-camera order, including the duplicate
ticket that demonstrates dedup).

### 6. Watch

```bash
docker compose logs -f orchestrator   # structured JSON-lines lifecycle events
curl -s localhost:8080/status | jq    # live rollup
curl -s localhost:8080/report         # the same as markdown
docker compose exec orchestrator cat /srv/data/report.md   # rewritten on every terminal outcome
```

### Dry run: real webhooks, no ACUs

`DRY_RUN=true` runs the whole live path — signature check, label gate, body
parse, dedup claim, prompt build — and stops just before the Devin API call,
logging `session.dry_run` with the prompt it *would* have sent. Only
`GITHUB_WEBHOOK_SECRET` is needed; the Devin and GitHub tokens can be left
empty. Dry-run records carry no session id, so they never reach the poller and
never land in the metrics. Use it to prove your tunnel and webhook config are
right before spending a single ACU.

## Simulate the workflow (no tokens, no network)

The simulation runs the **real** webhook app, orchestrator, store, client and
poller against a fake Devin API and a fake GitHub, and deliberately exercises
every resilience path:

```bash
make install
make simulate            # or: .venv/bin/python -m scripts.simulate
```

It replays the five demo findings plus:

- **Scenario A** — GitHub redelivers issue #101's webhook → no second session.
- **Scenario B** — issue #106 repeats #101's SonarQube key → skipped, commented, counted.
- a **cold replica** whose empty store forces a create against an already-existing
  `devin_id` → the API returns **409** and the client reuses the session.
- an **unlabeled** issue and a **closed** action → both ignored.
- a **429** on create → retried with backoff.
- a **503** on a poll → session stays `in_progress`, retried next tick (never mis-marked failed).
- a session stopped by the ACU cap → `blocked_on_budget`, not `failed`.
- the ECharts false positive → `declined` with a reason, no PR.

Expected tail of the run:

| Metric | Value |
| --- | --- |
| Issues addressed | 5 |
| succeeded / declined / blocked_on_budget | 3 / 1 / 1 |
| failed / timed_out | 0 / 0 |
| Success rate | 60% |
| PRs opened | 3 |
| Total ACUs | 18.5 |
| **Cost per fix** | 6.17 ACU/PR |
| Duplicates ignored | 2 (1 redelivery, 1 same finding) |
| Sessions reused via 409 | 1 |

## Tests

```bash
make test    # 30 tests
make lint
```

They cover HMAC rejection, the label/action/event gate, markdown parsing and the
`(file, rule)` fallback, concurrent claims of the same finding, 409 reuse, 429
retry, 5xx exhaustion, non-retryable 401/403/422, the create-request contract,
every outcome bucket, transient-poll-failure-is-not-failure, timeout, restart
rehydration, and the metrics rollup.

## Design notes

**Two kinds of duplicate, two defences.** A redelivered webhook is caught by the
deterministic `devin_id` (`devin-s6440-issue-<n>`) — even if our store were
wiped, the Devin API answers `409` and we adopt the existing session. The same
defect filed as two issues is caught by the SonarQube issue key (falling back to
`(file, rule)`), which is a `PRIMARY KEY` claimed under `BEGIN IMMEDIATE`, so two
simultaneous webhooks cannot both win the check-and-insert.

**Unreachable ≠ errored.** The poller only marks a session `failed` when the API
*reports* `status == "error"`. A network error or 5xx on the GET leaves the
session non-terminal and it is retried on the next tick; the per-session
`SESSION_MAX_WAIT_SECONDS` deadline is what eventually resolves it as
`timed_out`.

**The pull request is the finish line.** With `TERMINAL_ON_PR=true` (the default)
a session is settled as `succeeded` as soon as it has a PR, and the ACUs are
snapshotted at that tick — a Devin session often keeps running in
`waiting_for_user` long after the deliverable exists, and metering that idle
time would distort cost-per-fix. Set `TERMINAL_ON_PR=false` to wait for the
session's own `exit`/`finished` instead.

**Dedup blocks a live fix, not a dead one.** A finding stays claimed only while
its fix is real. When the same finding is filed again, the orchestrator asks
GitHub about the previous attempt: if its pull request was closed without being
merged, or the earlier issue was closed with nothing merged, the defect is still
in the tree, so the claim is re-opened (guarded by the record's `updated_at`, so
two racing refiles cannot both take it) and a fresh session starts under the new
issue number. A merged PR, an open PR, or a session still running still wins the
dedup and the new issue just gets a pointer comment. If GitHub cannot be
reached, the finding stays deduped — a refile is cheap to repeat, a duplicate
session is not.

**A budget stop is not a defect.** `status == "suspended"` with an
out-of-credits/usage-limit detail, or hitting `MAX_ACU_LIMIT`, is its own
`blocked_on_budget` bucket, so cost caps never pollute the failure rate.

**Declining is a result.** A terminal session with `fixed == false` is
`declined`, not a failure — finding 5 is a genuine false positive and the
demo's point is that Devin says so instead of "fixing" it.

**Restart durability.** All state is in SQLite; on startup the poller rehydrates
every non-terminal record, so `docker compose restart` mid-demo loses nothing.

## Security

No secrets in the repo — `.env` is gitignored and `.env.example` holds
placeholders only. Tokens are read from the environment, sent as bearer headers,
and never logged. `X-Hub-Signature-256` is compared with `hmac.compare_digest`,
and an unsigned or wrongly-signed request is rejected with 401 before the body
is parsed. A correctly-signed payload is still dropped unless
`repository.full_name` is `TARGET_REPO`, so a secret shared with another repo's
webhook cannot aim sessions at the fork. `/status` and `/report` expose finding
paths, PR links and costs; set `STATUS_TOKEN` to require
`Authorization: Bearer <token>` on them whenever the tunnel URL is public.
