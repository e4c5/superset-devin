"""SQLite-backed state store.

The store is the dedup authority: ``finding_key`` carries a ``UNIQUE``
constraint and claims are taken inside a ``BEGIN IMMEDIATE`` transaction, so two
near-simultaneous webhooks for the same finding cannot both win.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

NON_TERMINAL_OUTCOME = "in_progress"
TERMINAL_OUTCOMES = {"succeeded", "declined", "failed", "blocked_on_budget", "timed_out", "errored"}
#: A finding whose previous attempt ended in one of these may be retried by a later issue.
RECLAIMABLE_OUTCOMES = {"failed", "timed_out", "errored"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    finding_key    TEXT PRIMARY KEY,
    issue_number   INTEGER NOT NULL,
    rule           TEXT,
    file           TEXT,
    lines          TEXT,
    message        TEXT,
    session_id     TEXT,
    devin_id       TEXT,
    tags           TEXT NOT NULL DEFAULT '[]',
    status         TEXT NOT NULL DEFAULT 'pending',
    status_detail  TEXT,
    pr_url         TEXT,
    acus_consumed  REAL NOT NULL DEFAULT 0,
    outcome        TEXT NOT NULL DEFAULT 'in_progress',
    structured_output TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    terminal_at    REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_issues_devin_id ON issues(devin_id)
    WHERE devin_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dedup_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_key   TEXT NOT NULL,
    issue_number  INTEGER NOT NULL,
    scenario      TEXT NOT NULL,
    existing_issue INTEGER,
    created_at    REAL NOT NULL
);
"""


@dataclass
class Claim:
    """Outcome of an atomic check-and-insert on a finding key."""

    acquired: bool
    record: dict[str, Any]
    scenario: str  # "new" | "duplicate_finding" | "reclaimed"


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["tags"] = json.loads(record.get("tags") or "[]")
    if record.get("structured_output"):
        try:
            record["structured_output"] = json.loads(record["structured_output"])
        except json.JSONDecodeError:
            record["structured_output"] = None
    return record


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._local = threading.local()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    # -- dedup -----------------------------------------------------------
    def claim_finding(
        self,
        finding_key: str,
        issue_number: int,
        *,
        rule: str = "",
        file: str = "",
        lines: str = "",
        message: str = "",
    ) -> Claim:
        """Atomically reserve ``finding_key`` for ``issue_number``.

        Returns ``acquired=False`` when another issue already owns a live or
        successful attempt at the same finding (dedup scenario B).
        """
        now = time.time()
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT * FROM issues WHERE finding_key = ?", (finding_key,)
            ).fetchone()
            if existing is not None:
                record = _row_to_dict(existing)
                if record["issue_number"] == issue_number:
                    # Scenario A: GitHub redelivered the same issue's webhook.
                    conn.execute(
                        "INSERT INTO dedup_events"
                        " (finding_key, issue_number, scenario, existing_issue, created_at)"
                        " VALUES (?, ?, 'redelivery', ?, ?)",
                        (finding_key, issue_number, issue_number, now),
                    )
                    self._bump(conn, "dedup_skips")
                    self._bump(conn, "redeliveries_ignored")
                    conn.execute("COMMIT")
                    return Claim(acquired=False, record=record, scenario="redelivery")
                if record["outcome"] not in RECLAIMABLE_OUTCOMES:
                    conn.execute(
                        "INSERT INTO dedup_events"
                        " (finding_key, issue_number, scenario, existing_issue, created_at)"
                        " VALUES (?, ?, 'duplicate_finding', ?, ?)",
                        (finding_key, issue_number, record["issue_number"], now),
                    )
                    self._bump(conn, "dedup_skips")
                    conn.execute("COMMIT")
                    return Claim(acquired=False, record=record, scenario="duplicate_finding")
                conn.execute(
                    "UPDATE issues SET issue_number = ?, status = 'pending', status_detail = NULL,"
                    " outcome = ?, session_id = NULL, devin_id = NULL, terminal_at = NULL,"
                    " updated_at = ? WHERE finding_key = ?",
                    (issue_number, NON_TERMINAL_OUTCOME, now, finding_key),
                )
                row = conn.execute(
                    "SELECT * FROM issues WHERE finding_key = ?", (finding_key,)
                ).fetchone()
                conn.execute("COMMIT")
                return Claim(acquired=True, record=_row_to_dict(row), scenario="reclaimed")

            conn.execute(
                "INSERT INTO issues (finding_key, issue_number, rule, file, lines, message,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (finding_key, issue_number, rule, file, lines, message, now, now),
            )
            row = conn.execute(
                "SELECT * FROM issues WHERE finding_key = ?", (finding_key,)
            ).fetchone()
            conn.execute("COMMIT")
            return Claim(acquired=True, record=_row_to_dict(row), scenario="new")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # -- counters --------------------------------------------------------
    @staticmethod
    def _bump(conn: sqlite3.Connection, name: str, amount: int = 1) -> None:
        conn.execute(
            "INSERT INTO counters (name, value) VALUES (?, ?)"
            " ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
            (name, amount),
        )

    def increment(self, name: str, amount: int = 1) -> None:
        self._bump(self._connect(), name, amount)

    def counters(self) -> dict[str, int]:
        rows = self._connect().execute("SELECT name, value FROM counters").fetchall()
        return {row["name"]: row["value"] for row in rows}

    # -- records ---------------------------------------------------------
    def update(self, finding_key: str, **fields: Any) -> None:
        if not fields:
            return
        if "tags" in fields and not isinstance(fields["tags"], str):
            fields["tags"] = json.dumps(list(fields["tags"]))
        if "structured_output" in fields and not isinstance(fields["structured_output"], (str, type(None))):
            fields["structured_output"] = json.dumps(fields["structured_output"])
        fields["updated_at"] = time.time()
        if fields.get("outcome") in TERMINAL_OUTCOMES:
            fields.setdefault("terminal_at", fields["updated_at"])
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self._connect().execute(
            f"UPDATE issues SET {assignments} WHERE finding_key = ?",
            (*fields.values(), finding_key),
        )

    def get(self, finding_key: str) -> dict[str, Any] | None:
        row = self._connect().execute(
            "SELECT * FROM issues WHERE finding_key = ?", (finding_key,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def get_by_issue(self, issue_number: int) -> dict[str, Any] | None:
        row = self._connect().execute(
            "SELECT * FROM issues WHERE issue_number = ? ORDER BY updated_at DESC LIMIT 1",
            (issue_number,),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def all_records(self) -> list[dict[str, Any]]:
        rows = self._connect().execute("SELECT * FROM issues ORDER BY created_at").fetchall()
        return [_row_to_dict(row) for row in rows]

    def non_terminal(self) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in TERMINAL_OUTCOMES)
        rows = self._connect().execute(
            f"SELECT * FROM issues WHERE outcome NOT IN ({placeholders})"
            " AND devin_id IS NOT NULL ORDER BY created_at",
            tuple(TERMINAL_OUTCOMES),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def dedup_events(self) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            "SELECT * FROM dedup_events ORDER BY created_at"
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def tags_for(issue_number: int, rule: str) -> Iterable[str]:
    return ["devin-fix", f"issue-{issue_number}", f"sonar-{rule}"]
