"""Business service implementing the筑福链 MVP invariants.

The service is the only layer allowed to mutate business facts.  HTTP and AI
adapters call these methods with a :class:`RequestContext`; neither adapter
receives a database connection or chain client.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from .calculations import calculate_shortage, check_ppe_coverage, detect_consumption_anomaly, forecast_inventory, snapshot_hash
from .config import Settings, decimal_string, iso_utc, parse_decimal, utc_now
from .db import Database
from .evidence import ChainUnavailable, EvidenceSubmission, MockEvidenceAdapter, build_evidence_payload, canonical_json, hash_document, hash_payload
from .i18n import error_message, render_for_locale
from .models import (
    BatchStatus,
    DraftStatus,
    EvidenceEventType,
    EvidenceStatus,
    InspectionStatus,
    MaterialCategory,
    PurchaseOrderStatus,
    PurchaseRequestStatus,
    RequestContext,
    ShipmentStatus,
    StockMoveType,
    UserRole,
)


class DomainError(Exception):
    """Structured domain failure suitable for HTTP and AI tool responses."""

    STATUS_BY_CODE = {
        "UNAUTHORIZED": 401,
        "FORBIDDEN": 403,
        "PROJECT_NOT_FOUND": 404,
        "MATERIAL_NOT_FOUND": 404,
        "NOT_FOUND": 404,
        "DUPLICATE_IDEMPOTENCY": 409,
        "VERSION_CONFLICT": 409,
        "TOKEN_REPLAYED": 409,
        "INVALID_STATE": 400,
        "TOKEN_EXPIRED": 400,
        "INSUFFICIENT_STOCK": 400,
        "VALIDATION_ERROR": 422,
        "UNIT_MISMATCH": 422,
        "MISSING_DATA": 422,
        "CHAIN_UNAVAILABLE": 503,
        "THREAD_BUSY": 409,
    }

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None, *, message_key: str | None = None, message_params: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.message_key = message_key
        self.message_params = dict(message_params or {})
        self.status_code = self.STATUS_BY_CODE.get(code, 400)
        super().__init__(message)

    def as_dict(self, locale: str | None = None) -> dict[str, Any]:
        key, fallback = error_message(self.code, locale)
        message_key = self.message_key or key
        return {
            "code": self.code,
            "message": render_for_locale({"message_key": message_key, "message_params": self.message_params}, locale)["text"] if self.message_key else fallback,
            "message_key": message_key,
            "details": render_for_locale(self.details, locale),
        }


_ROLE_ACTIONS: dict[str, set[str]] = {
    "read": {"PROJECT_OWNER", "PROCUREMENT", "SUPPLIER", "SITE_ADMIN", "INSPECTOR", "AUDITOR"},
    "stock_write": {"PROJECT_OWNER", "PROCUREMENT", "SITE_ADMIN"},
    "stock_reverse": {"PROJECT_OWNER"},
    "purchase_draft": {"PROJECT_OWNER", "PROCUREMENT", "SITE_ADMIN"},
    "purchase_submit": {"PROJECT_OWNER", "PROCUREMENT"},
    "purchase_approve": {"PROJECT_OWNER"},
    "shipment_submit": {"SUPPLIER"},
    "delivery_confirm": {"PROJECT_OWNER", "SITE_ADMIN"},
    "inspection_confirm": {"PROJECT_OWNER", "INSPECTOR"},
    "ppe_issue": {"PROJECT_OWNER", "SITE_ADMIN"},
    "handover": {"PROJECT_OWNER", "PROCUREMENT", "SITE_ADMIN", "SUPPLIER"},
    "evidence_anchor": {"PROJECT_OWNER", "PROCUREMENT", "SUPPLIER", "SITE_ADMIN", "INSPECTOR"},
    "audit": {"PROJECT_OWNER", "AUDITOR"},
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _row(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in rows]


class BusinessService:
    """Transactional business operations backed by :class:`Database`."""

    def __init__(
        self,
        db: Database | None = None,
        *,
        evidence_adapter: Any | None = None,
        settings: Settings | None = None,
        document_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.db = db or Database()
        self.settings = settings or Settings.from_env()
        self.evidence_adapter = evidence_adapter or MockEvidenceAdapter()
        self.document_root = Path(document_root or os.getenv("DOCUMENT_ROOT", "backend/data/documents"))
        # Token records are persisted so restarts cannot make a confirmation
        # token reusable.  This table is additive for databases created by an
        # earlier schema version.
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS confirmation_tokens (
                 token_hash TEXT PRIMARY KEY, scope TEXT NOT NULL, entity_id TEXT NOT NULL,
                 payload_hash TEXT NOT NULL, user_id TEXT, expires_at TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0
               )"""
        )
        if isinstance(self.evidence_adapter, MockEvidenceAdapter):
            # The mock chain is in-memory, while event status/tx_id is
            # persisted in SQLite.  Hydrating it keeps verification stable
            # across an API restart and still exercises the adapter boundary.
            for row in self.db.query_all("SELECT id,tx_id,block_no,payload_hash FROM evidence_events WHERE tx_id IS NOT NULL"):
                self.evidence_adapter.restore(str(row["id"]), EvidenceSubmission(str(row["tx_id"]), row["block_no"], str(row["payload_hash"])))

    # ------------------------------------------------------------------
    # Context, permissions and common persistence helpers
    # ------------------------------------------------------------------
    def _context(self, actor: RequestContext | Mapping[str, Any] | str | None, project_id: str | None = None) -> dict[str, Any]:
        if actor is None:
            return {"request_id": str(uuid.uuid4()), "user_id": "system", "roles": ["PROJECT_OWNER"], "project_id": project_id}
        if isinstance(actor, str):
            actor = {"user_id": actor}
        context = dict(actor)
        context.setdefault("request_id", str(uuid.uuid4()))
        context.setdefault("user_id", "system")
        context.setdefault("roles", [])
        if isinstance(context["roles"], str):
            context["roles"] = [context["roles"]]
        if project_id is not None:
            claimed_project = context.get("project_id")
            if claimed_project not in (None, ""):
                try:
                    claimed_id = self._project(str(claimed_project))["id"]
                    target_id = self._project(str(project_id))["id"]
                except DomainError as exc:
                    raise DomainError("FORBIDDEN", "项目范围不匹配") from exc
                if claimed_id != target_id:
                    raise DomainError("FORBIDDEN", "项目范围不匹配")
            context["project_id"] = project_id
        return context

    def _roles(self, context: Mapping[str, Any]) -> set[str]:
        roles = {str(item.value if hasattr(item, "value") else item) for item in context.get("roles", [])}
        if context.get("user_id") not in {None, "system"}:
            row = self.db.query_one("SELECT role FROM users WHERE id=? OR username=?", (context.get("user_id"), context.get("user_id")))
            if row:
                # RequestContext roles are an optimization hint only.  A
                # caller must never elevate a known user by supplying a
                # different role; the persisted identity is authoritative.
                return {str(row["role"])}
        return roles or {"PROJECT_OWNER"} if context.get("user_id") == "system" else roles

    def _organization(self, context: Mapping[str, Any]) -> str | None:
        if context.get("organization_id"):
            return str(context["organization_id"])
        user_id = context.get("user_id")
        if user_id and user_id != "system":
            row = self.db.query_one("SELECT organization_id FROM users WHERE id=? OR username=?", (user_id, user_id))
            if row:
                return str(row["organization_id"])
        return None

    def _member_scopes(self, context: Mapping[str, Any], project_id: str) -> dict[str, Any]:
        """Return server-owned project scopes for a request context.

        Scope data is never accepted from the browser.  The helper is kept
        separate from ``authorize`` so read projections can apply the same
        warehouse/area restrictions without duplicating identity lookups.
        """

        user_id = context.get("user_id")
        if not user_id or user_id == "system":
            return {}
        project = self._project(project_id)
        row = self.db.query_one("SELECT scopes_json FROM project_members WHERE project_id=? AND user_id=?", (project["id"], user_id))
        return _loads(row["scopes_json"], {}) if row and row["scopes_json"] else {}

    @staticmethod
    def _scope_list(scopes: Mapping[str, Any], key: str) -> list[str]:
        value = scopes.get(key)
        if value is None:
            value = scopes.get(key.rstrip("s"))
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value]
        return [str(value)] if value else []

    def _project(self, project_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM projects WHERE id=? OR code=?", (project_id, project_id))
        if not row:
            raise DomainError("PROJECT_NOT_FOUND", "项目不存在")
        return dict(row)

    def authorize(self, user: RequestContext | Mapping[str, Any] | str | None, project_id: str, action: str, resource: Mapping[str, Any] | None = None) -> dict[str, Any]:
        context = self._context(user, project_id)
        try:
            project = self._project(project_id)
        except DomainError:
            raise
        roles = self._roles(context)
        allowed_roles = _ROLE_ACTIONS.get(action, _ROLE_ACTIONS["read"])
        if not roles.intersection(allowed_roles):
            raise DomainError("FORBIDDEN", "无权执行该操作")
        if context.get("user_id") not in {None, "system"}:
            member = self.db.query_one(
                "SELECT role,scopes_json FROM project_members WHERE project_id=? AND "
                "(user_id=? OR user_id=(SELECT id FROM users WHERE username=?))",
                (project["id"], context["user_id"], context["user_id"]),
            )
            if not member:
                raise DomainError("FORBIDDEN", "无权访问该项目")
            scopes = _loads(member["scopes_json"], {}) or {}
            if resource and scopes:
                for scope_key in ("warehouse_id", "area_id", "building_id", "supplier_id"):
                    permitted = scopes.get(scope_key + "s") or scopes.get(scope_key)
                    if permitted and resource.get(scope_key) not in permitted:
                        raise DomainError("FORBIDDEN", "无权访问该资源")
        return {"allowed": True, "project_id": project["id"], "user_id": context.get("user_id"), "roles": sorted(roles)}

    def get_project_context(self, user: RequestContext | Mapping[str, Any] | str | None, project_id: str) -> dict[str, Any]:
        self.authorize(user, project_id, "read")
        project = self._project(project_id)
        return {"project": project, "timezone": project.get("timezone") or self.settings.app_timezone, "organization_id": self._organization(self._context(user, project_id))}

    def _payload_hash(self, payload: Any) -> str:
        try:
            return hash_payload(payload)
        except (TypeError, ValueError):
            return hashlib.sha256(_json(payload).encode()).hexdigest()

    def _idempotent_lookup(self, conn: Any, scope: str, key: str | None, payload: Any) -> dict[str, Any] | None:
        if not key:
            return None
        digest = self._payload_hash(payload)
        found = conn.execute("SELECT payload_hash,result_json FROM idempotencies WHERE scope=? AND idempotency_key=?", (scope, str(key))).fetchone()
        if not found:
            return None
        if found["payload_hash"] != digest:
            raise DomainError("DUPLICATE_IDEMPOTENCY", "幂等键已用于其他请求")
        return _loads(found["result_json"], {})

    def _idempotent_store(self, conn: Any, scope: str, key: str | None, payload: Any, result: Mapping[str, Any]) -> None:
        if not key:
            return
        conn.execute("INSERT INTO idempotencies(scope,idempotency_key,payload_hash,result_json,created_at) VALUES(?,?,?,?,?)", (scope, str(key), self._payload_hash(payload), _json(result), iso_utc(utc_now())))

    def _audit(self, conn: Any, project_id: str, actor: Mapping[str, Any] | None, action: str, entity_type: str, entity_id: str, before: Any = None, after: Any = None) -> None:
        context = self._context(actor, project_id)
        conn.execute("INSERT INTO audit_logs(id,project_id,actor_type,actor_id,action,entity_type,entity_id,before_json,after_json,request_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), self._project(project_id)["id"], "USER", context.get("user_id"), action, entity_type, entity_id, _json(before) if before is not None else None, _json(after) if after is not None else None, context["request_id"], iso_utc(context.get("now_utc") or utc_now())))

    def _next_no(self, conn: Any, table: str, prefix: str, column: str = "move_no") -> str:
        today = utc_now().strftime("%Y%m%d")
        count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE ?", (f"{prefix}-{today}-%",)).fetchone()[0] + 1
        return f"{prefix}-{today}-{count:04d}"

    def _material(self, project_id: str, material_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        row = self.db.query_one("SELECT * FROM materials WHERE id=? AND project_id=?", (material_id, project["id"]))
        if not row:
            raise DomainError("MATERIAL_NOT_FOUND", "物料不存在")
        return dict(row)

    def _warehouse(self, project_id: str, warehouse_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        row = self.db.query_one("SELECT * FROM warehouses WHERE id=? AND project_id=?", (warehouse_id, project["id"]))
        if not row:
            raise DomainError("FORBIDDEN", "仓库不属于当前项目")
        return dict(row)

    def _authorize_warehouse(
        self,
        context: RequestContext | Mapping[str, Any] | str | None,
        project_id: str,
        action: str,
        warehouse_id: str,
    ) -> dict[str, Any]:
        """Authorize an operation against the warehouse's server-owned scope.

        Warehouse scopes are not enough on their own: a member may instead be
        restricted by the warehouse's area.  Resolve the warehouse first, then
        pass both identifiers to ``authorize`` so callers cannot choose an
        unrelated area in a command payload.
        """

        if not warehouse_id or str(warehouse_id) == "None":
            raise DomainError("VALIDATION_ERROR", "仓库必填")
        warehouse = self._warehouse(project_id, warehouse_id)
        self.authorize(
            context,
            project_id,
            action,
            {"warehouse_id": warehouse["id"], "area_id": warehouse.get("area_id")},
        )
        return warehouse

    def _validate_unit(self, material: Mapping[str, Any], unit: str) -> None:
        if str(material["unit"]) != str(unit):
            raise DomainError("UNIT_MISMATCH", "物料单位不匹配", {"expected": material["unit"], "received": unit})

    # ------------------------------------------------------------------
    # Inventory and deterministic query functions
    # ------------------------------------------------------------------
    def _balance_row(self, conn: Any, warehouse_id: str, material_id: str, batch_id: str | None, *, create: bool = False) -> Any:
        batch_key = batch_id or ""
        row = conn.execute("SELECT * FROM stock_balances WHERE warehouse_id=? AND material_id=? AND batch_key=?", (warehouse_id, material_id, batch_key)).fetchone()
        if not row and create:
            balance_id = str(uuid.uuid4())
            conn.execute("INSERT INTO stock_balances(id,warehouse_id,material_id,batch_id,batch_key,qty_on_hand,qty_reserved,qty_quarantined,version) VALUES(?,?,?,?,?,?,?,?,?)", (balance_id, warehouse_id, material_id, batch_id, batch_key, "0", "0", "0", 1))
            row = conn.execute("SELECT * FROM stock_balances WHERE id=?", (balance_id,)).fetchone()
        return row

    def _change_balance(self, conn: Any, warehouse_id: str, material_id: str, batch_id: str | None, delta: Decimal, *, quarantine_delta: Decimal = Decimal("0"), expected_version: int | None = None) -> dict[str, Any]:
        row = self._balance_row(conn, warehouse_id, material_id, batch_id, create=True)
        on_hand = parse_decimal(row["qty_on_hand"])
        reserved = parse_decimal(row["qty_reserved"])
        quarantined = parse_decimal(row["qty_quarantined"])
        if expected_version is not None and int(row["version"]) != int(expected_version):
            raise DomainError("VERSION_CONFLICT", "库存版本已变化")
        next_on_hand = on_hand + delta
        next_quarantined = quarantined + quarantine_delta
        if next_on_hand < 0 or next_quarantined < 0 or next_quarantined > next_on_hand:
            raise DomainError("INSUFFICIENT_STOCK", "库存可用量不足")
        available = next_on_hand - reserved - next_quarantined
        if available < 0:
            raise DomainError("INSUFFICIENT_STOCK", "库存可用量不足")
        next_version = int(row["version"]) + 1
        updated = conn.execute("UPDATE stock_balances SET qty_on_hand=?,qty_quarantined=?,version=? WHERE id=? AND version=?", (decimal_string(next_on_hand), decimal_string(next_quarantined), next_version, row["id"], row["version"]))
        if updated.rowcount != 1:
            raise DomainError("VERSION_CONFLICT", "库存版本已变化")
        return {"warehouse_id": warehouse_id, "material_id": material_id, "batch_id": batch_id, "qty_on_hand": decimal_string(next_on_hand), "qty_reserved": decimal_string(reserved), "qty_quarantined": decimal_string(next_quarantined), "available": decimal_string(available), "version": next_version}

    def validate_stock_move_command(self, cmd: Mapping[str, Any]) -> dict[str, Any]:
        required = {"project_id", "material_id", "move_type", "qty", "unit", "reason", "idempotency_key"}
        unknown = set(cmd) - (required | {"batch_id", "from_warehouse_id", "to_warehouse_id", "crew_id", "plan_id", "reference_type", "reference_id", "occurred_at", "expected_version", "correlation_id"})
        if unknown:
            raise DomainError("VALIDATION_ERROR", "请求包含未知字段", {"fields": sorted(unknown)})
        move_type = str(cmd.get("move_type"))
        try:
            move_type = StockMoveType(move_type).value
        except ValueError as exc:
            raise DomainError("VALIDATION_ERROR", "无效库存流水类型") from exc
        qty = parse_decimal(cmd.get("qty"), field="qty")
        if qty <= 0:
            raise DomainError("VALIDATION_ERROR", "数量必须大于 0")
        from_wh, to_wh = cmd.get("from_warehouse_id"), cmd.get("to_warehouse_id")
        matrix = {
            "RECEIPT": (False, True), "ISSUE": (True, False), "RETURN": (False, True),
            "TRANSFER_OUT": (True, False), "TRANSFER_IN": (False, True), "SCRAP": (True, False), "ADJUSTMENT": (None, None),
        }
        expected_from, expected_to = matrix[move_type]
        if expected_from is True and not from_wh or expected_from is False and from_wh:
            raise DomainError("VALIDATION_ERROR", "来源仓库字段与流水类型不匹配")
        if expected_to is True and not to_wh or expected_to is False and to_wh:
            raise DomainError("VALIDATION_ERROR", "目标仓库字段与流水类型不匹配")
        if move_type == "RECEIPT" and str(cmd.get("reference_type")) != "INSPECTION":
            raise DomainError("VALIDATION_ERROR", "入库必须引用验收记录")
        if move_type == "RETURN" and (str(cmd.get("reference_type")) not in {"ISSUE", "REVERSAL"} or not cmd.get("reference_id")):
            raise DomainError("VALIDATION_ERROR", "退库必须引用原领用记录或报废更正")
        if move_type == "SCRAP" and not str(cmd.get("reason", "")).strip():
            raise DomainError("VALIDATION_ERROR", "报废必须填写原因")
        if move_type in {"TRANSFER_OUT", "TRANSFER_IN"}:
            raise DomainError("VALIDATION_ERROR", "调拨请调用 transfer_material")
        return {"move_type": move_type, "qty": qty}

    def _create_move_tx(self, conn: Any, cmd: Mapping[str, Any], actor: Mapping[str, Any], *, skip_idempotency: bool = False) -> dict[str, Any]:
        validated = self.validate_stock_move_command(cmd)
        project = self._project(str(cmd["project_id"]))
        material = self._material(project["id"], str(cmd["material_id"]))
        self._validate_unit(material, str(cmd["unit"]))
        move_type, qty = validated["move_type"], validated["qty"]
        batch_id = cmd.get("batch_id")
        if batch_id:
            batch = conn.execute("SELECT * FROM batches WHERE id=? AND material_id=?", (batch_id, material["id"])).fetchone()
            if not batch:
                raise DomainError("VALIDATION_ERROR", "批次不属于该物料")
            if move_type == "RECEIPT" and batch["status"] != BatchStatus.INSPECTED.value:
                raise DomainError("VALIDATION_ERROR", "只有验收通过批次才能入库")
            if move_type == "ISSUE" and batch["status"] in {BatchStatus.QUARANTINED.value, BatchStatus.REJECTED.value}:
                raise DomainError("INSUFFICIENT_STOCK", "隔离或拒收批次不可领用")
        from_wh, to_wh = cmd.get("from_warehouse_id"), cmd.get("to_warehouse_id")
        for warehouse in (from_wh, to_wh):
            if warehouse:
                self._warehouse(project["id"], str(warehouse))
        if move_type == "RETURN":
            reference_type = str(cmd.get("reference_type"))
            original_types = ("ISSUE",) if reference_type == "ISSUE" else ("SCRAP",)
            original = conn.execute(
                "SELECT * FROM stock_moves WHERE id=? AND project_id=? AND move_type IN ("
                + ",".join("?" * len(original_types))
                + ")",
                (cmd.get("reference_id"), project["id"], *original_types),
            ).fetchone()
            if not original or original["material_id"] != material["id"] or original["from_warehouse_id"] != to_wh:
                raise DomainError("VALIDATION_ERROR", "退库引用的原流水不匹配")
            returned = conn.execute(
                "SELECT COALESCE(SUM(CAST(qty AS REAL)),0) AS qty FROM stock_moves WHERE project_id=? AND move_type='RETURN' AND reference_type=? AND reference_id=?",
                (project["id"], reference_type, original["id"]),
            ).fetchone()["qty"]
            if Decimal(str(returned or 0)) + qty > parse_decimal(original["qty"]):
                raise DomainError("VALIDATION_ERROR", "退库数量超过原流水未更正数量")
        balance_result = None
        expected_version = cmd.get("expected_version")
        if expected_version is not None:
            expected_version = int(expected_version)
        if move_type in {"ISSUE", "SCRAP"}:
            balance_result = self._change_balance(conn, str(from_wh), material["id"], batch_id, -qty, expected_version=expected_version)
        elif move_type in {"RECEIPT", "RETURN"}:
            balance_result = self._change_balance(conn, str(to_wh), material["id"], batch_id, qty, expected_version=expected_version)
        elif move_type == "ADJUSTMENT":
            if from_wh and not to_wh:
                balance_result = self._change_balance(conn, str(from_wh), material["id"], batch_id, -qty, expected_version=expected_version)
            elif to_wh and not from_wh:
                balance_result = self._change_balance(conn, str(to_wh), material["id"], batch_id, qty, expected_version=expected_version)
            else:
                raise DomainError("VALIDATION_ERROR", "盘点调整必须指定差异方向")
        move_id = str(uuid.uuid4())
        move_no = self._next_no(conn, "stock_moves", "SM")
        occurred = iso_utc(cmd.get("occurred_at") or utc_now())
        conn.execute("INSERT INTO stock_moves(id,project_id,move_no,move_type,material_id,batch_id,from_warehouse_id,to_warehouse_id,qty,unit,reason,reference_type,reference_id,actor_id,occurred_at,idempotency_key,reversed_move_id,correlation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (move_id, project["id"], move_no, move_type, material["id"], batch_id, from_wh, to_wh, decimal_string(qty), material["unit"], str(cmd["reason"]), cmd.get("reference_type"), cmd.get("reference_id"), actor.get("user_id"), occurred, str(cmd["idempotency_key"]), None, cmd.get("correlation_id")))
        # Keep the deterministic daily consumption cache as a derived view.
        if move_type in {"ISSUE", "RETURN", "SCRAP"}:
            day = occurred[:10]
            issued = qty if move_type == "ISSUE" else Decimal("0")
            returned = qty if move_type == "RETURN" else Decimal("0")
            scrapped = qty if move_type == "SCRAP" else Decimal("0")
            existing = conn.execute("SELECT * FROM consumption_snapshots WHERE material_id=? AND area_id IS NULL AND date=?", (material["id"], day)).fetchone()
            if existing:
                conn.execute("UPDATE consumption_snapshots SET issued_qty=?,returned_qty=?,scrapped_qty=?,net_consumed_qty=? WHERE id=?", (decimal_string(parse_decimal(existing["issued_qty"]) + issued), decimal_string(parse_decimal(existing["returned_qty"]) + returned), decimal_string(parse_decimal(existing["scrapped_qty"]) + scrapped), decimal_string(parse_decimal(existing["net_consumed_qty"]) + issued - returned + scrapped), existing["id"]))
            else:
                conn.execute("INSERT INTO consumption_snapshots(id,project_id,material_id,date,issued_qty,returned_qty,scrapped_qty,net_consumed_qty) VALUES(?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), project["id"], material["id"], day, decimal_string(issued), decimal_string(returned), decimal_string(scrapped), decimal_string(issued - returned + scrapped)))
        result = {"id": move_id, "move_no": move_no, "project_id": project["id"], "material_id": material["id"], "batch_id": batch_id, "move_type": move_type, "qty": decimal_string(qty), "unit": material["unit"], "from_warehouse_id": from_wh, "to_warehouse_id": to_wh, "occurred_at": occurred, "balance": balance_result}
        self._audit(conn, project["id"], actor, "CREATE_STOCK_MOVE", "stock_move", move_id, after=result)
        return result

    def create_stock_move(self, cmd: Mapping[str, Any], actor: RequestContext | Mapping[str, Any] | str | None = None) -> dict[str, Any]:
        context = self._context(actor, str(cmd.get("project_id")))
        warehouse_id = cmd.get("from_warehouse_id") or cmd.get("to_warehouse_id")
        if warehouse_id:
            self._authorize_warehouse(context, str(cmd["project_id"]), "stock_write", str(warehouse_id))
        else:
            self.authorize(context, str(cmd["project_id"]), "stock_write")
        with self.db.transaction() as conn:
            previous = self._idempotent_lookup(conn, "stock_move", cmd.get("idempotency_key"), cmd)
            if previous is not None:
                return previous
            result = self._create_move_tx(conn, cmd, context)
            self._idempotent_store(conn, "stock_move", cmd.get("idempotency_key"), cmd, result)
            return result

    def transfer_material(self, cmd: Mapping[str, Any], actor: RequestContext | Mapping[str, Any] | str | None = None) -> dict[str, Any]:
        context = self._context(actor, str(cmd.get("project_id")))
        project_id = self._project(str(cmd["project_id"]))["id"]
        qty = parse_decimal(cmd.get("qty"), field="qty")
        if qty <= 0 or not cmd.get("from_warehouse_id") or not cmd.get("to_warehouse_id") or cmd["from_warehouse_id"] == cmd["to_warehouse_id"]:
            raise DomainError("VALIDATION_ERROR", "调拨需要不同的来源和目标仓库")
        # A transfer changes both balances.  A scoped site administrator must
        # be authorized for both ends before any transactional work begins.
        self._authorize_warehouse(context, project_id, "stock_write", str(cmd["from_warehouse_id"]))
        self._authorize_warehouse(context, project_id, "stock_write", str(cmd["to_warehouse_id"]))
        key = str(cmd.get("idempotency_key") or "")
        with self.db.transaction() as conn:
            previous = self._idempotent_lookup(conn, "transfer", key, cmd)
            if previous is not None:
                return previous
            correlation = str(uuid.uuid4())
            base = dict(cmd)
            base.update({"move_type": "TRANSFER_OUT", "to_warehouse_id": None, "correlation_id": correlation, "idempotency_key": key + ":out"})
            # TRANSFER_OUT/IN are internal calls; bypass public rejection while
            # retaining matrix and balance validation.
            material = self._material(project_id, str(cmd["material_id"]))
            self._validate_unit(material, str(cmd["unit"]))
            batch_id = cmd.get("batch_id")
            out_balance = self._change_balance(conn, str(cmd["from_warehouse_id"]), material["id"], batch_id, -qty)
            in_balance = self._change_balance(conn, str(cmd["to_warehouse_id"]), material["id"], batch_id, qty)
            ids = []
            for move_type, from_wh, to_wh, suffix in (("TRANSFER_OUT", cmd["from_warehouse_id"], None, "out"), ("TRANSFER_IN", None, cmd["to_warehouse_id"], "in")):
                move_id, move_no = str(uuid.uuid4()), self._next_no(conn, "stock_moves", "SM")
                conn.execute("INSERT INTO stock_moves(id,project_id,move_no,move_type,material_id,batch_id,from_warehouse_id,to_warehouse_id,qty,unit,reason,reference_type,reference_id,actor_id,occurred_at,idempotency_key,correlation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (move_id, project_id, move_no, move_type, material["id"], batch_id, from_wh, to_wh, decimal_string(qty), material["unit"], str(cmd.get("reason", "仓库调拨")), "TRANSFER", correlation, context.get("user_id"), iso_utc(cmd.get("occurred_at") or utc_now()), key + ":" + suffix, correlation))
                ids.append({"id": move_id, "move_no": move_no, "move_type": move_type})
            result = {"correlation_id": correlation, "moves": ids, "from_balance": out_balance, "to_balance": in_balance}
            self._audit(conn, project_id, context, "TRANSFER_STOCK", "stock_transfer", correlation, after=result)
            self._idempotent_store(conn, "transfer", key, cmd, result)
            return result

    def reverse_stock_move(self, move_id: str, reason: str, actor: RequestContext | Mapping[str, Any] | str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        original = self.db.query_one("SELECT * FROM stock_moves WHERE id=? OR move_no=?", (move_id, move_id))
        if not original:
            raise DomainError("NOT_FOUND", "库存流水不存在")
        context = self._context(actor, original["project_id"])
        self.authorize(context, original["project_id"], "stock_reverse")
        key = idempotency_key or str(uuid.uuid4())
        with self.db.transaction() as conn:
            previous = self._idempotent_lookup(conn, "reverse_stock_move", key, {"move_id": move_id, "reason": reason})
            if previous is not None:
                return previous
            row = conn.execute("SELECT * FROM stock_moves WHERE id=?", (original["id"],)).fetchone()
            if row["reversed_move_id"]:
                raise DomainError("INVALID_STATE", "该流水已更正")
            if row["move_type"] in {"TRANSFER_OUT", "TRANSFER_IN"}:
                raise DomainError("INVALID_STATE", "调拨流水必须通过关联调拨更正")
            inverse = {"project_id": row["project_id"], "material_id": row["material_id"], "batch_id": row["batch_id"], "qty": row["qty"], "unit": row["unit"], "reason": reason, "reference_type": "REVERSAL", "reference_id": row["id"], "occurred_at": utc_now(), "idempotency_key": key}
            if row["move_type"] in {"RECEIPT", "RETURN"}:
                inverse.update({"move_type": "SCRAP", "from_warehouse_id": row["to_warehouse_id"], "to_warehouse_id": None})
            elif row["move_type"] in {"ISSUE", "SCRAP"}:
                inverse.update({"move_type": "RETURN", "from_warehouse_id": None, "to_warehouse_id": row["from_warehouse_id"]})
                if row["move_type"] == "ISSUE":
                    inverse["reference_type"] = "ISSUE"
            elif row["move_type"] == "ADJUSTMENT":
                if row["from_warehouse_id"]:
                    inverse.update({"move_type": "ADJUSTMENT", "from_warehouse_id": None, "to_warehouse_id": row["from_warehouse_id"]})
                else:
                    inverse.update({"move_type": "ADJUSTMENT", "from_warehouse_id": row["to_warehouse_id"], "to_warehouse_id": None})
            result = self._create_move_tx(conn, inverse, context)
            conn.execute("UPDATE stock_moves SET reversed_move_id=? WHERE id=?", (result["id"], row["id"]))
            result["reverses_move_id"] = row["id"]
            self._idempotent_store(conn, "reverse_stock_move", key, {"move_id": move_id, "reason": reason}, result)
            return result

    def _stocktake_snapshot(
        self,
        project_id: str,
        warehouse_id: str,
        material_id: str,
        batch_id: str | None,
    ) -> tuple[str, dict[str, Any], str | None, Any, Decimal, int]:
        """Resolve the exact balance row a stocktake is allowed to adjust."""

        pid = self._project(project_id)["id"]
        self._warehouse(pid, warehouse_id)
        material = self._material(pid, material_id)
        if batch_id:
            batch = self.db.query_one(
                "SELECT b.id FROM batches b JOIN materials m ON m.id=b.material_id WHERE b.id=? AND b.material_id=? AND m.project_id=?",
                (batch_id, material["id"], pid),
            )
            if not batch:
                raise DomainError("VALIDATION_ERROR", "批次不属于该物料")
        if batch_id is None:
            candidates = self.db.query_all(
                "SELECT batch_id FROM stock_balances WHERE warehouse_id=? AND material_id=?",
                (warehouse_id, material["id"]),
            )
            if len(candidates) == 1:
                batch_id = candidates[0]["batch_id"]
            elif len(candidates) > 1:
                raise DomainError("VALIDATION_ERROR", "该物料存在多个批次，盘点必须指定批次")
        current = self.db.query_one(
            "SELECT * FROM stock_balances WHERE warehouse_id=? AND material_id=? AND batch_key=?",
            (warehouse_id, material["id"], batch_id or ""),
        )
        current_qty = parse_decimal(current["qty_on_hand"]) if current else Decimal("0")
        current_version = int(current["version"]) if current else 0
        return pid, material, batch_id, current, current_qty, current_version

    @staticmethod
    def _stocktake_token_subject(project_id: str, warehouse_id: str, material_id: str, batch_id: str | None) -> str:
        return f"{project_id}:{warehouse_id}:{material_id}:{batch_id or ''}"

    def issue_stocktake_confirmation_token(
        self,
        project_id: str,
        warehouse_id: str,
        material_id: str,
        counted_qty: Any,
        reason: str,
        actor: Any = None,
        batch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Issue a one-time owner token for a high-difference stocktake."""

        context = self._context(actor, project_id)
        self._authorize_warehouse(context, project_id, "stock_write", warehouse_id)
        if "PROJECT_OWNER" not in self._roles(context):
            raise DomainError("FORBIDDEN", "只有项目负责人可以确认大差异盘点")
        counted = parse_decimal(counted_qty, field="counted_qty")
        if counted < 0:
            raise DomainError("VALIDATION_ERROR", "盘点数量不能为负")
        pid, _material, resolved_batch, current, current_qty, current_version = self._stocktake_snapshot(project_id, warehouse_id, material_id, batch_id)
        if not current_qty or abs(counted - current_qty) / current_qty <= self.settings.max_adjustment_ratio:
            raise DomainError("INVALID_STATE", "当前盘点差异无需负责人确认")
        reason_text = str(reason or "").strip()
        payload = {
            "project_id": pid,
            "warehouse_id": str(warehouse_id),
            "material_id": str(material_id),
            "batch_id": resolved_batch or "",
            "current_version": current_version,
            "counted_qty": decimal_string(counted),
            "reason": reason_text,
        }
        subject = self._stocktake_token_subject(pid, warehouse_id, material_id, resolved_batch)
        token_result = self._issue_confirmation_token_idempotently("stocktake_adjustment", subject, payload, context.get("user_id"), idempotency_key)
        return {"project_id": pid, "warehouse_id": warehouse_id, "material_id": material_id, "batch_id": resolved_batch, **token_result, "payload_hash": self._payload_hash(payload)}

    def reconcile_stock(self, project_id: str, warehouse_id: str, material_id: str, counted_qty: Any, reason: str, actor: RequestContext | Mapping[str, Any] | str | None = None, confirmation_token: str | None = None, batch_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        context = self._context(actor, project_id)
        self._authorize_warehouse(context, project_id, "stock_write", warehouse_id)
        counted = parse_decimal(counted_qty, field="counted_qty")
        if counted < 0:
            raise DomainError("VALIDATION_ERROR", "盘点数量不能为负")
        pid, material, resolved_batch, current, current_qty, current_version = self._stocktake_snapshot(project_id, warehouse_id, material_id, batch_id)
        reason_text = str(reason or "").strip()
        key = str(idempotency_key or "")
        command = {
            "project_id": pid,
            "warehouse_id": str(warehouse_id),
            "material_id": str(material["id"]),
            "batch_id": resolved_batch or "",
            "counted_qty": decimal_string(counted),
            "reason": reason_text,
            "confirmation_token_hash": hashlib.sha256(str(confirmation_token or "").encode()).hexdigest(),
        }
        if key:
            with self.db.transaction() as conn:
                previous = self._idempotent_lookup(conn, "stocktake", key, command)
                if previous is not None:
                    return previous
        requires_confirmation = bool(current_qty and abs(counted - current_qty) / current_qty > self.settings.max_adjustment_ratio)
        if requires_confirmation and not confirmation_token:
            raise DomainError(
                "VERSION_CONFLICT",
                "盘点差异超过阈值，需要负责人确认",
                {
                    "requires_confirmation": True,
                    "confirmation_scope": "stocktake_adjustment",
                    "entity_id": self._stocktake_token_subject(pid, warehouse_id, material["id"], resolved_batch),
                    "current_version": current_version,
                },
            )
        if requires_confirmation and "PROJECT_OWNER" not in self._roles(context):
            raise DomainError("FORBIDDEN", "大差异盘点必须由项目负责人确认")
        delta = counted - current_qty
        if delta == 0:
            result: dict[str, Any] = {"ok": True, "unchanged": True, "qty_on_hand": decimal_string(current_qty), "version": current_version}
            if key:
                with self.db.transaction() as conn:
                    self._idempotent_store(conn, "stocktake", key, command, result)
            return result
        move_cmd = {
            "project_id": pid,
            "material_id": material["id"],
            "batch_id": resolved_batch,
            "move_type": "ADJUSTMENT",
            "qty": abs(delta),
            "unit": material["unit"],
            "from_warehouse_id": warehouse_id if delta < 0 else None,
            "to_warehouse_id": warehouse_id if delta > 0 else None,
            "reason": reason_text,
            "reference_type": "STOCKTAKE",
            "reference_id": warehouse_id,
            "expected_version": current_version if current is not None else None,
            "idempotency_key": key or str(uuid.uuid4()),
        }
        if requires_confirmation:
            subject = self._stocktake_token_subject(pid, warehouse_id, material["id"], resolved_batch)
            payload = {
                "project_id": pid,
                "warehouse_id": str(warehouse_id),
                "material_id": str(material["id"]),
                "batch_id": resolved_batch or "",
                "current_version": current_version,
                "counted_qty": decimal_string(counted),
                "reason": reason_text,
            }
            with self.db.transaction() as conn:
                previous = self._idempotent_lookup(conn, "stocktake", key, command) if key else None
                if previous is not None:
                    return previous
                latest = conn.execute(
                    "SELECT * FROM stock_balances WHERE warehouse_id=? AND material_id=? AND batch_key=?",
                    (warehouse_id, material["id"], resolved_batch or ""),
                ).fetchone()
                latest_version = int(latest["version"]) if latest else 0
                latest_qty = parse_decimal(latest["qty_on_hand"]) if latest else Decimal("0")
                if latest_version != current_version or latest_qty != current_qty:
                    raise DomainError("VERSION_CONFLICT", "库存账面已变化，请重新确认盘点")
                self._consume_token(conn, confirmation_token, "stocktake_adjustment", subject, payload, context.get("user_id"))
                result = self.create_stock_move(move_cmd, context)
                if key:
                    self._idempotent_store(conn, "stocktake", key, command, result)
                return result
        result = self.create_stock_move(move_cmd, context)
        if key:
            with self.db.transaction() as conn:
                previous = self._idempotent_lookup(conn, "stocktake", key, command)
                if previous is not None:
                    return previous
                self._idempotent_store(conn, "stocktake", key, command, result)
        return result

    def get_stock(self, project_id: str, material_ids: list[str] | None = None, warehouse_ids: list[str] | None = None, batch_ids: list[str] | None = None, include_quarantined: bool = False, actor: RequestContext | Mapping[str, Any] | str | None = None) -> dict[str, Any]:
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        project = self._project(project_id)
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            return {"items": [], "data_completeness": 0.0, "source_refs": ["stock_balances"], "warnings": ["供应商视角仅开放自身订单与发货数据"]}
        clauses, params = ["w.project_id=?"], [project["id"]]
        scopes = self._member_scopes(context, project["id"])
        scoped_warehouses = self._scope_list(scopes, "warehouse_ids")
        if scoped_warehouses:
            clauses.append("s.warehouse_id IN (" + ",".join("?" * len(scoped_warehouses)) + ")")
            params.extend(scoped_warehouses)
        if material_ids:
            clauses.append("s.material_id IN (" + ",".join("?" * len(material_ids)) + ")")
            params.extend(material_ids)
        if warehouse_ids:
            clauses.append("s.warehouse_id IN (" + ",".join("?" * len(warehouse_ids)) + ")")
            params.extend(warehouse_ids)
        if batch_ids:
            clauses.append("s.batch_id IN (" + ",".join("?" * len(batch_ids)) + ")")
            params.extend(batch_ids)
        rows = _rows(self.db.query_all("SELECT s.*,m.sku,m.name,m.category,m.specification,m.unit,m.safety_stock,m.reorder_point,m.lead_time_days,w.code AS warehouse_code,w.name AS warehouse_name,b.batch_no,b.status AS batch_status,(SELECT MAX(sm.occurred_at) FROM stock_moves sm WHERE sm.project_id=w.project_id AND sm.material_id=s.material_id AND (sm.batch_id=s.batch_id OR (sm.batch_id IS NULL AND s.batch_id IS NULL)) AND (sm.from_warehouse_id=s.warehouse_id OR sm.to_warehouse_id=s.warehouse_id)) AS updated_at FROM stock_balances s JOIN warehouses w ON w.id=s.warehouse_id JOIN materials m ON m.id=s.material_id LEFT JOIN batches b ON b.id=s.batch_id WHERE " + " AND ".join(clauses) + " ORDER BY m.sku,w.code,b.batch_no", tuple(params)))
        for item in rows:
            item["available"] = decimal_string(parse_decimal(item["qty_on_hand"]) - parse_decimal(item["qty_reserved"]) - parse_decimal(item["qty_quarantined"]))
            if not include_quarantined:
                item.pop("qty_quarantined", None)
        return {"items": rows, "data_completeness": 1.0, "source_refs": ["stock_balances"]}

    def resolve_material(self, project_id: str, query: str, unit: str | None = None, specification: str | None = None, actor: Any = None) -> list[dict[str, Any]]:
        self.authorize(actor, project_id, "read")
        project = self._project(project_id)
        like = f"%{query}%"
        rows = _rows(self.db.query_all("SELECT DISTINCT m.* FROM materials m LEFT JOIN material_aliases a ON a.material_id=m.id WHERE m.project_id=? AND (m.name LIKE ? OR m.sku LIKE ? OR a.alias LIKE ?) ORDER BY m.sku", (project["id"], like, like, like)))
        if unit:
            rows = [item for item in rows if item["unit"] == unit]
        if specification:
            rows = [item for item in rows if specification.lower() in str(item.get("specification") or "").lower()]
        return rows

    def get_material_plan(self, project_id: str, material_ids: list[str], date_from: Any, date_to: Any, area_ids: list[str] | None = None, actor: Any = None) -> dict[str, Any]:
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        project = self._project(project_id)
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            return {"items": [], "data_completeness": 0.0, "source_refs": ["material_plans"], "warnings": ["供应商视角不开放施工计划"]}
        clauses, params = ["project_id=?", "plan_date>=?", "plan_date<=?"], [project["id"], str(date_from)[:10], str(date_to)[:10]]
        scopes = self._member_scopes(self._context(actor, project_id), project["id"])
        scoped_areas = self._scope_list(scopes, "area_ids")
        if scoped_areas:
            clauses.append("area_id IN (" + ",".join("?" * len(scoped_areas)) + ")")
            params.extend(scoped_areas)
        if material_ids:
            clauses.append("material_id IN (" + ",".join("?" * len(material_ids)) + ")"); params.extend(material_ids)
        if area_ids:
            clauses.append("area_id IN (" + ",".join("?" * len(area_ids)) + ")"); params.extend(area_ids)
        rows = _rows(self.db.query_all("SELECT * FROM material_plans WHERE " + " AND ".join(clauses) + " ORDER BY plan_date", tuple(params)))
        for item in rows:
            item["planned_qty"] = decimal_string(item["planned_qty"]); item["consumed_qty_cached"] = decimal_string(item["consumed_qty_cached"])
        return {"items": rows, "data_completeness": 1.0 if rows else 0.0, "source_refs": ["material_plans"]}

    def get_purchase_orders(self, project_id: str, statuses: list[str] | None = None, material_ids: list[str] | None = None, date_from: Any = None, date_to: Any = None, actor: Any = None) -> dict[str, Any]:
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        project = self._project(project_id)
        clauses, params = ["o.project_id=?"], [project["id"]]
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            organization_id = self._organization(context)
            clauses.append("o.supplier_id IN (SELECT id FROM suppliers WHERE organization_id=?)")
            params.append(organization_id or "")
        if statuses:
            clauses.append("o.status IN (" + ",".join("?" * len(statuses)) + ")"); params.extend(statuses)
        if date_from:
            clauses.append("o.order_date>=?"); params.append(str(date_from)[:10])
        if date_to:
            clauses.append("o.order_date<=?"); params.append(str(date_to)[:10])
        rows = _rows(self.db.query_all("SELECT o.*,s.code AS supplier_code,s.name AS supplier_name FROM purchase_orders o JOIN suppliers s ON s.id=o.supplier_id WHERE " + " AND ".join(clauses) + " ORDER BY o.order_date DESC", tuple(params)))
        if material_ids:
            filtered = []
            for item in rows:
                if self.db.query_one("SELECT 1 FROM purchase_order_items WHERE order_id=? AND material_id IN (" + ",".join("?" * len(material_ids)) + ")", tuple([item["id"], *material_ids])):
                    filtered.append(item)
            rows = filtered
        for order in rows:
            order["items"] = _rows(
                self.db.query_all(
                    "SELECT poi.*,m.sku,m.name AS material_name,m.category,m.specification FROM purchase_order_items poi JOIN materials m ON m.id=poi.material_id WHERE poi.order_id=? ORDER BY poi.id",
                    (order["id"],),
                )
            )
            for item in order["items"]:
                for field in ("qty_ordered", "qty_shipped", "qty_received", "unit_price"):
                    if item.get(field) is not None:
                        item[field] = decimal_string(item[field])
        return {"items": rows, "data_completeness": 1.0 if rows else 0.0, "source_refs": ["purchase_orders"]}

    def get_batch_trace(self, project_id: str, batch_id: str | None = None, business_document_id: str | None = None, actor: Any = None) -> dict[str, Any]:
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        project = self._project(project_id)
        if batch_id:
            batch = self.db.query_one(
                "SELECT m.project_id FROM batches b JOIN materials m ON m.id=b.material_id WHERE b.id=?",
                (batch_id,),
            )
            if not batch:
                raise DomainError("NOT_FOUND", "批次不存在")
            if str(batch["project_id"]) != str(project["id"]):
                raise DomainError("FORBIDDEN", "批次不属于当前项目")
        clauses, params = ["project_id=?"], [project["id"]]
        supplier_scope = "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context)
        if supplier_scope:
            organization_id = self._organization(context) or ""
            clauses.append(
                "((event_type='SHIPMENT' AND business_document_id IN "
                "(SELECT sh.id FROM shipments sh JOIN suppliers su ON su.id=sh.supplier_id WHERE su.organization_id=?)) "
                "OR (event_type='HANDOVER' AND business_document_id IN "
                "(SELECT id FROM handovers WHERE source_org_id=? OR target_org_id=?)))"
            )
            params.extend((organization_id, organization_id, organization_id))
        if batch_id:
            clauses.append("batch_id=?"); params.append(batch_id)
        if business_document_id:
            clauses.append("business_document_id=?"); params.append(business_document_id)
        events = _rows(self.db.query_all("SELECT * FROM evidence_events WHERE " + " AND ".join(clauses) + " ORDER BY occurred_at", tuple(params)))
        for event in events:
            event["document_hashes"] = _loads(event.pop("document_hashes_json", "[]"), [])
            event["confirmed_org_ids"] = _loads(event.pop("confirmed_org_ids_json", "[]"), [])
            document_id = event.get("business_document_id")
            record = None
            if event.get("event_type") == EvidenceEventType.SHIPMENT.value:
                record = self.db.query_one("SELECT shipment_no,submitted_by,status FROM shipments WHERE id=?", (document_id,))
                if record:
                    event["business_reference"] = record["shipment_no"]
                    event["actor_id"] = record["submitted_by"]
            elif event.get("event_type") == EvidenceEventType.DELIVERY.value:
                record = self.db.query_one("SELECT delivery_no,confirmed_by,status FROM deliveries WHERE id=?", (document_id,))
                if record:
                    event["business_reference"] = record["delivery_no"]
                    event["actor_id"] = record["confirmed_by"]
            elif event.get("event_type") == EvidenceEventType.INSPECTION.value:
                record = self.db.query_one("SELECT inspection_no,inspector_id,status FROM inspections WHERE id=?", (document_id,))
                if record:
                    event["business_reference"] = record["inspection_no"]
                    event["actor_id"] = record["inspector_id"]
            elif event.get("event_type") == EvidenceEventType.HANDOVER.value:
                record = self.db.query_one("SELECT handover_no,initiated_by,status FROM handovers WHERE id=?", (document_id,))
                if record:
                    event["business_reference"] = record["handover_no"]
                    event["actor_id"] = record["initiated_by"]
            if event.get("actor_id"):
                actor_row = self.db.query_one("SELECT display_name FROM users WHERE id=?", (event["actor_id"],))
                event["actor_name"] = actor_row["display_name"] if actor_row else event["actor_id"]
            if event.get("event_type") == EvidenceEventType.SHIPMENT.value:
                shipment_status = str(record["status"]) if record else ""
                progress = "链上提交失败，可重试。" if event.get("status") == EvidenceStatus.FAILED.value else "已到货，等待验收。" if shipment_status == ShipmentStatus.ARRIVED.value else "已提交，等待现场确认。"
                event["detail"] = f"发货单 {event.get('business_reference', event.get('business_document_id'))} {progress}"
            elif event.get("event_type") == EvidenceEventType.DELIVERY.value:
                delivery_status = str(record["status"]) if record else ""
                progress = "链上提交失败，可重试。" if event.get("status") == EvidenceStatus.FAILED.value else "已确认，验收结果决定是否入库。" if delivery_status == ShipmentStatus.ACCEPTED.value else "已记录，等待现场确认。"
                event["detail"] = f"到货单 {event.get('business_reference', event.get('business_document_id'))} {progress}"
            elif event.get("event_type") == EvidenceEventType.INSPECTION.value:
                inspection = self.db.query_one("SELECT status,notes FROM inspections WHERE id=?", (event.get("business_document_id"),))
                if inspection:
                    event["detail"] = f"验收状态 {inspection['status']}；{inspection['notes'] or '暂无备注'}"
            elif event.get("event_type") == EvidenceEventType.HANDOVER.value:
                handover_status = str(record["status"]) if record else ""
                event["detail"] = f"交接单 {event.get('business_reference', event.get('business_document_id'))} {'双方已确认。' if handover_status == 'CONFIRMED' else '等待双方确认。'}"
        moves = [] if supplier_scope else _rows(self.db.query_all("SELECT * FROM stock_moves WHERE project_id=? AND (batch_id=? OR ? IS NULL) ORDER BY occurred_at", (project["id"], batch_id, batch_id)))
        return {"batch_id": batch_id, "business_document_id": business_document_id, "events": events, "stock_moves": moves, "source_refs": ["evidence_events", "stock_moves"], "data_completeness": 1.0}

    def list_batches(self, project_id: str, actor: Any = None) -> dict[str, Any]:
        """List project batches with their material and current balance summary."""

        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        project = self._project(project_id)
        supplier_scope = "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context)
        shipment_filter = ""
        shipment_params: tuple[Any, ...] = ()
        if supplier_scope:
            shipment_filter = " AND EXISTS (SELECT 1 FROM shipment_items si JOIN shipments sh ON sh.id=si.shipment_id JOIN suppliers su ON su.id=sh.supplier_id WHERE si.batch_id=b.id AND su.organization_id=?)"
            shipment_params = (self._organization(context) or "",)
        else:
            scoped_warehouses = self._scope_list(self._member_scopes(context, project["id"]), "warehouse_ids")
            if scoped_warehouses:
                shipment_filter = " AND EXISTS (SELECT 1 FROM stock_balances scoped_sb WHERE scoped_sb.batch_id=b.id AND scoped_sb.warehouse_id IN (" + ",".join("?" * len(scoped_warehouses)) + "))"
                shipment_params = tuple(scoped_warehouses)
        rows = _rows(
            self.db.query_all(
                """
                SELECT b.*,m.sku,m.name AS material_name,m.unit,
                       COALESCE(SUM(CAST(sb.qty_on_hand AS REAL)),0) AS qty_on_hand,
                       COALESCE(SUM(CAST(sb.qty_reserved AS REAL)),0) AS qty_reserved,
                       COALESCE(SUM(CAST(sb.qty_quarantined AS REAL)),0) AS qty_quarantined
                FROM batches b
                JOIN materials m ON m.id=b.material_id
                LEFT JOIN stock_balances sb ON sb.batch_id=b.id
                WHERE m.project_id=?""" + shipment_filter + """
                GROUP BY b.id,m.sku,m.name,m.unit
                ORDER BY b.batch_no
                """,
                (project["id"], *shipment_params),
            )
        )
        for batch in rows:
            on_hand = parse_decimal(str(batch.pop("qty_on_hand", "0")))
            reserved = parse_decimal(str(batch.pop("qty_reserved", "0")))
            quarantined = parse_decimal(str(batch.pop("qty_quarantined", "0")))
            batch["qty_on_hand"] = decimal_string(on_hand)
            batch["qty_reserved"] = decimal_string(reserved)
            batch["qty_quarantined"] = decimal_string(quarantined)
            batch["available"] = decimal_string(on_hand - reserved - quarantined)
        return {"items": rows, "total": len(rows), "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["batches", "stock_balances"]}

    def list_stock_moves(
        self,
        project_id: str,
        actor: Any = None,
        material_id: str | None = None,
        move_type: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Read the immutable stock ledger with material and location labels."""

        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        pid = self._project(project_id)["id"]
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            return {"items": [], "total": 0, "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["stock_moves"], "warnings": ["供应商视角不开放项目库存流水"]}
        page = max(1, int(page))
        page_size = min(200, max(1, int(page_size)))
        clauses = ["sm.project_id=?"]
        params: list[Any] = [pid]
        scopes = self._member_scopes(context, pid)
        scoped_warehouses = self._scope_list(scopes, "warehouse_ids")
        if scoped_warehouses:
            placeholders = ",".join("?" * len(scoped_warehouses))
            clauses.append(f"(sm.from_warehouse_id IN ({placeholders}) OR sm.to_warehouse_id IN ({placeholders}))")
            params.extend([*scoped_warehouses, *scoped_warehouses])
        if material_id:
            clauses.append("sm.material_id=?")
            params.append(material_id)
        if move_type:
            clauses.append("sm.move_type=?")
            params.append(move_type)
        where = " AND ".join(clauses)
        total = self.db.query_one(f"SELECT COUNT(*) AS count FROM stock_moves sm WHERE {where}", tuple(params))["count"]
        rows = _rows(
            self.db.query_all(
                f"""
                SELECT sm.*,m.sku,m.name AS material_name,m.category,m.specification,m.unit,
                       b.batch_no,fw.code AS from_warehouse_code,tw.code AS to_warehouse_code
                FROM stock_moves sm
                JOIN materials m ON m.id=sm.material_id
                LEFT JOIN batches b ON b.id=sm.batch_id
                LEFT JOIN warehouses fw ON fw.id=sm.from_warehouse_id
                LEFT JOIN warehouses tw ON tw.id=sm.to_warehouse_id
                WHERE {where} ORDER BY sm.occurred_at DESC LIMIT ? OFFSET ?
                """,
                tuple([*params, page_size, (page - 1) * page_size]),
            )
        )
        for row in rows:
            row["qty"] = decimal_string(row["qty"])
        return {"items": rows, "total": total, "page": page, "page_size": page_size, "next_cursor": None, "source_refs": ["stock_moves"]}

    def list_consumption_snapshots(self, project_id: str, actor: Any = None, date_from: Any = None, date_to: Any = None) -> dict[str, Any]:
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        pid = self._project(project_id)["id"]
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            return {"items": [], "total": 0, "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["consumption_snapshots"], "warnings": ["供应商视角不开放项目消耗汇总"]}
        clauses = ["cs.project_id=?"]
        params: list[Any] = [pid]
        if date_from:
            clauses.append("cs.date>=?"); params.append(str(date_from)[:10])
        if date_to:
            clauses.append("cs.date<=?"); params.append(str(date_to)[:10])
        rows = _rows(self.db.query_all("SELECT cs.*,m.name AS material_name,m.sku,m.unit FROM consumption_snapshots cs JOIN materials m ON m.id=cs.material_id WHERE " + " AND ".join(clauses) + " ORDER BY cs.date, m.sku", tuple(params)))
        for row in rows:
            for field in ("issued_qty", "returned_qty", "scrapped_qty", "net_consumed_qty"):
                row[field] = decimal_string(row[field])
        return {"items": rows, "total": len(rows), "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["consumption_snapshots"]}

    def list_drafts(self, project_id: str, actor: Any = None, draft_type: str | None = None) -> dict[str, Any]:
        """List non-expired drafts, retaining payload hashes and token state."""

        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        pid = self._project(project_id)["id"]
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            return {"items": [], "total": 0, "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["drafts"], "warnings": ["供应商视角不开放采购草稿"]}
        now = iso_utc(utc_now())
        # Expiry is a state transition, not just a presentation filter.  Mark
        # waiting drafts before reading them so a resumed client cannot keep
        # presenting an unusable confirmation action.
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE drafts SET status=? WHERE project_id=? AND status=? AND expires_at IS NOT NULL AND expires_at<=?",
                (DraftStatus.EXPIRED.value, pid, DraftStatus.PENDING_CONFIRMATION.value, now),
            )
        clauses = ["project_id=?"]
        params: list[Any] = [pid]
        if draft_type:
            clauses.append("draft_type=?")
            params.append(draft_type)
        clauses.append("(expires_at IS NULL OR expires_at>?)")
        params.append(now)
        rows = _rows(self.db.query_all("SELECT id,project_id,draft_type,payload_json,payload_hash,status,expires_at,created_by,confirmed_by,confirmed_at,rejection_reason,purchase_request_id,version,idempotency_key FROM drafts WHERE " + " AND ".join(clauses) + " ORDER BY rowid DESC", tuple(params)))
        for row in rows:
            row["payload"] = _loads(row.pop("payload_json"), {})
            row["confirmation_token"] = None
        return {"items": rows, "total": len(rows), "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["drafts"]}

    def issue_draft_confirmation_token(self, draft_id: str, actor: Any = None, idempotency_key: str | None = None) -> dict[str, Any]:
        """Issue a fresh one-time token for an unexecuted draft.

        The token value is never persisted in clear text; this endpoint exists
        so a waiting confirmation can be safely resumed after a page reload.
        """

        draft = self.db.query_one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if not draft:
            raise DomainError("NOT_FOUND", "草稿不存在")
        context = self._context(actor, draft["project_id"])
        action = "publish_daily_report" if draft["draft_type"] == "DAILY_REPORT" else "draft_submit"
        self.authorize(context, draft["project_id"], "purchase_submit" if action == "draft_submit" else "purchase_approve")
        if draft["status"] != DraftStatus.PENDING_CONFIRMATION.value:
            raise DomainError("INVALID_STATE", "草稿当前不可确认")
        payload = {"payload_hash": draft["payload_hash"], "version": draft["version"]}
        token_result = self._issue_confirmation_token_idempotently(action, draft["id"], payload, context.get("user_id"), idempotency_key)
        return {"draft_id": draft["id"], **token_result, "payload_hash": self._payload_hash(payload)}

    def reject_draft(
        self,
        draft_id: str,
        reason: str,
        actor: Any = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Reject a waiting draft without creating any formal business fact."""

        draft = self.db.query_one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if not draft:
            raise DomainError("NOT_FOUND", "草稿不存在")
        context = self._context(actor, draft["project_id"])
        roles = self._roles(context)
        # A draft creator may discard their own draft.  A project owner may
        # reject any project draft; other members cannot silently remove it.
        if context.get("user_id") != draft["created_by"] and "PROJECT_OWNER" not in roles:
            raise DomainError("FORBIDDEN", "只有草稿创建者或项目负责人可以放弃草稿")
        self.authorize(context, draft["project_id"], "read")
        reason_text = str(reason or "").strip()
        if not reason_text:
            raise DomainError("VALIDATION_ERROR", "放弃草稿必须填写原因")
        key = str(idempotency_key) if idempotency_key else None
        command = {"draft_id": draft_id, "reason": reason_text}
        with self.db.transaction() as conn:
            if key:
                previous = self._idempotent_lookup(conn, "draft_reject", key, command)
                if previous is not None:
                    return previous
            current = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if not current:
                raise DomainError("NOT_FOUND", "草稿不存在")
            if current["status"] != DraftStatus.PENDING_CONFIRMATION.value:
                raise DomainError("INVALID_STATE", "草稿当前不可放弃")
            before = dict(current)
            conn.execute(
                "UPDATE drafts SET status=?,rejection_reason=?,confirmed_by=?,confirmed_at=?,version=version+1 WHERE id=?",
                (DraftStatus.REJECTED.value, reason_text, context.get("user_id"), iso_utc(utc_now()), draft_id),
            )
            # Invalidate every still-open token for this draft.  The status
            # guard already prevents execution, but consuming the rows makes
            # replay semantics explicit and keeps stale tokens unusable.
            conn.execute(
                "UPDATE confirmation_tokens SET consumed=1 WHERE entity_id=? AND consumed=0",
                (draft_id,),
            )
            result = _row(conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()) or {}
            self._audit(conn, draft["project_id"], context, "REJECT_DRAFT", "draft", draft_id, before=before, after=result)
            if key:
                self._idempotent_store(conn, "draft_reject", key, command, result)
            return result

    def get_purchase_order_detail(self, project_id: str, order_id: str, actor: Any = None) -> dict[str, Any]:
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        pid = self._project(project_id)["id"]
        supplier_clause = ""
        supplier_params: tuple[Any, ...] = ()
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            supplier_clause = " AND s.organization_id=?"
            supplier_params = (self._organization(context) or "",)
        order = self.db.query_one("SELECT o.*,s.code AS supplier_code,s.name AS supplier_name FROM purchase_orders o JOIN suppliers s ON s.id=o.supplier_id WHERE (o.id=? OR o.po_no=?) AND o.project_id=?" + supplier_clause, (order_id, order_id, pid, *supplier_params))
        if not order:
            raise DomainError("NOT_FOUND", "采购订单不存在")
        result = _row(order) or {}
        result["items"] = _rows(self.db.query_all("SELECT poi.*,m.sku,m.name AS material_name,m.category,m.specification FROM purchase_order_items poi JOIN materials m ON m.id=poi.material_id WHERE poi.order_id=? ORDER BY poi.id", (order["id"],)))
        for item in result["items"]:
            for key in ("qty_ordered", "qty_shipped", "qty_received", "unit_price"):
                if item.get(key) is not None:
                    item[key] = decimal_string(item[key])
        result["shipments"] = _rows(self.db.query_all("SELECT * FROM shipments WHERE order_id=? ORDER BY shipped_at", (order["id"],)))
        for shipment in result["shipments"]:
            shipment["items"] = _rows(self.db.query_all("SELECT si.*,m.name AS material_name,m.unit,b.batch_no FROM shipment_items si JOIN materials m ON m.id=si.material_id LEFT JOIN batches b ON b.id=si.batch_id WHERE si.shipment_id=? ORDER BY si.id", (shipment["id"],)))
            for item in shipment["items"]:
                item["quality_file_ids"] = _loads(item.pop("quality_file_ids_json", "[]"), [])
            shipment["deliveries"] = _rows(self.db.query_all("SELECT d.*,w.code AS warehouse_code,w.name AS warehouse_name FROM deliveries d JOIN warehouses w ON w.id=d.warehouse_id WHERE d.shipment_id=? ORDER BY d.arrived_at", (shipment["id"],)))
            for delivery in shipment["deliveries"]:
                delivery["items"] = _rows(self.db.query_all("SELECT di.*,si.material_id,si.batch_id,m.name AS material_name,m.unit FROM delivery_items di JOIN shipment_items si ON si.id=di.shipment_item_id JOIN materials m ON m.id=si.material_id WHERE di.delivery_id=? ORDER BY di.id", (delivery["id"],)))
                delivery["inspections"] = _rows(self.db.query_all("SELECT * FROM inspections WHERE delivery_id=? ORDER BY inspected_at", (delivery["id"],)))
                for inspection in delivery["inspections"]:
                    inspection["items"] = _rows(self.db.query_all("SELECT ii.*,m.name AS material_name,m.unit FROM inspection_items ii JOIN delivery_items di ON di.id=ii.delivery_item_id JOIN shipment_items si ON si.id=di.shipment_item_id JOIN materials m ON m.id=si.material_id WHERE ii.inspection_id=? ORDER BY ii.id", (inspection["id"],)))
        return {"order": result, "source_refs": [f"purchase_order:{order['id']}"]}

    def list_shipments(self, project_id: str, status: str | None = None, actor: Any = None) -> dict[str, Any]:
        """Read shipment headers and their delivery trail for work queues."""

        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        pid = self._project(project_id)["id"]
        clauses = ["sh.project_id=?"]
        params: list[Any] = [pid]
        if status:
            clauses.append("sh.status=?"); params.append(status)
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            clauses.append("su.organization_id=?"); params.append(self._organization(context) or "")
        rows = _rows(self.db.query_all(
            "SELECT sh.*,po.po_no,su.code AS supplier_code,su.name AS supplier_name "
            "FROM shipments sh JOIN purchase_orders po ON po.id=sh.order_id "
            "JOIN suppliers su ON su.id=sh.supplier_id WHERE " + " AND ".join(clauses) + " ORDER BY sh.shipped_at DESC",
            tuple(params),
        ))
        for row in rows:
            row["items"] = _rows(self.db.query_all(
                "SELECT si.*,m.sku,m.name AS material_name,m.unit,b.batch_no "
                "FROM shipment_items si JOIN materials m ON m.id=si.material_id LEFT JOIN batches b ON b.id=si.batch_id "
                "WHERE si.shipment_id=? ORDER BY si.id",
                (row["id"],),
            ))
            for item in row["items"]:
                item["quality_file_ids"] = _loads(item.pop("quality_file_ids_json", "[]"), [])
            row["deliveries"] = _rows(self.db.query_all(
                "SELECT d.*,w.code AS warehouse_code,w.name AS warehouse_name FROM deliveries d "
                "JOIN warehouses w ON w.id=d.warehouse_id WHERE d.shipment_id=? ORDER BY d.arrived_at",
                (row["id"],),
            ))
        return {"items": rows, "total": len(rows), "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["shipments", "shipment_items", "deliveries"]}

    def list_deliveries(self, project_id: str, status: str | None = None, actor: Any = None) -> dict[str, Any]:
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        pid = self._project(project_id)["id"]
        clauses = ["d.project_id=?"]
        params: list[Any] = [pid]
        if status:
            clauses.append("d.status=?"); params.append(status)
        scopes = self._member_scopes(context, pid)
        scoped_warehouses = self._scope_list(scopes, "warehouse_ids")
        if scoped_warehouses:
            clauses.append("d.warehouse_id IN (" + ",".join("?" * len(scoped_warehouses)) + ")"); params.extend(scoped_warehouses)
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            clauses.append("EXISTS (SELECT 1 FROM shipments sh JOIN suppliers su ON su.id=sh.supplier_id WHERE sh.id=d.shipment_id AND su.organization_id=?)")
            params.append(self._organization(context) or "")
        rows = _rows(self.db.query_all(
            "SELECT d.*,sh.shipment_no,w.code AS warehouse_code,w.name AS warehouse_name "
            "FROM deliveries d JOIN shipments sh ON sh.id=d.shipment_id JOIN warehouses w ON w.id=d.warehouse_id "
            "WHERE " + " AND ".join(clauses) + " ORDER BY d.arrived_at DESC",
            tuple(params),
        ))
        for row in rows:
            row["items"] = _rows(self.db.query_all(
                "SELECT di.*,si.material_id,si.batch_id,m.sku,m.name AS material_name,m.unit,b.batch_no "
                "FROM delivery_items di JOIN shipment_items si ON si.id=di.shipment_item_id JOIN materials m ON m.id=si.material_id "
                "LEFT JOIN batches b ON b.id=si.batch_id WHERE di.delivery_id=? ORDER BY di.id",
                (row["id"],),
            ))
            row["inspections"] = _rows(self.db.query_all("SELECT * FROM inspections WHERE delivery_id=? ORDER BY inspected_at", (row["id"],)))
        return {"items": rows, "total": len(rows), "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["deliveries", "delivery_items", "inspections"]}

    def list_inspections(self, project_id: str, status: str | None = None, actor: Any = None) -> dict[str, Any]:
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        pid = self._project(project_id)["id"]
        clauses = ["i.project_id=?"]
        params: list[Any] = [pid]
        if status:
            clauses.append("i.status=?"); params.append(status)
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            clauses.append("EXISTS (SELECT 1 FROM deliveries d2 JOIN shipments sh2 ON sh2.id=d2.shipment_id JOIN suppliers su2 ON su2.id=sh2.supplier_id WHERE d2.id=i.delivery_id AND su2.organization_id=?)")
            params.append(self._organization(context) or "")
        rows = _rows(self.db.query_all(
            "SELECT i.*,d.delivery_no,u.display_name AS inspector_name FROM inspections i "
            "JOIN deliveries d ON d.id=i.delivery_id LEFT JOIN users u ON u.id=i.inspector_id "
            "WHERE " + " AND ".join(clauses) + " ORDER BY i.inspected_at DESC,i.inspection_no",
            tuple(params),
        ))
        for row in rows:
            row["items"] = _rows(self.db.query_all(
                "SELECT ii.*,di.qty_arrived,si.material_id,si.batch_id,m.sku,m.name AS material_name,m.unit,b.batch_no "
                "FROM inspection_items ii JOIN delivery_items di ON di.id=ii.delivery_item_id JOIN shipment_items si ON si.id=di.shipment_item_id "
                "JOIN materials m ON m.id=si.material_id LEFT JOIN batches b ON b.id=si.batch_id WHERE ii.inspection_id=? ORDER BY ii.id",
                (row["id"],),
            ))
        return {"items": rows, "total": len(rows), "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["inspections", "inspection_items"]}

    def list_handovers(self, project_id: str, status: str | None = None, actor: Any = None) -> dict[str, Any]:
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        pid = self._project(project_id)["id"]
        clauses = ["h.project_id=?"]
        params: list[Any] = [pid]
        if status:
            clauses.append("h.status=?"); params.append(status)
        organization = self._organization(context)
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            clauses.append("(h.source_org_id=? OR h.target_org_id=?)"); params.extend([organization or "", organization or ""])
        rows = _rows(self.db.query_all("SELECT h.* FROM handovers h WHERE " + " AND ".join(clauses) + " ORDER BY h.rowid DESC", tuple(params)))
        for row in rows:
            row["items"] = _rows(self.db.query_all(
                "SELECT hi.*,m.sku,m.name AS material_name,m.unit,b.batch_no FROM handover_items hi "
                "JOIN materials m ON m.id=hi.material_id LEFT JOIN batches b ON b.id=hi.batch_id WHERE hi.handover_id=? ORDER BY hi.id",
                (row["id"],),
            ))
        return {"items": rows, "total": len(rows), "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["handovers", "handover_items"]}

    def list_crews(self, project_id: str, actor: Any = None) -> dict[str, Any]:
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        pid = self._project(project_id)["id"]
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            return {"items": [], "total": 0, "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["crews", "crew_assignments"], "warnings": ["供应商视角不开放项目班组数据"]}
        rows = _rows(self.db.query_all("SELECT id,project_id,code,name,trade,planned_headcount,active FROM crews WHERE project_id=? ORDER BY code", (pid,)))
        for row in rows:
            row["assignments"] = _rows(self.db.query_all("SELECT id,crew_id,anonymous_worker_no,role,entry_date,exit_date,status FROM crew_assignments WHERE crew_id=? ORDER BY anonymous_worker_no", (row["id"],)))
        return {"items": rows, "total": len(rows), "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["crews", "crew_assignments"]}

    def list_ppe_issues(self, project_id: str, actor: Any = None) -> dict[str, Any]:
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            return {"items": [], "total": 0, "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["ppe_issues"]}
        pid = self._project(project_id)["id"]
        rows = _rows(self.db.query_all(
            "SELECT pi.*,c.code AS crew_code,c.name AS crew_name,m.sku,m.name AS material_name,m.unit,b.batch_no "
            "FROM ppe_issues pi JOIN crews c ON c.id=pi.crew_id JOIN materials m ON m.id=pi.material_id "
            "LEFT JOIN batches b ON b.id=pi.batch_id WHERE pi.project_id=? ORDER BY pi.issued_at DESC",
            (pid,),
        ))
        return {"items": rows, "total": len(rows), "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["ppe_issues", "crews", "materials"]}

    def list_documents(self, project_id: str, actor: Any = None) -> dict[str, Any]:
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        pid = self._project(project_id)["id"]
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            rows = _rows(self.db.query_all(
                "SELECT DISTINCT d.id,d.project_id,d.document_type,d.mime,d.size_bytes,d.sha256,d.uploader_id,d.version,d.uploaded_at,d.supersedes_document_id "
                "FROM documents d JOIN shipment_items si ON instr(si.quality_file_ids_json, d.id)>0 "
                "JOIN shipments sh ON sh.id=si.shipment_id JOIN suppliers su ON su.id=sh.supplier_id "
                "WHERE d.project_id=? AND su.organization_id=? ORDER BY d.uploaded_at DESC",
                (pid, self._organization(context) or ""),
            ))
        else:
            rows = _rows(self.db.query_all("SELECT id,project_id,document_type,mime,size_bytes,sha256,uploader_id,version,uploaded_at,supersedes_document_id FROM documents WHERE project_id=? ORDER BY uploaded_at DESC", (pid,)))
        return {"items": rows, "total": len(rows), "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["documents"]}

    def get_document_bytes(self, document_id: str, actor: Any = None) -> tuple[dict[str, Any], bytes]:
        row = self.db.query_one("SELECT * FROM documents WHERE id=?", (document_id,))
        if not row:
            raise DomainError("NOT_FOUND", "文件不存在")
        self.authorize(actor, row["project_id"], "read")
        project_root = self.document_root / row["project_id"]
        # Current adapter stores either the digest or document id as the final
        # path segment.  Resolve the recorded object key without accepting a
        # caller-provided filesystem path.
        candidates = [project_root / row["id"], project_root / row["sha256"]]
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            raise DomainError("NOT_FOUND", "文件内容不存在")
        return _row(row) or {}, path.read_bytes()

    def get_evidence_business_record(self, project_id: str, event_id: str, actor: Any = None) -> dict[str, Any]:
        """Resolve a chain evidence event to its authoritative business record.

        The response deliberately excludes attachment bytes and download URLs.
        It is the read model used by the trace UI and preserves the database as
        the source of operational truth.
        """

        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        project = self._project(project_id)
        event = self.db.query_one("SELECT * FROM evidence_events WHERE id=? AND project_id=?", (event_id, project["id"]))
        if not event:
            raise DomainError("NOT_FOUND", "证据事件不存在")
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            organization_id = self._organization(context) or ""
            if event["event_type"] == EvidenceEventType.SHIPMENT.value:
                scoped_record = self.db.query_one(
                    "SELECT 1 FROM shipments sh JOIN suppliers su ON su.id=sh.supplier_id WHERE sh.id=? AND su.organization_id=?",
                    (event["business_document_id"], organization_id),
                )
            elif event["event_type"] == EvidenceEventType.HANDOVER.value:
                # A target-side supplier must be able to review and confirm
                # the handover it is party to, without gaining access to
                # unrelated project evidence records.
                scoped_record = self.db.query_one(
                    "SELECT 1 FROM handovers WHERE id=? AND (source_org_id=? OR target_org_id=?)",
                    (event["business_document_id"], organization_id, organization_id),
                )
            else:
                scoped_record = None
            if not scoped_record:
                raise DomainError("FORBIDDEN", "无权访问该资源")
        event_data = _row(event) or {}
        event_data["document_hashes"] = _loads(event_data.pop("document_hashes_json", "[]"), [])
        event_data["confirmed_org_ids"] = _loads(event_data.pop("confirmed_org_ids_json", "[]"), [])
        document_id = event["business_document_id"]
        record: dict[str, Any] | None = None
        items: list[dict[str, Any]] = []
        event_type = event["event_type"]
        if event_type == EvidenceEventType.SHIPMENT.value:
            record = _row(self.db.query_one("SELECT s.*,po.po_no,su.name AS supplier_name FROM shipments s JOIN purchase_orders po ON po.id=s.order_id JOIN suppliers su ON su.id=s.supplier_id WHERE s.id=?", (document_id,)))
            items = _rows(self.db.query_all("SELECT si.*,m.sku,m.name AS material_name,m.unit,b.batch_no FROM shipment_items si JOIN materials m ON m.id=si.material_id LEFT JOIN batches b ON b.id=si.batch_id WHERE si.shipment_id=? ORDER BY si.id", (document_id,)))
        elif event_type == EvidenceEventType.DELIVERY.value:
            record = _row(self.db.query_one("SELECT d.*,s.shipment_no,w.code AS warehouse_code,w.name AS warehouse_name FROM deliveries d JOIN shipments s ON s.id=d.shipment_id JOIN warehouses w ON w.id=d.warehouse_id WHERE d.id=?", (document_id,)))
            items = _rows(self.db.query_all("SELECT di.*,si.batch_id,si.quality_file_ids_json,m.sku,m.name AS material_name,m.unit,b.batch_no FROM delivery_items di JOIN shipment_items si ON si.id=di.shipment_item_id JOIN materials m ON m.id=si.material_id LEFT JOIN batches b ON b.id=si.batch_id WHERE di.delivery_id=? ORDER BY di.id", (document_id,)))
        elif event_type == EvidenceEventType.INSPECTION.value:
            record = _row(self.db.query_one("SELECT i.*,d.delivery_no,u.display_name AS inspector_name FROM inspections i JOIN deliveries d ON d.id=i.delivery_id LEFT JOIN users u ON u.id=i.inspector_id WHERE i.id=?", (document_id,)))
            items = _rows(self.db.query_all("SELECT ii.*,di.qty_arrived,si.batch_id,si.quality_file_ids_json,m.sku,m.name AS material_name,m.unit,b.batch_no FROM inspection_items ii JOIN delivery_items di ON di.id=ii.delivery_item_id JOIN shipment_items si ON si.id=di.shipment_item_id JOIN materials m ON m.id=si.material_id LEFT JOIN batches b ON b.id=si.batch_id WHERE ii.inspection_id=? ORDER BY ii.id", (document_id,)))
        elif event_type == EvidenceEventType.HANDOVER.value:
            record = _row(self.db.query_one("SELECT * FROM handovers WHERE id=?", (document_id,)))
            items = _rows(self.db.query_all("SELECT hi.*,m.sku,m.name AS material_name,m.unit AS material_unit,b.batch_no FROM handover_items hi JOIN materials m ON m.id=hi.material_id LEFT JOIN batches b ON b.id=hi.batch_id WHERE hi.handover_id=? ORDER BY hi.id", (document_id,)))
        if record is None:
            raise DomainError("NOT_FOUND", "对应业务记录不存在")
        for item in items:
            if "quality_file_ids_json" in item:
                item["quality_file_ids"] = _loads(item.pop("quality_file_ids_json"), [])
        audits = _rows(self.db.query_all("SELECT action,actor_id,created_at,before_json,after_json FROM audit_logs WHERE project_id=? AND entity_id=? ORDER BY created_at", (project["id"], document_id)))
        for entry in audits:
            entry["before"] = _loads(entry.pop("before_json", None), None)
            entry["after"] = _loads(entry.pop("after_json", None), None)
        return {"event": event_data, "record_type": event_type, "record": record, "items": items, "audit_logs": audits, "source_refs": [f"evidence_events:{event_id}", f"business_record:{document_id}"]}

    def get_ai_run(self, project_id: str, run_id: str, actor: Any = None) -> dict[str, Any]:
        """Return the structured, non-sensitive audit trail for one AI run."""

        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        project = self._project(project_id)
        run = self.db.query_one("SELECT * FROM ai_runs WHERE id=? AND project_id=?", (run_id, project["id"]))
        if not run:
            raise DomainError("NOT_FOUND", "AI 运行不存在")
        result = _row(run) or {}
        result["output"] = _loads(result.pop("output_json", "{}"), {})
        calls = _rows(self.db.query_all("SELECT * FROM ai_tool_calls WHERE ai_run_id=? ORDER BY started_at", (run_id,)))
        for call in calls:
            call["input"] = _loads(call.pop("input_json", "{}"), {})
            call["output"] = _loads(call.pop("output_json", "{}"), {})
        result["tool_calls"] = calls
        return result

    # ------------------------------------------------------------------
    # AI thread history
    # ------------------------------------------------------------------
    @staticmethod
    def _ai_thread_id(thread_id: Any) -> str:
        value = str(thread_id or "").strip()
        if not value:
            raise DomainError("VALIDATION_ERROR", "线程标识不能为空")
        if len(value) > 128:
            raise DomainError("VALIDATION_ERROR", "线程标识不能超过 128 个字符")
        return value

    def _ai_thread_context(self, project_id: str, actor: Any) -> tuple[dict[str, Any], dict[str, Any], str]:
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        project = self._project(project_id)
        user_id = str(context.get("user_id") or "")
        if not user_id:
            raise DomainError("UNAUTHORIZED", "未认证用户")
        return context, project, user_id

    def start_ai_run(
        self,
        project_id: str,
        thread_id: str,
        request_text: str,
        intent: str,
        input_hash: str,
        actor: Any = None,
        *,
        model_name: str = "deterministic-tool-router",
        prompt_version: str = "mvp-v1",
    ) -> dict[str, Any]:
        """Persist a user turn and allocate its durable thread sequence.

        The sequence is independent of wall-clock precision, which makes
        history ordering and logical clears deterministic even when requests
        land in the same second.
        """

        context, project, user_id = self._ai_thread_context(project_id, actor)
        normalized_thread_id = self._ai_thread_id(thread_id)
        text = str(request_text or "").strip()
        if not text:
            raise DomainError("VALIDATION_ERROR", "消息不能为空")
        now = iso_utc(utc_now()) or ""
        run_id = str(uuid.uuid4())
        with self.db.transaction() as conn:
            state = conn.execute(
                "SELECT next_sequence FROM ai_thread_states WHERE project_id=? AND user_id=? AND thread_id=?",
                (project["id"], user_id, normalized_thread_id),
            ).fetchone()
            if state is None:
                sequence = 1
                conn.execute(
                    "INSERT INTO ai_thread_states(project_id,user_id,thread_id,next_sequence,cleared_through_sequence,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (project["id"], user_id, normalized_thread_id, 2, 0, now),
                )
            else:
                sequence = int(state["next_sequence"])
                conn.execute(
                    "UPDATE ai_thread_states SET next_sequence=?,updated_at=? "
                    "WHERE project_id=? AND user_id=? AND thread_id=?",
                    (sequence + 1, now, project["id"], user_id, normalized_thread_id),
                )
            conn.execute(
                "INSERT INTO ai_runs(id,project_id,user_id,thread_id,request_text,intent,model_name,prompt_version,status,started_at,input_hash,created_at,thread_sequence) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, project["id"], user_id, normalized_thread_id, text, intent, model_name, prompt_version, "RUNNING", now, input_hash, now, sequence),
            )
            self._audit(
                conn,
                project["id"],
                context,
                "CREATE_AI_RUN",
                "ai_run",
                run_id,
                after={"thread_id": normalized_thread_id, "thread_sequence": sequence, "status": "RUNNING"},
            )
        return {"run_id": run_id, "thread_id": normalized_thread_id, "thread_sequence": sequence, "started_at": now}

    def finish_ai_run(
        self,
        project_id: str,
        run_id: str,
        status: str,
        output: Mapping[str, Any],
        actor: Any = None,
        error_code: str | None = None,
        *,
        idempotency_key: str | None = None,
        idempotency_command: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist a completed AI response without letting another user alter it."""

        context, project, user_id = self._ai_thread_context(project_id, actor)
        with self.db.transaction() as conn:
            run = conn.execute(
                "SELECT id,user_id,status FROM ai_runs WHERE id=? AND project_id=?",
                (run_id, project["id"]),
            ).fetchone()
            if not run:
                raise DomainError("NOT_FOUND", "AI 运行不存在")
            if str(run["user_id"] or "") != user_id:
                raise DomainError("FORBIDDEN", "无权更新其他用户的 AI 运行")
            conn.execute(
                "UPDATE ai_runs SET status=?,ended_at=?,output_json=?,error_code=? WHERE id=?",
                (status, iso_utc(utc_now()), _json(output), error_code, run_id),
            )
            self._audit(
                conn,
                project["id"],
                context,
                "COMPLETE_AI_RUN",
                "ai_run",
                run_id,
                before={"status": run["status"]},
                after={"status": status},
            )
            if idempotency_key and idempotency_command is not None:
                self._idempotent_store(
                    conn,
                    "ai_message",
                    idempotency_key,
                    idempotency_command,
                    output,
                )

    def list_ai_thread_messages(
        self,
        project_id: str,
        thread_id: str,
        actor: Any = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return the current user's newest retained thread turns.

        One run has one user message and, when available, one assistant
        message. The response is intentionally capped at 20 messages as
        required by the agent context contract; full runs remain available to
        the audit API and are never discarded by this read projection.
        """

        context, project, user_id = self._ai_thread_context(project_id, actor)
        del context  # Authorization is complete; no mutation occurs below.
        normalized_thread_id = self._ai_thread_id(thread_id)
        try:
            requested_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise DomainError("VALIDATION_ERROR", "limit 必须是整数") from exc
        # Keep complete user/assistant pairs together. A thread context has a
        # maximum of 20 messages, so callers cannot use this route as an
        # unbounded audit export.
        message_limit = min(20, max(2, requested_limit))
        if message_limit % 2:
            message_limit -= 1
        run_limit = message_limit // 2
        state = self.db.query_one(
            "SELECT cleared_through_sequence,cleared_at FROM ai_thread_states "
            "WHERE project_id=? AND user_id=? AND thread_id=?",
            (project["id"], user_id, normalized_thread_id),
        )
        cleared_through = int(state["cleared_through_sequence"]) if state else 0
        totals = self.db.query_one(
            "SELECT COUNT(*) AS run_count,"
            "SUM(CASE WHEN output_json IS NOT NULL THEN 1 ELSE 0 END) AS response_count "
            "FROM ai_runs WHERE project_id=? AND COALESCE(user_id,'')=? AND thread_id=? AND thread_sequence>?",
            (project["id"], user_id, normalized_thread_id, cleared_through),
        )
        total_runs = int(totals["run_count"] if totals else 0)
        total_messages = total_runs + int(totals["response_count"] or 0) if totals else 0
        rows = _rows(
            self.db.query_all(
                "SELECT id,request_text,intent,status,started_at,ended_at,created_at,output_json,thread_sequence "
                "FROM ai_runs WHERE project_id=? AND COALESCE(user_id,'')=? AND thread_id=? AND thread_sequence>? "
                "ORDER BY thread_sequence DESC LIMIT ?",
                (project["id"], user_id, normalized_thread_id, cleared_through, run_limit),
            )
        )
        rows.reverse()
        messages: list[dict[str, Any]] = []
        for run in rows:
            run_id = str(run["id"])
            created_at = run.get("created_at") or run.get("started_at")
            messages.append(
                {
                    "id": f"{run_id}:user",
                    "run_id": run_id,
                    "role": "user",
                    "text": run["request_text"],
                    "created_at": created_at,
                    "thread_sequence": run["thread_sequence"],
                }
            )
            output = _loads(run.get("output_json"), {})
            if not isinstance(output, Mapping):
                output = {}
            if output or run["status"] != "RUNNING":
                response_message = {
                    "id": run_id,
                    "run_id": run_id,
                    "role": "assistant",
                    "text": str(output.get("text") or "该次运行未生成可展示的回复。"),
                    "created_at": run.get("ended_at") or created_at,
                    "thread_sequence": run["thread_sequence"],
                    "status": run["status"],
                    "intent": run.get("intent"),
                    "facts": output.get("facts", []),
                    "calculations": output.get("calculations", []),
                    "hypotheses": output.get("hypotheses", []),
                    "warnings": output.get("warnings", []),
                    "next_actions": output.get("next_actions", []),
                    "source_refs": output.get("source_refs", []),
                    "draft_id": output.get("draft_id"),
                    "approval_required": bool(output.get("approval_required")),
                }
                if isinstance(output.get("message_key"), str):
                    response_message["message_key"] = output["message_key"]
                    response_message["message_params"] = output.get("message_params", {})
                messages.append(response_message)
        return {
            "thread_id": normalized_thread_id,
            "items": messages,
            "total": total_messages,
            "total_runs": total_runs,
            "returned_runs": len(rows),
            "message_limit": message_limit,
            "has_more": total_runs > len(rows),
            "cleared_at": state["cleared_at"] if state else None,
            "source_refs": [f"ai_thread:{normalized_thread_id}", "ai_runs"],
        }

    def clear_ai_thread(
        self,
        project_id: str,
        thread_id: str,
        actor: Any = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Logically clear one user's visible history without deleting audits."""

        if not idempotency_key:
            raise DomainError("VALIDATION_ERROR", "缺少 Idempotency-Key")
        context, project, user_id = self._ai_thread_context(project_id, actor)
        normalized_thread_id = self._ai_thread_id(thread_id)
        command = {
            "project_id": project["id"],
            "user_id": user_id,
            "thread_id": normalized_thread_id,
        }
        now = iso_utc(utc_now()) or ""
        with self.db.transaction() as conn:
            previous = self._idempotent_lookup(conn, "ai_thread_clear", idempotency_key, command)
            if previous is not None:
                return previous
            state = conn.execute(
                "SELECT next_sequence,cleared_through_sequence FROM ai_thread_states "
                "WHERE project_id=? AND user_id=? AND thread_id=?",
                (project["id"], user_id, normalized_thread_id),
            ).fetchone()
            max_row = conn.execute(
                "SELECT COALESCE(MAX(thread_sequence),0) AS max_sequence FROM ai_runs "
                "WHERE project_id=? AND COALESCE(user_id,'')=? AND thread_id=?",
                (project["id"], user_id, normalized_thread_id),
            ).fetchone()
            max_sequence = int(max_row["max_sequence"] or 0)
            previous_cutoff = int(state["cleared_through_sequence"]) if state else 0
            next_sequence = max(int(state["next_sequence"]) if state else 1, max_sequence + 1)
            cleared_runs = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM ai_runs WHERE project_id=? AND COALESCE(user_id,'')=? "
                    "AND thread_id=? AND thread_sequence>? AND thread_sequence<=?",
                    (project["id"], user_id, normalized_thread_id, previous_cutoff, max_sequence),
                ).fetchone()["n"]
            )
            if state is None:
                conn.execute(
                    "INSERT INTO ai_thread_states(project_id,user_id,thread_id,next_sequence,cleared_through_sequence,cleared_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (project["id"], user_id, normalized_thread_id, next_sequence, max_sequence, now, now),
                )
            else:
                conn.execute(
                    "UPDATE ai_thread_states SET next_sequence=?,cleared_through_sequence=?,cleared_at=?,updated_at=? "
                    "WHERE project_id=? AND user_id=? AND thread_id=?",
                    (next_sequence, max_sequence, now, now, project["id"], user_id, normalized_thread_id),
                )
            result = {
                "project_id": project["id"],
                "thread_id": normalized_thread_id,
                "status": "CLEARED",
                "cleared_runs": cleared_runs,
                "cleared_at": now,
                "warnings": [{"message_key": "notice.thread_cleared"}],
                "source_refs": [f"ai_thread:{normalized_thread_id}", "ai_runs", "audit_logs"],
            }
            self._audit(
                conn,
                project["id"],
                context,
                "CLEAR_AI_THREAD",
                "ai_thread",
                f"{user_id}:{normalized_thread_id}",
                before={"cleared_through_sequence": previous_cutoff},
                after={"cleared_through_sequence": max_sequence, "cleared_runs": cleared_runs},
            )
            self._idempotent_store(conn, "ai_thread_clear", idempotency_key, command, result)
            return result

    # ------------------------------------------------------------------
    # Confirmation tokens and purchase workflow
    # ------------------------------------------------------------------
    def issue_confirmation_token(self, scope: str, entity_id: str, payload: Any, user_id: str | None = None, ttl_minutes: int | None = None) -> str:
        token = secrets.token_urlsafe(32)
        expires = utc_now() + timedelta(minutes=ttl_minutes or self.settings.approval_token_ttl_minutes)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        self.db.execute("INSERT INTO confirmation_tokens(token_hash,scope,entity_id,payload_hash,user_id,expires_at) VALUES(?,?,?,?,?,?)", (token_hash, scope, entity_id, self._payload_hash(payload), user_id, iso_utc(expires)))
        return token

    def _issue_confirmation_token_idempotently(
        self,
        scope: str,
        entity_id: str,
        payload: Any,
        user_id: str | None,
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
        """Issue a recoverable confirmation token without minting duplicates.

        Token issuance persists a server-side record, so it is a write command
        too.  A client retry must receive the original token rather than leave
        multiple valid tokens for the same transition in the database.
        """

        ttl_minutes = self.settings.approval_token_ttl_minutes
        expires_at = iso_utc(utc_now() + timedelta(minutes=ttl_minutes))
        command = {
            "entity_id": entity_id,
            "payload_hash": self._payload_hash(payload),
            "user_id": user_id,
        }
        if not idempotency_key:
            return {
                "confirmation_token": self.issue_confirmation_token(scope, entity_id, payload, user_id, ttl_minutes),
                "expires_at": expires_at,
            }
        with self.db.transaction() as conn:
            previous = self._idempotent_lookup(conn, f"{scope}_token", idempotency_key, command)
            if previous is not None:
                return previous
            result = {
                "confirmation_token": self.issue_confirmation_token(scope, entity_id, payload, user_id, ttl_minutes),
                "expires_at": expires_at,
            }
            self._idempotent_store(conn, f"{scope}_token", idempotency_key, command, result)
            return result

    def _consume_token(self, conn: Any, token: str | None, scope: str, entity_id: str, payload: Any, user_id: str | None = None) -> None:
        if not token:
            raise DomainError("TOKEN_EXPIRED", "需要一次性确认 token")
        token_hash = hashlib.sha256(str(token).encode()).hexdigest()
        row = conn.execute("SELECT * FROM confirmation_tokens WHERE token_hash=?", (token_hash,)).fetchone()
        if not row:
            raise DomainError("TOKEN_EXPIRED", "确认 token 无效或已过期")
        if int(row["consumed"]):
            raise DomainError("TOKEN_REPLAYED", "确认 token 已使用")
        expiry = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
        if expiry <= utc_now():
            raise DomainError("TOKEN_EXPIRED", "确认 token 已过期")
        if row["scope"] != scope or row["entity_id"] != entity_id:
            raise DomainError("VERSION_CONFLICT", "确认 token 与当前记录不匹配")
        if row["payload_hash"] != self._payload_hash(payload):
            raise DomainError("VERSION_CONFLICT", "记录已发生变化，请重新确认")
        if row["user_id"] and user_id and row["user_id"] != user_id:
            raise DomainError("FORBIDDEN", "确认 token 不属于当前用户")
        conn.execute("UPDATE confirmation_tokens SET consumed=1 WHERE token_hash=? AND consumed=0", (token_hash,))

    def create_purchase_request(self, cmd: Mapping[str, Any], actor: RequestContext | Mapping[str, Any] | str | None = None) -> dict[str, Any]:
        project_id = str(cmd.get("project_id"))
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "purchase_draft")
        items = list(cmd.get("items") or [])
        if not items:
            raise DomainError("VALIDATION_ERROR", "采购申请至少需要一条物料明细")
        key = str(cmd.get("idempotency_key") or "")
        with self.db.transaction() as conn:
            previous = self._idempotent_lookup(conn, "purchase_request", key, cmd)
            if previous is not None:
                return previous
            project = self._project(project_id)
            request_id, request_no = str(uuid.uuid4()), str(cmd.get("request_no") or self._next_no(conn, "purchase_requests", "PR", "request_no"))
            conn.execute("INSERT INTO purchase_requests(id,project_id,request_no,requester_id,status,needed_by,justification,total_estimated_amount,source_ai_run_id,version,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (request_id, project["id"], request_no, context.get("user_id"), PurchaseRequestStatus.DRAFT.value, str(cmd.get("needed_by"))[:10] if cmd.get("needed_by") else None, cmd.get("justification"), decimal_string(cmd["total_estimated_amount"]) if cmd.get("total_estimated_amount") is not None else None, cmd.get("source_ai_run_id"), 1, key))
            clean_items = []
            for item in items:
                material = self._material(project["id"], str(item.get("material_id")))
                qty = parse_decimal(item.get("qty"), field="qty")
                if qty <= 0:
                    raise DomainError("VALIDATION_ERROR", "采购数量必须大于 0")
                self._validate_unit(material, str(item.get("unit")))
                self._warehouse(project["id"], str(item.get("warehouse_id")))
                item_id = str(uuid.uuid4())
                conn.execute("INSERT INTO purchase_request_items(id,request_id,material_id,specification_snapshot,unit,qty,warehouse_id,reason,shortage_calc_id) VALUES(?,?,?,?,?,?,?,?,?)", (item_id, request_id, material["id"], item.get("specification_snapshot") or material.get("specification"), material["unit"], decimal_string(qty), item["warehouse_id"], item.get("reason", ""), item.get("calculation_id") or item.get("shortage_calc_id")))
                clean_items.append({"id": item_id, "material_id": material["id"], "qty": decimal_string(qty), "unit": material["unit"], "warehouse_id": item["warehouse_id"], "reason": item.get("reason", "")})
            result = {"id": request_id, "request_no": request_no, "project_id": project["id"], "status": PurchaseRequestStatus.DRAFT.value, "needed_by": str(cmd.get("needed_by"))[:10] if cmd.get("needed_by") else None, "items": clean_items, "version": 1}
            self._audit(conn, project["id"], context, "CREATE_PURCHASE_REQUEST", "purchase_request", request_id, after=result)
            self._idempotent_store(conn, "purchase_request", key, cmd, result)
            return result

    def draft_material_request(self, shortage_result: Mapping[str, Any], selected_items: list[Mapping[str, Any]], needed_by: Any, supplier_id: str | None = None, actor: RequestContext | Mapping[str, Any] | str | None = None) -> dict[str, Any]:
        project_id = str(shortage_result.get("project_id") or (actor.get("project_id") if isinstance(actor, Mapping) else "") or "")
        project_id = self._project(project_id)["id"]
        context = self._context(actor, project_id)
        if not project_id:
            raise DomainError("VALIDATION_ERROR", "缺料结果缺少 project_id")
        self.authorize(context, project_id, "purchase_draft")
        calculation_id = shortage_result.get("calculation_id")
        valid_ids = {item.get("material_id") for item in shortage_result.get("items", [])}
        payload_items = []
        for item in selected_items:
            if item.get("material_id") not in valid_ids:
                raise DomainError("VALIDATION_ERROR", "采购草稿物料不在最近一次缺料计算中")
            quantity = item.get("qty", item.get("recommended_order_qty"))
            if quantity is None:
                raise DomainError("VALIDATION_ERROR", "采购草稿缺少数量")
            expected = next(x for x in shortage_result.get("items", []) if x.get("material_id") == item.get("material_id"))
            material = self._material(project_id, str(item["material_id"]))
            warehouse_id = item.get("warehouse_id")
            if not warehouse_id:
                raise DomainError("VALIDATION_ERROR", "采购草稿缺少目标仓库")
            warehouse = self._warehouse(project_id, str(warehouse_id))
            payload_items.append({"material_id": item["material_id"], "material_name": material["name"], "qty": decimal_string(quantity), "unit": item.get("unit", expected.get("unit")), "warehouse_id": warehouse["id"], "warehouse_name": warehouse["name"], "warehouse_code": warehouse["code"], "reason": item.get("reason", "AI 缺料建议"), "calculation_id": calculation_id, "user_override": decimal_string(quantity) != str(expected.get("recommended_order_qty"))})
        payload = {"project_id": project_id, "needed_by": str(needed_by)[:10], "supplier_id": supplier_id, "items": payload_items, "calculation_id": calculation_id}
        key = str(context.get("idempotency_key") or uuid.uuid4())
        draft_id = str(uuid.uuid4())
        expires = utc_now() + timedelta(minutes=self.settings.approval_token_ttl_minutes)
        digest = self._payload_hash(payload)
        with self.db.transaction() as conn:
            previous = self._idempotent_lookup(conn, "material_request_draft", key, payload)
            if previous is not None:
                return previous
            conn.execute("INSERT INTO drafts(id,project_id,draft_type,payload_json,payload_hash,source_ai_run_id,status,expires_at,created_by,version,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (draft_id, project_id, "MATERIAL_REQUEST", _json(payload), digest, None, DraftStatus.PENDING_CONFIRMATION.value, iso_utc(expires), context.get("user_id"), 1, key))
            token = self.issue_confirmation_token("draft_submit", draft_id, {"payload_hash": digest, "version": 1}, context.get("user_id"), self.settings.approval_token_ttl_minutes)
            conn.execute("UPDATE drafts SET confirmation_token_hash=? WHERE id=?", (hashlib.sha256(token.encode()).hexdigest(), draft_id))
            self._audit(conn, project_id, context, "CREATE_DRAFT", "draft", draft_id, after={"status": DraftStatus.PENDING_CONFIRMATION.value, "payload_hash": digest})
            result = {"draft_id": draft_id, "draft_type": "MATERIAL_REQUEST", "status": DraftStatus.PENDING_CONFIRMATION.value, "payload": payload, "payload_hash": digest, "expires_at": iso_utc(expires), "purchase_request_id": None, "confirmation_token": token, "warnings": []}
            self._idempotent_store(conn, "material_request_draft", key, payload, result)
            return result

    def submit_material_request(self, draft_id: str, confirmation_token: str, actor: RequestContext | Mapping[str, Any] | str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        draft = self.db.query_one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if not draft:
            raise DomainError("NOT_FOUND", "采购草稿不存在")
        context = self._context(actor, draft["project_id"])
        self.authorize(context, draft["project_id"], "purchase_submit")
        key = str(idempotency_key) if idempotency_key else None
        with self.db.transaction() as conn:
            draft = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            command = {"draft_id": draft_id}
            if key:
                previous = self._idempotent_lookup(conn, "draft_submit", key, command)
                if previous is not None:
                    return previous
            if draft["status"] != DraftStatus.PENDING_CONFIRMATION.value:
                raise DomainError("TOKEN_REPLAYED" if draft["status"] in {DraftStatus.EXECUTED.value, DraftStatus.CONFIRMED.value} else "INVALID_STATE", "草稿已不能提交")
            payload = _loads(draft["payload_json"], {})
            self._consume_token(conn, confirmation_token, "draft_submit", draft_id, {"payload_hash": draft["payload_hash"], "version": draft["version"]}, context.get("user_id"))
            cmd = {"project_id": draft["project_id"], "needed_by": payload.get("needed_by"), "items": payload.get("items", []), "justification": "AI 草稿提交", "idempotency_key": "draft:" + draft_id}
            request = self.create_purchase_request(cmd, context)
            # Confirming an AI/material draft creates the formal request in
            # PENDING_APPROVAL.  It must never skip straight to APPROVED.
            conn.execute(
                "UPDATE purchase_requests SET status=?,version=version+1 WHERE id=? AND status=?",
                (PurchaseRequestStatus.PENDING_APPROVAL.value, request["id"], PurchaseRequestStatus.DRAFT.value),
            )
            request["status"] = PurchaseRequestStatus.PENDING_APPROVAL.value
            request["version"] = int(request["version"]) + 1
            request["purchase_request_id"] = request["id"]
            self._audit(
                conn,
                draft["project_id"],
                context,
                "SUBMIT_PURCHASE_REQUEST",
                "purchase_request",
                request["id"],
                before={"status": PurchaseRequestStatus.DRAFT.value},
                after=request,
            )
            conn.execute(
                "UPDATE drafts SET status=?,purchase_request_id=?,confirmed_by=?,confirmed_at=?,version=version+1 WHERE id=?",
                (DraftStatus.EXECUTED.value, request["id"], context.get("user_id"), iso_utc(utc_now()), draft_id),
            )
            if key:
                self._idempotent_store(conn, "draft_submit", key, command, request)
            return request

    def submit_purchase_request(self, request_id: str, actor: RequestContext | Mapping[str, Any] | str | None = None, confirmation_token: str | None = None) -> dict[str, Any]:
        request = self.db.query_one("SELECT * FROM purchase_requests WHERE id=? OR request_no=?", (request_id, request_id))
        if not request:
            raise DomainError("NOT_FOUND", "采购申请不存在")
        context = self._context(actor, request["project_id"])
        self.authorize(context, request["project_id"], "purchase_submit")
        with self.db.transaction() as conn:
            request = conn.execute("SELECT * FROM purchase_requests WHERE id=?", (request["id"],)).fetchone()
            if request["status"] != PurchaseRequestStatus.DRAFT.value:
                raise DomainError("INVALID_STATE", "仅草稿可提交")
            if not confirmation_token:
                confirmation_token = self.issue_confirmation_token("request_submit", request["id"], {"version": request["version"]}, context.get("user_id"))
            self._consume_token(conn, confirmation_token, "request_submit", request["id"], {"version": request["version"]}, context.get("user_id"))
            conn.execute("UPDATE purchase_requests SET status=?,version=version+1 WHERE id=?", (PurchaseRequestStatus.PENDING_APPROVAL.value, request["id"]))
            result = _row(conn.execute("SELECT * FROM purchase_requests WHERE id=?", (request["id"],)).fetchone())
            self._audit(conn, request["project_id"], context, "SUBMIT_PURCHASE_REQUEST", "purchase_request", request["id"], before=dict(request), after=result)
            return result or {}

    def list_purchase_requests(
        self,
        project_id: str,
        status: str | None = None,
        actor: Any = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Return formal requests and pending AI drafts for the project."""

        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        project = self._project(project_id)
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            return {"items": [], "total": 0, "page": 1, "page_size": 50, "next_cursor": None, "source_refs": ["purchase_requests"], "warnings": ["供应商视角不开放项目采购申请"]}
        page = max(1, int(page))
        page_size = min(200, max(1, int(page_size)))
        clauses: list[str] = ["project_id=?"]
        params: list[Any] = [project["id"]]
        if status:
            clauses.append("status=?")
            params.append(status)
        total = self.db.query_one("SELECT COUNT(*) AS count FROM purchase_requests WHERE " + " AND ".join(clauses), tuple(params))["count"]
        requests = _rows(
            self.db.query_all(
                "SELECT * FROM purchase_requests WHERE " + " AND ".join(clauses) + " ORDER BY rowid DESC LIMIT ? OFFSET ?",
                tuple([*params, page_size, (page - 1) * page_size]),
            )
        )
        for request in requests:
            request["items"] = _rows(self.db.query_all("SELECT pri.*,m.sku,m.name AS material_name,m.category,m.specification,w.code AS warehouse_code,w.name AS warehouse_name FROM purchase_request_items pri JOIN materials m ON m.id=pri.material_id LEFT JOIN warehouses w ON w.id=pri.warehouse_id WHERE pri.request_id=? ORDER BY pri.rowid", (request["id"],)))
            for item in request["items"]:
                item["qty"] = decimal_string(item["qty"])
        return {"items": requests, "total": total, "page": page, "page_size": page_size, "next_cursor": None, "source_refs": ["purchase_requests"]}

    def issue_purchase_approval_token(self, request_id: str, actor: Any = None, idempotency_key: str | None = None) -> dict[str, Any]:
        request = self.db.query_one("SELECT * FROM purchase_requests WHERE id=? OR request_no=?", (request_id, request_id))
        if not request:
            raise DomainError("NOT_FOUND", "采购申请不存在")
        context = self._context(actor, request["project_id"])
        self.authorize(context, request["project_id"], "purchase_approve")
        if request["status"] != PurchaseRequestStatus.PENDING_APPROVAL.value:
            raise DomainError("INVALID_STATE", "仅待审批申请可生成确认 token")
        payload = {"version": request["version"]}
        token_result = self._issue_confirmation_token_idempotently("request_approve", request["id"], payload, context.get("user_id"), idempotency_key)
        return {"request_id": request["id"], **token_result, "payload_hash": self._payload_hash(payload)}

    def approve_purchase_request(self, request_id: str, actor: RequestContext | Mapping[str, Any] | str | None = None, confirmation_token: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        request = self.db.query_one("SELECT * FROM purchase_requests WHERE id=? OR request_no=?", (request_id, request_id))
        if not request:
            raise DomainError("NOT_FOUND", "采购申请不存在")
        context = self._context(actor, request["project_id"])
        self.authorize(context, request["project_id"], "purchase_approve")
        key = str(idempotency_key) if idempotency_key else None
        with self.db.transaction() as conn:
            request = conn.execute("SELECT * FROM purchase_requests WHERE id=?", (request["id"],)).fetchone()
            command = {"request_id": request["id"]}
            if key:
                previous = self._idempotent_lookup(conn, "request_approve", key, command)
                if previous is not None:
                    return previous
            if request["status"] != PurchaseRequestStatus.PENDING_APPROVAL.value:
                raise DomainError("INVALID_STATE", "仅待审批申请可批准")
            if not confirmation_token:
                raise DomainError("TOKEN_EXPIRED", "批准需要一次性确认 token")
            self._consume_token(conn, confirmation_token, "request_approve", request["id"], {"version": request["version"]}, context.get("user_id"))
            before = dict(request)
            now = iso_utc(utc_now())
            conn.execute("UPDATE purchase_requests SET status=?,approved_by=?,approved_at=?,version=version+1 WHERE id=?", (PurchaseRequestStatus.APPROVED.value, context.get("user_id"), now, request["id"]))
            result = _row(conn.execute("SELECT * FROM purchase_requests WHERE id=?", (request["id"],)).fetchone()) or {}
            self._audit(conn, request["project_id"], context, "APPROVE_PURCHASE_REQUEST", "purchase_request", request["id"], before=before, after=result)
            self._enqueue_evidence_tx(conn, request["project_id"], request["id"], EvidenceEventType.PURCHASE_APPROVAL.value, result, context)
            if key:
                self._idempotent_store(conn, "request_approve", key, command, result)
            return result

    def reject_purchase_request(self, request_id: str, reason: str, actor: RequestContext | Mapping[str, Any] | str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        request = self.db.query_one("SELECT * FROM purchase_requests WHERE id=? OR request_no=?", (request_id, request_id))
        if not request:
            raise DomainError("NOT_FOUND", "采购申请不存在")
        context = self._context(actor, request["project_id"])
        self.authorize(context, request["project_id"], "purchase_approve")
        if not reason.strip():
            raise DomainError("VALIDATION_ERROR", "驳回必须填写原因")
        key = str(idempotency_key) if idempotency_key else None
        with self.db.transaction() as conn:
            command = {"request_id": request["id"], "reason": reason}
            if key:
                previous = self._idempotent_lookup(conn, "request_reject", key, command)
                if previous is not None:
                    return previous
            if request["status"] != PurchaseRequestStatus.PENDING_APPROVAL.value:
                raise DomainError("INVALID_STATE", "仅待审批申请可驳回")
            conn.execute("UPDATE purchase_requests SET status=?,rejection_reason=?,version=version+1 WHERE id=?", (PurchaseRequestStatus.REJECTED.value, reason, request["id"]))
            result = _row(conn.execute("SELECT * FROM purchase_requests WHERE id=?", (request["id"],)).fetchone()) or {}
            self._audit(conn, request["project_id"], context, "REJECT_PURCHASE_REQUEST", "purchase_request", request["id"], before=dict(request), after=result)
            if key:
                self._idempotent_store(conn, "request_reject", key, command, result)
            return result

    def create_purchase_order(self, cmd: Mapping[str, Any], actor: RequestContext | Mapping[str, Any] | str | None = None) -> dict[str, Any]:
        project_id = str(cmd.get("project_id"))
        project_id = self._project(project_id)["id"]
        context = self._context(actor, project_id)
        roles = self._roles(context)
        if not roles.intersection({"PROJECT_OWNER", "PROCUREMENT"}):
            raise DomainError("FORBIDDEN", "无权创建采购订单")
        self._project(project_id)
        items = list(cmd.get("items") or [])
        if not items:
            raise DomainError("VALIDATION_ERROR", "采购订单至少需要一条物料明细")
        key = str(cmd.get("idempotency_key") or "")
        with self.db.transaction() as conn:
            previous = self._idempotent_lookup(conn, "purchase_order", key, cmd)
            if previous is not None:
                return previous
            supplier = conn.execute("SELECT * FROM suppliers WHERE id=?", (cmd.get("supplier_id"),)).fetchone()
            if not supplier:
                raise DomainError("VALIDATION_ERROR", "供应商不存在")
            order_id, po_no = str(uuid.uuid4()), str(cmd.get("po_no") or self._next_no(conn, "purchase_orders", "PO", "po_no"))
            conn.execute("INSERT INTO purchase_orders(id,project_id,po_no,supplier_id,request_id,status,order_date,expected_date,currency,version) VALUES(?,?,?,?,?,?,?,?,?,?)", (order_id, project_id, po_no, supplier["id"], cmd.get("request_id"), str(cmd.get("status") or PurchaseOrderStatus.DRAFT.value), str(cmd.get("order_date") or utc_now().date())[:10], str(cmd.get("expected_date"))[:10] if cmd.get("expected_date") else None, cmd.get("currency", "CNY"), 1))
            clean_items = []
            for item in items:
                material = self._material(project_id, str(item.get("material_id")))
                self._validate_unit(material, str(item.get("unit")))
                qty = parse_decimal(item.get("qty_ordered", item.get("qty")), field="qty_ordered")
                if qty <= 0:
                    raise DomainError("VALIDATION_ERROR", "订单数量必须大于 0")
                item_id = str(uuid.uuid4())
                conn.execute("INSERT INTO purchase_order_items(id,order_id,material_id,qty_ordered,unit_price,unit) VALUES(?,?,?,?,?,?)", (item_id, order_id, material["id"], decimal_string(qty), decimal_string(item["unit_price"]) if item.get("unit_price") is not None else None, material["unit"]))
                clean_items.append({"id": item_id, "material_id": material["id"], "qty_ordered": decimal_string(qty), "qty_shipped": "0", "qty_received": "0", "unit": material["unit"]})
            result = {"id": order_id, "po_no": po_no, "project_id": project_id, "supplier_id": supplier["id"], "status": cmd.get("status", PurchaseOrderStatus.DRAFT.value), "items": clean_items, "version": 1}
            self._audit(conn, project_id, context, "CREATE_PURCHASE_ORDER", "purchase_order", order_id, after=result)
            self._idempotent_store(conn, "purchase_order", key, cmd, result)
            return result

    def _enqueue_evidence_tx(self, conn: Any, project_id: str, business_document_id: str, event_type: str, business_record: Mapping[str, Any], actor: Mapping[str, Any] | None = None, documents: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        project = self._project(project_id)
        payload = build_evidence_payload(event_type, {**dict(business_record), "business_document_id": business_document_id, "project_id": project["id"]}, documents)
        payload_hash = hash_payload(payload)
        version = str(business_record.get("version", 1))
        key = hashlib.sha256(f"{project['id']}|{business_document_id}|{event_type}|{version}".encode()).hexdigest()
        existing = conn.execute("SELECT * FROM evidence_events WHERE idempotency_key=?", (key,)).fetchone()
        if existing:
            return _row(existing) or {}
        event_id = str(uuid.uuid4())
        org = self._organization(actor or {})
        occurred = iso_utc(business_record.get("occurred_at") or utc_now())
        conn.execute("INSERT INTO evidence_events(id,project_id,business_document_id,batch_id,event_type,payload_hash,document_hashes_json,submitted_org_id,confirmed_org_ids_json,occurred_at,schema_version,status,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (event_id, project["id"], business_document_id, business_record.get("batch_id"), event_type, payload_hash, _json(payload.get("document_hashes", [])), org, "[]", occurred, "1", EvidenceStatus.PENDING.value, key))
        conn.execute("INSERT INTO evidence_outbox(id,evidence_event_id,payload_json,status,attempt_count,next_attempt_at) VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), event_id, canonical_json(payload), EvidenceStatus.PENDING.value, 0, occurred))
        return {"id": event_id, "project_id": project["id"], "business_document_id": business_document_id, "event_type": event_type, "payload_hash": payload_hash, "status": EvidenceStatus.PENDING.value, "idempotency_key": key, "occurred_at": occurred}

    def _supplier_for_user(self, context: Mapping[str, Any]) -> str | None:
        org = self._organization(context)
        if org:
            row = self.db.query_one("SELECT id FROM suppliers WHERE organization_id=?", (org,))
            return str(row["id"]) if row else None
        return None

    def submit_shipment(self, cmd: Mapping[str, Any], actor: RequestContext | Mapping[str, Any] | str | None = None) -> dict[str, Any]:
        project_id = str(cmd.get("project_id"))
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "shipment_submit")
        key = str(cmd.get("idempotency_key") or "")
        if not key:
            raise DomainError("VALIDATION_ERROR", "缺少幂等键")
        with self.db.transaction() as conn:
            previous = self._idempotent_lookup(conn, "shipment", key, cmd)
            if previous is not None:
                return previous
            project = self._project(project_id)
            order = conn.execute("SELECT * FROM purchase_orders WHERE id=? AND project_id=?", (cmd.get("purchase_order_id"), project["id"])).fetchone()
            if not order:
                raise DomainError("NOT_FOUND", "采购订单不存在")
            own_supplier = self._supplier_for_user(context)
            if own_supplier and own_supplier != order["supplier_id"]:
                raise DomainError("FORBIDDEN", "供应商只能操作自身订单")
            items = list(cmd.get("items") or [])
            if not items:
                raise DomainError("VALIDATION_ERROR", "发货必须包含明细")
            shipment_id = str(uuid.uuid4())
            shipment_no = str(cmd.get("shipment_no") or self._next_no(conn, "shipments", "SH", "shipment_no"))
            conn.execute("INSERT INTO shipments(id,project_id,shipment_no,order_id,supplier_id,status,shipped_at,eta,submitted_by,version,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (shipment_id, project["id"], shipment_no, order["id"], order["supplier_id"], ShipmentStatus.SUBMITTED.value, iso_utc(cmd.get("shipped_at") or utc_now()), iso_utc(cmd.get("eta")), context.get("user_id"), 1, key))
            clean = []
            for item in items:
                material = self._material(project["id"], str(item.get("material_id")))
                qty = parse_decimal(item.get("qty"), field="qty")
                self._validate_unit(material, str(item.get("unit")))
                po_item = conn.execute("SELECT * FROM purchase_order_items WHERE order_id=? AND material_id=?", (order["id"], material["id"])).fetchone()
                if not po_item:
                    raise DomainError("VALIDATION_ERROR", "发货物料不在采购订单中")
                shipped = parse_decimal(po_item["qty_shipped"]) + qty
                if shipped > parse_decimal(po_item["qty_ordered"]):
                    raise DomainError("VALIDATION_ERROR", "发货量超过订单剩余量")
                batch_id = item.get("batch_id")
                if not batch_id:
                    batch_no = item.get("batch_no")
                    if not batch_no:
                        raise DomainError("VALIDATION_ERROR", "发货明细缺少批次")
                    found = conn.execute("SELECT id FROM batches WHERE material_id=? AND batch_no=?", (material["id"], batch_no)).fetchone()
                    batch_id = found["id"] if found else str(uuid.uuid4())
                    if not found:
                        conn.execute("INSERT INTO batches(id,material_id,batch_no,supplier_id,status) VALUES(?,?,?,?,?)", (batch_id, material["id"], batch_no, order["supplier_id"], BatchStatus.IN_TRANSIT.value))
                else:
                    batch = conn.execute("SELECT * FROM batches WHERE id=? AND material_id=?", (batch_id, material["id"])).fetchone()
                    if not batch:
                        raise DomainError("VALIDATION_ERROR", "批次不属于发货物料")
                    conn.execute("UPDATE batches SET status=? WHERE id=?", (BatchStatus.IN_TRANSIT.value, batch_id))
                item_id = str(uuid.uuid4())
                conn.execute("INSERT INTO shipment_items(id,shipment_id,material_id,batch_id,qty_shipped,quality_file_ids_json) VALUES(?,?,?,?,?,?)", (item_id, shipment_id, material["id"], batch_id, decimal_string(qty), _json(item.get("document_ids", []))))
                conn.execute("UPDATE purchase_order_items SET qty_shipped=? WHERE id=?", (decimal_string(shipped), po_item["id"]))
                clean.append({"id": item_id, "material_id": material["id"], "batch_id": batch_id, "qty_shipped": decimal_string(qty), "unit": material["unit"]})
            # Derive the PO shipment status from all item quantities.
            po_items = conn.execute("SELECT qty_ordered,qty_shipped FROM purchase_order_items WHERE order_id=?", (order["id"],)).fetchall()
            po_status = PurchaseOrderStatus.SHIPPED.value if po_items and all(parse_decimal(x["qty_shipped"]) >= parse_decimal(x["qty_ordered"]) for x in po_items) else PurchaseOrderStatus.PARTIAL_SHIPPED.value
            conn.execute("UPDATE purchase_orders SET status=?,version=version+1 WHERE id=?", (po_status, order["id"]))
            result = {"id": shipment_id, "shipment_no": shipment_no, "project_id": project["id"], "order_id": order["id"], "supplier_id": order["supplier_id"], "status": ShipmentStatus.SUBMITTED.value, "items": clean, "batch_id": clean[0]["batch_id"] if len(clean) == 1 else None, "version": 1}
            self._audit(conn, project["id"], context, "SUBMIT_SHIPMENT", "shipment", shipment_id, after=result)
            result["evidence_event"] = self._enqueue_evidence_tx(conn, project["id"], shipment_id, EvidenceEventType.SHIPMENT.value, result, context)
            self._idempotent_store(conn, "shipment", key, cmd, result)
            return result

    def record_delivery(self, cmd: Mapping[str, Any], actor: RequestContext | Mapping[str, Any] | str | None = None) -> dict[str, Any]:
        project_id = str(cmd.get("project_id") or "")
        shipment = self.db.query_one("SELECT * FROM shipments WHERE id=?", (cmd.get("shipment_id"),))
        if not shipment:
            raise DomainError("NOT_FOUND", "发货记录不存在")
        project_id = project_id or shipment["project_id"]
        project_id = self._project(project_id)["id"]
        context = self._context(actor, project_id)
        self._authorize_warehouse(context, project_id, "delivery_confirm", str(cmd.get("warehouse_id")))
        key = str(cmd.get("idempotency_key") or "")
        with self.db.transaction() as conn:
            previous = self._idempotent_lookup(conn, "delivery", key, cmd)
            if previous is not None:
                return previous
            shipment = conn.execute("SELECT * FROM shipments WHERE id=? AND project_id=?", (cmd.get("shipment_id"), project_id)).fetchone()
            if not shipment:
                raise DomainError("NOT_FOUND", "发货记录不存在")
            self._warehouse(project_id, str(cmd.get("warehouse_id")))
            items = list(cmd.get("items") or [])
            if not items:
                raise DomainError("VALIDATION_ERROR", "到货必须包含明细")
            delivery_id, delivery_no = str(uuid.uuid4()), str(cmd.get("delivery_no") or self._next_no(conn, "deliveries", "DL", "delivery_no"))
            conn.execute("INSERT INTO deliveries(id,project_id,delivery_no,shipment_id,warehouse_id,status,arrived_at,version,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?)", (delivery_id, project_id, delivery_no, shipment["id"], cmd["warehouse_id"], ShipmentStatus.ARRIVED.value, iso_utc(cmd.get("arrived_at") or utc_now()), 1, key))
            clean = []
            for item in items:
                shipment_item = conn.execute("SELECT * FROM shipment_items WHERE id=? AND shipment_id=?", (item.get("shipment_item_id"), shipment["id"])).fetchone()
                if not shipment_item:
                    raise DomainError("VALIDATION_ERROR", "到货明细不属于该发货单")
                qty = parse_decimal(item.get("qty_arrived"), field="qty_arrived")
                prior = conn.execute("SELECT COALESCE(SUM(CAST(qty_arrived AS REAL)),0) AS qty FROM delivery_items WHERE shipment_item_id=?", (shipment_item["id"],)).fetchone()["qty"]
                if Decimal(str(prior)) + qty > parse_decimal(shipment_item["qty_shipped"]):
                    raise DomainError("VALIDATION_ERROR", "到货量超过发货剩余量")
                item_id = str(uuid.uuid4())
                conn.execute("INSERT INTO delivery_items(id,delivery_id,shipment_item_id,qty_arrived,discrepancy_reason) VALUES(?,?,?,?,?)", (item_id, delivery_id, shipment_item["id"], decimal_string(qty), item.get("discrepancy_reason") or cmd.get("discrepancy_reason")))
                conn.execute("UPDATE batches SET status=? WHERE id=?", (BatchStatus.DELIVERED.value, shipment_item["batch_id"]))
                clean.append({"id": item_id, "shipment_item_id": shipment_item["id"], "batch_id": shipment_item["batch_id"], "qty_arrived": decimal_string(qty)})
            result = {"id": delivery_id, "delivery_no": delivery_no, "project_id": project_id, "shipment_id": shipment["id"], "warehouse_id": cmd["warehouse_id"], "status": ShipmentStatus.ARRIVED.value, "items": clean, "batch_id": clean[0]["batch_id"] if len(clean) == 1 else None, "version": 1}
            token = self.issue_confirmation_token("delivery_confirm", delivery_id, {"version": 1}, context.get("user_id"))
            result["confirmation_token"] = token
            self._audit(conn, project_id, context, "RECORD_DELIVERY", "delivery", delivery_id, after=result)
            self._idempotent_store(conn, "delivery", key, cmd, result)
            return result

    def confirm_delivery(self, delivery_id: str, confirmation_token: str, actor: RequestContext | Mapping[str, Any] | str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        delivery = self.db.query_one("SELECT * FROM deliveries WHERE id=? OR delivery_no=?", (delivery_id, delivery_id))
        if not delivery:
            raise DomainError("NOT_FOUND", "到货记录不存在")
        context = self._context(actor, delivery["project_id"])
        self._authorize_warehouse(context, delivery["project_id"], "delivery_confirm", str(delivery["warehouse_id"]))
        key = str(idempotency_key) if idempotency_key else None
        with self.db.transaction() as conn:
            delivery = conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery["id"],)).fetchone()
            command = {"delivery_id": delivery["id"]}
            if key:
                previous = self._idempotent_lookup(conn, "delivery_confirm", key, command)
                if previous is not None:
                    return previous
            if delivery["status"] not in {ShipmentStatus.ARRIVED.value, ShipmentStatus.SUBMITTED.value}:
                raise DomainError("TOKEN_REPLAYED" if delivery["status"] == ShipmentStatus.ACCEPTED.value else "INVALID_STATE", "到货状态不允许确认")
            self._consume_token(conn, confirmation_token, "delivery_confirm", delivery["id"], {"version": delivery["version"]}, context.get("user_id"))
            before = dict(delivery)
            conn.execute("UPDATE deliveries SET status=?,confirmed_by=?,version=version+1 WHERE id=?", (ShipmentStatus.ACCEPTED.value, context.get("user_id"), delivery["id"]))
            # Delivery confirmation records physical arrival.  Accepted
            # quantities remain zero until inspection confirmation; this keeps
            # the order progress and expected-receipt calculation truthful.
            conn.execute(
                "UPDATE shipments SET status=?,version=version+1 WHERE id=? AND status NOT IN (?,?)",
                (ShipmentStatus.ARRIVED.value, delivery["shipment_id"], ShipmentStatus.CANCELLED.value, ShipmentStatus.REJECTED.value),
            )
            result = _row(conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery["id"],)).fetchone()) or {}
            batch = conn.execute("SELECT si.batch_id FROM delivery_items di JOIN shipment_items si ON si.id=di.shipment_item_id WHERE di.delivery_id=? ORDER BY di.id LIMIT 1", (delivery["id"],)).fetchone()
            if batch:
                result["batch_id"] = batch["batch_id"]
            self._audit(conn, delivery["project_id"], context, "CONFIRM_DELIVERY", "delivery", delivery["id"], before=before, after=result)
            result["evidence_event"] = self._enqueue_evidence_tx(conn, delivery["project_id"], delivery["id"], EvidenceEventType.DELIVERY.value, result, context)
            if key:
                self._idempotent_store(conn, "delivery_confirm", key, command, result)
            return result

    def submit_inspection(self, cmd: Mapping[str, Any], actor: RequestContext | Mapping[str, Any] | str | None = None) -> dict[str, Any]:
        delivery = self.db.query_one("SELECT * FROM deliveries WHERE id=?", (cmd.get("delivery_id"),))
        if not delivery:
            raise DomainError("NOT_FOUND", "到货记录不存在")
        context = self._context(actor, delivery["project_id"])
        self._authorize_warehouse(
            context,
            delivery["project_id"],
            "inspection_confirm",
            str(delivery["warehouse_id"]),
        )
        if delivery["status"] != ShipmentStatus.ACCEPTED.value:
            raise DomainError("INVALID_STATE", "到货尚未确认，不能执行验收")
        items = list(cmd.get("items") or [])
        if not items:
            raise DomainError("VALIDATION_ERROR", "验收必须包含明细")
        key = str(cmd.get("idempotency_key") or "")
        with self.db.transaction() as conn:
            previous = self._idempotent_lookup(conn, "inspection", key, cmd)
            if previous is not None:
                return previous
            existing = conn.execute(
                "SELECT status FROM inspections WHERE delivery_id=? ORDER BY rowid DESC LIMIT 1",
                (delivery["id"],),
            ).fetchone()
            if existing:
                raise DomainError("INVALID_STATE", "该到货记录已经存在验收记录")
            inspection_id, inspection_no = str(uuid.uuid4()), str(cmd.get("inspection_no") or self._next_no(conn, "inspections", "IN", "inspection_no"))
            result_name = str(cmd.get("result") or "PARTIAL").upper()
            if result_name not in {item.value for item in InspectionStatus if item != InspectionStatus.PENDING}:
                raise DomainError("VALIDATION_ERROR", "无效验收结果")
            conn.execute("INSERT INTO inspections(id,project_id,inspection_no,delivery_id,status,inspector_id,notes,version,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?)", (inspection_id, delivery["project_id"], inspection_no, delivery["id"], InspectionStatus.PENDING.value, context.get("user_id"), cmd.get("notes", ""), 1, key))
            clean = []
            for item in items:
                delivery_item = conn.execute("SELECT * FROM delivery_items WHERE id=? AND delivery_id=?", (item.get("delivery_item_id"), delivery["id"])).fetchone()
                if not delivery_item:
                    raise DomainError("VALIDATION_ERROR", "验收明细不属于该到货单")
                passed, failed = parse_decimal(item.get("qty_passed", "0"), field="qty_passed"), parse_decimal(item.get("qty_failed", "0"), field="qty_failed")
                if passed < 0 or failed < 0 or passed + failed != parse_decimal(delivery_item["qty_arrived"]):
                    raise DomainError("VALIDATION_ERROR", "验收通过与失败数量之和必须等于到货数量")
                item_id = str(uuid.uuid4())
                conn.execute("INSERT INTO inspection_items(id,inspection_id,delivery_item_id,qty_passed,qty_failed,failure_reason) VALUES(?,?,?,?,?,?)", (item_id, inspection_id, delivery_item["id"], decimal_string(passed), decimal_string(failed), item.get("failure_reason")))
                clean.append({"id": item_id, "delivery_item_id": delivery_item["id"], "qty_passed": decimal_string(passed), "qty_failed": decimal_string(failed), "failure_reason": item.get("failure_reason")})
            result = {"id": inspection_id, "inspection_no": inspection_no, "project_id": delivery["project_id"], "delivery_id": delivery["id"], "status": InspectionStatus.PENDING.value, "result": result_name, "items": clean, "version": 1}
            token = self.issue_confirmation_token("inspection_confirm", inspection_id, {"version": 1, "result": result_name}, context.get("user_id"))
            result["confirmation_token"] = token
            self._audit(conn, delivery["project_id"], context, "SUBMIT_INSPECTION", "inspection", inspection_id, after=result)
            self._idempotent_store(conn, "inspection", key, cmd, result)
            return result

    def issue_inspection_confirmation_token(self, inspection_id: str, actor: Any = None, idempotency_key: str | None = None) -> dict[str, Any]:
        """Mint a recoverable token for an already-recorded pending inspection.

        Fresh inspection creation returns its token inline.  This companion is
        needed for a persisted pending record after a page reload or a service
        restart, and binds the token to the result derived from immutable
        inspection items.
        """

        inspection = self.db.query_one("SELECT * FROM inspections WHERE id=? OR inspection_no=?", (inspection_id, inspection_id))
        if not inspection:
            raise DomainError("NOT_FOUND", "验收记录不存在")
        context = self._context(actor, inspection["project_id"])
        delivery = self.db.query_one(
            "SELECT warehouse_id FROM deliveries WHERE id=? AND project_id=?",
            (inspection["delivery_id"], inspection["project_id"]),
        )
        if not delivery:
            raise DomainError("NOT_FOUND", "到货记录不存在")
        self._authorize_warehouse(context, inspection["project_id"], "inspection_confirm", str(delivery["warehouse_id"]))
        if inspection["status"] != InspectionStatus.PENDING.value:
            raise DomainError("INVALID_STATE", "仅待确认验收记录可获取确认 token")
        rows = self.db.query_all("SELECT qty_passed,qty_failed FROM inspection_items WHERE inspection_id=?", (inspection["id"],))
        total_pass = sum((parse_decimal(row["qty_passed"]) for row in rows), Decimal("0"))
        total_fail = sum((parse_decimal(row["qty_failed"]) for row in rows), Decimal("0"))
        result = InspectionStatus.PASSED.value if total_fail == 0 else InspectionStatus.FAILED.value if total_pass == 0 else InspectionStatus.PARTIAL.value
        payload = {"version": inspection["version"], "result": result}
        token_result = self._issue_confirmation_token_idempotently("inspection_confirm", inspection["id"], payload, context.get("user_id"), idempotency_key)
        return {"inspection_id": inspection["id"], "result": result, **token_result, "payload_hash": self._payload_hash(payload)}

    def confirm_inspection(self, inspection_id: str, confirmation_token: str, actor: RequestContext | Mapping[str, Any] | str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        inspection = self.db.query_one("SELECT * FROM inspections WHERE id=? OR inspection_no=?", (inspection_id, inspection_id))
        if not inspection:
            raise DomainError("NOT_FOUND", "验收记录不存在")
        context = self._context(actor, inspection["project_id"])
        delivery = self.db.query_one(
            "SELECT warehouse_id FROM deliveries WHERE id=? AND project_id=?",
            (inspection["delivery_id"], inspection["project_id"]),
        )
        if not delivery:
            raise DomainError("NOT_FOUND", "到货记录不存在")
        self._authorize_warehouse(
            context,
            inspection["project_id"],
            "inspection_confirm",
            str(delivery["warehouse_id"]),
        )
        key = str(idempotency_key) if idempotency_key else None
        with self.db.transaction() as conn:
            inspection = conn.execute("SELECT * FROM inspections WHERE id=?", (inspection["id"],)).fetchone()
            command = {"inspection_id": inspection["id"]}
            if key:
                previous = self._idempotent_lookup(conn, "inspection_confirm", key, command)
                if previous is not None:
                    return previous
            if inspection["status"] != InspectionStatus.PENDING.value:
                raise DomainError("TOKEN_REPLAYED" if inspection["status"] != InspectionStatus.PENDING.value else "INVALID_STATE", "验收已确认")
            items = conn.execute("SELECT ii.*,di.qty_arrived,di.shipment_item_id,d.warehouse_id,s.material_id,s.batch_id,s.shipment_id FROM inspection_items ii JOIN delivery_items di ON di.id=ii.delivery_item_id JOIN deliveries d ON d.id=di.delivery_id JOIN shipment_items s ON s.id=di.shipment_item_id WHERE ii.inspection_id=?", (inspection["id"],)).fetchall()
            total_pass, total_fail = Decimal("0"), Decimal("0")
            for item in items:
                total_pass += parse_decimal(item["qty_passed"]); total_fail += parse_decimal(item["qty_failed"])
            requested_result = InspectionStatus.PASSED.value if total_fail == 0 else (InspectionStatus.FAILED.value if total_pass == 0 else InspectionStatus.PARTIAL.value)
            self._consume_token(conn, confirmation_token, "inspection_confirm", inspection["id"], {"version": inspection["version"], "result": requested_result}, context.get("user_id"))
            receipts: list[dict[str, Any]] = []
            quarantined: list[dict[str, Any]] = []
            for item in items:
                batch_id = item["batch_id"]
                passed_qty = parse_decimal(item["qty_passed"])
                failed_qty = parse_decimal(item["qty_failed"])
                if passed_qty > 0:
                    conn.execute("UPDATE batches SET status=? WHERE id=?", (BatchStatus.INSPECTED.value, batch_id))
                    move_cmd = {"project_id": inspection["project_id"], "material_id": item["material_id"], "batch_id": batch_id, "move_type": "RECEIPT", "qty": passed_qty, "unit": self._material(inspection["project_id"], item["material_id"])["unit"], "from_warehouse_id": None, "to_warehouse_id": item["warehouse_id"], "reason": "验收通过入库", "reference_type": "INSPECTION", "reference_id": inspection["id"], "idempotency_key": f"inspection:{inspection['id']}:{item['id']}"}
                    receipts.append(self._create_move_tx(conn, move_cmd, context))
                if failed_qty > 0:
                    # Rejected goods are physically at the site but never
                    # become available stock.  Recording them as on-hand and
                    # quarantined preserves the balance invariant and the
                    # inspection quantity without creating a RECEIPT move.
                    balance = self._change_balance(
                        conn,
                        item["warehouse_id"],
                        item["material_id"],
                        batch_id,
                        failed_qty,
                        quarantine_delta=failed_qty,
                    )
                    quarantined.append({"material_id": item["material_id"], "batch_id": batch_id, "qty": decimal_string(failed_qty), "balance": balance})
                    # A partial result keeps its accepted quantity usable;
                    # only a wholly failed batch is blocked by batch status.
                    if passed_qty == 0:
                        conn.execute("UPDATE batches SET status=? WHERE id=?", (BatchStatus.QUARANTINED.value, batch_id))
            conn.execute("UPDATE inspections SET status=?,inspected_at=?,version=version+1 WHERE id=?", (requested_result, iso_utc(utc_now()), inspection["id"]))
            order_id = conn.execute(
                "SELECT s.order_id FROM deliveries d JOIN shipments s ON s.id=d.shipment_id WHERE d.id=?",
                (inspection["delivery_id"],),
            ).fetchone()["order_id"]
            self._refresh_order_receipts_tx(conn, order_id)
            result = _row(conn.execute("SELECT * FROM inspections WHERE id=?", (inspection["id"],)).fetchone()) or {}
            result["status"] = requested_result
            result["batch_id"] = items[0]["batch_id"] if len(items) == 1 else None
            result["receipts"] = receipts
            result["quarantined"] = quarantined
            self._audit(conn, inspection["project_id"], context, "CONFIRM_INSPECTION", "inspection", inspection["id"], before=dict(inspection), after=result)
            result["evidence_event"] = self._enqueue_evidence_tx(conn, inspection["project_id"], inspection["id"], EvidenceEventType.INSPECTION.value, result, context)
            if key:
                self._idempotent_store(conn, "inspection_confirm", key, command, result)
            return result

    def _refresh_order_receipts_tx(self, conn: Any, order_id: str) -> None:
        """Recompute accepted quantities from confirmed inspection facts."""

        order_items = conn.execute(
            "SELECT id,material_id,qty_ordered,qty_shipped FROM purchase_order_items WHERE order_id=?",
            (order_id,),
        ).fetchall()
        for item in order_items:
            passed = conn.execute(
                "SELECT COALESCE(SUM(CAST(ii.qty_passed AS REAL)),0) AS qty "
                "FROM inspection_items ii "
                "JOIN inspections i ON i.id=ii.inspection_id "
                "JOIN delivery_items di ON di.id=ii.delivery_item_id "
                "JOIN shipment_items si ON si.id=di.shipment_item_id "
                "JOIN deliveries d ON d.id=di.delivery_id "
                "JOIN shipments sh ON sh.id=d.shipment_id "
                "WHERE sh.order_id=? AND si.material_id=? AND i.status IN ('PASSED','PARTIAL')",
                (order_id, item["material_id"]),
            ).fetchone()["qty"]
            accepted = min(parse_decimal(str(passed or "0")), parse_decimal(item["qty_shipped"]), parse_decimal(item["qty_ordered"]))
            conn.execute("UPDATE purchase_order_items SET qty_received=? WHERE id=?", (decimal_string(accepted), item["id"]))
        updated = conn.execute(
            "SELECT qty_ordered,qty_shipped,qty_received FROM purchase_order_items WHERE order_id=?",
            (order_id,),
        ).fetchall()
        if not updated:
            return
        all_received = all(parse_decimal(row["qty_received"]) >= parse_decimal(row["qty_ordered"]) for row in updated)
        any_received = any(parse_decimal(row["qty_received"]) > 0 for row in updated)
        status = PurchaseOrderStatus.RECEIVED.value if all_received else PurchaseOrderStatus.PARTIAL_RECEIVED.value if any_received else PurchaseOrderStatus.SHIPPED.value
        conn.execute("UPDATE purchase_orders SET status=?,version=version+1 WHERE id=?", (status, order_id))
        shipment_rows = conn.execute("SELECT id FROM shipments WHERE order_id=?", (order_id,)).fetchall()
        for shipment in shipment_rows:
            item_rows = conn.execute(
                "SELECT si.qty_shipped,COALESCE((SELECT SUM(CAST(di.qty_arrived AS REAL)) FROM delivery_items di JOIN deliveries d ON d.id=di.delivery_id WHERE di.shipment_item_id=si.id AND d.status='ACCEPTED'),0) AS arrived "
                "FROM shipment_items si WHERE si.shipment_id=?",
                (shipment["id"],),
            ).fetchall()
            if item_rows and all(parse_decimal(str(row["arrived"] or "0")) >= parse_decimal(str(row["qty_shipped"] or "0")) for row in item_rows):
                inspection_state = conn.execute(
                    "SELECT COUNT(*) AS total, SUM(CASE WHEN i.status='FAILED' THEN 1 ELSE 0 END) AS failed, "
                    "SUM(CASE WHEN i.status IN ('PASSED','PARTIAL') THEN 1 ELSE 0 END) AS passed "
                    "FROM inspections i JOIN deliveries d ON d.id=i.delivery_id WHERE d.shipment_id=?",
                    (shipment["id"],),
                ).fetchone()
                if inspection_state and int(inspection_state["total"] or 0) > 0 and int(inspection_state["failed"] or 0) > 0 and not int(inspection_state["passed"] or 0):
                    next_status = ShipmentStatus.REJECTED.value
                elif inspection_state and int(inspection_state["passed"] or 0) > 0:
                    next_status = ShipmentStatus.ACCEPTED.value
                else:
                    next_status = ShipmentStatus.ARRIVED.value
                conn.execute("UPDATE shipments SET status=?,version=version+1 WHERE id=?", (next_status, shipment["id"]))

    def create_handover(self, cmd: Mapping[str, Any], actor: RequestContext | Mapping[str, Any] | str | None = None) -> dict[str, Any]:
        project_id = str(cmd.get("project_id"))
        project_id = self._project(project_id)["id"]
        context = self._context(actor, project_id)
        if cmd.get("source_org_id") == cmd.get("target_org_id"):
            raise DomainError("VALIDATION_ERROR", "交接双方组织必须不同")
        self._authorize_warehouse(context, project_id, "handover", str(cmd.get("source_warehouse_id")))
        self._authorize_warehouse(context, project_id, "handover", str(cmd.get("target_warehouse_id")))
        items = list(cmd.get("items") or [])
        if not items:
            raise DomainError("VALIDATION_ERROR", "交接必须包含明细")
        key = str(cmd.get("idempotency_key") or "")
        with self.db.transaction() as conn:
            previous = self._idempotent_lookup(conn, "handover", key, cmd)
            if previous is not None:
                return previous
            hid, hno = str(uuid.uuid4()), str(cmd.get("handover_no") or self._next_no(conn, "handovers", "HO", "handover_no"))
            conn.execute("INSERT INTO handovers(id,project_id,handover_no,source_org_id,target_org_id,source_warehouse_id,target_warehouse_id,status,initiated_by,notes,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (hid, project_id, hno, cmd["source_org_id"], cmd["target_org_id"], cmd["source_warehouse_id"], cmd["target_warehouse_id"], "PENDING", context.get("user_id"), cmd.get("notes"), key))
            clean = []
            for item in items:
                material = self._material(project_id, str(item.get("material_id"))); self._validate_unit(material, str(item.get("unit")))
                qty = parse_decimal(item.get("qty"), field="qty")
                if qty <= 0: raise DomainError("VALIDATION_ERROR", "交接数量必须大于 0")
                iid = str(uuid.uuid4()); conn.execute("INSERT INTO handover_items(id,handover_id,material_id,batch_id,qty,unit,reference_type,reference_id) VALUES(?,?,?,?,?,?,?,?)", (iid, hid, material["id"], item.get("batch_id"), decimal_string(qty), material["unit"], item.get("reference_type"), item.get("reference_id"))); clean.append({"id": iid, "material_id": material["id"], "batch_id": item.get("batch_id"), "qty": decimal_string(qty), "unit": material["unit"]})
            result = {"id": hid, "handover_no": hno, "project_id": project_id, "source_org_id": cmd["source_org_id"], "target_org_id": cmd["target_org_id"], "status": "PENDING", "items": clean, "version": 1, "source_confirmed": False, "target_confirmed": False}
            self._audit(conn, project_id, context, "CREATE_HANDOVER", "handover", hid, after=result); self._idempotent_store(conn, "handover", key, cmd, result); return result

    def confirm_handover(self, handover_id: str, confirmation_token: str, actor: RequestContext | Mapping[str, Any] | str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        handover = self.db.query_one("SELECT * FROM handovers WHERE id=? OR handover_no=?", (handover_id, handover_id))
        if not handover: raise DomainError("NOT_FOUND", "交接记录不存在")
        context = self._context(actor, handover["project_id"]); self.authorize(context, handover["project_id"], "handover")
        org = self._organization(context)
        if org not in {handover["source_org_id"], handover["target_org_id"]}: raise DomainError("FORBIDDEN", "交接方组织不匹配")
        field = "source_confirmed" if org == handover["source_org_id"] else "target_confirmed"
        # Confirmation can create a paired OUT/IN transfer, so re-check both
        # warehouse scopes at the point of confirmation (scopes may have
        # changed since the handover draft was created).
        self._authorize_warehouse(context, handover["project_id"], "handover", str(handover["source_warehouse_id"]))
        self._authorize_warehouse(context, handover["project_id"], "handover", str(handover["target_warehouse_id"]))
        key = str(idempotency_key) if idempotency_key else None
        with self.db.transaction() as conn:
            handover = conn.execute("SELECT * FROM handovers WHERE id=?", (handover["id"],)).fetchone()
            command = {"handover_id": handover["id"], "organization_id": org}
            if key:
                previous = self._idempotent_lookup(conn, "handover_confirm", key, command)
                if previous is not None:
                    return previous
            if handover[field]: raise DomainError("TOKEN_REPLAYED", "该组织已确认交接")
            self._consume_token(conn, confirmation_token, "handover_confirm", handover["id"], {"version": handover["version"], "organization_id": org}, context.get("user_id"))
            conn.execute(f"UPDATE handovers SET {field}=1,confirmed_by=?,version=version+1 WHERE id=?", (context.get("user_id"), handover["id"]))
            current = conn.execute("SELECT * FROM handovers WHERE id=?", (handover["id"],)).fetchone()
            if current["source_confirmed"] and current["target_confirmed"]:
                items = conn.execute("SELECT * FROM handover_items WHERE handover_id=?", (handover["id"],)).fetchall()
                for item in items:
                    material = self._material(handover["project_id"], item["material_id"])
                    corr = str(uuid.uuid4()); qty = item["qty"]
                    self._change_balance(conn, handover["source_warehouse_id"], item["material_id"], item["batch_id"], -parse_decimal(qty)); self._change_balance(conn, handover["target_warehouse_id"], item["material_id"], item["batch_id"], parse_decimal(qty))
                    for move_type, from_wh, to_wh, suffix in (("TRANSFER_OUT", handover["source_warehouse_id"], None, "out"), ("TRANSFER_IN", None, handover["target_warehouse_id"], "in")):
                        mid, mno = str(uuid.uuid4()), self._next_no(conn, "stock_moves", "SM"); conn.execute("INSERT INTO stock_moves(id,project_id,move_no,move_type,material_id,batch_id,from_warehouse_id,to_warehouse_id,qty,unit,reason,reference_type,reference_id,actor_id,occurred_at,idempotency_key,correlation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (mid, handover["project_id"], mno, move_type, item["material_id"], item["batch_id"], from_wh, to_wh, qty, material["unit"], "双方确认交接", "HANDOVER", handover["id"], context.get("user_id"), iso_utc(utc_now()), f"handover:{handover['id']}:{item['id']}:{suffix}", corr))
                conn.execute("UPDATE handovers SET status='CONFIRMED',confirmed_at=?,version=version+1 WHERE id=?", (iso_utc(utc_now()), handover["id"]))
            result = _row(conn.execute("SELECT * FROM handovers WHERE id=?", (handover["id"],)).fetchone()) or {}
            result["source_confirmed"] = bool(result.get("source_confirmed")); result["target_confirmed"] = bool(result.get("target_confirmed"))
            self._audit(conn, handover["project_id"], context, "CONFIRM_HANDOVER", "handover", handover["id"], before=dict(handover), after=result)
            if result.get("status") == "CONFIRMED": result["evidence_event"] = self._enqueue_evidence_tx(conn, handover["project_id"], handover["id"], EvidenceEventType.HANDOVER.value, result, context)
            if key:
                self._idempotent_store(conn, "handover_confirm", key, command, result)
            return result

    def issue_handover_confirmation_token(self, handover_id: str, actor: Any = None, idempotency_key: str | None = None) -> dict[str, Any]:
        handover = self.db.query_one("SELECT * FROM handovers WHERE id=? OR handover_no=?", (handover_id, handover_id))
        if not handover:
            raise DomainError("NOT_FOUND", "交接记录不存在")
        context = self._context(actor, handover["project_id"])
        self.authorize(context, handover["project_id"], "handover")
        organization_id = self._organization(context)
        if organization_id not in {handover["source_org_id"], handover["target_org_id"]}:
            raise DomainError("FORBIDDEN", "交接方组织不匹配")
        field = "source_confirmed" if organization_id == handover["source_org_id"] else "target_confirmed"
        self._authorize_warehouse(context, handover["project_id"], "handover", str(handover["source_warehouse_id"]))
        self._authorize_warehouse(context, handover["project_id"], "handover", str(handover["target_warehouse_id"]))
        if handover[field]:
            raise DomainError("TOKEN_REPLAYED", "该组织已确认交接")
        payload = {"version": handover["version"], "organization_id": organization_id}
        token_result = self._issue_confirmation_token_idempotently("handover_confirm", handover["id"], payload, context.get("user_id"), idempotency_key)
        return {"handover_id": handover["id"], **token_result, "payload_hash": self._payload_hash(payload)}

    # ------------------------------------------------------------------
    # PPE, planning and anomaly service wrappers
    # ------------------------------------------------------------------
    def register_crew(self, cmd: Mapping[str, Any], actor: Any = None) -> dict[str, Any]:
        project_id = self._project(str(cmd.get("project_id")))["id"]; context = self._context(actor, project_id); self.authorize(context, project_id, "ppe_issue")
        code, name = str(cmd.get("code")), str(cmd.get("name"))
        if not code or not name: raise DomainError("VALIDATION_ERROR", "班组编码和名称必填")
        with self.db.transaction() as conn:
            crew_id = str(uuid.uuid4())
            try:
                conn.execute("INSERT INTO crews(id,project_id,code,name,contractor_org_id,trade,planned_headcount,active) VALUES(?,?,?,?,?,?,?,1)", (crew_id, project_id, code, name, cmd.get("contractor_org_id"), cmd.get("trade"), int(cmd.get("planned_headcount", cmd.get("headcount", 0)))))
            except Exception as exc:
                if "UNIQUE" in str(exc).upper(): raise DomainError("DUPLICATE_IDEMPOTENCY", "班组编码已存在") from exc
                raise
            result = _row(conn.execute("SELECT * FROM crews WHERE id=?", (crew_id,)).fetchone()) or {}
            self._audit(conn, project_id, context, "REGISTER_CREW", "crew", crew_id, after=result); return result

    def upsert_crew_assignment(self, cmd: Mapping[str, Any], actor: Any = None) -> dict[str, Any]:
        crew = self.db.query_one("SELECT * FROM crews WHERE id=?", (cmd.get("crew_id"),))
        if not crew: raise DomainError("NOT_FOUND", "班组不存在")
        context = self._context(actor, crew["project_id"]); self.authorize(context, crew["project_id"], "ppe_issue")
        worker_no = str(cmd.get("anonymous_worker_no", "")); entry = str(cmd.get("entry_date"))[:10]
        if not worker_no or not entry: raise DomainError("VALIDATION_ERROR", "匿名编号和入场日期必填")
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM crew_assignments WHERE crew_id=? AND anonymous_worker_no=?", (crew["id"], worker_no)).fetchone()
            aid = existing["id"] if existing else str(uuid.uuid4())
            if existing:
                conn.execute("UPDATE crew_assignments SET role=?,entry_date=?,exit_date=?,status=? WHERE id=?", (cmd.get("role", existing["role"]), entry, str(cmd.get("exit_date"))[:10] if cmd.get("exit_date") else None, cmd.get("status", existing["status"]), aid))
            else:
                conn.execute("INSERT INTO crew_assignments(id,crew_id,anonymous_worker_no,role,entry_date,exit_date,status) VALUES(?,?,?,?,?,?,?)", (aid, crew["id"], worker_no, cmd.get("role", "WORKER"), entry, str(cmd.get("exit_date"))[:10] if cmd.get("exit_date") else None, cmd.get("status", "ACTIVE")))
            return _row(conn.execute("SELECT * FROM crew_assignments WHERE id=?", (aid,)).fetchone()) or {}

    def issue_ppe(self, cmd: Mapping[str, Any], actor: Any = None) -> dict[str, Any]:
        project_id = self._project(str(cmd.get("project_id")))["id"]; context = self._context(actor, project_id); self._authorize_warehouse(context, project_id, "ppe_issue", str(cmd.get("warehouse_id")))
        crew = self.db.query_one("SELECT * FROM crews WHERE id=? AND project_id=? AND active=1", (cmd.get("crew_id"), project_id))
        if not crew: raise DomainError("CREW_MEMBER_INVALID", "班组无效")
        material = self._material(project_id, str(cmd.get("material_id")))
        if material["category"] not in {MaterialCategory.PPE.value, MaterialCategory.LOGISTICS.value, MaterialCategory.FIRST_AID.value}: raise DomainError("VALIDATION_ERROR", "该物料不是保障物资")
        qty = parse_decimal(cmd.get("qty"), field="qty"); key = str(cmd.get("idempotency_key") or "")
        if qty <= 0 or not key: raise DomainError("VALIDATION_ERROR", "发放数量和幂等键必填")
        with self.db.transaction() as conn:
            previous = self._idempotent_lookup(conn, "ppe_issue", key, cmd)
            if previous is not None: return previous
            issued_at = iso_utc(cmd.get("issued_at") or utc_now()); due = cmd.get("replacement_due")
            if not due and material.get("ppe_standard_json"):
                ppe = _loads(material["ppe_standard_json"], {}); days = int(ppe.get("replacement_days", 0) or 0); due = (datetime.fromisoformat(issued_at.replace("Z", "+00:00")).date() + timedelta(days=days)).isoformat() if days else None
            move = {"project_id": project_id, "material_id": material["id"], "batch_id": cmd.get("batch_id"), "move_type": "ISSUE", "qty": qty, "unit": material["unit"], "from_warehouse_id": cmd.get("warehouse_id"), "to_warehouse_id": None, "reason": "PPE 发放", "reference_type": "PPE_ISSUE", "reference_id": crew["id"], "occurred_at": issued_at, "idempotency_key": "ppe-move:" + key}
            move_result = self._create_move_tx(conn, move, context)
            issue_id = str(uuid.uuid4()); conn.execute("INSERT INTO ppe_issues(id,project_id,crew_id,material_id,qty,issued_at,issued_by,batch_id,replacement_due,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?)", (issue_id, project_id, crew["id"], material["id"], decimal_string(qty), issued_at, context.get("user_id"), cmd.get("batch_id"), due, key))
            result = {"id": issue_id, "project_id": project_id, "crew_id": crew["id"], "material_id": material["id"], "qty": decimal_string(qty), "issued_at": issued_at, "replacement_due": due, "move": move_result}
            self._audit(conn, project_id, context, "ISSUE_PPE", "ppe_issue", issue_id, after=result); self._idempotent_store(conn, "ppe_issue", key, cmd, result); return result

    def get_ppe_coverage(self, project_id: str, crew_ids: list[str] | None = None, as_of: Any = None, warning_days: int | None = None, actor: Any = None) -> dict[str, Any]:
        context = self._context(actor, project_id); self.authorize(context, project_id, "read"); as_of = str(as_of or utc_now().date())[:10]
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            return {"calculation_id": None, "calculation_version": "ppe-v1", "as_of": as_of, "items": [], "expiring": [], "anonymous_crews": [], "missing_data": ["supplier_scope"], "warnings": ["供应商视角不开放 PPE 覆盖数据"], "confidence": 0.0}
        project = self._project(project_id); clauses = ["project_id=?"]; params: list[Any] = [project["id"]]
        if crew_ids: clauses.append("id IN (" + ",".join("?" * len(crew_ids)) + ")"); params.extend(crew_ids)
        crews = _rows(self.db.query_all("SELECT id,code,name,trade,planned_headcount AS headcount,planned_headcount FROM crews WHERE " + " AND ".join(clauses), tuple(params)))
        standards = _rows(self.db.query_all("SELECT material_id,trade,required_qty_per_person FROM ppe_standards WHERE project_id=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)", (project["id"], as_of, as_of)))
        issues = _rows(self.db.query_all("SELECT crew_id,material_id,qty,issued_at,replacement_due FROM ppe_issues WHERE project_id=? AND issued_at<=?", (project["id"], as_of + "T23:59:59Z")))
        result = check_ppe_coverage({"project_id": project_id, "as_of": as_of, "warning_days": warning_days if warning_days is not None else self.settings.ppe_expiry_warning_days}, crews, standards, issues)
        crew_names = {str(row["id"]): str(row["name"]) for row in crews}
        material_rows = _rows(self.db.query_all("SELECT id,name,unit FROM materials WHERE project_id=?", (project["id"],)))
        material_names = {str(row["id"]): str(row["name"]) for row in material_rows}
        material_units = {str(row["id"]): str(row["unit"]) for row in material_rows}
        for item in result.get("items", []):
            item["crew_name"] = crew_names.get(str(item.get("crew_id")), "匿名班组")
            item["material_name"] = material_names.get(str(item.get("material_id")), "保障物资")
            item["unit"] = material_units.get(str(item.get("material_id")), "piece")
            item["source_ref"] = f"calculation:{result.get('calculation_id')}"
        return result

    def calculate_shortage(self, cmd: Mapping[str, Any], actor: Any = None) -> dict[str, Any]:
        project = self._project(str(cmd.get("project_id")))
        project_id = project["id"]
        context = self._context(actor, project_id); self.authorize(context, project_id, "read")
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            return {"project_id": project_id, "calculation_id": None, "calculation_version": "shortage-v1", "items": [], "input_snapshot_hash": "", "missing_data": ["supplier_scope"], "warnings": ["供应商视角不开放项目缺料计算"], "confidence": 0.0}
        material_ids = [str(x) for x in cmd.get("material_ids", [])]
        if not material_ids:
            material_ids = [str(row["material_id"]) for row in self.db.query_all("SELECT DISTINCT material_id FROM stock_balances WHERE warehouse_id IN (SELECT id FROM warehouses WHERE project_id=?)", (project_id,))]
        stock_rows = self.get_stock(project_id, material_ids=material_ids, warehouse_ids=cmd.get("warehouse_ids"), include_quarantined=True, actor=context)["items"]
        plan_rows = self.get_material_plan(project_id, material_ids, cmd.get("date_from"), cmd.get("date_to"), cmd.get("area_ids"), actor=context)["items"]
        material_rows = _rows(self.db.query_all("SELECT * FROM materials WHERE project_id=? AND id IN (" + ",".join("?" * len(material_ids)) + ")", tuple([project_id, *material_ids]))) if material_ids else []
        receipts = _rows(self.db.query_all("SELECT poi.material_id,po.expected_date,po.status,poi.qty_ordered,poi.qty_shipped,poi.qty_received FROM purchase_order_items poi JOIN purchase_orders po ON po.id=poi.order_id WHERE po.project_id=?", (project_id,)))
        for receipt in receipts:
            shipped = parse_decimal(receipt.get("qty_shipped", "0"))
            received = parse_decimal(receipt.get("qty_received", "0"))
            receipt["qty"] = decimal_string(max(Decimal("0"), shipped - received))
            receipt["eta"] = receipt.get("expected_date")
            if receipt.get("status") in {PurchaseOrderStatus.SENT.value, PurchaseOrderStatus.PARTIAL_SHIPPED.value, PurchaseOrderStatus.SHIPPED.value}:
                receipt["status"] = "IN_TRANSIT"
            else:
                receipt["status"] = "EXPECTED"
        result = calculate_shortage({**dict(cmd), "project_id": project_id, "material_ids": material_ids, "stock": stock_rows, "plans": plan_rows, "materials": material_rows, "receipts": receipts})
        material_names = {str(row["id"]): str(row["name"]) for row in material_rows}
        for item in result.get("items", []):
            item["material_name"] = material_names.get(str(item["material_id"]), str(item["material_id"]))
            area = self.db.query_one(
                "SELECT mp.area_id,a.name AS area_name,b.code AS building_code "
                "FROM material_plans mp LEFT JOIN areas a ON a.id=mp.area_id "
                "LEFT JOIN buildings b ON b.id=a.building_id "
                "WHERE mp.project_id=? AND mp.material_id=? ORDER BY mp.plan_date LIMIT 1",
                (project_id, item["material_id"]),
            )
            if area:
                item["area_id"] = area["area_id"]
                item["area_name"] = area["area_name"] or area["building_code"] or "项目范围"
            shortage_qty = parse_decimal(item.get("shortage_qty", "0"))
            safety_qty = parse_decimal(item.get("safety_stock", "0"))
            ratio = shortage_qty / safety_qty if safety_qty > 0 else (Decimal("1") if shortage_qty > 0 else Decimal("0"))
            item["risk_level"] = "HIGH" if ratio >= Decimal("0.5") else "MEDIUM" if ratio >= Decimal("0.2") else "LOW"
        result["project_id"] = project_id; return result

    def forecast_inventory(self, cmd: Mapping[str, Any], actor: Any = None) -> dict[str, Any]:
        project_id = self._project(str(cmd.get("project_id")))["id"]; context = self._context(actor, project_id); self.authorize(context, project_id, "read")
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            return {"project_id": project_id, "calculation_id": None, "calculation_version": "forecast-v1", "items": [], "warnings": ["供应商视角不开放库存预测"], "confidence": 0.0}
        material_ids = [str(x) for x in cmd.get("material_ids", [])]
        if not material_ids:
            material_ids = [str(row["material_id"]) for row in self.db.query_all("SELECT DISTINCT material_id FROM stock_balances WHERE warehouse_id IN (SELECT id FROM warehouses WHERE project_id=?)", (project_id,))]
        history = _rows(self.db.query_all("SELECT material_id,date,net_consumed_qty FROM consumption_snapshots WHERE project_id=? ORDER BY date", (project_id,)))
        stock = self.get_stock(project_id, material_ids=material_ids, include_quarantined=True, actor=context)["items"]
        plans = self.get_material_plan(project_id, material_ids, cmd.get("date_from"), cmd.get("date_to"), actor=context)["items"]
        return forecast_inventory({**dict(cmd), "material_ids": material_ids, "history": history, "stock": stock, "plans": plans, "project_id": project_id})

    def detect_consumption_anomaly(self, cmd: Mapping[str, Any], actor: Any = None) -> dict[str, Any]:
        project_id = self._project(str(cmd.get("project_id")))["id"]; context = self._context(actor, project_id); self.authorize(context, project_id, "read")
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            return {"project_id": project_id, "calculation_id": None, "items": [], "warnings": ["供应商视角不开放项目异常分析"]}
        actual = _rows(self.db.query_all("SELECT material_id,date,net_consumed_qty FROM consumption_snapshots WHERE project_id=? AND date>=? AND date<=?", (project_id, str(cmd.get("date_from"))[:10], str(cmd.get("date_to"))[:10])))
        planned = _rows(self.db.query_all("SELECT material_id,plan_date,planned_qty FROM material_plans WHERE project_id=? AND plan_date>=? AND plan_date<=?", (project_id, str(cmd.get("date_from"))[:10], str(cmd.get("date_to"))[:10])))
        return detect_consumption_anomaly({**dict(cmd), "actual": actual, "planned": planned})

    def run_anomaly_scan(self, cmd: Mapping[str, Any], actor: Any = None) -> dict[str, Any]:
        project_id = self._project(str(cmd.get("project_id")))["id"]
        context = self._context(actor, project_id)
        # Scanning persists anomaly records, therefore the public command's
        # idempotency key must cover both the calculation input and inserts.
        key = str(cmd.get("idempotency_key") or "")
        if key:
            with self.db.transaction() as conn:
                previous = self._idempotent_lookup(conn, "anomaly_scan", key, cmd)
                if previous is not None:
                    return previous

        result = self.detect_consumption_anomaly(cmd, context)
        with self.db.transaction() as conn:
            # Re-check after the read-only calculation.  Another request with
            # the same key may have completed while this one was calculating.
            if key:
                previous = self._idempotent_lookup(conn, "anomaly_scan", key, cmd)
                if previous is not None:
                    return previous
            persisted = []
            for item in result.get("items", []):
                material_id = item.get("material_id")
                dedupe = snapshot_hash({"material_id": material_id, "date": item.get("date"), "types": item.get("types")})
                existing = conn.execute("SELECT * FROM anomalies WHERE project_id=? AND dedupe_key=?", (project_id, dedupe)).fetchone()
                if existing:
                    persisted.append(_row(existing)); continue
                anomaly_id = str(uuid.uuid4()); severity = "HIGH" if "PLAN_VARIANCE" in item.get("types", []) else "MEDIUM"
                conn.execute("INSERT INTO anomalies(id,project_id,dedupe_key,material_id,severity,type,status,payload_json) VALUES(?,?,?,?,?,?,?,?)", (anomaly_id, project_id, dedupe, material_id, severity, ",".join(item.get("types", [])), "OPEN", _json(item)))
                persisted.append(_row(conn.execute("SELECT * FROM anomalies WHERE id=?", (anomaly_id,)).fetchone()))
            for item in persisted:
                if item:
                    item["payload"] = _loads(item.pop("payload_json", "{}"), {})
            response = {"calculation_id": result.get("calculation_id"), "items": persisted, "warnings": result.get("warnings", [])}
            if key:
                self._idempotent_store(conn, "anomaly_scan", key, cmd, response)
            return response

    def list_anomalies(self, project_id: str, status: str | None = None, severity: str | None = None, actor: Any = None) -> list[dict[str, Any]]:
        context = self._context(actor, project_id)
        project_id = self._project(project_id)["id"]
        self.authorize(context, project_id, "read")
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            return []
        clauses, params = ["project_id=?"], [project_id]
        if status: clauses.append("status=?"); params.append(status)
        if severity: clauses.append("severity=?"); params.append(severity)
        result = _rows(self.db.query_all("SELECT * FROM anomalies WHERE " + " AND ".join(clauses) + " ORDER BY rowid DESC", tuple(params)))
        for item in result:
            item["payload"] = _loads(item.pop("payload_json", "{}"), {})
            item["source_ref"] = f"anomaly:{item.get('dedupe_key', item.get('id'))}"
            if item.get("material_id"):
                material = self.db.query_one("SELECT name FROM materials WHERE id=?", (item["material_id"],))
                if material:
                    item["material_name"] = material["name"]
        return result

    def acknowledge_anomaly(self, anomaly_id: str, actor: Any = None, note: str = "", idempotency_key: str | None = None) -> dict[str, Any]:
        anomaly = self.db.query_one("SELECT * FROM anomalies WHERE id=?", (anomaly_id,))
        if not anomaly: raise DomainError("NOT_FOUND", "异常不存在")
        context = self._context(actor, anomaly["project_id"]); self.authorize(context, anomaly["project_id"], "purchase_approve")
        with self.db.transaction() as conn:
            command = {"anomaly_id": anomaly_id, "note": note}
            previous = self._idempotent_lookup(conn, "anomaly_ack", idempotency_key, command)
            if previous is not None:
                return previous
            if anomaly["status"] != "OPEN": raise DomainError("INVALID_STATE", "异常不是开放状态")
            conn.execute("UPDATE anomalies SET status='ACKNOWLEDGED',acknowledged_by=?,acknowledged_at=?,resolution_note=? WHERE id=?", (context.get("user_id"), iso_utc(utc_now()), note, anomaly_id))
            result = _row(conn.execute("SELECT * FROM anomalies WHERE id=?", (anomaly_id,)).fetchone()) or {}; result["payload"] = _loads(result.pop("payload_json", "{}"), {}); self._audit(conn, anomaly["project_id"], context, "ACK_ANOMALY", "anomaly", anomaly_id, before=dict(anomaly), after=result); self._idempotent_store(conn, "anomaly_ack", idempotency_key, command, result); return result

    def resolve_anomaly(self, anomaly_id: str, actor: Any = None, resolution_code: str = "", note: str = "", idempotency_key: str | None = None) -> dict[str, Any]:
        anomaly = self.db.query_one("SELECT * FROM anomalies WHERE id=?", (anomaly_id,))
        if not anomaly: raise DomainError("NOT_FOUND", "异常不存在")
        context = self._context(actor, anomaly["project_id"]); self.authorize(context, anomaly["project_id"], "purchase_approve")
        if not resolution_code.strip(): raise DomainError("VALIDATION_ERROR", "关闭异常必须填写 resolution_code")
        with self.db.transaction() as conn:
            command = {"anomaly_id": anomaly_id, "resolution_code": resolution_code, "note": note}
            previous = self._idempotent_lookup(conn, "anomaly_resolve", idempotency_key, command)
            if previous is not None:
                return previous
            if anomaly["status"] != "ACKNOWLEDGED": raise DomainError("INVALID_STATE", "异常必须先标记已知悉后才能关闭")
            conn.execute("UPDATE anomalies SET status='RESOLVED',resolved_by=?,resolved_at=?,resolution_code=?,resolution_note=? WHERE id=?", (context.get("user_id"), iso_utc(utc_now()), resolution_code, note, anomaly_id))
            result = _row(conn.execute("SELECT * FROM anomalies WHERE id=?", (anomaly_id,)).fetchone()) or {}; result["payload"] = _loads(result.pop("payload_json", "{}"), {}); self._audit(conn, anomaly["project_id"], context, "RESOLVE_ANOMALY", "anomaly", anomaly_id, before=dict(anomaly), after=result); self._idempotent_store(conn, "anomaly_resolve", idempotency_key, command, result); return result

    # ------------------------------------------------------------------
    # Evidence outbox and verification
    # ------------------------------------------------------------------
    def build_evidence_payload(self, event_type: str, business_record: Mapping[str, Any], documents: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        return build_evidence_payload(event_type, business_record, documents)

    def enqueue_evidence(self, event: Mapping[str, Any], actor: Any = None) -> dict[str, Any]:
        project_id = str(event.get("project_id")); context = self._context(actor, project_id); self.authorize(context, project_id, "evidence_anchor")
        with self.db.transaction() as conn:
            return self._enqueue_evidence_tx(conn, project_id, str(event.get("business_document_id") or event.get("id")), str(event.get("event_type")), event, context)

    def submit_evidence(self, event_id: str) -> dict[str, Any]:
        event = self.db.query_one("SELECT * FROM evidence_events WHERE id=?", (event_id,))
        if not event: raise DomainError("NOT_FOUND", "证据事件不存在")
        if event["status"] not in {EvidenceStatus.PENDING.value, EvidenceStatus.RETRYING.value}: raise DomainError("INVALID_STATE", "证据事件不允许提交")
        outbox = self.db.query_one("SELECT * FROM evidence_outbox WHERE evidence_event_id=?", (event_id,)); payload = _loads(outbox["payload_json"], {}) if outbox else {}
        try:
            submission = self.evidence_adapter.submit(event_id, payload)
        except ChainUnavailable as exc:
            with self.db.transaction() as conn:
                current = conn.execute("SELECT * FROM evidence_events WHERE id=?", (event_id,)).fetchone(); retry = int(current["retry_count"]) + 1
                status = EvidenceStatus.RETRYING.value if retry < self.settings.evidence_max_retries else EvidenceStatus.FAILED.value
                backoff = self.settings.evidence_backoff_seconds[min(retry - 1, len(self.settings.evidence_backoff_seconds) - 1)] if self.settings.evidence_backoff_seconds else 60
                next_time = iso_utc(utc_now() + timedelta(seconds=backoff)); conn.execute("UPDATE evidence_events SET status=?,retry_count=?,next_retry_at=? WHERE id=?", (status, retry, next_time, event_id)); conn.execute("UPDATE evidence_outbox SET status=?,attempt_count=?,next_attempt_at=?,last_error=? WHERE evidence_event_id=?", (status, retry, next_time, str(exc), event_id)); return {"id": event_id, "status": status, "retry_count": retry, "next_retry_at": next_time, "error": "CHAIN_UNAVAILABLE"}
        with self.db.transaction() as conn:
            conn.execute("UPDATE evidence_events SET status='SUBMITTED',tx_id=?,block_no=? WHERE id=?", (submission.tx_id, submission.block_no, event_id)); conn.execute("UPDATE evidence_outbox SET status='SUBMITTED',attempt_count=attempt_count+1 WHERE evidence_event_id=?", (event_id,)); return {"id": event_id, "status": EvidenceStatus.SUBMITTED.value, "tx_id": submission.tx_id, "block_no": submission.block_no}

    def confirm_evidence(self, event_id: str) -> dict[str, Any]:
        event = self.db.query_one("SELECT * FROM evidence_events WHERE id=?", (event_id,))
        if not event: raise DomainError("NOT_FOUND", "证据事件不存在")
        if event["status"] not in {EvidenceStatus.SUBMITTED.value, EvidenceStatus.CONFIRMED.value}: raise DomainError("INVALID_STATE", "证据尚未提交")
        with self.db.transaction() as conn:
            conn.execute("UPDATE evidence_events SET status='CONFIRMED' WHERE id=?", (event_id,)); result = _row(conn.execute("SELECT * FROM evidence_events WHERE id=?", (event_id,)).fetchone()) or {}; return result

    def retry_failed_evidence(self, event_id: str, actor: Any = None, idempotency_key: str | None = None) -> dict[str, Any]:
        event = self.db.query_one("SELECT * FROM evidence_events WHERE id=?", (event_id,))
        if not event: raise DomainError("NOT_FOUND", "证据事件不存在")
        context = self._context(actor, event["project_id"]); self.authorize(context, event["project_id"], "evidence_anchor")
        # Internal workers may call the service without an HTTP header; use a
        # deterministic retry key for that trusted path.  The public API
        # still requires an explicit Idempotency-Key before reaching here.
        key = str(idempotency_key or f"retry:{event_id}:{event['retry_count']}")
        with self.db.transaction() as conn:
            command = {"event_id": event_id}
            previous = self._idempotent_lookup(conn, "evidence_retry", key, command)
            if previous is not None:
                return previous
            if event["status"] not in {EvidenceStatus.FAILED.value, EvidenceStatus.RETRYING.value}:
                raise DomainError("INVALID_STATE", "仅失败或重试中的事件可重试")
            conn.execute("UPDATE evidence_events SET status='RETRYING' WHERE id=?", (event_id,))
        result = self.submit_evidence(event_id)
        with self.db.transaction() as conn:
            self._idempotent_store(conn, "evidence_retry", key, command, result)
        return result

    def issue_evidence_anchor_token(self, event_id: str, actor: Any = None, idempotency_key: str | None = None) -> dict[str, Any]:
        event = self.db.query_one("SELECT * FROM evidence_events WHERE id=?", (event_id,))
        if not event:
            raise DomainError("NOT_FOUND", "证据事件不存在")
        context = self._context(actor, event["project_id"])
        self.authorize(context, event["project_id"], "evidence_anchor")
        if event["status"] not in {EvidenceStatus.PENDING.value, EvidenceStatus.RETRYING.value}:
            raise DomainError("INVALID_STATE", "当前证据事件不可锚定")
        payload = {"payload_hash": event["payload_hash"], "status": event["status"]}
        token_result = self._issue_confirmation_token_idempotently("evidence_anchor", event["id"], payload, context.get("user_id"), idempotency_key)
        return {"event_id": event["id"], **token_result, "payload_hash": self._payload_hash(payload)}

    def anchor_evidence(
        self,
        event_id: str,
        confirmation_token: str | None,
        actor: Any = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        event = self.db.query_one("SELECT * FROM evidence_events WHERE id=?", (event_id,))
        if not event:
            raise DomainError("NOT_FOUND", "证据事件不存在")
        context = self._context(actor, event["project_id"])
        self.authorize(context, event["project_id"], "evidence_anchor")
        key = str(idempotency_key) if idempotency_key else None
        # The token digest is safe to include in the idempotency fingerprint;
        # the bearer token itself never enters the request log/table.  Keep the
        # database lock held through submission so concurrent retries with the
        # same key cannot both consume the token or call the adapter twice.
        command = {
            "event_id": event["id"],
            "payload_hash": event["payload_hash"],
            "confirmation_token_hash": hashlib.sha256(str(confirmation_token or "").encode()).hexdigest(),
        }
        with self.db.transaction() as conn:
            if key:
                previous = self._idempotent_lookup(conn, "evidence_anchor", key, command)
                if previous is not None:
                    return previous
            current = conn.execute("SELECT * FROM evidence_events WHERE id=?", (event_id,)).fetchone()
            if not current:
                raise DomainError("NOT_FOUND", "证据事件不存在")
            if current["status"] not in {EvidenceStatus.PENDING.value, EvidenceStatus.RETRYING.value}:
                raise DomainError("INVALID_STATE", "当前证据事件不可锚定")
            payload = {"payload_hash": current["payload_hash"], "status": current["status"]}
            self._consume_token(conn, confirmation_token, "evidence_anchor", current["id"], payload, context.get("user_id"))
            result = self.submit_evidence(event_id)
            if key:
                self._idempotent_store(conn, "evidence_anchor", key, command, result)
            return result

    def verify_evidence(self, event_id: str, actor: Any = None) -> dict[str, Any]:
        event = self.db.query_one("SELECT * FROM evidence_events WHERE id=?", (event_id,))
        if not event: raise DomainError("NOT_FOUND", "证据事件不存在")
        context = self._context(actor, event["project_id"]); self.authorize(context, event["project_id"], "read")
        outbox = self.db.query_one("SELECT payload_json FROM evidence_outbox WHERE evidence_event_id=?", (event_id,)); payload = _loads(outbox["payload_json"], {}) if outbox else {}; db_hash = hash_payload(payload); chain = self.evidence_adapter.get(event_id)
        if chain is None and event["tx_id"] and isinstance(self.evidence_adapter, MockEvidenceAdapter):
            chain = EvidenceSubmission(str(event["tx_id"]), event["block_no"], str(event["payload_hash"]))
            self.evidence_adapter.restore(event_id, chain)
        matches = bool(chain and chain.payload_hash == db_hash)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO evidence_verifications(id,evidence_event_id,checked_at,chain_payload_hash,database_payload_hash,matches,tx_exists,verifier_id) VALUES(?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), event_id, iso_utc(utc_now()), chain.payload_hash if chain else None, db_hash, int(matches), int(bool(chain)), context.get("user_id")))
        return {"event_id": event_id, "database_payload_hash": db_hash, "chain_payload_hash": chain.payload_hash if chain else None, "matches": matches, "tx_exists": bool(chain), "tx_id": chain.tx_id if chain else event["tx_id"]}

    # ------------------------------------------------------------------
    # Documents, daily reports and action lists
    # ------------------------------------------------------------------
    def put_business_document(self, project_id: str, document_type: str, bytes_or_stream: Any, actor: Any = None, mime: str = "application/octet-stream", filename: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        allowed_types = {"CERTIFICATE", "TEST_REPORT", "INSPECTION_FORM", "HANDOVER_FORM", "OTHER"}
        allowed_mimes = {"application/pdf", "image/jpeg", "image/png", "text/plain"}
        if document_type not in allowed_types or mime not in allowed_mimes: raise DomainError("VALIDATION_ERROR", "文件类型或 MIME 不允许")
        data = bytes(bytes_or_stream.read() if hasattr(bytes_or_stream, "read") else bytes_or_stream)
        if len(data) > 20 * 1024 * 1024: raise DomainError("VALIDATION_ERROR", "文件超过 20 MiB 限制")
        project_id = self._project(project_id)["id"]
        context = self._context(actor, project_id); self.authorize(context, project_id, "read")
        digest = hash_document(data); document_id = str(uuid.uuid4()); object_key = f"{project_id}/{document_id}"
        path = self.document_root / project_id / document_id
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        with self.db.transaction() as conn:
            command = {"project_id": project_id, "document_type": document_type, "mime": mime, "sha256": digest}
            if idempotency_key:
                previous = self._idempotent_lookup(conn, "document_upload", idempotency_key, command)
                if previous is not None:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return previous
            conn.execute("INSERT INTO documents(id,project_id,document_type,object_key,mime,size_bytes,sha256,uploader_id,version,uploaded_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (document_id, project_id, document_type, object_key, mime, len(data), digest, context.get("user_id"), 1, iso_utc(utc_now())))
            result = _row(conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()) or {}; self._audit(conn, project_id, context, "UPLOAD_DOCUMENT", "document", document_id, after={"document_type": document_type, "sha256": digest});
            if idempotency_key:
                self._idempotent_store(conn, "document_upload", idempotency_key, command, result)
            return result

    def get_document_metadata(self, document_id: str, actor: Any = None) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM documents WHERE id=?", (document_id,))
        if not row: raise DomainError("NOT_FOUND", "文件不存在")
        self.authorize(actor, row["project_id"], "read"); result = _row(row) or {}; result.pop("object_key", None); return result

    def get_document_download_url(self, document_id: str, actor: Any = None, ttl_seconds: int = 300) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM documents WHERE id=?", (document_id,))
        if not row: raise DomainError("NOT_FOUND", "文件不存在")
        self.authorize(actor, row["project_id"], "read")
        expires = utc_now() + timedelta(seconds=min(max(int(ttl_seconds), 1), 900))
        stamp = int(expires.timestamp())
        signature = self._document_download_signature(document_id, stamp)
        return {"url": f"/api/v1/projects/{row['project_id']}/documents/{document_id}/download?expires={stamp}&signature={signature}", "expires_at": iso_utc(expires)}

    @staticmethod
    def _document_download_signature(document_id: str, expires: int) -> str:
        secret = os.getenv("DOCUMENT_SIGNING_SECRET", "zhufulian-demo-document-secret")
        message = f"{document_id}:{int(expires)}".encode("utf-8")
        return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

    def validate_document_download(self, document_id: str, expires: int | None, signature: str | None) -> None:
        if expires is None or not signature:
            raise DomainError("TOKEN_EXPIRED", "文件下载链接无效或已过期")
        try:
            expires_at = int(expires)
        except (TypeError, ValueError) as exc:
            raise DomainError("VALIDATION_ERROR", "文件下载链接的过期时间无效") from exc
        if expires_at < int(utc_now().timestamp()):
            raise DomainError("TOKEN_EXPIRED", "文件下载链接已过期")
        expected = self._document_download_signature(document_id, expires_at)
        if not hmac.compare_digest(expected, str(signature)):
            raise DomainError("FORBIDDEN", "文件下载链接无效")

    def create_daily_report_draft(self, cmd: Mapping[str, Any], actor: Any = None) -> dict[str, Any]:
        project_id = self._project(str(cmd.get("project_id")))["id"]
        context = self._context(actor, project_id)
        self.authorize(context, project_id, "read")
        report_date = str(cmd.get("report_date") or "")[:10]
        try:
            report_day = date.fromisoformat(report_date)
        except ValueError as exc:
            raise DomainError("VALIDATION_ERROR", "report_date 必须是 ISO 日期") from exc

        arrivals = _rows(self.db.query_all(
            "SELECT d.*,s.shipment_no,w.code AS warehouse_code,w.name AS warehouse_name "
            "FROM deliveries d JOIN shipments s ON s.id=d.shipment_id "
            "JOIN warehouses w ON w.id=d.warehouse_id "
            "WHERE d.project_id=? AND substr(d.arrived_at,1,10)=? AND d.status='ACCEPTED'",
            (project_id, report_date),
        ))
        for arrival in arrivals:
            arrival["source_ref"] = f"delivery:{arrival.get('delivery_no', arrival.get('id'))}"
            arrival["items"] = _rows(self.db.query_all(
                "SELECT di.*,si.material_id,m.name AS material_name,m.unit,b.batch_no "
                "FROM delivery_items di JOIN shipment_items si ON si.id=di.shipment_item_id "
                "JOIN materials m ON m.id=si.material_id LEFT JOIN batches b ON b.id=si.batch_id "
                "WHERE di.delivery_id=? ORDER BY di.id",
                (arrival["id"],),
            ))
        issues = _rows(self.db.query_all(
            "SELECT sm.*,m.name AS material_name,m.unit,w.code AS warehouse_code "
            "FROM stock_moves sm JOIN materials m ON m.id=sm.material_id "
            "LEFT JOIN warehouses w ON w.id=sm.from_warehouse_id "
            "WHERE sm.project_id=? AND substr(sm.occurred_at,1,10)=? AND sm.move_type='ISSUE' ORDER BY sm.occurred_at",
            (project_id, report_date),
        ))
        for issue in issues:
            issue["source_ref"] = f"stock_move:{issue.get('id')}"
        anomalies = self.list_anomalies(project_id, actor=context)
        ppe = self.get_ppe_coverage(project_id, as_of=report_date, actor=context)
        try:
            shortage = self.calculate_shortage(
                {"project_id": project_id, "date_from": report_date, "date_to": (report_day + timedelta(days=14)).isoformat()},
                context,
            )
        except (DomainError, ValueError):
            # A report remains publishable when a project has no inventory
            # snapshot yet; the missing calculation is explicit in warnings
            # instead of being replaced by invented risk rows.
            shortage = {"items": [], "calculation_id": None, "warnings": ["MISSING_DATA"]}
        shortage_risks = [item for item in shortage.get("items", []) if parse_decimal(item.get("shortage_qty", "0")) > 0]
        for item in shortage_risks:
            item["calculation_id"] = shortage.get("calculation_id")
        for item in ppe.get("items", []):
            item["calculation_id"] = ppe.get("calculation_id")
        todos = _rows(self.db.query_all(
            "SELECT id,title,owner_role,due_at,priority,source_ref,status,note FROM todos "
            "WHERE project_id=? AND status NOT IN ('DONE','CLOSED') ORDER BY due_at,id",
            (project_id,),
        ))
        warnings: list[dict[str, Any]] = []
        if not arrivals:
            warnings.append({"message_key": "daily.no_arrivals"})
        if not issues:
            warnings.append({"message_key": "daily.no_issues"})
        if not ppe.get("items"):
            warnings.append({"message_key": "daily.no_ppe"})
        shortage_ref = str(shortage.get("calculation_id") or "shortage:missing")
        ppe_ref = str(ppe.get("calculation_id") or "ppe:missing")
        fact_refs = [
            str(item["source_ref"])
            for item in [*arrivals, *issues]
            if item.get("source_ref")
        ]
        if not fact_refs:
            fact_refs = ["deliveries", "stock_moves"]
        claims = [
            {"type": "FACT", "message_key": "daily.arrivals", "message_params": {}, "source_refs": fact_refs},
            {"type": "CALCULATION", "message_key": "daily.shortage", "message_params": {"count": len(shortage_risks)}, "source_refs": [shortage_ref]},
            {"type": "CALCULATION", "message_key": "daily.ppe", "message_params": {"count": len(ppe.get("items", []))}, "source_refs": [ppe_ref]},
        ]
        payload = {
            "date": report_date,
            "arrivals": arrivals,
            "issues": issues,
            "consumption": issues,
            "shortage_risks": shortage_risks,
            "shortage_calculation_id": shortage.get("calculation_id"),
            "ppe_coverage": ppe.get("items", []),
            "ppe_calculation_id": ppe.get("calculation_id"),
            "todo": todos,
            "anomalies": anomalies,
            "claims": claims,
            "warnings": warnings,
            "data_completeness": round(sum(bool(section) for section in (arrivals, issues, shortage_risks, ppe.get("items"), todos, anomalies)) / 6, 4),
        }
        draft_id, digest = str(uuid.uuid4()), self._payload_hash(payload); expires = utc_now() + timedelta(hours=24); key = str(cmd.get("idempotency_key") or str(uuid.uuid4()))
        with self.db.transaction() as conn:
            previous = self._idempotent_lookup(conn, "daily_report_draft", key, payload)
            if previous is not None:
                return previous
            conn.execute("INSERT INTO drafts(id,project_id,draft_type,payload_json,payload_hash,status,expires_at,created_by,version,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?)", (draft_id, project_id, "DAILY_REPORT", _json(payload), digest, DraftStatus.PENDING_CONFIRMATION.value, iso_utc(expires), context.get("user_id"), 1, key)); token = self.issue_confirmation_token("daily_report_publish", draft_id, {"version": 1, "payload_hash": digest}, context.get("user_id")); conn.execute("UPDATE drafts SET confirmation_token_hash=? WHERE id=?", (hashlib.sha256(token.encode()).hexdigest(), draft_id)); result = {"draft_id": draft_id, "draft_type": "DAILY_REPORT", "status": DraftStatus.PENDING_CONFIRMATION.value, "payload": payload, "payload_hash": digest, "expires_at": iso_utc(expires), "confirmation_token": token}; self._audit(conn, project_id, context, "CREATE_DAILY_REPORT_DRAFT", "draft", draft_id, after={"status": DraftStatus.PENDING_CONFIRMATION.value, "payload_hash": digest}); self._idempotent_store(conn, "daily_report_draft", key, payload, result); return result

    def publish_daily_report(
        self,
        draft_id: str,
        confirmation_token: str,
        actor: Any = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        draft = self.db.query_one("SELECT * FROM drafts WHERE id=? AND draft_type='DAILY_REPORT'", (draft_id,))
        if not draft: raise DomainError("NOT_FOUND", "日报草稿不存在")
        context = self._context(actor, draft["project_id"]); self.authorize(context, draft["project_id"], "purchase_approve")
        key = str(idempotency_key) if idempotency_key else None
        with self.db.transaction() as conn:
            # Publishing is a write command at the HTTP boundary.  Keep its
            # replay record separate from draft creation so a retry returns
            # the original report without consuming a token twice.
            # Include only the token digest in the idempotency payload.  A
            # different token with the same key is a different request, while
            # the secret token value never reaches the idempotency table.
            command = {
                "draft_id": draft_id,
                "confirmation_token_hash": hashlib.sha256(str(confirmation_token or "").encode()).hexdigest(),
            }
            if key:
                previous = self._idempotent_lookup(conn, "daily_report_publish", key, command)
                if previous is not None:
                    return previous
            if draft["status"] != DraftStatus.PENDING_CONFIRMATION.value: raise DomainError("INVALID_STATE", "日报草稿已处理")
            self._consume_token(conn, confirmation_token, "daily_report_publish", draft_id, {"version": draft["version"], "payload_hash": draft["payload_hash"]}, context.get("user_id"))
            payload = _loads(draft["payload_json"], {})
            report_date = payload.get("date")
            latest = conn.execute(
                "SELECT id,version FROM daily_reports WHERE project_id=? AND report_date=? ORDER BY version DESC LIMIT 1",
                (draft["project_id"], report_date),
            ).fetchone()
            previous_report_id = str(latest["id"]) if latest else None
            next_version = int(latest["version"]) + 1 if latest else 1
            report_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO daily_reports(id,project_id,report_date,payload_json,version,supersedes_report_id,published_by,published_at) VALUES(?,?,?,?,?,?,?,?)",
                (report_id, draft["project_id"], report_date, _json(payload), next_version, previous_report_id, context.get("user_id"), iso_utc(utc_now())),
            )
            conn.execute("UPDATE drafts SET status='EXECUTED',confirmed_by=?,confirmed_at=?,version=version+1 WHERE id=?", (context.get("user_id"), iso_utc(utc_now()), draft_id))
            result = {"id": report_id, "project_id": draft["project_id"], "report_date": report_date, "version": next_version, "supersedes_report_id": previous_report_id, "payload": payload}
            self._audit(
                conn,
                draft["project_id"],
                context,
                "PUBLISH_DAILY_REPORT",
                "daily_report",
                report_id,
                before={"draft_id": draft_id, "draft_status": DraftStatus.PENDING_CONFIRMATION.value},
                after=result,
            )
            if key:
                self._idempotent_store(conn, "daily_report_publish", key, command, result)
            return result

    def list_daily_reports(self, project_id: str, date_from: Any, date_to: Any, page: int = 1, page_size: int = 50, actor: Any = None) -> dict[str, Any]:
        project_id = self._project(project_id)["id"]
        self.authorize(actor, project_id, "read"); page, page_size = max(1, int(page)), min(200, max(1, int(page_size))); total = self.db.query_one("SELECT COUNT(*) AS n FROM daily_reports WHERE project_id=? AND report_date>=? AND report_date<=?", (project_id, str(date_from)[:10], str(date_to)[:10]))["n"]; rows = _rows(self.db.query_all("SELECT * FROM daily_reports WHERE project_id=? AND report_date>=? AND report_date<=? ORDER BY report_date DESC,version DESC LIMIT ? OFFSET ?", (project_id, str(date_from)[:10], str(date_to)[:10], page_size, (page - 1) * page_size))); [item.update({"payload": _loads(item.pop("payload_json", "{}"), {})}) for item in rows]; return {"items": rows, "total": total, "page": page, "page_size": page_size, "next_cursor": None}

    def create_todo(self, cmd: Mapping[str, Any], actor: Any = None) -> dict[str, Any]:
        project_id = self._project(str(cmd.get("project_id")))["id"]; context = self._context(actor, project_id); self.authorize(context, project_id, "read"); key = str(cmd.get("idempotency_key") or "")
        if not key: raise DomainError("VALIDATION_ERROR", "缺少幂等键")
        with self.db.transaction() as conn:
            previous = self._idempotent_lookup(conn, "todo", key, cmd)
            if previous is not None: return previous
            todo_id = str(uuid.uuid4()); conn.execute("INSERT INTO todos(id,project_id,title,owner_role,due_at,priority,source_ref,status,note,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?)", (todo_id, project_id, str(cmd.get("title")), str(cmd.get("owner_role")), iso_utc(cmd.get("due_at")), str(cmd.get("priority", "MEDIUM")), str(cmd.get("source_ref")), "OPEN", None, key)); result = _row(conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()) or {}; self._audit(conn, project_id, context, "CREATE_TODO", "todo", todo_id, after=result); self._idempotent_store(conn, "todo", key, cmd, result); return result

    def update_todo(self, todo_id: str, patch: Mapping[str, Any], actor: Any = None, idempotency_key: str | None = None) -> dict[str, Any]:
        todo = self.db.query_one("SELECT * FROM todos WHERE id=?", (todo_id,))
        if not todo: raise DomainError("NOT_FOUND", "待办不存在")
        context = self._context(actor, todo["project_id"]); self.authorize(context, todo["project_id"], "read")
        allowed = {"title", "owner_role", "due_at", "priority", "status", "note"}; unknown = set(patch) - allowed
        if unknown: raise DomainError("VALIDATION_ERROR", "请求包含未知字段", {"fields": sorted(unknown)})
        with self.db.transaction() as conn:
            command = {"todo_id": todo_id, **dict(patch)}
            previous = self._idempotent_lookup(conn, "todo_update", idempotency_key, command)
            if previous is not None:
                return previous
            before = dict(todo); values = {key: patch[key] for key in allowed if key in patch}; values["due_at"] = iso_utc(values["due_at"]) if "due_at" in values else todo["due_at"]; values.update({key: todo[key] for key in allowed if key not in values}); conn.execute("UPDATE todos SET title=?,owner_role=?,due_at=?,priority=?,status=?,note=? WHERE id=?", (values["title"], values["owner_role"], values["due_at"], values["priority"], values["status"], values["note"], todo_id)); result = _row(conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()) or {}; self._audit(conn, todo["project_id"], context, "UPDATE_TODO", "todo", todo_id, before=before, after=result); self._idempotent_store(conn, "todo_update", idempotency_key, command, result); return result

    def list_todos(self, project_id: str, status: str | None = None, owner_role: str | None = None, actor: Any = None) -> list[dict[str, Any]]:
        context = self._context(actor, project_id)
        project_id = self._project(project_id)["id"]
        self.authorize(context, project_id, "read")
        if "SUPPLIER" in self._roles(context) and "PROJECT_OWNER" not in self._roles(context):
            return []
        clauses, params = ["project_id=?"], [project_id]
        if status: clauses.append("status=?"); params.append(status)
        if owner_role: clauses.append("owner_role=?"); params.append(owner_role)
        return _rows(self.db.query_all("SELECT * FROM todos WHERE " + " AND ".join(clauses) + " ORDER BY due_at", tuple(params)))

    def seed_demo(self) -> dict[str, Any]:
        return self.db.seed_demo()

    def reset_demo(self) -> None:
        self.db.reset_demo()


__all__ = ["BusinessService", "DomainError"]
