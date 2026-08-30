"""FastAPI service: GitHub webhook receiver + status/report endpoints.

The handler verifies ``X-Hub-Signature-256``, applies the ``devin-fix`` label
gate, and returns 200 immediately — all Devin work happens in a background task
so GitHub's own delivery retries never stack on top of our retry/backoff.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import Settings, get_settings
from .devin_client import DevinClient
from .github_client import GitHubClient
from .logging_setup import configure_logging, log_event
from .metrics import build_metrics, render_report, write_report
from .orchestrator import Orchestrator
from .poller import Poller
from .store import Store


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of GitHub's ``X-Hub-Signature-256`` header."""
    if not secret:
        return False
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def build_app(
    settings: Settings | None = None,
    *,
    devin_client: DevinClient | None = None,
    github_client: GitHubClient | None = None,
    store: Store | None = None,
    start_poller: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging()

    state_store = store or Store(settings.database_path)
    devin = devin_client or DevinClient(
        base_url=settings.devin_api_base,
        org_id=settings.devin_org_id,
        token=settings.devin_token,
        max_retries=settings.max_retries,
    )
    github = github_client or GitHubClient(
        token=settings.github_token,
        repo=settings.target_repo,
        base_url=settings.github_api_base,
    )
    poller = Poller(
        store=state_store,
        client=devin,
        interval_seconds=settings.poll_interval_seconds,
        max_wait_seconds=settings.session_max_wait_seconds,
        max_acu_limit=settings.max_acu_limit,
        on_terminal=lambda _record: write_report(state_store, settings.report_path),
    )
    orchestrator = Orchestrator(
        settings=settings, store=state_store, devin=devin, github=github, poller=poller
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log_event(
            "service.startup",
            target_repo=settings.target_repo,
            devin_api_base=settings.devin_api_base,
            org_id=settings.devin_org_id,
            poll_interval=settings.poll_interval_seconds,
            max_acu_limit=settings.max_acu_limit,
            resumed_sessions=len(state_store.non_terminal()),
        )
        if start_poller:
            # Rehydrates in-flight sessions from SQLite so a restart loses nothing.
            poller.start()
        yield
        await poller.stop()
        await devin.aclose()
        await github.aclose()

    app = FastAPI(title="superset-devin orchestrator", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = state_store
    app.state.orchestrator = orchestrator
    app.state.poller = poller

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    def authorized(header: str | None) -> bool:
        """Reports carry finding paths, PRs and costs; gate them when a token is set."""
        if not settings.status_token:
            return True
        return bool(header) and hmac.compare_digest(header, f"Bearer {settings.status_token}")

    @app.get("/status")
    async def status(authorization: str | None = Header(default=None)) -> JSONResponse:
        if not authorized(authorization):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return JSONResponse(build_metrics(state_store))

    @app.get("/report", response_class=PlainTextResponse)
    async def report(authorization: str | None = Header(default=None)) -> Response:
        if not authorized(authorization):
            return PlainTextResponse("unauthorized", status_code=401)
        return PlainTextResponse(render_report(build_metrics(state_store)))

    @app.post("/webhook")
    async def webhook(
        request: Request,
        background: BackgroundTasks,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
    ) -> Response:
        body = await request.body()
        if not verify_signature(settings.github_webhook_secret, body, x_hub_signature_256):
            log_event("webhook.rejected_signature", delivery=x_github_delivery)
            return JSONResponse({"status": "invalid signature"}, status_code=401)

        try:
            payload: dict[str, Any] = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"status": "invalid json"}, status_code=400)

        state_store.increment("webhooks_received")
        log_event(
            "webhook.received",
            delivery=x_github_delivery,
            github_event=x_github_event,
            action=payload.get("action"),
            issue=(payload.get("issue") or {}).get("number"),
        )

        if x_github_event != "issues":
            state_store.increment("webhooks_ignored")
            return JSONResponse({"status": "ignored", "reason": f"event={x_github_event}"})

        handle, reason = orchestrator.should_handle(payload)
        if not handle:
            state_store.increment("webhooks_ignored")
            log_event("webhook.ignored", reason=reason,
                      issue=(payload.get("issue") or {}).get("number"))
            return JSONResponse({"status": "ignored", "reason": reason})

        background.add_task(_dispatch, orchestrator, payload)
        return JSONResponse({"status": "accepted"}, status_code=200)

    return app


async def _dispatch(orchestrator: Orchestrator, payload: dict[str, Any]) -> None:
    try:
        await orchestrator.handle_issue_event(payload)
    except Exception as exc:  # noqa: BLE001 - background task must never crash the server
        log_event(
            "webhook.dispatch_error",
            issue=(payload.get("issue") or {}).get("number"),
            error=str(exc),
        )
    finally:
        orchestrator.write_report()


app = build_app()
