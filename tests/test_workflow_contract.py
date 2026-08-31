"""High-risk workflow contracts for procurement, inspection and evidence."""

from __future__ import annotations

import pytest

from backend.evidence import MockEvidenceAdapter
from backend.service import BusinessService, DomainError


def _context(service: BusinessService, role: str, project_id: str) -> dict[str, object]:
    user = service.db.query_one("SELECT * FROM users WHERE role=?", (role,))
    assert user
    return {
        "user_id": user["id"],
        "organization_id": user["organization_id"],
        "roles": [role],
        "project_id": project_id,
    }


def _ids(service: BusinessService) -> dict[str, str]:
    project = service.db.query_one("SELECT id FROM projects WHERE code='P001'")
    material = service.db.query_one("SELECT id,unit FROM materials WHERE sku='MAT-BLOCK'")
    supplier = service.db.query_one("SELECT id FROM suppliers WHERE code='SUP-A'")
    warehouse = service.db.query_one("SELECT id FROM warehouses WHERE code='WH-MAIN'")
    assert project and material and supplier and warehouse
    return {
        "project": project["id"],
        "material": material["id"],
        "unit": material["unit"],
        "supplier": supplier["id"],
        "warehouse": warehouse["id"],
    }


def test_confirmed_purchase_draft_enters_pending_approval(
    seeded_service: BusinessService,
) -> None:
    ids = _ids(seeded_service)
    owner = _context(seeded_service, "PROJECT_OWNER", ids["project"])
    shortage = {
        "project_id": ids["project"],
        "calculation_id": "calc-test",
        "items": [{"material_id": ids["material"], "recommended_order_qty": "10", "unit": ids["unit"]}],
    }
    draft = seeded_service.draft_material_request(
        shortage,
        [{"material_id": ids["material"], "qty": "10", "unit": ids["unit"], "warehouse_id": ids["warehouse"]}],
        "2026-09-01",
        actor=owner,
    )

    request = seeded_service.submit_material_request(draft["draft_id"], draft["confirmation_token"], owner)

    assert request["status"] == "PENDING_APPROVAL"
    assert request["purchase_request_id"] == request["id"]
    persisted_draft = seeded_service.db.query_one(
        "SELECT status,purchase_request_id FROM drafts WHERE id=?",
        (draft["draft_id"],),
    )
    assert persisted_draft and persisted_draft["status"] == "EXECUTED"
    assert persisted_draft["purchase_request_id"] == request["id"]
    projection = seeded_service.list_drafts(ids["project"], owner, "MATERIAL_REQUEST")
    linked_draft = next(item for item in projection["items"] if item["id"] == draft["draft_id"])
    assert linked_draft["purchase_request_id"] == request["id"]
    approval = seeded_service.issue_purchase_approval_token(request["id"], owner)
    approved = seeded_service.approve_purchase_request(request["id"], owner, approval["confirmation_token"])
    assert approved["status"] == "APPROVED"
    assert seeded_service.db.query_one("SELECT COUNT(*) AS n FROM evidence_events WHERE event_type='PURCHASE_APPROVAL'")["n"] == 1
    with pytest.raises(DomainError) as error:
        seeded_service.approve_purchase_request(request["id"], owner, approval["confirmation_token"])
    assert error.value.code in {"INVALID_STATE", "TOKEN_REPLAYED"}


def test_partial_inspection_separates_available_and_quarantined_stock(
    seeded_service: BusinessService,
) -> None:
    ids = _ids(seeded_service)
    owner = _context(seeded_service, "PROJECT_OWNER", ids["project"])
    supplier = _context(seeded_service, "SUPPLIER", ids["project"])
    site = _context(seeded_service, "SITE_ADMIN", ids["project"])
    inspector = _context(seeded_service, "INSPECTOR", ids["project"])

    order = seeded_service.create_purchase_order(
        {
            "project_id": "P001",
            "supplier_id": ids["supplier"],
            "items": [{"material_id": ids["material"], "qty": "10", "unit": ids["unit"]}],
            "idempotency_key": "workflow-po",
        },
        owner,
    )
    shipment = seeded_service.submit_shipment(
        {
            "project_id": "P001",
            "purchase_order_id": order["id"],
            "items": [{"material_id": ids["material"], "qty": "10", "unit": ids["unit"], "batch_no": "TEST-PARTIAL-01"}],
            "idempotency_key": "workflow-shipment",
        },
        supplier,
    )
    delivery = seeded_service.record_delivery(
        {
            "project_id": "P001",
            "shipment_id": shipment["id"],
            "warehouse_id": ids["warehouse"],
            "items": [{"shipment_item_id": shipment["items"][0]["id"], "qty_arrived": "10"}],
            "idempotency_key": "workflow-delivery",
        },
        site,
    )
    seeded_service.confirm_delivery(delivery["id"], delivery["confirmation_token"], site)
    inspection = seeded_service.submit_inspection(
        {
            "delivery_id": delivery["id"],
            "result": "PARTIAL",
            "items": [{"delivery_item_id": delivery["items"][0]["id"], "qty_passed": "8", "qty_failed": "2", "failure_reason": "包装破损"}],
            "idempotency_key": "workflow-inspection",
        },
        inspector,
    )
    confirmed = seeded_service.confirm_inspection(inspection["id"], inspection["confirmation_token"], inspector)

    assert confirmed["status"] == "PARTIAL"
    assert confirmed["receipts"][0]["qty"] == "8"
    assert confirmed["quarantined"][0]["qty"] == "2"
    balance = seeded_service.db.query_one(
        "SELECT qty_on_hand,qty_quarantined FROM stock_balances WHERE warehouse_id=? AND batch_id=?",
        (ids["warehouse"], shipment["items"][0]["batch_id"]),
    )
    assert balance["qty_on_hand"] == "10"
    assert balance["qty_quarantined"] == "2"
    assert seeded_service.db.query_one("SELECT COUNT(*) AS n FROM stock_moves WHERE reference_id=?", (inspection["id"],))["n"] == 1
    order_item = seeded_service.db.query_one(
        "SELECT poi.qty_received,po.status FROM purchase_order_items poi JOIN purchase_orders po ON po.id=poi.order_id WHERE po.id=?",
        (order["id"],),
    )
    assert order_item["qty_received"] == "8"
    assert order_item["status"] == "PARTIAL_RECEIVED"
    with pytest.raises(DomainError) as error:
        seeded_service.confirm_inspection(inspection["id"], inspection["confirmation_token"], inspector)
    assert error.value.code == "TOKEN_REPLAYED"


def test_evidence_failure_does_not_rollback_confirmed_business_record(
    seeded_service: BusinessService,
) -> None:
    ids = _ids(seeded_service)
    owner = _context(seeded_service, "PROJECT_OWNER", ids["project"])
    adapter = MockEvidenceAdapter(available=False)
    seeded_service.evidence_adapter = adapter
    event = seeded_service.enqueue_evidence(
        {
            "project_id": ids["project"],
            "business_document_id": "manual-event-1",
            "event_type": "HANDOVER",
            "occurred_at": "2026-08-27T00:00:00Z",
            "version": 1,
        },
        owner,
    )
    token = seeded_service.issue_evidence_anchor_token(event["id"], owner)
    failed = seeded_service.anchor_evidence(event["id"], token["confirmation_token"], owner)

    assert failed["status"] == "RETRYING"
    assert seeded_service.db.query_one("SELECT status FROM evidence_events WHERE id=?", (event["id"],))["status"] == "RETRYING"
    adapter.available = True
    retried = seeded_service.retry_failed_evidence(event["id"], owner)
    assert retried["tx_id"] == f"mocktx_{event['id']}"
