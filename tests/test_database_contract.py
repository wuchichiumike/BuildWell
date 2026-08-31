"""Database seed/reset contracts from the AI implementation specification."""

from __future__ import annotations

import sqlite3

import pytest

from backend.db import Database
from backend.service import BusinessService


def test_seed_demo_is_idempotent() -> None:
    db = Database()
    try:
        first = db.seed_demo()
        second = db.seed_demo()
        assert first["project_code"] == second["project_code"] == "P001"
        assert first["materials"] == second["materials"] == 20
        assert db.query_one("SELECT COUNT(*) AS count FROM projects WHERE code='P001'")["count"] == 1
        assert db.query_one("SELECT COUNT(*) AS count FROM materials WHERE project_id=(SELECT id FROM projects WHERE code='P001')")["count"] == 20
        assert db.query_one("SELECT COUNT(*) AS count FROM consumption_snapshots")["count"] >= 14
        assert db.query_one("SELECT COUNT(*) AS count FROM purchase_orders WHERE project_id=(SELECT id FROM projects WHERE code='P001')")["count"] >= 1
        assert db.query_one("SELECT COUNT(*) AS count FROM evidence_events WHERE project_id=(SELECT id FROM projects WHERE code='P001')")["count"] >= 4
        assert db.query_one("SELECT COUNT(*) AS count FROM evidence_events WHERE status='SUBMITTED'")["count"] >= 1
        assert db.query_one("SELECT COUNT(*) AS count FROM evidence_events WHERE status='FAILED'")["count"] >= 1
    finally:
        db.close()


def test_reset_demo_requires_explicit_demo_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database()
    try:
        db.seed_demo()
        monkeypatch.delenv("DEMO_MODE", raising=False)
        with pytest.raises(RuntimeError, match="DEMO_MODE=true"):
            db.reset_demo()
        assert db.query_one("SELECT COUNT(*) AS count FROM projects WHERE code='P001'")["count"] == 1

        monkeypatch.setenv("DEMO_MODE", "true")
        db.reset_demo()
        assert db.query_one("SELECT COUNT(*) AS count FROM projects WHERE code='P001'")["count"] == 0
    finally:
        db.close()


def test_reset_demo_clears_only_p001_replay_records(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database()
    try:
        db.seed_demo()
        service = BusinessService(db)
        project_id = str(db.query_one("SELECT id FROM projects WHERE code='P001'")["id"])
        event_id = str(db.query_one("SELECT id FROM evidence_events LIMIT 1")["id"])
        service.issue_confirmation_token("reset-test", event_id, {}, "reset-user")
        service.issue_confirmation_token("reset-test", "other-entity", {}, "reset-user")
        db.execute(
            "INSERT INTO idempotencies(scope,idempotency_key,payload_hash,result_json,created_at) VALUES(?,?,?,?,?)",
            ("reset-test", "p001-key", "p001-hash", '{"project_id":"' + project_id + '"}', "now"),
        )
        db.execute(
            "INSERT INTO idempotencies(scope,idempotency_key,payload_hash,result_json,created_at) VALUES(?,?,?,?,?)",
            ("reset-test", "other-key", "other-hash", '{"project_id":"other-project"}', "now"),
        )

        monkeypatch.setenv("DEMO_MODE", "true")
        db.reset_demo()

        assert db.query_one("SELECT COUNT(*) AS count FROM idempotencies WHERE idempotency_key='p001-key'")["count"] == 0
        assert db.query_one("SELECT COUNT(*) AS count FROM idempotencies WHERE idempotency_key='other-key'")["count"] == 1
        assert db.query_one("SELECT COUNT(*) AS count FROM confirmation_tokens WHERE entity_id=?", (event_id,))["count"] == 0
        assert db.query_one("SELECT COUNT(*) AS count FROM confirmation_tokens WHERE entity_id='other-entity'")["count"] == 1
    finally:
        db.close()


def test_database_migrates_legacy_drafts_with_purchase_request_link(tmp_path) -> None:
    """Persistent demo files gain the draft linkage without a reset."""

    path = tmp_path / "legacy-demo.sqlite3"
    legacy = sqlite3.connect(path)
    try:
        legacy.execute(
            """
            CREATE TABLE drafts (
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL, draft_type TEXT NOT NULL,
              payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, source_ai_run_id TEXT,
              status TEXT NOT NULL, expires_at TEXT NOT NULL, created_by TEXT NOT NULL,
              confirmed_by TEXT, confirmed_at TEXT, rejection_reason TEXT,
              version INTEGER NOT NULL DEFAULT 1, idempotency_key TEXT NOT NULL UNIQUE,
              confirmation_token_hash TEXT
            )
            """
        )
        legacy.commit()
    finally:
        legacy.close()

    db = Database(path)
    try:
        columns = {row["name"] for row in db.query_all("PRAGMA table_info(drafts)")}
        assert "purchase_request_id" in columns
        assert db.query_one(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_drafts_purchase_request'"
        )
    finally:
        db.close()


def test_reset_demo_deletes_draft_link_before_purchase_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new nullable FK cannot make a P001 demo reset fail."""

    db = Database()
    try:
        db.seed_demo()
        project = db.query_one("SELECT id FROM projects WHERE code='P001'")
        request = db.query_one("SELECT id FROM purchase_requests WHERE project_id=? LIMIT 1", (project["id"],))
        owner = db.query_one("SELECT id FROM users WHERE role='PROJECT_OWNER' LIMIT 1")
        assert project and request and owner
        db.execute(
            "INSERT INTO drafts(id,project_id,draft_type,payload_json,payload_hash,status,expires_at,created_by,"
            "purchase_request_id,version,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "reset-linked-draft",
                project["id"],
                "MATERIAL_REQUEST",
                "{}",
                "reset-linked-draft-hash",
                "EXECUTED",
                "2099-01-01T00:00:00Z",
                owner["id"],
                request["id"],
                2,
                "reset-linked-draft-key",
            ),
        )

        monkeypatch.setenv("DEMO_MODE", "true")
        db.reset_demo()

        assert db.query_one("SELECT COUNT(*) AS count FROM projects WHERE code='P001'")["count"] == 0
    finally:
        db.close()
