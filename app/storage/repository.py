"""SQLite persistence for handovers, their revisions and receiver decisions.

Revisions are append-only: saving changed content adds a revision, saving
identical content does not. Each revision stores a content hash that is checked
again on load.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from app.models import AcceptanceRecord, Decision, Handover

SCHEMA = """
CREATE TABLE IF NOT EXISTS handovers (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    handover_id TEXT NOT NULL REFERENCES handovers(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (handover_id, revision)
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_id TEXT NOT NULL REFERENCES handovers(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    receiver TEXT NOT NULL,
    decision TEXT NOT NULL,
    note TEXT NOT NULL,
    acknowledged_gap_ids TEXT NOT NULL,
    readiness_score INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


class StorageError(RuntimeError):
    """Persistence failed. The message is safe to show to the user."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repository:
    def __init__(self, db_path: Path | str):
        self.path = Path(db_path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"Cannot create the data directory: {exc}") from exc
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            conn = sqlite3.connect(self.path, timeout=5)
        except sqlite3.Error as exc:
            raise StorageError(f"Cannot open the local database: {exc}") from exc
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:  # commits on success, rolls back on error
                yield conn
        except sqlite3.Error as exc:
            raise StorageError(f"Local database error: {exc}") from exc
        finally:
            conn.close()

    # -- handovers and revisions -------------------------------------------

    def latest_revision(self, handover_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(revision) FROM revisions WHERE handover_id = ?", (handover_id,)
            ).fetchone()
        return row[0] or 0

    def save_handover(self, handover: Handover) -> int:
        """Persist the handover; returns its revision number.

        A new revision is created only when the content actually changed.
        """
        payload = json.dumps(handover.model_dump(mode="json"), sort_keys=True)
        digest = handover.content_hash()
        now = utc_now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT revision, content_hash FROM revisions WHERE handover_id = ? "
                "ORDER BY revision DESC LIMIT 1",
                (handover.id,),
            ).fetchone()
            if row and row[1] == digest:
                return row[0]
            if not row:
                conn.execute("INSERT INTO handovers (id, created_at) VALUES (?, ?)", (handover.id, now))
            revision = (row[0] if row else 0) + 1
            conn.execute(
                "INSERT INTO revisions (handover_id, revision, content_hash, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (handover.id, revision, digest, payload, now),
            )
        return revision

    def load_handover(self, handover_id: str, revision: int | None = None) -> tuple[Handover, int]:
        with self._connect() as conn:
            if revision is None:
                row = conn.execute(
                    "SELECT revision, content_hash, payload FROM revisions WHERE handover_id = ? "
                    "ORDER BY revision DESC LIMIT 1",
                    (handover_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT revision, content_hash, payload FROM revisions WHERE handover_id = ? AND revision = ?",
                    (handover_id, revision),
                ).fetchone()
        if not row:
            wanted = f" revision {revision}" if revision else ""
            raise StorageError(f"Handover '{handover_id}'{wanted} was not found.")
        try:
            handover = Handover.model_validate(json.loads(row[2]))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise StorageError(f"Stored record for '{handover_id}' revision {row[0]} is invalid.") from exc
        if handover.content_hash() != row[1]:
            raise StorageError(f"Integrity check failed for '{handover_id}' revision {row[0]}.")
        return handover, row[0]

    def list_handovers(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.handover_id, r.revision, r.payload, r.created_at FROM revisions r "
                "JOIN (SELECT handover_id, MAX(revision) AS revision FROM revisions GROUP BY handover_id) latest "
                "ON latest.handover_id = r.handover_id AND latest.revision = r.revision "
                "ORDER BY r.created_at DESC, r.handover_id"
            ).fetchall()
        result = []
        for handover_id, revision, payload, updated_at in rows:
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = {}
            result.append(
                {
                    "id": handover_id,
                    "title": data.get("title", ""),
                    "scenario": data.get("scenario", ""),
                    "revision": revision,
                    "updated_at": updated_at,
                }
            )
        return result

    def list_revisions(self, handover_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT revision, content_hash, created_at FROM revisions WHERE handover_id = ? ORDER BY revision",
                (handover_id,),
            ).fetchall()
        return [{"revision": r[0], "content_hash": r[1], "created_at": r[2]} for r in rows]

    def delete_handovers(self, handover_ids: list[str]) -> int:
        """Explicit deletion, used only by the demo reset for the fixture ids."""
        if not handover_ids:
            return 0
        marks = ",".join("?" for _ in handover_ids)
        with self._connect() as conn:
            conn.execute(f"DELETE FROM decisions WHERE handover_id IN ({marks})", handover_ids)
            conn.execute(f"DELETE FROM revisions WHERE handover_id IN ({marks})", handover_ids)
            cursor = conn.execute(f"DELETE FROM handovers WHERE id IN ({marks})", handover_ids)
        return cursor.rowcount

    # -- receiver decisions --------------------------------------------------

    def add_decision(self, record: AcceptanceRecord) -> AcceptanceRecord:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO decisions (handover_id, revision, receiver, decision, note, "
                "acknowledged_gap_ids, readiness_score, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.handover_id,
                    record.revision,
                    record.receiver,
                    record.decision.value,
                    record.note,
                    json.dumps(record.acknowledged_gap_ids),
                    record.readiness_score,
                    record.created_at,
                ),
            )
        return record.model_copy(update={"id": cursor.lastrowid})

    def list_decisions(self, handover_id: str) -> list[AcceptanceRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, handover_id, revision, receiver, decision, note, acknowledged_gap_ids, "
                "readiness_score, created_at FROM decisions WHERE handover_id = ? ORDER BY id",
                (handover_id,),
            ).fetchall()
        return [
            AcceptanceRecord(
                id=r[0],
                handover_id=r[1],
                revision=r[2],
                receiver=r[3],
                decision=Decision(r[4]),
                note=r[5],
                acknowledged_gap_ids=json.loads(r[6]),
                readiness_score=r[7],
                created_at=r[8],
            )
            for r in rows
        ]
