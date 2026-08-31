"""Small transactional repository used by the demo and contract tests.

The production deployment can replace this repository with SQLAlchemy/Postgres
without changing :mod:`backend.service`.  SQLite is intentionally dependency
free, enables the demo to run with one command, and still exercises the same
transaction and uniqueness rules.
"""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .config import decimal_string, iso_utc, utc_now


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS organizations (
  id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  role_type TEXT NOT NULL CHECK (role_type IN ('PROJECT','SUPPLIER')), is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
  username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, role TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1, timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'
);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
  status TEXT NOT NULL DEFAULT 'ACTIVE', owner_org_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
  start_date TEXT, end_date TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_members (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT, role TEXT NOT NULL, scopes_json TEXT,
  PRIMARY KEY(project_id,user_id)
);
CREATE TABLE IF NOT EXISTS buildings (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  code TEXT NOT NULL, name TEXT NOT NULL, UNIQUE(project_id,code)
);
CREATE TABLE IF NOT EXISTS areas (
  id TEXT PRIMARY KEY, building_id TEXT NOT NULL REFERENCES buildings(id) ON DELETE RESTRICT,
  code TEXT NOT NULL, name TEXT NOT NULL, stage_id TEXT, UNIQUE(building_id,code)
);
CREATE TABLE IF NOT EXISTS warehouses (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  code TEXT NOT NULL, name TEXT NOT NULL, area_id TEXT REFERENCES areas(id) ON DELETE RESTRICT,
  UNIQUE(project_id,code)
);
CREATE TABLE IF NOT EXISTS materials (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  sku TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL, specification TEXT,
  unit TEXT NOT NULL, safety_stock TEXT NOT NULL DEFAULT '0', reorder_point TEXT NOT NULL DEFAULT '0',
  lead_time_days INTEGER NOT NULL DEFAULT 0 CHECK(lead_time_days >= 0), min_order_qty TEXT NOT NULL DEFAULT '0',
  order_multiple TEXT NOT NULL DEFAULT '1', anomaly_min_qty TEXT NOT NULL DEFAULT '0',
  ppe_standard_json TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE', UNIQUE(project_id,sku)
);
CREATE TABLE IF NOT EXISTS material_aliases (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE RESTRICT, alias TEXT NOT NULL,
  UNIQUE(project_id,alias)
);
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY, material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE RESTRICT,
  batch_no TEXT NOT NULL, supplier_id TEXT, manufacture_date TEXT, expiry_date TEXT,
  quality_file_ids_json TEXT, status TEXT NOT NULL DEFAULT 'EXPECTED', UNIQUE(material_id,batch_no)
);
CREATE TABLE IF NOT EXISTS stock_balances (
  id TEXT PRIMARY KEY, warehouse_id TEXT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
  material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE RESTRICT, batch_id TEXT,
  batch_key TEXT NOT NULL DEFAULT '', qty_on_hand TEXT NOT NULL DEFAULT '0',
  qty_reserved TEXT NOT NULL DEFAULT '0', qty_quarantined TEXT NOT NULL DEFAULT '0', version INTEGER NOT NULL DEFAULT 1,
  UNIQUE(warehouse_id,material_id,batch_key), CHECK(CAST(qty_on_hand AS REAL) >= 0),
  CHECK(CAST(qty_reserved AS REAL) >= 0), CHECK(CAST(qty_quarantined AS REAL) >= 0)
);
CREATE TABLE IF NOT EXISTS stock_moves (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  move_no TEXT NOT NULL UNIQUE, move_type TEXT NOT NULL, material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE RESTRICT,
  batch_id TEXT, from_warehouse_id TEXT, to_warehouse_id TEXT, qty TEXT NOT NULL CHECK(CAST(qty AS REAL) > 0),
  unit TEXT NOT NULL, reason TEXT NOT NULL, reference_type TEXT, reference_id TEXT, actor_id TEXT,
  occurred_at TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, reversed_move_id TEXT, correlation_id TEXT
);
CREATE TABLE IF NOT EXISTS construction_stages (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  code TEXT NOT NULL, name TEXT NOT NULL, start_date TEXT, end_date TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE',
  UNIQUE(project_id,code)
);
CREATE TABLE IF NOT EXISTS material_plans (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  stage_id TEXT REFERENCES construction_stages(id) ON DELETE RESTRICT, area_id TEXT, material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE RESTRICT,
  plan_date TEXT NOT NULL, planned_qty TEXT NOT NULL CHECK(CAST(planned_qty AS REAL) >= 0), consumed_qty_cached TEXT NOT NULL DEFAULT '0',
  source TEXT NOT NULL DEFAULT 'MANUAL', version INTEGER NOT NULL DEFAULT 1, UNIQUE(area_id,material_id,plan_date)
);
CREATE TABLE IF NOT EXISTS consumption_snapshots (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE RESTRICT, area_id TEXT, date TEXT NOT NULL,
  issued_qty TEXT NOT NULL DEFAULT '0', returned_qty TEXT NOT NULL DEFAULT '0', scrapped_qty TEXT NOT NULL DEFAULT '0',
  net_consumed_qty TEXT NOT NULL DEFAULT '0', UNIQUE(material_id,area_id,date)
);
CREATE TABLE IF NOT EXISTS suppliers (
  id TEXT PRIMARY KEY, organization_id TEXT REFERENCES organizations(id) ON DELETE RESTRICT,
  code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, contact_json TEXT, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS purchase_requests (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  request_no TEXT NOT NULL UNIQUE, requester_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'DRAFT', needed_by TEXT,
  justification TEXT, total_estimated_amount TEXT, source_ai_run_id TEXT, version INTEGER NOT NULL DEFAULT 1,
  approved_by TEXT, approved_at TEXT, rejection_reason TEXT, idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS purchase_request_items (
  id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES purchase_requests(id) ON DELETE RESTRICT,
  material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE RESTRICT, specification_snapshot TEXT,
  unit TEXT NOT NULL, qty TEXT NOT NULL CHECK(CAST(qty AS REAL) > 0), warehouse_id TEXT NOT NULL, reason TEXT NOT NULL,
  shortage_calc_id TEXT
);
CREATE TABLE IF NOT EXISTS purchase_orders (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  po_no TEXT NOT NULL UNIQUE, supplier_id TEXT NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
  request_id TEXT REFERENCES purchase_requests(id) ON DELETE RESTRICT, status TEXT NOT NULL DEFAULT 'DRAFT',
  order_date TEXT NOT NULL, expected_date TEXT, currency TEXT NOT NULL DEFAULT 'CNY', version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS purchase_order_items (
  id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES purchase_orders(id) ON DELETE RESTRICT,
  material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE RESTRICT, qty_ordered TEXT NOT NULL CHECK(CAST(qty_ordered AS REAL) >= 0),
  qty_shipped TEXT NOT NULL DEFAULT '0', qty_received TEXT NOT NULL DEFAULT '0', unit_price TEXT, unit TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shipments (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  shipment_no TEXT NOT NULL UNIQUE, order_id TEXT NOT NULL REFERENCES purchase_orders(id) ON DELETE RESTRICT,
  supplier_id TEXT NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT, status TEXT NOT NULL DEFAULT 'DRAFT', shipped_at TEXT,
  eta TEXT, submitted_by TEXT, version INTEGER NOT NULL DEFAULT 1, idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS shipment_items (
  id TEXT PRIMARY KEY, shipment_id TEXT NOT NULL REFERENCES shipments(id) ON DELETE RESTRICT,
  material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE RESTRICT, batch_id TEXT, qty_shipped TEXT NOT NULL CHECK(CAST(qty_shipped AS REAL) > 0),
  quality_file_ids_json TEXT
);
CREATE TABLE IF NOT EXISTS deliveries (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  delivery_no TEXT NOT NULL UNIQUE, shipment_id TEXT NOT NULL REFERENCES shipments(id) ON DELETE RESTRICT,
  warehouse_id TEXT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT, status TEXT NOT NULL DEFAULT 'ARRIVED', arrived_at TEXT,
  confirmed_by TEXT, version INTEGER NOT NULL DEFAULT 1, idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS delivery_items (
  id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL REFERENCES deliveries(id) ON DELETE RESTRICT,
  shipment_item_id TEXT NOT NULL REFERENCES shipment_items(id) ON DELETE RESTRICT, qty_arrived TEXT NOT NULL CHECK(CAST(qty_arrived AS REAL) > 0),
  discrepancy_reason TEXT
);
CREATE TABLE IF NOT EXISTS inspections (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  inspection_no TEXT NOT NULL UNIQUE, delivery_id TEXT NOT NULL REFERENCES deliveries(id) ON DELETE RESTRICT,
  status TEXT NOT NULL DEFAULT 'PENDING', inspector_id TEXT NOT NULL, inspected_at TEXT, notes TEXT,
  version INTEGER NOT NULL DEFAULT 1, idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS inspection_items (
  id TEXT PRIMARY KEY, inspection_id TEXT NOT NULL REFERENCES inspections(id) ON DELETE RESTRICT,
  delivery_item_id TEXT NOT NULL REFERENCES delivery_items(id) ON DELETE RESTRICT, qty_passed TEXT NOT NULL CHECK(CAST(qty_passed AS REAL) >= 0),
  qty_failed TEXT NOT NULL CHECK(CAST(qty_failed AS REAL) >= 0), failure_reason TEXT
);
CREATE TABLE IF NOT EXISTS handovers (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  handover_no TEXT NOT NULL UNIQUE, source_org_id TEXT NOT NULL, target_org_id TEXT NOT NULL,
  source_warehouse_id TEXT, target_warehouse_id TEXT, status TEXT NOT NULL DEFAULT 'PENDING', initiated_by TEXT,
  confirmed_by TEXT, confirmed_at TEXT, notes TEXT, version INTEGER NOT NULL DEFAULT 1, idempotency_key TEXT NOT NULL UNIQUE,
  source_confirmed INTEGER NOT NULL DEFAULT 0, target_confirmed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS handover_items (
  id TEXT PRIMARY KEY, handover_id TEXT NOT NULL REFERENCES handovers(id) ON DELETE RESTRICT,
  material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE RESTRICT, batch_id TEXT, qty TEXT NOT NULL CHECK(CAST(qty AS REAL) > 0),
  unit TEXT NOT NULL, reference_type TEXT, reference_id TEXT
);
CREATE TABLE IF NOT EXISTS crews (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, contractor_org_id TEXT, trade TEXT, planned_headcount INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS crew_assignments (
  id TEXT PRIMARY KEY, crew_id TEXT NOT NULL REFERENCES crews(id) ON DELETE RESTRICT, anonymous_worker_no TEXT NOT NULL,
  role TEXT NOT NULL, entry_date TEXT NOT NULL, exit_date TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE', UNIQUE(crew_id,anonymous_worker_no)
);
CREATE TABLE IF NOT EXISTS ppe_standards (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT, trade TEXT,
  material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE RESTRICT, required_qty_per_person TEXT NOT NULL,
  replacement_days INTEGER, effective_from TEXT NOT NULL, effective_to TEXT
);
CREATE TABLE IF NOT EXISTS ppe_issues (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT, crew_id TEXT NOT NULL REFERENCES crews(id) ON DELETE RESTRICT,
  material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE RESTRICT, qty TEXT NOT NULL CHECK(CAST(qty AS REAL) > 0),
  issued_at TEXT NOT NULL, issued_by TEXT, batch_id TEXT, replacement_due TEXT, idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS ai_runs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  user_id TEXT, thread_id TEXT NOT NULL, request_text TEXT NOT NULL, intent TEXT,
  model_name TEXT NOT NULL, prompt_version TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, input_hash TEXT NOT NULL,
  output_json TEXT, error_code TEXT, created_at TEXT NOT NULL,
  thread_sequence INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_thread_states (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  user_id TEXT NOT NULL, thread_id TEXT NOT NULL,
  next_sequence INTEGER NOT NULL DEFAULT 1,
  cleared_through_sequence INTEGER NOT NULL DEFAULT 0,
  cleared_at TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY(project_id,user_id,thread_id)
);
CREATE TABLE IF NOT EXISTS ai_tool_calls (
  id TEXT PRIMARY KEY, ai_run_id TEXT NOT NULL REFERENCES ai_runs(id) ON DELETE RESTRICT,
  tool_name TEXT NOT NULL, tool_version TEXT NOT NULL, input_json TEXT NOT NULL,
  input_hash TEXT NOT NULL, output_json TEXT, status TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, permission_decision TEXT NOT NULL,
  error_code TEXT
);
CREATE TABLE IF NOT EXISTS drafts (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT, draft_type TEXT NOT NULL,
  payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, source_ai_run_id TEXT, status TEXT NOT NULL,
  expires_at TEXT NOT NULL, created_by TEXT NOT NULL, confirmed_by TEXT, confirmed_at TEXT, rejection_reason TEXT,
  purchase_request_id TEXT REFERENCES purchase_requests(id) ON DELETE RESTRICT,
  version INTEGER NOT NULL DEFAULT 1, idempotency_key TEXT NOT NULL UNIQUE, confirmation_token_hash TEXT
);
CREATE TABLE IF NOT EXISTS audit_logs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT, actor_type TEXT NOT NULL,
  actor_id TEXT, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, before_json TEXT,
  after_json TEXT, ai_run_id TEXT, request_id TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_events (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT, business_document_id TEXT NOT NULL,
  batch_id TEXT, event_type TEXT NOT NULL, payload_hash TEXT NOT NULL, document_hashes_json TEXT NOT NULL DEFAULT '[]',
  submitted_org_id TEXT, confirmed_org_ids_json TEXT NOT NULL DEFAULT '[]', occurred_at TEXT NOT NULL, schema_version TEXT NOT NULL DEFAULT '1',
  status TEXT NOT NULL DEFAULT 'PENDING', tx_id TEXT, block_no INTEGER, retry_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT, supersedes_event_id TEXT, idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS evidence_outbox (
  id TEXT PRIMARY KEY, evidence_event_id TEXT NOT NULL UNIQUE REFERENCES evidence_events(id) ON DELETE RESTRICT,
  payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL, last_error TEXT
);
CREATE TABLE IF NOT EXISTS evidence_verifications (
  id TEXT PRIMARY KEY, evidence_event_id TEXT NOT NULL REFERENCES evidence_events(id) ON DELETE RESTRICT,
  checked_at TEXT NOT NULL, chain_payload_hash TEXT, database_payload_hash TEXT NOT NULL, matches INTEGER NOT NULL,
  tx_exists INTEGER NOT NULL, verifier_id TEXT
);
CREATE TABLE IF NOT EXISTS idempotencies (
  scope TEXT NOT NULL, idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL, result_json TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY(scope,idempotency_key)
);
CREATE TABLE IF NOT EXISTS daily_reports (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT, report_date TEXT NOT NULL,
  payload_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, supersedes_report_id TEXT, published_by TEXT, published_at TEXT,
  UNIQUE(project_id,report_date,version)
);
CREATE TABLE IF NOT EXISTS todos (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT, title TEXT NOT NULL,
  owner_role TEXT NOT NULL, due_at TEXT NOT NULL, priority TEXT NOT NULL, source_ref TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN', note TEXT, idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS anomalies (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT, dedupe_key TEXT NOT NULL,
  material_id TEXT, severity TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN', payload_json TEXT NOT NULL,
  acknowledged_by TEXT, acknowledged_at TEXT, resolved_by TEXT, resolved_at TEXT, resolution_code TEXT, resolution_note TEXT,
  UNIQUE(project_id,dedupe_key)
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT, document_type TEXT NOT NULL,
  object_key TEXT NOT NULL UNIQUE, mime TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
  uploader_id TEXT, version INTEGER NOT NULL DEFAULT 1, uploaded_at TEXT NOT NULL, supersedes_document_id TEXT
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class Database:
    """Thread-safe SQLite connection with explicit transaction boundaries."""

    def __init__(self, path: str | os.PathLike[str] = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self._tx_local = threading.local()
        self.initialize()

    def initialize(self) -> None:
        with self._lock:
            self.connection.executescript(SCHEMA)
            self._migrate_ai_thread_schema()
            self._migrate_draft_purchase_request_schema()

    def _migrate_draft_purchase_request_schema(self) -> None:
        """Add the formal-request link to drafts created by older demos."""

        draft_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(drafts)").fetchall()
        }
        if "purchase_request_id" not in draft_columns:
            self.connection.execute(
                "ALTER TABLE drafts ADD COLUMN purchase_request_id TEXT "
                "REFERENCES purchase_requests(id) ON DELETE RESTRICT"
            )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_drafts_purchase_request "
            "ON drafts(purchase_request_id)"
        )

    def _migrate_ai_thread_schema(self) -> None:
        """Upgrade pre-thread-history demo databases without losing runs.

        SQLite's ``CREATE TABLE IF NOT EXISTS`` cannot add columns to an
        existing local database. Assigning a stable sequence to old runs lets
        a later logical thread clear hide them without deleting audit data.
        """

        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(ai_runs)").fetchall()
        }
        if "created_at" not in columns:
            self.connection.execute("ALTER TABLE ai_runs ADD COLUMN created_at TEXT")
            self.connection.execute(
                "UPDATE ai_runs SET created_at=COALESCE(ended_at,started_at) WHERE created_at IS NULL"
            )
        if "thread_sequence" not in columns:
            self.connection.execute(
                "ALTER TABLE ai_runs ADD COLUMN thread_sequence INTEGER NOT NULL DEFAULT 0"
            )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_runs_thread_created_at ON ai_runs(thread_id,created_at)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_runs_thread_history "
            "ON ai_runs(project_id,user_id,thread_id,thread_sequence)"
        )

        # Old rows did not have a per-thread order. Backfill one once, using
        # the original run timestamp and identifier as a deterministic tie
        # breaker. New writes allocate the sequence transactionally.
        rows = self.connection.execute(
            "SELECT id,project_id,user_id,thread_id FROM ai_runs "
            "WHERE thread_sequence IS NULL OR thread_sequence<=0 "
            "ORDER BY project_id,COALESCE(user_id,''),thread_id,created_at,started_at,id"
        ).fetchall()
        sequence_by_thread: dict[tuple[str, str, str], int] = {}
        existing = self.connection.execute(
            "SELECT project_id,COALESCE(user_id,'') AS user_id,thread_id,MAX(thread_sequence) AS max_sequence "
            "FROM ai_runs WHERE thread_sequence>0 GROUP BY project_id,COALESCE(user_id,''),thread_id"
        ).fetchall()
        for row in existing:
            sequence_by_thread[(str(row["project_id"]), str(row["user_id"]), str(row["thread_id"]))] = int(row["max_sequence"])
        for row in rows:
            key = (str(row["project_id"]), str(row["user_id"] or ""), str(row["thread_id"]))
            next_sequence = sequence_by_thread.get(key, 0) + 1
            sequence_by_thread[key] = next_sequence
            self.connection.execute("UPDATE ai_runs SET thread_sequence=? WHERE id=?", (next_sequence, row["id"]))

        # Keep allocation state in sync for databases upgraded after they
        # already accumulated runs. The state table is deliberately separate
        # from ai_runs so clearing is logical and audit records remain intact.
        grouped = self.connection.execute(
            "SELECT project_id,COALESCE(user_id,'') AS user_id,thread_id,MAX(thread_sequence) AS max_sequence "
            "FROM ai_runs GROUP BY project_id,COALESCE(user_id,''),thread_id"
        ).fetchall()
        for row in grouped:
            self.connection.execute(
                "INSERT OR IGNORE INTO ai_thread_states(project_id,user_id,thread_id,next_sequence,cleared_through_sequence,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (row["project_id"], row["user_id"], row["thread_id"], int(row["max_sequence"] or 0) + 1, 0, iso_utc(utc_now())),
            )
            self.connection.execute(
                "UPDATE ai_thread_states SET next_sequence=?,updated_at=? "
                "WHERE project_id=? AND user_id=? AND thread_id=? AND next_sequence<?",
                (int(row["max_sequence"] or 0) + 1, iso_utc(utc_now()), row["project_id"], row["user_id"], row["thread_id"], int(row["max_sequence"] or 0) + 1),
            )
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            depth = int(getattr(self._tx_local, "depth", 0))
            savepoint = f"sp_{depth + 1}"
            if depth == 0:
                self.connection.execute("BEGIN IMMEDIATE")
            else:
                self.connection.execute(f"SAVEPOINT {savepoint}")
            self._tx_local.depth = depth + 1
            try:
                yield self.connection
            except Exception:
                if depth == 0:
                    self.connection.rollback()
                else:
                    self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                if depth == 0:
                    self.connection.commit()
                else:
                    self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            finally:
                self._tx_local.depth = depth

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.connection.execute(sql, params)

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        # Keep the connection lock through fetch as well as execute.  The
        # adapter intentionally uses one connection for the demo database;
        # releasing the lock between those calls lets concurrent ASGI worker
        # threads interleave cursor work and produce inconsistent reads.
        with self._lock:
            return self.connection.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.connection.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def reset_demo(self) -> None:
        """Reset only the P001 namespace and require an explicit demo flag."""

        if os.getenv("DEMO_MODE", "").lower() not in {"1", "true", "yes", "on"}:
            raise RuntimeError("DEMO_MODE=true is required to reset demo data")
        project = self.query_one("SELECT id FROM projects WHERE code='P001'")
        if not project:
            return
        pid = project["id"]
        with self.transaction() as conn:
            # ``idempotencies`` and ``confirmation_tokens`` intentionally do
            # not carry a project foreign key: they are generic request
            # records and token subjects.  Capture every P001 entity ID
            # before deleting the namespace so reset can remove only records
            # that belong to this demo project.  This keeps another tenant's
            # idempotency history intact while preventing stale P001 replay
            # records from referring to freshly seeded rows.
            demo_entity_ids: set[str] = {str(pid)}
            project_tables = (
                "ai_runs", "evidence_events", "inspections", "deliveries",
                "shipments", "purchase_orders", "purchase_requests", "handovers",
                "ppe_issues", "crews", "stock_moves", "material_plans",
                "consumption_snapshots", "material_aliases", "materials", "warehouses",
                "buildings", "construction_stages", "audit_logs",
                "drafts", "daily_reports", "todos", "anomalies", "documents",
            )
            for table in project_tables:
                demo_entity_ids.update(
                    str(row["id"])
                    for row in conn.execute(f"SELECT id FROM {table} WHERE project_id=?", (pid,)).fetchall()
                    if row["id"] is not None
                )
            child_tables = (
                ("ai_tool_calls", "ai_run_id", "ai_runs"),
                ("evidence_verifications", "evidence_event_id", "evidence_events"),
                ("evidence_outbox", "evidence_event_id", "evidence_events"),
                ("inspection_items", "inspection_id", "inspections"),
                ("delivery_items", "delivery_id", "deliveries"),
                ("shipment_items", "shipment_id", "shipments"),
                ("purchase_order_items", "order_id", "purchase_orders"),
                ("purchase_request_items", "request_id", "purchase_requests"),
                ("handover_items", "handover_id", "handovers"),
                ("crew_assignments", "crew_id", "crews"),
            )
            for table, foreign_key, parent_table in child_tables:
                parent_ids = [
                    str(row["id"])
                    for row in conn.execute(f"SELECT id FROM {parent_table} WHERE project_id=?", (pid,)).fetchall()
                    if row["id"] is not None
                ]
                if parent_ids:
                    placeholders = ",".join("?" * len(parent_ids))
                    demo_entity_ids.update(
                        str(row["id"])
                        for row in conn.execute(
                            f"SELECT id FROM {table} WHERE {foreign_key} IN ({placeholders})",
                            tuple(parent_ids),
                        ).fetchall()
                        if row["id"] is not None
                    )

            # Idempotency scopes are server-defined command families.  Their
            # stored result JSON includes a project/entity reference; remove
            # only rows belonging to captured P001 IDs so other tenants keep
            # their replay history.
            for row in conn.execute("SELECT scope,idempotency_key,result_json FROM idempotencies").fetchall():
                result_json = str(row["result_json"] or "")
                if any(entity_id in result_json for entity_id in demo_entity_ids):
                    conn.execute(
                        "DELETE FROM idempotencies WHERE scope=? AND idempotency_key=?",
                        (row["scope"], row["idempotency_key"]),
                    )
            if demo_entity_ids and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='confirmation_tokens'"
            ).fetchone():
                placeholders = ",".join("?" * len(demo_entity_ids))
                conn.execute(
                    f"DELETE FROM confirmation_tokens WHERE entity_id IN ({placeholders})",
                    tuple(demo_entity_ids),
                )

            # Child-first deletion preserves the RESTRICT foreign keys and
            # leaves any unrelated tenant data untouched.
            for table, column in (
                ("ai_tool_calls", "ai_run_id"), ("ai_thread_states", "project_id"), ("ai_runs", "project_id"),
                ("evidence_verifications", "evidence_event_id"), ("evidence_outbox", "evidence_event_id"),
                ("inspection_items", "inspection_id"), ("inspections", "project_id"),
                ("delivery_items", "delivery_id"), ("deliveries", "project_id"),
                ("shipment_items", "shipment_id"), ("shipments", "project_id"),
                ("purchase_order_items", "order_id"), ("purchase_orders", "project_id"),
                ("drafts", "project_id"), ("purchase_request_items", "request_id"), ("purchase_requests", "project_id"),
                ("handover_items", "handover_id"), ("handovers", "project_id"),
                ("ppe_issues", "project_id"), ("ppe_standards", "project_id"), ("crew_assignments", "crew_id"),
                ("crews", "project_id"), ("stock_moves", "project_id"), ("stock_balances", "warehouse_id"),
                ("material_plans", "project_id"), ("consumption_snapshots", "project_id"),
                ("material_aliases", "project_id"), ("batches", "material_id"), ("materials", "project_id"),
                ("warehouses", "project_id"), ("areas", "building_id"), ("buildings", "project_id"),
                ("construction_stages", "project_id"), ("project_members", "project_id"), ("audit_logs", "project_id"),
                ("evidence_events", "project_id"), ("daily_reports", "project_id"),
                ("todos", "project_id"), ("anomalies", "project_id"), ("documents", "project_id"),
            ):
                # A few child tables use a different foreign-key column.
                if table == "ai_tool_calls":
                    conn.execute("DELETE FROM ai_tool_calls WHERE ai_run_id IN (SELECT id FROM ai_runs WHERE project_id=?)", (pid,))
                elif table == "evidence_verifications":
                    conn.execute("DELETE FROM evidence_verifications WHERE evidence_event_id IN (SELECT id FROM evidence_events WHERE project_id=?)", (pid,))
                elif table == "evidence_outbox":
                    conn.execute("DELETE FROM evidence_outbox WHERE evidence_event_id IN (SELECT id FROM evidence_events WHERE project_id=?)", (pid,))
                elif table == "inspection_items":
                    conn.execute("DELETE FROM inspection_items WHERE inspection_id IN (SELECT id FROM inspections WHERE project_id=?)", (pid,))
                elif table == "delivery_items":
                    conn.execute("DELETE FROM delivery_items WHERE delivery_id IN (SELECT id FROM deliveries WHERE project_id=?)", (pid,))
                elif table == "shipment_items":
                    conn.execute("DELETE FROM shipment_items WHERE shipment_id IN (SELECT id FROM shipments WHERE project_id=?)", (pid,))
                elif table == "purchase_order_items":
                    conn.execute("DELETE FROM purchase_order_items WHERE order_id IN (SELECT id FROM purchase_orders WHERE project_id=?)", (pid,))
                elif table == "purchase_request_items":
                    conn.execute("DELETE FROM purchase_request_items WHERE request_id IN (SELECT id FROM purchase_requests WHERE project_id=?)", (pid,))
                elif table == "handover_items":
                    conn.execute("DELETE FROM handover_items WHERE handover_id IN (SELECT id FROM handovers WHERE project_id=?)", (pid,))
                elif table == "stock_balances":
                    conn.execute("DELETE FROM stock_balances WHERE warehouse_id IN (SELECT id FROM warehouses WHERE project_id=?)", (pid,))
                elif table == "crew_assignments":
                    conn.execute("DELETE FROM crew_assignments WHERE crew_id IN (SELECT id FROM crews WHERE project_id=?)", (pid,))
                elif table == "batches":
                    conn.execute("DELETE FROM batches WHERE material_id IN (SELECT id FROM materials WHERE project_id=?)", (pid,))
                elif table == "areas":
                    conn.execute("DELETE FROM areas WHERE building_id IN (SELECT id FROM buildings WHERE project_id=?)", (pid,))
                else:
                    conn.execute(f"DELETE FROM {table} WHERE {column}=?", (pid,))
            conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        # Object files are outside SQLite.  Remove only the fixed P001 object
        # prefix after the namespace transaction commits; unrelated tenants
        # and files remain untouched.
        project_objects = Path(os.getenv("DOCUMENT_ROOT", "backend/data/documents")) / str(pid)
        if self.path != ":memory:" and project_objects.is_dir():
            shutil.rmtree(project_objects, ignore_errors=True)

    def seed_demo(self) -> dict[str, Any]:
        """Idempotently load the fixed P001 demonstration snapshot."""

        now = iso_utc(utc_now())
        ids = {
            "project": "00000000-0000-4000-8000-000000000001",
            "project_org": "00000000-0000-4000-8000-000000000002",
            "supplier_org": "00000000-0000-4000-8000-000000000003",
            "owner": "00000000-0000-4000-8000-000000000011",
            "supplier_user": "00000000-0000-4000-8000-000000000012",
            "site": "00000000-0000-4000-8000-000000000013",
            "inspector": "00000000-0000-4000-8000-000000000014",
            "procurement": "00000000-0000-4000-8000-000000000015",
            "auditor": "00000000-0000-4000-8000-000000000016",
            "building": "00000000-0000-4000-8000-000000000021",
            "area": "00000000-0000-4000-8000-000000000022",
            "warehouse": "00000000-0000-4000-8000-000000000023",
            "stage": "00000000-0000-4000-8000-000000000024",
            "supplier": "00000000-0000-4000-8000-000000000031",
            "crew": "00000000-0000-4000-8000-000000000041",
            "po": "00000000-0000-4000-8000-000000000051",
            "po_item": "00000000-0000-4000-8000-000000000052",
            "shipment": "00000000-0000-4000-8000-000000000053",
            "shipment_item": "00000000-0000-4000-8000-000000000054",
            "delivery": "00000000-0000-4000-8000-000000000055",
            "delivery_item": "00000000-0000-4000-8000-000000000056",
            "inspection": "00000000-0000-4000-8000-000000000057",
            "handover": "00000000-0000-4000-8000-000000000058",
            "handover_item": "00000000-0000-4000-8000-000000000059",
            "purchase_request": "00000000-0000-4000-8000-000000000065",
            "purchase_request_item": "00000000-0000-4000-8000-000000000066",
            "todo_1": "00000000-0000-4000-8000-000000000071",
            "todo_2": "00000000-0000-4000-8000-000000000072",
            "todo_3": "00000000-0000-4000-8000-000000000073",
            "anomaly_1": "00000000-0000-4000-8000-000000000081",
            "daily_report": "00000000-0000-4000-8000-000000000091",
            "quality_document": "00000000-0000-4000-8000-000000000092",
            "evidence_success": "00000000-0000-4000-8000-000000000093",
            "evidence_failure": "00000000-0000-4000-8000-000000000094",
        }
        materials = [
            ("MAT-BLOCK", "砌块", "CONSTRUCTION", "piece", "100", "20", "5", "10", "1"),
            ("MAT-CEMENT", "水泥", "CONSTRUCTION", "kg", "500", "100", "7", "50", "25"),
            ("MAT-REBAR", "钢筋", "CONSTRUCTION", "kg", "1000", "200", "14", "100", "50"),
            ("MAT-HELMET", "安全帽", "PPE", "piece", "20", "5", "3", "10", "5"),
            ("MAT-VEST", "反光衣", "PPE", "piece", "20", "5", "3", "10", "5"),
            ("MAT-GLOVE", "防护手套", "PPE", "pair", "30", "10", "3", "10", "5"),
            ("MAT-SHOE", "防护鞋", "PPE", "pair", "20", "5", "5", "5", "1"),
            ("MAT-HEAT", "防暑用品", "FIRST_AID", "box", "10", "2", "3", "5", "1"),
            ("MAT-FIRST-AID", "急救用品", "FIRST_AID", "box", "10", "2", "3", "5", "1"),
        ]
        # Add the remaining fixed catalogue entries without inventing business
        # semantics; the names are stable and useful for search demonstrations.
        for i in range(10, 21):
            materials.append((f"MAT-{i:02d}", f"演示物料{i:02d}", "OTHER", "piece", "0", "0", "5", "0", "1"))
        with self.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO organizations(id,code,name,role_type) VALUES(?,?,?,?)", (ids["project_org"], "ORG-PROJECT", "海之子项目方", "PROJECT"))
            conn.execute("INSERT OR IGNORE INTO organizations(id,code,name,role_type) VALUES(?,?,?,?)", (ids["supplier_org"], "ORG-SUPPLIER-A", "供应商 A", "SUPPLIER"))
            users = [
                (ids["owner"], ids["project_org"], "owner@example.test", "项目负责人", "PROJECT_OWNER"),
                (ids["supplier_user"], ids["supplier_org"], "supplier@example.test", "供应商用户", "SUPPLIER"),
                (ids["site"], ids["project_org"], "site@example.test", "现场管理员", "SITE_ADMIN"),
                (ids["inspector"], ids["project_org"], "inspector@example.test", "验收人员", "INSPECTOR"),
                (ids["procurement"], ids["project_org"], "procurement@example.test", "采购人员", "PROCUREMENT"),
                (ids["auditor"], ids["project_org"], "auditor@example.test", "审计人员", "AUDITOR"),
            ]
            conn.executemany("INSERT OR IGNORE INTO users(id,organization_id,username,display_name,role) VALUES(?,?,?,?,?)", users)
            conn.execute("INSERT OR IGNORE INTO projects(id,code,name,timezone,status,owner_org_id,start_date,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (ids["project"], "P001", "海之子示范项目", "Asia/Shanghai", "ACTIVE", ids["project_org"], "2026-01-01", now, now))
            conn.executemany("INSERT OR IGNORE INTO project_members(project_id,user_id,role,scopes_json) VALUES(?,?,?,?)", [(ids["project"], u[0], u[4], None) for u in users])
            conn.execute("INSERT OR IGNORE INTO buildings(id,project_id,code,name) VALUES(?,?,?,?)", (ids["building"], ids["project"], "BLD-03", "三号楼"))
            conn.execute("INSERT OR IGNORE INTO construction_stages(id,project_id,code,name,start_date,status) VALUES(?,?,?,?,?,?)", (ids["stage"], ids["project"], "STAGE-MASONRY", "砌体阶段", "2026-01-01", "ACTIVE"))
            conn.execute("INSERT OR IGNORE INTO areas(id,building_id,code,name,stage_id) VALUES(?,?,?,?,?)", (ids["area"], ids["building"], "AREA-MASONRY-03", "三号楼砌体区", ids["stage"]))
            conn.execute("INSERT OR IGNORE INTO warehouses(id,project_id,code,name,area_id) VALUES(?,?,?,?,?)", (ids["warehouse"], ids["project"], "WH-MAIN", "主仓库", ids["area"]))
            conn.execute("UPDATE project_members SET scopes_json=? WHERE project_id=? AND user_id=?", (_json({"warehouse_ids": [ids["warehouse"]], "area_ids": [ids["area"]]}), ids["project"], ids["site"]))
            conn.execute("INSERT OR IGNORE INTO suppliers(id,organization_id,code,name,contact_json) VALUES(?,?,?,?,?)", (ids["supplier"], ids["supplier_org"], "SUP-A", "示范供应商 A", _json({"phone": "***"})))
            for index, (sku, name, category, unit, safety, reorder, lead, minimum, multiple) in enumerate(materials, 1):
                mid = f"00000000-0000-4000-8000-{1000000000 + index:012d}"
                ppe = _json({"per_person": "1", "replacement_days": 180, "roles": ["MASON"]}) if category == "PPE" else None
                conn.execute("INSERT OR IGNORE INTO materials(id,project_id,sku,name,category,unit,safety_stock,reorder_point,lead_time_days,min_order_qty,order_multiple,anomaly_min_qty,ppe_standard_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (mid, ids["project"], sku, name, category, unit, safety, reorder, lead, minimum, multiple, "1", ppe))
                specification = {
                    "MAT-BLOCK": "600×240×200mm",
                    "MAT-CEMENT": "P.O 42.5 · 50kg/袋",
                    "MAT-REBAR": "HRB400E · Φ20 · 9m",
                    "MAT-HELMET": "ABS · 黄色",
                    "MAT-VEST": "反光 · 均码",
                    "MAT-GLOVE": "耐磨 · L码",
                    "MAT-SHOE": "防砸 · 42码",
                    "MAT-HEAT": "现场防暑组合箱",
                    "MAT-FIRST-AID": "现场急救组合箱",
                }.get(sku)
                if specification:
                    conn.execute("UPDATE materials SET specification=? WHERE id=?", (specification, mid))
                conn.execute("INSERT OR IGNORE INTO material_aliases(id,project_id,material_id,alias) VALUES(?,?,?,?)", (str(uuid.uuid5(uuid.NAMESPACE_URL, "haizhizi:" + sku)), ids["project"], mid, name))
                if sku in {"MAT-BLOCK", "MAT-GLOVE"}:
                    batch = f"00000000-0000-4000-8000-{2000000000 + index:012d}"
                    conn.execute("INSERT OR IGNORE INTO batches(id,material_id,batch_no,supplier_id,status) VALUES(?,?,?,?,?)", (batch, mid, f"BATCH-{sku}", ids["supplier"], "INSPECTED"))
                    qty = "30" if sku == "MAT-BLOCK" else "5"
                    conn.execute("INSERT OR IGNORE INTO stock_balances(id,warehouse_id,material_id,batch_id,batch_key,qty_on_hand,version) VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid5(uuid.NAMESPACE_URL, "balance:" + sku)), ids["warehouse"], mid, batch, batch, qty, 1))
                # 14 valid historical points, with a small deterministic daily use.
                for offset in range(14):
                    d = date(2026, 8, 12) + timedelta(days=offset)
                    issued = "2" if sku == "MAT-BLOCK" else ("1" if sku == "MAT-GLOVE" else "0")
                    sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"snapshot:{sku}:{d.isoformat()}"))
                    conn.execute("INSERT OR IGNORE INTO consumption_snapshots(id,project_id,material_id,date,issued_qty,net_consumed_qty) VALUES(?,?,?,?,?,?)", (sid, ids["project"], mid, d.isoformat(), issued, issued))
            conn.execute("INSERT OR IGNORE INTO crews(id,project_id,code,name,contractor_org_id,trade,planned_headcount) VALUES(?,?,?,?,?,?,?)", (ids["crew"], ids["project"], "CREW-MASON", "砌体班组", ids["project_org"], "MASON", 10))
            # PPE standard and one deliberate plan-variance day.
            glove_id = conn.execute("SELECT id FROM materials WHERE sku='MAT-GLOVE'").fetchone()[0]
            block_id = conn.execute("SELECT id FROM materials WHERE sku='MAT-BLOCK'").fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO ppe_standards(id,project_id,trade,material_id,required_qty_per_person,effective_from) VALUES(?,?,?,?,?,?)", (str(uuid.uuid5(uuid.NAMESPACE_URL, "ppe:glove")), ids["project"], "MASON", glove_id, "1", "2026-01-01"))
            for offset in range(7):
                d = date(2026, 8, 27) + timedelta(days=offset)
                conn.execute("INSERT OR IGNORE INTO material_plans(id,project_id,stage_id,area_id,material_id,plan_date,planned_qty,source) VALUES(?,?,?,?,?,?,?,?)", (str(uuid.uuid5(uuid.NAMESPACE_URL, f"plan:block:{d}")), ids["project"], ids["stage"], ids["area"], block_id, d.isoformat(), "20", "BOQ"))
            # A stable, partly delivered order gives the demo a full supplier
            # workflow without treating it as current available inventory.
            block_batch = conn.execute("SELECT id FROM batches WHERE material_id=? ORDER BY batch_no LIMIT 1", (block_id,)).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO purchase_orders(id,project_id,po_no,supplier_id,status,order_date,expected_date,currency,version) VALUES(?,?,?,?,?,?,?,?,?)", (ids["po"], ids["project"], "PO-20260820-0001", ids["supplier"], "PARTIAL_SHIPPED", "2026-08-20", "2026-08-30", "CNY", 1))
            conn.execute("INSERT OR IGNORE INTO purchase_order_items(id,order_id,material_id,qty_ordered,qty_shipped,qty_received,unit) VALUES(?,?,?,?,?,?,?)", (ids["po_item"], ids["po"], block_id, "100", "60", "0", "piece"))
            conn.execute("INSERT OR IGNORE INTO purchase_requests(id,project_id,request_no,requester_id,status,needed_by,justification,version,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?)", (ids["purchase_request"], ids["project"], "PR-20260827-0001", ids["owner"], "PENDING_APPROVAL", "2026-09-01", "恢复砌块安全库存", 1, "seed-purchase-request"))
            conn.execute("INSERT OR IGNORE INTO purchase_request_items(id,request_id,material_id,specification_snapshot,unit,qty,warehouse_id,reason,shortage_calc_id) VALUES(?,?,?,?,?,?,?,?,?)", (ids["purchase_request_item"], ids["purchase_request"], block_id, "砌块", "piece", "150", ids["warehouse"], "由演示缺料计算生成", "seed-shortage-v1"))
            conn.execute("INSERT OR IGNORE INTO shipments(id,project_id,shipment_no,order_id,supplier_id,status,shipped_at,eta,submitted_by,version,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (ids["shipment"], ids["project"], "SH-20260826-0001", ids["po"], ids["supplier"], "SUBMITTED", "2026-08-26T02:16:00Z", "2026-08-27T06:00:00Z", ids["supplier_user"], 1, "seed-shipment"))
            # The baseline delivery is already accepted, so the shipment is
            # physically arrived even though its inspection remains pending.
            conn.execute("UPDATE shipments SET status=? WHERE id=? AND status=?", ("ARRIVED", ids["shipment"], "SUBMITTED"))
            conn.execute("INSERT OR IGNORE INTO shipment_items(id,shipment_id,material_id,batch_id,qty_shipped,quality_file_ids_json) VALUES(?,?,?,?,?,?)", (ids["shipment_item"], ids["shipment"], block_id, block_batch, "60", "[]"))
            conn.execute("INSERT OR IGNORE INTO deliveries(id,project_id,delivery_no,shipment_id,warehouse_id,status,arrived_at,confirmed_by,version,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?)", (ids["delivery"], ids["project"], "DL-20260826-0001", ids["shipment"], ids["warehouse"], "ACCEPTED", "2026-08-26T07:42:00Z", ids["site"], 2, "seed-delivery"))
            conn.execute("INSERT OR IGNORE INTO delivery_items(id,delivery_id,shipment_item_id,qty_arrived,discrepancy_reason) VALUES(?,?,?,?,?)", (ids["delivery_item"], ids["delivery"], ids["shipment_item"], "60", None))
            conn.execute("INSERT OR IGNORE INTO inspections(id,project_id,inspection_no,delivery_id,status,inspector_id,notes,version,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?)", (ids["inspection"], ids["project"], "IN-20260827-0001", ids["delivery"], "PENDING", ids["inspector"], "演示待验收记录", 1, "seed-inspection"))
            conn.execute("INSERT OR IGNORE INTO inspection_items(id,inspection_id,delivery_item_id,qty_passed,qty_failed,failure_reason) VALUES(?,?,?,?,?,?)", (str(uuid.uuid5(uuid.NAMESPACE_URL, "inspection-item:seed")), ids["inspection"], ids["delivery_item"], "50", "10", "包装破损，待隔离处理"))
            conn.execute("INSERT OR IGNORE INTO handovers(id,project_id,handover_no,source_org_id,target_org_id,source_warehouse_id,target_warehouse_id,status,initiated_by,version,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (ids["handover"], ids["project"], "HO-20260827-0001", ids["project_org"], ids["supplier_org"], ids["warehouse"], ids["warehouse"], "PENDING", ids["owner"], 1, "seed-handover"))
            conn.execute("INSERT OR IGNORE INTO handover_items(id,handover_id,material_id,batch_id,qty,unit) VALUES(?,?,?,?,?,?)", (ids["handover_item"], ids["handover"], block_id, block_batch, "1", "piece"))
            # A small real file backs the quality-document reference shown in
            # the trace view.  Only immutable metadata is exposed to the API.
            document_bytes = "筑福链演示质量文件\n批次: BATCH-MAT-BLOCK\n状态: 待验收\n".encode("utf-8")
            document_root = Path(os.getenv("DOCUMENT_ROOT", "backend/data/documents")) / ids["project"]
            document_root.mkdir(parents=True, exist_ok=True)
            document_sha = hashlib.sha256(document_bytes).hexdigest()
            (document_root / ids["quality_document"]).write_bytes(document_bytes)
            conn.execute("INSERT OR IGNORE INTO documents(id,project_id,document_type,object_key,mime,size_bytes,sha256,uploader_id,version,uploaded_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (ids["quality_document"], ids["project"], "INSPECTION_FORM", f"{ids['project']}/{ids['quality_document']}", "text/plain", len(document_bytes), document_sha, ids["inspector"], 1, "2026-08-27T00:00:00Z"))
            conn.execute("UPDATE shipment_items SET quality_file_ids_json=? WHERE id=?", (_json([ids["quality_document"]]), ids["shipment_item"]))
            # The four core evidence classes begin PENDING.  The worker/API
            # later anchors them through MockEvidenceAdapter in development.
            event_rows = [
                ("00000000-0000-4000-8000-000000000061", ids["shipment"], "SHIPMENT"),
                ("00000000-0000-4000-8000-000000000062", ids["delivery"], "DELIVERY"),
                ("00000000-0000-4000-8000-000000000063", ids["inspection"], "INSPECTION"),
                ("00000000-0000-4000-8000-000000000064", ids["handover"], "HANDOVER"),
            ]
            for event_id, document_id, event_type in event_rows:
                document_hashes = [document_sha] if event_type in {"SHIPMENT", "DELIVERY", "INSPECTION"} else []
                payload = {"event_type": event_type, "business_document_id": document_id, "project_id": ids["project"], "batch_id": block_batch, "document_hashes": document_hashes, "schema_version": "1"}
                payload_json = _json(payload)
                payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                event_key = hashlib.sha256(f"{ids['project']}|{document_id}|{event_type}|1".encode("utf-8")).hexdigest()
                conn.execute("INSERT OR IGNORE INTO evidence_events(id,project_id,business_document_id,batch_id,event_type,payload_hash,document_hashes_json,submitted_org_id,confirmed_org_ids_json,occurred_at,schema_version,status,retry_count,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (event_id, ids["project"], document_id, block_batch, event_type, payload_hash, _json(document_hashes), ids["project_org"], "[]", "2026-08-27T00:00:00Z", "1", "PENDING", 0, event_key))
                conn.execute("INSERT OR IGNORE INTO evidence_outbox(id,evidence_event_id,payload_json,status,attempt_count,next_attempt_at) VALUES(?,?,?,?,?,?)", (str(uuid.uuid5(uuid.NAMESPACE_URL, f"outbox:{event_id}")), event_id, payload_json, "PENDING", 0, "2026-08-27T00:00:00Z"))
            # Two auxiliary adapter samples make the async evidence controls
            # demonstrable without changing the four required core events:
            # one persisted submission can be verified immediately, while a
            # failed delivery can be retried through the real endpoint.
            adapter_samples = [
                (ids["evidence_success"], ids["shipment"], "SHIPMENT", "SUBMITTED", "mocktx_" + ids["evidence_success"], 5, 0, "2026-08-27T01:00:00Z", "SUBMITTED", 1, None),
                (ids["evidence_failure"], ids["delivery"], "DELIVERY", "FAILED", None, None, 2, "2026-08-27T02:00:00Z", "FAILED", 2, "mock evidence adapter unavailable"),
            ]
            for event_id, document_id, event_type, event_status, tx_id, block_no, retry_count, occurred_at, outbox_status, attempts, last_error in adapter_samples:
                document_hashes = [document_sha] if event_type == "SHIPMENT" else []
                business_no = "SH-20260826-0001" if event_type == "SHIPMENT" else "DL-20260826-0001"
                payload = {
                    "event_type": event_type,
                    "business_document_id": document_id,
                    "project_id": ids["project"],
                    "batch_id": block_batch,
                    "version": 1,
                    "occurred_at": occurred_at,
                    "document_hashes": document_hashes,
                    "schema_version": "1",
                    "shipment_no" if event_type == "SHIPMENT" else "delivery_no": business_no,
                    "status": "SUBMITTED" if event_type == "SHIPMENT" else "ACCEPTED",
                }
                payload_json = _json(payload)
                payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                event_key = hashlib.sha256(f"{ids['project']}|{document_id}|{event_type}|2".encode("utf-8")).hexdigest()
                conn.execute("INSERT OR IGNORE INTO evidence_events(id,project_id,business_document_id,batch_id,event_type,payload_hash,document_hashes_json,submitted_org_id,confirmed_org_ids_json,occurred_at,schema_version,status,tx_id,block_no,retry_count,next_retry_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (event_id, ids["project"], document_id, block_batch, event_type, payload_hash, _json(document_hashes), ids["project_org"], "[]", occurred_at, "1", event_status, tx_id, block_no, retry_count, "2026-08-27T03:00:00Z" if event_status == "FAILED" else None, event_key))
                conn.execute("INSERT OR IGNORE INTO evidence_outbox(id,evidence_event_id,payload_json,status,attempt_count,next_attempt_at,last_error) VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid5(uuid.NAMESPACE_URL, f"outbox:{event_id}")), event_id, payload_json, outbox_status, attempts, "2026-08-27T03:00:00Z", last_error))
            todos = [
                (ids["todo_1"], "确认砌块采购申请并完成审批", "PROJECT_OWNER", "2026-08-27T09:00:00Z", "HIGH", "purchase_request:PR-20260827-0001", "OPEN", "seed-todo-1"),
                (ids["todo_2"], "确认到货批次的质量验收", "INSPECTOR", "2026-08-27T10:00:00Z", "HIGH", "inspection:IN-20260827-0001", "OPEN", "seed-todo-2"),
                (ids["todo_3"], "复核砌体计划与实际消耗偏差", "PROJECT_OWNER", "2026-08-28T02:00:00Z", "MEDIUM", "anomaly:seed-plan-variance", "IN_PROGRESS", "seed-todo-3"),
            ]
            conn.executemany("INSERT OR IGNORE INTO todos(id,project_id,title,owner_role,due_at,priority,source_ref,status,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?)", [(todo_id, ids["project"], title, owner_role, due_at, priority, source_ref, status, key) for todo_id, title, owner_role, due_at, priority, source_ref, status, key in todos])
            anomaly_payload = _json({"material_id": block_id, "date": "2026-08-27", "types": ["PLAN_VARIANCE"], "actual_net": "32", "planned_qty": "20", "plan_variance_ratio": "0.6", "possible_causes": ["计划版本或现场领用记录需要复核"], "review_steps": ["核对领用流水和计划版本"]})
            conn.execute("INSERT OR IGNORE INTO anomalies(id,project_id,dedupe_key,material_id,severity,type,status,payload_json) VALUES(?,?,?,?,?,?,?,?)", (ids["anomaly_1"], ids["project"], "seed-plan-variance", block_id, "HIGH", "PLAN_VARIANCE", "OPEN", anomaly_payload))
            # The published baseline is a real read-model snapshot too.  Its
            # references point at the seeded delivery, deterministic
            # calculations, anomaly and open todos rather than placeholder
            # prose, so the first page load is truthful before a new draft is
            # generated.
            report_payload = _json({
                "date": "2026-08-26",
                "arrivals": [{"delivery_id": ids["delivery"], "delivery_no": "DL-20260826-0001", "status": "ACCEPTED", "source_ref": "delivery:DL-20260826-0001"}],
                "issues": [],
                "consumption": [],
                "shortage_risks": [{"material_id": block_id, "material_name": "砌块", "shortage_qty": "150", "unit": "piece", "shortage_date": "2026-08-27", "calculation_id": "seed-shortage-v1", "source_ref": "calculation:seed-shortage-v1"}],
                "shortage_calculation_id": "seed-shortage-v1",
                "ppe_coverage": [{"crew_id": ids["crew"], "material_id": glove_id, "material_name": "防护手套", "shortage_qty": "10", "unit": "pair", "calculation_id": "seed-ppe-v1", "source_ref": "calculation:seed-ppe-v1"}],
                "ppe_calculation_id": "seed-ppe-v1",
                "anomalies": [{"anomaly_id": ids["anomaly_1"], "material_id": block_id, "material_name": "砌块", "type": "PLAN_VARIANCE", "status": "OPEN", "source_ref": "anomaly:seed-plan-variance"}],
                "todo": [{"id": ids["todo_1"], "title": "确认砌块采购申请并完成审批", "owner_role": "PROJECT_OWNER", "priority": "HIGH", "source_ref": "purchase_request:PR-20260827-0001"}],
                "claims": [{"type": "FACT", "text": "日报仅引用已确认业务记录和带版本的计算结果", "source_refs": ["delivery:DL-20260826-0001", "calculation:seed-shortage-v1", "calculation:seed-ppe-v1", "anomaly:seed-plan-variance", "todo:00000000-0000-4000-8000-000000000071"]}],
                "warnings": [],
                "data_completeness": 1.0,
            })
            conn.execute("INSERT OR IGNORE INTO daily_reports(id,project_id,report_date,payload_json,version,published_by,published_at) VALUES(?,?,?,?,?,?,?)", (ids["daily_report"], ids["project"], "2026-08-26", report_payload, 1, ids["owner"], "2026-08-26T12:00:00Z"))
            # Keep the fixed demo row aligned when ``make seed`` is rerun on
            # an existing SQLite file; user-created later report versions are
            # left untouched.
            conn.execute("UPDATE daily_reports SET payload_json=? WHERE id=? AND version=1", (report_payload, ids["daily_report"]))
        return {"project_id": ids["project"], "project_code": "P001", "materials": len(materials), "seeded": True}
