"""FastAPI adapter for the business service.

The adapter is intentionally thin: authentication context is read from the
server-side session/header shim, while every mutation delegates to
``BusinessService``.  Deployments can replace the header shim with OIDC
middleware without changing routes or domain behavior.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .db import Database
from .i18n import current_locale, locale_from_headers, render_for_locale, reset_current_locale, set_current_locale, translate_key
from .service import BusinessService, DomainError

try:  # FastAPI is an installable production dependency, not needed by pure tests.
    from fastapi import Body, FastAPI, File, Header, Query, Request, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, Response
except ImportError:  # pragma: no cover - exercised only in dependency-free shells
    FastAPI = None  # type: ignore[assignment]
    Body = File = Header = Query = Request = UploadFile = None  # type: ignore[assignment]
    CORSMiddleware = None  # type: ignore[assignment]
    JSONResponse = Response = None  # type: ignore[assignment]


def _stable_hash(value: Any) -> str:
    """Hash structured AI inputs without accepting arbitrary executable data."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _run_ai_message(
    svc: BusinessService,
    project_id: str,
    thread_id: str,
    request_text: str,
    actor: dict[str, Any],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Execute the MVP's deterministic, allow-listed agent workflow.

    This is deliberately a tool planner rather than a free-form database
    assistant.  It persists a run plus every tool call, and only the explicit
    purchase-draft intent is allowed to create a reversible draft.
    """

    project = svc._project(project_id)
    pid = project["id"]
    text = request_text.strip()
    command = {
        "project_id": pid,
        "thread_id": thread_id,
        "request_text": text,
        "user_id": actor.get("user_id"),
    }
    if idempotency_key:
        with svc.db.transaction() as conn:
            previous = svc._idempotent_lookup(conn, "ai_message", idempotency_key, command)
            if previous is not None:
                return previous
    normalized_text = text.lower()
    if any(term in normalized_text for term in ("待审批", "待審批", "查看采购申请", "查看採購申請", "采购申请进度", "採購申請進度", "订单状态", "訂單狀態", "purchase request", "purchase order", "approval status")):
        intent = "PURCHASE_QUERY"
    elif any(term in normalized_text for term in ("缺", "短缺", "補貨", "补货", "採購", "采购", "shortage", "replenish", "restock", "procurement")):
        intent = "DRAFT_PURCHASE" if any(term in normalized_text for term in ("草稿", "draft", "创建采购申请", "建立採購申請", "create purchase request", "補貨", "补货")) else "SHORTAGE_PREDICTION"
    elif any(term in normalized_text for term in ("防护", "防護", "安全帽", "手套", "ppe", "班组", "班組", "safety helmet", "glove", "crew")):
        intent = "PPE_CHECK"
    elif any(term in normalized_text for term in ("异常", "異常", "偏差", "消耗", "anomaly", "variance", "consumption")):
        intent = "ANOMALY_ANALYSIS"
    elif any(term in normalized_text for term in ("追溯", "批次", "鏈", "链", "trace", "batch", "evidence")):
        intent = "TRACE_QUERY"
    elif any(term in normalized_text for term in ("库存", "庫存", "可用量", "台账", "台帳", "inventory", "stock", "available quantity", "ledger")):
        intent = "STOCK_QUERY"
    else:
        intent = "UNKNOWN"

    run = svc.start_ai_run(
        pid,
        thread_id,
        text,
        intent,
        _stable_hash({"project_id": pid, "text": text}),
        actor,
    )
    run_id = str(run["run_id"])
    started_at = str(run["started_at"])

    tool_refs: list[str] = []
    facts: list[dict[str, Any]] = []
    calculations: list[dict[str, Any]] = []
    hypotheses: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    next_actions: list[dict[str, Any]] = []

    def message(key: str, **params: Any) -> dict[str, Any]:
        return {"message_key": key, "message_params": params}

    def claim(kind: str, key: str, refs: list[Any], **params: Any) -> dict[str, Any]:
        return {"type": kind, **message(key, **params), "source_refs": refs}

    def call_tool(name: str, arguments: dict[str, Any], callback):
        input_hash = _stable_hash(arguments)
        call_id = str(uuid.uuid4())
        try:
            output = callback()
            status, error_code = "SUCCEEDED", None
        except DomainError as exc:
            output = {"error": exc.as_dict(actor.get("locale"))}
            status, error_code = "FAILED", exc.code
        with svc.db.transaction() as conn:
            conn.execute(
                "INSERT INTO ai_tool_calls(id,ai_run_id,tool_name,tool_version,input_json,input_hash,output_json,status,started_at,ended_at,permission_decision,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (call_id, run_id, name, "mvp-v1", json.dumps(arguments, ensure_ascii=False, default=str), input_hash, json.dumps(output, ensure_ascii=False, default=str), status, started_at, date.today().isoformat() + "T00:00:00Z", "ALLOW", error_code),
            )
        tool_refs.append(f"tool:{name}:{call_id}")
        if status == "FAILED":
            warnings.append(message("ai.tool_failed", tool=name, message=output["error"]["message"]))
            return None
        return output

    status = "COMPLETED"
    draft_id: str | None = None
    if intent in {"SHORTAGE_PREDICTION", "DRAFT_PURCHASE"}:
        start = date.today().isoformat()
        end = (date.today() + timedelta(days=14)).isoformat()
        shortage = call_tool(
            "calculate_shortage",
            {"project_id": pid, "date_from": start, "date_to": end, "include_expected_receipts": True},
            lambda: svc.calculate_shortage({"project_id": pid, "date_from": start, "date_to": end, "include_expected_receipts": True}, actor),
        )
        if shortage:
            risk_items = [item for item in shortage.get("items", []) if item.get("shortage_qty") not in {"0", "0.0", "0.000"}]
            calculations.append(claim("CALCULATION", "ai.shortage.summary", [shortage.get("calculation_id"), *tool_refs[-1:]], count=len(risk_items)))
            for item in risk_items[:3]:
                facts.append(claim("FACT", "ai.shortage.risk", [shortage.get("calculation_id"), *item.get("source_refs", [])], material_id=item["material_id"], shortage_date=item.get("shortage_date") or "current_window", shortage_qty=item["shortage_qty"], unit=item["unit"]))
            if shortage.get("missing_data"):
                warnings.append(message("ai.shortage.missing_data", items=", ".join(shortage["missing_data"])))
                next_actions.append(message("ai.shortage.fill_data"))
            missing_ids = {str(value).split(":", 1)[0] for value in shortage.get("missing_data", [])}
            draftable_items = [item for item in risk_items if str(item.get("material_id")) not in missing_ids]
            if shortage.get("missing_data"):
                warnings.append(message("ai.shortage.complete_data"))
            if intent == "DRAFT_PURCHASE" and draftable_items:
                warehouse = svc.db.query_one("SELECT id FROM warehouses WHERE project_id=? ORDER BY code LIMIT 1", (pid,))
                selected = [
                    {
                        "material_id": item["material_id"],
                        "qty": item["recommended_order_qty"],
                        "unit": item["unit"],
                        "warehouse_id": warehouse["id"] if warehouse else None,
                        "reason": "AI_SHORTAGE_RECOMMENDATION",
                    }
                    for item in draftable_items
                ]
                draft = call_tool(
                    "draft_material_request",
                    {"project_id": pid, "calculation_id": shortage.get("calculation_id"), "items": selected},
                    lambda: svc.draft_material_request(shortage, selected, draftable_items[0].get("shortage_date") or end, actor=actor),
                )
                if draft:
                    draft_id = draft["draft_id"]
                    status = "WAITING_CONFIRMATION"
                    facts.append(claim("FACT", "ai.shortage.created_draft", [draft_id, shortage.get("calculation_id")]))
                    next_actions.append(message("ai.shortage.review_draft"))
            elif intent == "DRAFT_PURCHASE":
                next_actions.append(message("ai.shortage.retry_draft"))
            else:
                next_actions.append(message("ai.shortage.create_draft"))
    elif intent == "PURCHASE_QUERY":
        requests = call_tool(
            "list_purchase_requests",
            {"project_id": pid, "status": "PENDING_APPROVAL"},
            lambda: svc.list_purchase_requests(pid, "PENDING_APPROVAL", actor),
        )
        if requests:
            rows = requests.get("items", [])
            facts.append(claim("FACT", "ai.purchase.summary", [*requests.get("source_refs", []), *tool_refs[-1:]], count=len(rows)))
            for request in rows[:5]:
                facts.append(claim("FACT", "ai.purchase.item", [f"purchase_request:{request.get('id')}"], request_no=request.get("request_no"), status=request.get("status"), needed_by=request.get("needed_by") or "not_set"))
            next_actions.append(message("ai.purchase.open"))
        else:
            warnings.append(message("ai.purchase.none"))
            next_actions.append(message("ai.shortage.create_draft"))
    elif intent == "PPE_CHECK":
        coverage = call_tool(
            "check_ppe_coverage",
            {"project_id": pid, "as_of": date.today().isoformat()},
            lambda: svc.get_ppe_coverage(pid, as_of=date.today().isoformat(), actor=actor),
        )
        if coverage:
            deficits = [item for item in coverage.get("items", []) if item.get("shortage_qty") not in {"0", "0.0", "0.000"}]
            calculations.append(claim("CALCULATION", "ai.ppe.summary", [coverage.get("calculation_id"), *tool_refs[-1:]], count=len(deficits)))
            facts.append(claim("FACT", "ai.ppe.privacy", [coverage.get("calculation_id")]))
            next_actions.append(message("ai.ppe.open"))
    elif intent == "ANOMALY_ANALYSIS":
        start = (date.today() - timedelta(days=14)).isoformat()
        end = date.today().isoformat()
        anomalies = call_tool(
            "detect_consumption_anomaly",
            {"project_id": pid, "date_from": start, "date_to": end},
            lambda: svc.detect_consumption_anomaly({"project_id": pid, "date_from": start, "date_to": end}, actor),
        )
        if anomalies:
            calculations.append(claim("CALCULATION", "ai.anomaly.summary", [anomalies.get("calculation_id"), *tool_refs[-1:]], count=len(anomalies.get("items", []))))
            hypotheses.append(claim("HYPOTHESIS", "ai.anomaly.caution", [anomalies.get("calculation_id")]))
            next_actions.append(message("ai.anomaly.open"))
    elif intent == "TRACE_QUERY":
        batch = svc.db.query_one("SELECT id FROM batches WHERE material_id IN (SELECT id FROM materials WHERE project_id=?) ORDER BY batch_no LIMIT 1", (pid,))
        trace = call_tool(
            "get_batch_trace",
            {"project_id": pid, "batch_id": batch["id"] if batch else None},
            lambda: svc.get_batch_trace(pid, batch_id=batch["id"] if batch else None, actor=actor),
        )
        if trace:
            facts.append(claim("FACT", "ai.trace.summary", [*trace.get("source_refs", []), *tool_refs[-1:]], events=len(trace.get("events", [])), moves=len(trace.get("stock_moves", []))))
            next_actions.append(message("ai.trace.open"))
    elif intent == "STOCK_QUERY":
        stock = call_tool("get_stock", {"project_id": pid}, lambda: svc.get_stock(pid, actor=actor))
        if stock:
            facts.append(claim("FACT", "ai.stock.summary", [*stock.get("source_refs", []), *tool_refs[-1:]], count=len(stock.get("items", []))))
            next_actions.append(message("ai.stock.open"))
    else:
        warnings.append(message("ai.unknown"))
        next_actions.append(message("ai.unknown.next"))

    source_refs = sorted({str(ref) for group in facts + calculations + hypotheses for ref in group.get("source_refs", []) if ref})
    result = {
        "run_id": run_id,
        "thread_id": thread_id,
        "status": status,
        "intent": intent,
        "facts": facts,
        "calculations": calculations,
        "hypotheses": hypotheses,
        "warnings": warnings,
        "next_actions": next_actions,
        "source_refs": source_refs,
        "draft_id": draft_id,
        "approval_required": status == "WAITING_CONFIRMATION",
        **message("ai.complete"),
    }
    svc.finish_ai_run(
        pid,
        run_id,
        status,
        result,
        actor,
        idempotency_key=idempotency_key,
        idempotency_command=command,
    )
    return result


def create_app(db: Database | None = None, service: BusinessService | None = None):
    if FastAPI is None:
        raise RuntimeError("FastAPI is not installed; install backend/requirements.txt")
    database = db or (service.db if service is not None else Database(os.getenv("DATABASE_PATH", str(Path(__file__).resolve().parent / "data" / "demo.sqlite3"))))
    # The fixture is additive and idempotent.  Running it at startup also
    # upgrades an existing local P001 database when a later demo revision adds
    # a role, catalog row, or other stable reference record.
    database.seed_demo()
    # Seed before constructing the service so the MockEvidenceAdapter can
    # hydrate persisted successful submissions even on a cold start.
    svc = service or BusinessService(database)
    app = FastAPI(title="筑福链 API", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def normalize_demo_identity(request: Request, call_next):
        """Normalize demo identity and establish one request-scoped locale."""

        if not request.headers.get("X-User-Id") and request.headers.get("X-Demo-User"):
            headers = list(request.scope.get("headers", []))
            headers.append((b"x-user-id", request.headers["X-Demo-User"].encode("latin-1")))
            request.scope["headers"] = headers
        locale = locale_from_headers(request.headers.get("X-Locale"), request.headers.get("Accept-Language"))
        request.state.locale = locale
        token = set_current_locale(locale)
        try:
            response = await call_next(request)
        finally:
            reset_current_locale(token)
        response.headers["Content-Language"] = locale
        return response

    def respond(fn, *, request_id: str | None = None):
        try:
            data = render_for_locale(fn(), current_locale())
            return {"ok": True, "data": data, "request_id": request_id or str(uuid.uuid4()), "trace_id": str(uuid.uuid4()), "locale": current_locale(), "warnings": data.get("warnings", []) if isinstance(data, dict) else [], "source_refs": data.get("source_refs", []) if isinstance(data, dict) else []}
        except DomainError as exc:
            return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": exc.as_dict(current_locale()), "request_id": request_id or str(uuid.uuid4()), "trace_id": str(uuid.uuid4()), "locale": current_locale()})
        except (ValueError, TypeError):
            error = DomainError("VALIDATION_ERROR", "invalid input")
            return JSONResponse(status_code=error.status_code, content={"ok": False, "error": error.as_dict(current_locale()), "request_id": request_id or str(uuid.uuid4()), "trace_id": str(uuid.uuid4()), "locale": current_locale()})

    def actor(project_id: str, x_user_id: str | None, x_roles: str | None, x_organization_id: str | None, x_demo_user: str | None = None):
        """Build context from a server-side demo identity, never request roles.

        ``X-Roles`` remains accepted for backwards-compatible local scripts,
        but is intentionally ignored.  A production OIDC middleware replaces
        this lookup while keeping the business-service contract unchanged.
        """

        identity = x_demo_user or x_user_id
        if not identity:
            raise DomainError("UNAUTHORIZED", "未认证用户")
        user = database.query_one("SELECT * FROM users WHERE is_active=1 AND (username=? OR id=?)", (identity, identity))
        if not user:
            raise DomainError("UNAUTHORIZED", "未认证用户")
        return {
            "request_id": str(uuid.uuid4()),
            "user_id": user["id"],
            "roles": [user["role"]],
            "organization_id": user["organization_id"],
            "project_id": project_id,
            "locale": current_locale(),
        }

    def require_entity_project(project_id: str, table: str, entity_id: str, *, not_found: str = "资源不存在") -> str:
        """Bind an entity URL to the project segment before invoking a command.

        Entity services historically derive the project from the entity ID.
        That is useful for internal calls but unsafe at the HTTP boundary:
        ``/projects/A/.../entity-from-B`` must never be accepted.  Table names
        are selected only by this module (never user input), so the small
        helper keeps every route check consistent while retaining the service
        layer's own authorization checks.
        """

        path_project = svc._project(project_id)["id"]
        row = database.query_one(f"SELECT project_id FROM {table} WHERE id=?", (entity_id,))
        if not row:
            raise DomainError("NOT_FOUND", not_found)
        if str(row["project_id"]) != str(path_project):
            raise DomainError("FORBIDDEN", "资源不属于当前项目")
        return str(path_project)

    def require_batch_project(project_id: str, batch_id: str) -> str:
        path_project = svc._project(project_id)["id"]
        row = database.query_one(
            "SELECT m.project_id FROM batches b JOIN materials m ON m.id=b.material_id WHERE b.id=?",
            (batch_id,),
        )
        if not row:
            raise DomainError("NOT_FOUND", "批次不存在")
        if str(row["project_id"]) != str(path_project):
            raise DomainError("FORBIDDEN", "资源不属于当前项目")
        return str(path_project)

    def require_idempotency(key: str | None) -> str:
        if not key or not str(key).strip():
            raise DomainError("VALIDATION_ERROR", "缺少 Idempotency-Key")
        return str(key)

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "ok": False,
                "error": exc.as_dict(current_locale()),
                "request_id": request.headers.get("X-Request-Id") or str(uuid.uuid4()),
                "trace_id": str(uuid.uuid4()),
                "locale": current_locale(),
            },
        )

    @app.get("/health")
    def health():
        return {"ok": True, "service": "haizhizi", "chain": "mock", "locale": current_locale()}

    @app.get("/api/v1/projects/{project_id}/stock")
    def get_stock(project_id: str, material_id: list[str] | None = Query(default=None), warehouse_id: list[str] | None = Query(default=None), batch_id: list[str] | None = Query(default=None), include_quarantined: bool = False, x_demo_user: str | None = Header(default=None), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id, x_demo_user); return respond(lambda: svc.get_stock(project_id, material_id, warehouse_id, batch_id, include_quarantined, ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/stock-moves")
    def get_stock_moves(project_id: str, material_id: str | None = None, move_type: str | None = None, page: int = 1, page_size: int = 50, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.list_stock_moves(project_id, ctx, material_id, move_type, page, page_size), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/consumption")
    def get_consumption(project_id: str, date_from: str | None = None, date_to: str | None = None, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.list_consumption_snapshots(project_id, ctx, date_from, date_to), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}")
    def project_context(project_id: str, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.get_project_context(ctx, project_id), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/plans")
    def get_plans(project_id: str, from_: str = Query(alias="from"), to: str = Query(alias="to"), material_id: list[str] | None = Query(default=None), area_id: list[str] | None = Query(default=None), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.get_material_plan(project_id, material_id or [], from_, to, area_id, ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/purchase-orders")
    def get_purchase_orders(project_id: str, status: list[str] | None = Query(default=None), material_id: list[str] | None = Query(default=None), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.get_purchase_orders(project_id, status, material_id, actor=ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/purchase-requests")
    def get_purchase_requests(project_id: str, status: str | None = None, page: int = 1, page_size: int = 50, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.list_purchase_requests(project_id, status, ctx, page, page_size), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/drafts")
    def get_drafts(project_id: str, draft_type: str | None = None, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.list_drafts(project_id, ctx, draft_type), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/batches")
    def batches(project_id: str, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.list_batches(project_id, ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/batches/{batch_id}/trace")
    def trace(project_id: str, batch_id: str, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_batch_project(project_id, batch_id); return respond(lambda: svc.get_batch_trace(project_id, batch_id=batch_id, actor=ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/evidence/{event_id}/business-record")
    def evidence_business_record(project_id: str, event_id: str, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "evidence_events", event_id, not_found="证据事件不存在"); return respond(lambda: svc.get_evidence_business_record(project_id, event_id, ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/purchase-orders/{order_id}")
    def purchase_order_detail(project_id: str, order_id: str, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "purchase_orders", order_id, not_found="采购订单不存在"); return respond(lambda: svc.get_purchase_order_detail(project_id, order_id, ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/shipments")
    def list_shipments(project_id: str, status: str | None = None, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.list_shipments(project_id, status, ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/deliveries")
    def list_deliveries(project_id: str, status: str | None = None, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.list_deliveries(project_id, status, ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/inspections")
    def list_inspections(project_id: str, status: str | None = None, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.list_inspections(project_id, status, ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/handovers")
    def list_handovers(project_id: str, status: str | None = None, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.list_handovers(project_id, status, ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/crews")
    def list_crews(project_id: str, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.list_crews(project_id, ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/ppe/issues")
    def list_ppe_issues(project_id: str, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.list_ppe_issues(project_id, ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/ppe/coverage")
    def ppe_coverage(project_id: str, crew_id: list[str] | None = Query(default=None), as_of: str | None = None, warning_days: int | None = None, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.get_ppe_coverage(project_id, crew_id, as_of, warning_days, ctx), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/shortage:calculate")
    def shortage(project_id: str, payload: dict[str, Any] = Body(...), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); payload = {**payload, "project_id": project_id}; return respond(lambda: svc.calculate_shortage(payload, ctx), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/forecast:calculate")
    def forecast(project_id: str, payload: dict[str, Any] = Body(...), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); payload = {**payload, "project_id": project_id}; return respond(lambda: svc.forecast_inventory(payload, ctx), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/anomalies:detect")
    def anomalies(project_id: str, payload: dict[str, Any] = Body(...), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); payload = {**payload, "project_id": project_id}; return respond(lambda: svc.detect_consumption_anomaly(payload, ctx), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/stock-moves")
    def stock_move(project_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); payload = {**payload, "project_id": project_id, "idempotency_key": key}; return respond(lambda: svc.create_stock_move(payload, ctx), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/stock-moves/{move_id}:reverse")
    def reverse_move(project_id: str, move_id: str, payload: dict[str, Any] = Body(default={}), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "stock_moves", move_id, not_found="库存流水不存在"); return respond(lambda: svc.reverse_stock_move(move_id, payload.get("reason", "更正"), ctx, key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/stocktakes")
    def stocktake(project_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        if not idempotency_key:
            raise DomainError("VALIDATION_ERROR", "缺少 Idempotency-Key")
        required = {"warehouse_id", "material_id", "counted_qty", "reason"}
        unknown = set(payload) - (required | {"batch_id", "confirmation_token"})
        if unknown:
            raise DomainError("VALIDATION_ERROR", "请求包含未知字段", {"fields": sorted(unknown)})
        if not required.issubset(payload):
            raise DomainError("VALIDATION_ERROR", "盘点请求缺少必填字段", {"fields": sorted(required - set(payload))})
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id)
        return respond(lambda: svc.reconcile_stock(project_id, payload["warehouse_id"], payload["material_id"], payload["counted_qty"], payload["reason"], ctx, payload.get("confirmation_token"), payload.get("batch_id"), idempotency_key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/stocktakes:confirmation-token")
    def stocktake_confirmation_token(project_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key)
        required = {"warehouse_id", "material_id", "counted_qty", "reason"}
        unknown = set(payload) - (required | {"batch_id"})
        if unknown:
            raise DomainError("VALIDATION_ERROR", "请求包含未知字段", {"fields": sorted(unknown)})
        if not required.issubset(payload):
            raise DomainError("VALIDATION_ERROR", "盘点确认请求缺少必填字段", {"fields": sorted(required - set(payload))})
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id)
        return respond(lambda: svc.issue_stocktake_confirmation_token(project_id, payload["warehouse_id"], payload["material_id"], payload["counted_qty"], payload["reason"], ctx, payload.get("batch_id"), key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/purchase-requests:draft")
    def draft_request(project_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        if not idempotency_key:
            raise DomainError("VALIDATION_ERROR", "缺少 Idempotency-Key")
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); shortage_result = {**payload.get("shortage_result", {}), "project_id": project_id}
        return respond(lambda: svc.draft_material_request(shortage_result, payload.get("selected_items", []), payload.get("needed_by"), payload.get("supplier_id"), {**ctx, "idempotency_key": idempotency_key}), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/purchase-requests/{draft_id}:confirm")
    def confirm_draft_request(project_id: str, draft_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "drafts", draft_id, not_found="采购草稿不存在"); return respond(lambda: svc.submit_material_request(draft_id, payload.get("confirmation_token"), ctx, key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/drafts/{draft_id}:confirmation-token")
    def draft_confirmation_token(project_id: str, draft_id: str, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "drafts", draft_id, not_found="采购草稿不存在"); return respond(lambda: svc.issue_draft_confirmation_token(draft_id, ctx, key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/drafts/{draft_id}:reject")
    def reject_draft(project_id: str, draft_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key)
        reason = str(payload.get("reason", "")).strip()
        if not reason:
            raise DomainError("VALIDATION_ERROR", "放弃草稿必须填写原因")
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id)
        require_entity_project(project_id, "drafts", draft_id, not_found="草稿不存在")
        return respond(lambda: svc.reject_draft(draft_id, reason, ctx, key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/purchase-requests/{request_id}:approval-token")
    def purchase_approval_token(project_id: str, request_id: str, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "purchase_requests", request_id, not_found="采购申请不存在"); return respond(lambda: svc.issue_purchase_approval_token(request_id, ctx, key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/purchase-requests/{request_id}:approve")
    def approve_request(project_id: str, request_id: str, payload: dict[str, Any] = Body(default={}), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "purchase_requests", request_id, not_found="采购申请不存在"); return respond(lambda: svc.approve_purchase_request(request_id, ctx, payload.get("confirmation_token"), key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/purchase-requests/{request_id}:reject")
    def reject_request(project_id: str, request_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "purchase_requests", request_id, not_found="采购申请不存在"); return respond(lambda: svc.reject_purchase_request(request_id, str(payload.get("reason", "")), ctx, key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/purchase-orders")
    def purchase_order(project_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.create_purchase_order({**payload, "project_id": project_id, "idempotency_key": key}, ctx), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/shipments")
    def shipment(project_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.submit_shipment({**payload, "project_id": project_id, "idempotency_key": key}, ctx), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/deliveries")
    def delivery(project_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.record_delivery({**payload, "project_id": project_id, "idempotency_key": key}, ctx), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/deliveries/{delivery_id}:confirm")
    def delivery_confirm(project_id: str, delivery_id: str, payload: dict[str, Any] = Body(default={}), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "deliveries", delivery_id, not_found="到货记录不存在"); return respond(lambda: svc.confirm_delivery(delivery_id, payload.get("confirmation_token"), ctx, key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/inspections")
    def inspection(project_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.submit_inspection({**payload, "idempotency_key": key}, ctx), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/inspections/{inspection_id}:confirmation-token")
    def inspection_confirmation_token(project_id: str, inspection_id: str, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "inspections", inspection_id, not_found="验收记录不存在"); return respond(lambda: svc.issue_inspection_confirmation_token(inspection_id, ctx, key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/inspections/{inspection_id}:confirm")
    def inspection_confirm(project_id: str, inspection_id: str, payload: dict[str, Any] = Body(default={}), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "inspections", inspection_id, not_found="验收记录不存在"); return respond(lambda: svc.confirm_inspection(inspection_id, payload.get("confirmation_token"), ctx, key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/ppe/issues")
    def ppe_issue(project_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.issue_ppe({**payload, "project_id": project_id, "idempotency_key": key}, ctx), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/handovers")
    def create_handover(project_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.create_handover({**payload, "project_id": project_id, "idempotency_key": key}, ctx), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/handovers/{handover_id}:confirmation-token")
    def handover_token(project_id: str, handover_id: str, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "handovers", handover_id, not_found="交接记录不存在"); return respond(lambda: svc.issue_handover_confirmation_token(handover_id, ctx, key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/handovers/{handover_id}:confirm")
    def confirm_handover(project_id: str, handover_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "handovers", handover_id, not_found="交接记录不存在"); return respond(lambda: svc.confirm_handover(handover_id, payload.get("confirmation_token"), ctx, key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/documents")
    async def upload_document(project_id: str, document_type: str = "OTHER", file: UploadFile = File(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key)
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id)
        return respond(lambda: svc.put_business_document(project_id, document_type, file.file, ctx, file.content_type or "application/octet-stream", file.filename, key), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/documents")
    def get_documents(project_id: str, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.list_documents(project_id, ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/documents/{document_id}")
    def document_metadata(project_id: str, document_id: str, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "documents", document_id, not_found="文件不存在"); return respond(lambda: svc.get_document_metadata(document_id, ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/documents/{document_id}/download")
    def download_document(project_id: str, document_id: str, expires: int | None = None, signature: str | None = None, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "documents", document_id, not_found="文件不存在")
        svc.validate_document_download(document_id, expires, signature)
        metadata, data = svc.get_document_bytes(document_id, ctx)
        return Response(content=data, media_type=metadata.get("mime", "application/octet-stream"), headers={"Content-Disposition": f"attachment; filename=zhufulian-{document_id}"})

    @app.get("/api/v1/projects/{project_id}/documents/{document_id}/download-url")
    def document_download_url(project_id: str, document_id: str, ttl_seconds: int = 300, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "documents", document_id, not_found="文件不存在")
        return respond(lambda: svc.get_document_download_url(document_id, ctx, ttl_seconds), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/daily-reports:draft")
    def draft_daily_report(project_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        if not idempotency_key:
            raise DomainError("VALIDATION_ERROR", "缺少 Idempotency-Key")
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id)
        command = {"project_id": project_id, "report_date": payload.get("report_date", date.today().isoformat()), "source_record_ids": payload.get("source_record_ids"), "include_calculations": payload.get("include_calculations", True), "idempotency_key": idempotency_key}
        return respond(lambda: svc.create_daily_report_draft(command, ctx), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/daily-reports/{draft_id}:publish")
    def publish_daily_report(project_id: str, draft_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "drafts", draft_id, not_found="日报草稿不存在"); return respond(lambda: svc.publish_daily_report(draft_id, payload.get("confirmation_token"), ctx, key), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/daily-reports")
    def list_daily_reports(project_id: str, from_: str = Query(alias="from"), to: str = Query(alias="to"), page: int = 1, page_size: int = 50, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.list_daily_reports(project_id, from_, to, page, page_size, ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/todos")
    def list_todos(project_id: str, status: str | None = None, owner_role: str | None = None, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id)
        def read_todos():
            items = svc.list_todos(project_id, status, owner_role, ctx)
            return {"items": items, "total": len(items), "page": 1, "page_size": 50, "next_cursor": None}
        return respond(read_todos, request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/todos")
    def create_todo(project_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.create_todo({**payload, "project_id": project_id, "idempotency_key": key}, ctx), request_id=ctx["request_id"])

    @app.patch("/api/v1/projects/{project_id}/todos/{todo_id}")
    def update_todo(project_id: str, todo_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        if not idempotency_key:
            raise DomainError("VALIDATION_ERROR", "缺少 Idempotency-Key")
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "todos", todo_id, not_found="待办不存在"); return respond(lambda: svc.update_todo(todo_id, payload, ctx, idempotency_key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/evidence/{event_id}:anchor")
    def evidence_anchor(project_id: str, event_id: str, payload: dict[str, Any] = Body(default={}), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "evidence_events", event_id, not_found="证据事件不存在"); return respond(lambda: svc.anchor_evidence(event_id, payload.get("confirmation_token"), ctx, key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/evidence/{event_id}:anchor-token")
    def evidence_anchor_token(project_id: str, event_id: str, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key); ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "evidence_events", event_id, not_found="证据事件不存在"); return respond(lambda: svc.issue_evidence_anchor_token(event_id, ctx, key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/evidence/{event_id}:retry")
    def evidence_retry(project_id: str, event_id: str, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        if not idempotency_key:
            raise DomainError("VALIDATION_ERROR", "缺少 Idempotency-Key")
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "evidence_events", event_id, not_found="证据事件不存在"); return respond(lambda: svc.retry_failed_evidence(event_id, ctx, idempotency_key), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/evidence/{event_id}/verify")
    def evidence_verify(project_id: str, event_id: str, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "evidence_events", event_id, not_found="证据事件不存在"); return respond(lambda: svc.verify_evidence(event_id, ctx), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/anomalies")
    def anomaly_list(project_id: str, status: str | None = None, severity: str | None = None, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); return respond(lambda: svc.list_anomalies(project_id, status, severity, ctx), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/anomalies/{anomaly_id}:ack")
    def acknowledge_anomaly(project_id: str, anomaly_id: str, payload: dict[str, Any] = Body(default={}), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        if not idempotency_key:
            raise DomainError("VALIDATION_ERROR", "缺少 Idempotency-Key")
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "anomalies", anomaly_id, not_found="异常不存在"); return respond(lambda: svc.acknowledge_anomaly(anomaly_id, ctx, str(payload.get("note", "")), idempotency_key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/anomalies/{anomaly_id}:resolve")
    def resolve_anomaly(project_id: str, anomaly_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        if not idempotency_key:
            raise DomainError("VALIDATION_ERROR", "缺少 Idempotency-Key")
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "anomalies", anomaly_id, not_found="异常不存在"); return respond(lambda: svc.resolve_anomaly(anomaly_id, ctx, str(payload.get("resolution_code", "")), str(payload.get("note", "")), idempotency_key), request_id=ctx["request_id"])

    @app.post("/api/v1/projects/{project_id}/anomalies:scan")
    def anomaly_scan(project_id: str, payload: dict[str, Any] = Body(default={}), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key)
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id)
        command = {**payload, "project_id": project_id, "date_from": payload.get("date_from", (date.today() - timedelta(days=14)).isoformat()), "date_to": payload.get("date_to", date.today().isoformat()), "idempotency_key": key}
        return respond(lambda: svc.run_anomaly_scan(command, ctx), request_id=ctx["request_id"])

    @app.post("/api/v1/ai/threads/{thread_id}/messages")
    def ai_message(thread_id: str, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key)
        project_id = str(payload.get("project_id") or "P001")
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id)
        text = str(payload.get("text") or payload.get("request_text") or "").strip()
        if not text:
            raise DomainError("VALIDATION_ERROR", "消息不能为空")
        envelope = respond(lambda: _run_ai_message(svc, project_id, thread_id, text, ctx, key), request_id=ctx["request_id"])
        if isinstance(envelope, dict) and envelope.get("ok") and isinstance(envelope.get("data"), dict) and envelope["data"].get("approval_required"):
            return JSONResponse(status_code=202, content=envelope)
        return envelope

    @app.get("/api/v1/projects/{project_id}/ai/threads/{thread_id}/messages")
    def ai_thread_messages(project_id: str, thread_id: str, limit: int = 20, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id)
        return respond(lambda: svc.list_ai_thread_messages(project_id, thread_id, ctx, limit), request_id=ctx["request_id"])

    @app.delete("/api/v1/projects/{project_id}/ai/threads/{thread_id}/messages")
    def clear_ai_thread_messages(project_id: str, thread_id: str, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        key = require_idempotency(idempotency_key)
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id)
        return respond(lambda: svc.clear_ai_thread(project_id, thread_id, ctx, key), request_id=ctx["request_id"])

    @app.get("/api/v1/projects/{project_id}/ai/runs/{run_id}")
    def ai_run(project_id: str, run_id: str, x_user_id: str | None = Header(default=None), x_roles: str | None = Header(default=None), x_organization_id: str | None = Header(default=None)):
        ctx = actor(project_id, x_user_id, x_roles, x_organization_id); require_entity_project(project_id, "ai_runs", run_id, not_found="AI 运行不存在"); return respond(lambda: svc.get_ai_run(project_id, run_id, ctx), request_id=ctx["request_id"])

    return app


app = create_app() if FastAPI is not None else None

__all__ = ["app", "create_app"]
