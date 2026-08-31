"""API smoke coverage for the envelope, auth context and AI tool boundary."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from backend.api import create_app
from backend.db import Database


def test_api_seeds_p001_and_returns_uniform_envelope() -> None:
    db = Database()
    client = TestClient(create_app(db))
    assert client.get("/api/v1/projects/P001/stock").status_code == 401
    response = client.get("/api/v1/projects/P001/stock", headers={"X-Demo-User": "owner@example.test"})
    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["request_id"] and body["trace_id"]
    assert body["data"]["items"]


def test_demo_role_switches_resolve_to_distinct_server_identities() -> None:
    db = Database()
    client = TestClient(create_app(db))

    for username, role in (
        ("owner@example.test", "PROJECT_OWNER"),
        ("procurement@example.test", "PROCUREMENT"),
        ("site@example.test", "SITE_ADMIN"),
        ("supplier@example.test", "SUPPLIER"),
        ("inspector@example.test", "INSPECTOR"),
        ("auditor@example.test", "AUDITOR"),
    ):
        user = db.query_one("SELECT role FROM users WHERE username=?", (username,))
        assert user and user["role"] == role
        response = client.get("/api/v1/projects/P001", headers={"X-Demo-User": username})
        assert response.status_code == 200, (username, response.text)


def test_api_ignores_client_role_header_and_persists_controlled_ai_run() -> None:
    db = Database()
    client = TestClient(create_app(db))
    forbidden = client.post(
        "/api/v1/projects/P001/stock-moves",
        json={"material_id": "x", "move_type": "ISSUE", "qty": "1", "unit": "piece", "from_warehouse_id": "x", "reason": "bad"},
        headers={"X-User-Id": "supplier@example.test", "X-Roles": "PROJECT_OWNER", "Idempotency-Key": "api-role-test"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"

    ai = client.post(
        "/api/v1/ai/threads/demo/messages",
        json={"project_id": "P001", "text": "下周三号楼砌体可能短缺什么？"},
        headers={"X-Demo-User": "owner@example.test", "Idempotency-Key": "api-ai-shortage"},
    )
    assert ai.status_code == 200
    data = ai.json()["data"]
    assert data["intent"] == "SHORTAGE_PREDICTION"
    assert data["calculations"]
    assert db.query_one("SELECT COUNT(*) AS n FROM ai_runs")["n"] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM ai_tool_calls")["n"] >= 1


def test_ai_message_idempotency_prevents_duplicate_runs() -> None:
    db = Database()
    client = TestClient(create_app(db))
    path = "/api/v1/ai/threads/idempotent/messages"
    headers = {"X-Demo-User": "owner@example.test", "Idempotency-Key": "api-ai-replay"}
    first = client.post(path, json={"project_id": "P001", "text": "库存有多少？"}, headers=headers)
    second = client.post(path, json={"project_id": "P001", "text": "库存有多少？"}, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["data"] == second.json()["data"]
    assert db.query_one("SELECT COUNT(*) AS n FROM ai_runs")["n"] == 1
    conflict = client.post(path, json={"project_id": "P001", "text": "查看缺料"}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "DUPLICATE_IDEMPOTENCY"


def test_ai_thread_history_persists_across_app_restarts_and_is_user_scoped() -> None:
    db = Database()
    client = TestClient(create_app(db))
    owner = {"X-Demo-User": "owner@example.test"}
    thread_id = "persistent-history"
    message_path = f"/api/v1/ai/threads/{thread_id}/messages"
    history_path = f"/api/v1/projects/P001/ai/threads/{thread_id}/messages"

    first = client.post(
        message_path,
        json={"project_id": "P001", "text": "库存有多少？"},
        headers={**owner, "Idempotency-Key": "thread-history-first"},
    )
    second = client.post(
        message_path,
        json={"project_id": "P001", "text": "追溯砌块批次"},
        headers={**owner, "Idempotency-Key": "thread-history-second"},
    )
    assert first.status_code == second.status_code == 200

    history = client.get(history_path, headers=owner)
    assert history.status_code == 200
    payload = history.json()["data"]
    assert payload["thread_id"] == thread_id
    assert payload["total_runs"] == 2
    assert payload["total"] == 4
    assert [item["role"] for item in payload["items"]] == ["user", "assistant", "user", "assistant"]
    assert [item["text"] for item in payload["items"] if item["role"] == "user"] == ["库存有多少？", "追溯砌块批次"]
    assert all(item["run_id"] for item in payload["items"])
    assert all(item["created_at"] for item in payload["items"])

    # The same SQLite repository can be mounted by a new application process;
    # history is derived from persistent ai_runs rather than browser state.
    restarted = TestClient(create_app(db))
    reloaded = restarted.get(history_path, headers=owner)
    assert reloaded.status_code == 200
    assert reloaded.json()["data"]["items"] == payload["items"]

    # A familiar thread ID is not a cross-user conversation capability.
    other_user = client.get(history_path, headers={"X-Demo-User": "supplier@example.test"})
    assert other_user.status_code == 200
    assert other_user.json()["data"]["items"] == []


def test_ai_thread_clear_hides_history_but_preserves_runs_and_audit() -> None:
    db = Database()
    client = TestClient(create_app(db))
    owner = {"X-Demo-User": "owner@example.test"}
    thread_id = "clear-history"
    message_path = f"/api/v1/ai/threads/{thread_id}/messages"
    history_path = f"/api/v1/projects/P001/ai/threads/{thread_id}/messages"

    created = client.post(
        message_path,
        json={"project_id": "P001", "text": "库存有多少？"},
        headers={**owner, "Idempotency-Key": "thread-clear-message"},
    )
    assert created.status_code == 200
    run_id = created.json()["data"]["run_id"]
    assert client.get(history_path, headers=owner).json()["data"]["total"] == 2

    missing_key = client.delete(history_path, headers=owner)
    assert missing_key.status_code == 422
    assert missing_key.json()["error"]["code"] == "VALIDATION_ERROR"

    clear_headers = {**owner, "Idempotency-Key": "thread-clear-command"}
    cleared = client.delete(history_path, headers=clear_headers)
    assert cleared.status_code == 200
    clear_data = cleared.json()["data"]
    assert clear_data["status"] == "CLEARED"
    assert clear_data["cleared_runs"] == 1
    assert clear_data["warnings"]
    assert client.get(history_path, headers=owner).json()["data"]["items"] == []

    # Clearing changes only the thread projection: the completed run, tool
    # records and immutable audit event stay available to the backend.
    assert db.query_one("SELECT COUNT(*) AS n FROM ai_runs WHERE id=?", (run_id,))["n"] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM ai_tool_calls WHERE ai_run_id=?", (run_id,))["n"] >= 1
    assert db.query_one("SELECT COUNT(*) AS n FROM audit_logs WHERE action='CLEAR_AI_THREAD'")["n"] == 1
    replay = client.delete(history_path, headers=clear_headers)
    assert replay.status_code == 200
    assert replay.json()["data"] == clear_data
    assert db.query_one("SELECT COUNT(*) AS n FROM audit_logs WHERE action='CLEAR_AI_THREAD'")["n"] == 1

    new_turn = client.post(
        message_path,
        json={"project_id": "P001", "text": "追溯砌块批次"},
        headers={**owner, "Idempotency-Key": "thread-clear-new-message"},
    )
    assert new_turn.status_code == 200
    visible = client.get(history_path, headers=owner).json()["data"]
    assert visible["total_runs"] == 1
    assert visible["total"] == 2
    assert [item["text"] for item in visible["items"] if item["role"] == "user"] == ["追溯砌块批次"]
    assert visible["items"][0]["thread_sequence"] == 2


def test_pending_inspection_can_recover_a_confirmation_token_and_handover_party_can_read_record() -> None:
    """Persisted demo records remain actionable after the original token is gone."""

    db = Database()
    client = TestClient(create_app(db))
    inspection = db.query_one("SELECT id FROM inspections WHERE inspection_no='IN-20260827-0001'")
    handover_event = db.query_one("SELECT id,batch_id FROM evidence_events WHERE event_type='HANDOVER' LIMIT 1")
    assert inspection and handover_event
    inspector_headers = {"X-Demo-User": "inspector@example.test", "Idempotency-Key": "recover-seed-inspection"}
    token_path = f"/api/v1/projects/P001/inspections/{inspection['id']}:confirmation-token"
    token_response = client.post(token_path, headers=inspector_headers)
    assert token_response.status_code == 200, token_response.text
    token = token_response.json()["data"]["confirmation_token"]
    replay = client.post(token_path, headers=inspector_headers)
    assert replay.status_code == 200
    assert replay.json()["data"] == token_response.json()["data"]

    confirmed = client.post(
        f"/api/v1/projects/P001/inspections/{inspection['id']}:confirm",
        json={"confirmation_token": token},
        headers={"X-Demo-User": "inspector@example.test", "Idempotency-Key": "confirm-seed-inspection"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["data"]["status"] == "PARTIAL"

    handover = client.get(
        f"/api/v1/projects/P001/evidence/{handover_event['id']}/business-record",
        headers={"X-Demo-User": "supplier@example.test"},
    )
    assert handover.status_code == 200, handover.text
    assert handover.json()["data"]["record_type"] == "HANDOVER"
    trace = client.get(
        f"/api/v1/projects/P001/batches/{handover_event['batch_id']}/trace",
        headers={"X-Demo-User": "supplier@example.test"},
    )
    assert trace.status_code == 200, trace.text
    assert "HANDOVER" in [item["event_type"] for item in trace.json()["data"]["events"]]


def test_http_supplier_to_site_to_inspector_flow_creates_real_trace_records() -> None:
    """The role-specific commands used by the workbench form a real MVP chain."""

    db = Database()
    client = TestClient(create_app(db))
    order = db.query_one("SELECT id FROM purchase_orders WHERE po_no='PO-20260820-0001'")
    assert order
    item = db.query_one("SELECT material_id,unit FROM purchase_order_items WHERE order_id=?", (order["id"],))
    warehouse = db.query_one("SELECT id FROM warehouses WHERE code='WH-MAIN'")
    assert item and warehouse

    shipment = client.post(
        "/api/v1/projects/P001/shipments",
        json={"purchase_order_id": order["id"], "items": [{"material_id": item["material_id"], "qty": "40", "unit": item["unit"], "batch_no": "UI-FLOW-001"}]},
        headers={"X-Demo-User": "supplier@example.test", "Idempotency-Key": "ui-flow-shipment"},
    )
    assert shipment.status_code == 200, shipment.text
    shipment_data = shipment.json()["data"]

    delivery = client.post(
        "/api/v1/projects/P001/deliveries",
        json={"shipment_id": shipment_data["id"], "warehouse_id": warehouse["id"], "items": [{"shipment_item_id": shipment_data["items"][0]["id"], "qty_arrived": "40"}]},
        headers={"X-Demo-User": "site@example.test", "Idempotency-Key": "ui-flow-delivery"},
    )
    assert delivery.status_code == 200, delivery.text
    delivery_data = delivery.json()["data"]
    delivery_confirm = client.post(
        f"/api/v1/projects/P001/deliveries/{delivery_data['id']}:confirm",
        json={"confirmation_token": delivery_data["confirmation_token"]},
        headers={"X-Demo-User": "site@example.test", "Idempotency-Key": "ui-flow-delivery-confirm"},
    )
    assert delivery_confirm.status_code == 200, delivery_confirm.text

    inspection = client.post(
        "/api/v1/projects/P001/inspections",
        json={"delivery_id": delivery_data["id"], "result": "PASSED", "items": [{"delivery_item_id": delivery_data["items"][0]["id"], "qty_passed": "40", "qty_failed": "0"}], "notes": "工作台验收"},
        headers={"X-Demo-User": "inspector@example.test", "Idempotency-Key": "ui-flow-inspection"},
    )
    assert inspection.status_code == 200, inspection.text
    inspection_data = inspection.json()["data"]
    inspection_confirm = client.post(
        f"/api/v1/projects/P001/inspections/{inspection_data['id']}:confirm",
        json={"confirmation_token": inspection_data["confirmation_token"]},
        headers={"X-Demo-User": "inspector@example.test", "Idempotency-Key": "ui-flow-inspection-confirm"},
    )
    assert inspection_confirm.status_code == 200, inspection_confirm.text
    assert inspection_confirm.json()["data"]["status"] == "PASSED"

    trace = client.get(
        f"/api/v1/projects/P001/batches/{shipment_data['batch_id']}/trace",
        headers={"X-Demo-User": "owner@example.test"},
    )
    assert trace.status_code == 200, trace.text
    assert {"SHIPMENT", "DELIVERY", "INSPECTION"}.issubset({event["event_type"] for event in trace.json()["data"]["events"]})


def test_entity_routes_bind_ids_to_project_path() -> None:
    """A valid P001 entity must not be reachable through another project URL."""

    db = Database()
    client = TestClient(create_app(db))
    other_project_id = "00000000-0000-4000-8000-000000009999"
    owner_id = str(db.query_one("SELECT id FROM users WHERE username='owner@example.test'")["id"])
    owner_org = str(db.query_one("SELECT organization_id FROM users WHERE id=?", (owner_id,))["organization_id"])
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    db.execute(
        "INSERT INTO projects(id,code,name,timezone,status,owner_org_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (other_project_id, "OTHER", "另一个项目", "Asia/Shanghai", "ACTIVE", owner_org, now, now),
    )
    db.execute("INSERT INTO project_members(project_id,user_id,role,scopes_json) VALUES(?,?,?,?)", (other_project_id, owner_id, "PROJECT_OWNER", "{}"))
    document_id = str(db.query_one("SELECT id FROM documents LIMIT 1")["id"])
    event_id = str(db.query_one("SELECT id FROM evidence_events LIMIT 1")["id"])
    order_id = str(db.query_one("SELECT id FROM purchase_orders LIMIT 1")["id"])
    headers = {"X-User-Id": "owner@example.test"}

    for path in (
        f"/api/v1/projects/OTHER/documents/{document_id}",
        f"/api/v1/projects/OTHER/documents/{document_id}/download-url",
        f"/api/v1/projects/OTHER/evidence/{event_id}/verify",
        f"/api/v1/projects/OTHER/purchase-orders/{order_id}",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 403, (path, response.text)
        assert response.json()["error"]["code"] == "FORBIDDEN"

    descriptor = client.get(f"/api/v1/projects/P001/documents/{document_id}/download-url", headers=headers)
    assert descriptor.status_code == 200
    signed_url = descriptor.json()["data"]["url"]
    download = client.get(signed_url, headers=headers)
    assert download.status_code == 200
    assert download.content
    assert client.get(signed_url.replace("signature=", "signature=bad"), headers=headers).status_code == 403


def test_concurrent_read_requests_are_serialized() -> None:
    """The single-connection demo repository remains deterministic under ASGI concurrency."""

    from concurrent.futures import ThreadPoolExecutor

    db = Database()
    client = TestClient(create_app(db))
    headers = {"X-Demo-User": "owner@example.test"}

    def read_stock(_: int) -> int:
        return client.get("/api/v1/projects/P001/stock", headers=headers).status_code

    with ThreadPoolExecutor(max_workers=12) as pool:
        statuses = list(pool.map(read_stock, range(120)))
    assert statuses == [200] * 120


def test_workflow_read_projections_and_role_scopes() -> None:
    db = Database()
    client = TestClient(create_app(db))
    owner = {"X-Demo-User": "owner@example.test"}
    supplier = {"X-Demo-User": "supplier@example.test"}
    for path, key in (
        ("/api/v1/projects/P001/shipments", "shipments"),
        ("/api/v1/projects/P001/deliveries", "deliveries"),
        ("/api/v1/projects/P001/inspections", "inspections"),
        ("/api/v1/projects/P001/handovers", "handovers"),
        ("/api/v1/projects/P001/crews", "crews"),
        ("/api/v1/projects/P001/ppe/issues", "ppe_issues"),
    ):
        response = client.get(path, headers=owner)
        assert response.status_code == 200
        assert key in response.json()["data"]["source_refs"]
    assert client.get("/api/v1/projects/P001/stock", headers=supplier).json()["data"]["items"] == []
    assert client.get("/api/v1/projects/P001/shipments", headers=supplier).json()["data"]["items"]
    assert client.get("/api/v1/projects/P001/crews", headers=supplier).json()["data"]["items"] == []
    supplier_inspections = client.get("/api/v1/projects/P001/inspections", headers=supplier)
    assert supplier_inspections.status_code == 200
    assert supplier_inspections.json()["data"]["items"]


def test_daily_report_publish_requires_idempotency_key() -> None:
    db = Database()
    client = TestClient(create_app(db))
    response = client.post(
        "/api/v1/projects/P001/daily-reports/not-a-draft:publish",
        json={"confirmation_token": "placeholder"},
        headers={"X-Demo-User": "owner@example.test"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_stocktake_confirmation_token_completes_large_difference() -> None:
    db = Database()
    client = TestClient(create_app(db))
    project_id = str(db.query_one("SELECT id FROM projects WHERE code='P001'")["id"])
    material_id = str(db.query_one("SELECT id FROM materials WHERE sku='MAT-BLOCK'")["id"])
    warehouse_id = str(db.query_one("SELECT id FROM warehouses WHERE code='WH-MAIN'")["id"])
    headers = {"X-Demo-User": "owner@example.test"}
    payload = {"warehouse_id": warehouse_id, "material_id": material_id, "counted_qty": "20", "reason": "盘点差异"}

    needs_token = client.post(
        "/api/v1/projects/P001/stocktakes",
        json=payload,
        headers={**headers, "Idempotency-Key": "api-stocktake-needs-token"},
    )
    assert needs_token.status_code == 409
    assert needs_token.json()["error"]["details"]["requires_confirmation"] is True

    token_response = client.post(
        "/api/v1/projects/P001/stocktakes:confirmation-token",
        json=payload,
        headers={**headers, "Idempotency-Key": "api-stocktake-token"},
    )
    assert token_response.status_code == 200
    token = token_response.json()["data"]["confirmation_token"]
    replay_token = client.post(
        "/api/v1/projects/P001/stocktakes:confirmation-token",
        json=payload,
        headers={**headers, "Idempotency-Key": "api-stocktake-token"},
    )
    assert replay_token.json()["data"]["confirmation_token"] == token

    completed = client.post(
        "/api/v1/projects/P001/stocktakes",
        json={**payload, "confirmation_token": token},
        headers={**headers, "Idempotency-Key": "api-stocktake-complete"},
    )
    assert completed.status_code == 200
    assert completed.json()["data"]["move_type"] == "ADJUSTMENT"


def test_reject_draft_persists_rejection_and_invalidates_confirmation() -> None:
    db = Database()
    client = TestClient(create_app(db))
    headers = {"X-Demo-User": "owner@example.test"}
    draft = client.post(
        "/api/v1/projects/P001/daily-reports:draft",
        json={"report_date": "2026-08-30"},
        headers={**headers, "Idempotency-Key": "api-reject-daily-draft"},
    )
    assert draft.status_code == 200
    draft_data = draft.json()["data"]
    draft_id = draft_data["draft_id"]
    token = draft_data["confirmation_token"]

    rejected = client.post(
        f"/api/v1/projects/P001/drafts/{draft_id}:reject",
        json={"reason": "不发布本次日报"},
        headers={**headers, "Idempotency-Key": "api-reject-daily-command"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["data"]["status"] == "REJECTED"
    replay = client.post(
        f"/api/v1/projects/P001/drafts/{draft_id}:reject",
        json={"reason": "不发布本次日报"},
        headers={**headers, "Idempotency-Key": "api-reject-daily-command"},
    )
    assert replay.json()["data"] == rejected.json()["data"]

    publish = client.post(
        f"/api/v1/projects/P001/daily-reports/{draft_id}:publish",
        json={"confirmation_token": token},
        headers={**headers, "Idempotency-Key": "api-rejected-draft-publish"},
    )
    assert publish.status_code == 400
    assert publish.json()["error"]["code"] == "INVALID_STATE"


def test_executed_purchase_draft_projection_exposes_formal_request_link() -> None:
    db = Database()
    client = TestClient(create_app(db))
    headers = {"X-Demo-User": "owner@example.test"}
    material = db.query_one("SELECT id,unit FROM materials WHERE sku='MAT-BLOCK'")
    warehouse = db.query_one("SELECT id FROM warehouses WHERE code='WH-MAIN'")
    assert material and warehouse
    draft = client.post(
        "/api/v1/projects/P001/purchase-requests:draft",
        json={
            "shortage_result": {
                "calculation_id": "api-draft-link-calc",
                "items": [{"material_id": material["id"], "recommended_order_qty": "10", "unit": material["unit"]}],
            },
            "selected_items": [{"material_id": material["id"], "qty": "10", "unit": material["unit"], "warehouse_id": warehouse["id"]}],
            "needed_by": "2026-09-01",
        },
        headers={**headers, "Idempotency-Key": "api-draft-link-create"},
    )
    assert draft.status_code == 200
    draft_data = draft.json()["data"]
    assert draft_data["purchase_request_id"] is None

    confirmed = client.post(
        f"/api/v1/projects/P001/purchase-requests/{draft_data['draft_id']}:confirm",
        json={"confirmation_token": draft_data["confirmation_token"]},
        headers={**headers, "Idempotency-Key": "api-draft-link-confirm"},
    )
    assert confirmed.status_code == 200
    request = confirmed.json()["data"]
    assert request["purchase_request_id"] == request["id"]

    projection = client.get(
        "/api/v1/projects/P001/drafts?draft_type=MATERIAL_REQUEST",
        headers=headers,
    )
    assert projection.status_code == 200
    linked_draft = next(item for item in projection.json()["data"]["items"] if item["id"] == draft_data["draft_id"])
    assert linked_draft["status"] == "EXECUTED"
    assert linked_draft["purchase_request_id"] == request["id"]
