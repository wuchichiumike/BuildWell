"""Resource-scope checks for site administrators and scoped inspectors."""

from __future__ import annotations

import json
import uuid

import pytest

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


def _project_ids(service: BusinessService) -> dict[str, str]:
    project = service.db.query_one("SELECT id FROM projects WHERE code='P001'")
    area = service.db.query_one(
        "SELECT a.id FROM areas a JOIN buildings b ON b.id=a.building_id "
        "WHERE b.project_id=? ORDER BY a.code LIMIT 1",
        (project["id"],),
    )
    material = service.db.query_one("SELECT id,unit FROM materials WHERE sku='MAT-BLOCK'")
    batch = service.db.query_one("SELECT id FROM batches WHERE material_id=?", (material["id"],))
    supplier = service.db.query_one("SELECT id FROM suppliers WHERE code='SUP-A'")
    warehouse = service.db.query_one("SELECT id FROM warehouses WHERE code='WH-MAIN'")
    assert project and area and material and batch and supplier and warehouse
    return {
        "project": str(project["id"]),
        "area": str(area["id"]),
        "material": str(material["id"]),
        "unit": str(material["unit"]),
        "batch": str(batch["id"]),
        "supplier": str(supplier["id"]),
        "warehouse": str(warehouse["id"]),
    }


def _add_warehouse(service: BusinessService, project_id: str, area_id: str, code: str) -> str:
    warehouse_id = str(uuid.uuid4())
    service.db.execute(
        "INSERT INTO warehouses(id,project_id,code,name,area_id) VALUES(?,?,?,?,?)",
        (warehouse_id, project_id, code, code, area_id),
    )
    return warehouse_id


def _add_area_and_warehouse(service: BusinessService, project_id: str, code: str) -> tuple[str, str]:
    building_id = str(uuid.uuid4())
    area_id = str(uuid.uuid4())
    service.db.execute(
        "INSERT INTO buildings(id,project_id,code,name) VALUES(?,?,?,?)",
        (building_id, project_id, code, code),
    )
    service.db.execute(
        "INSERT INTO areas(id,building_id,code,name) VALUES(?,?,?,?)",
        (area_id, building_id, code, code),
    )
    return area_id, _add_warehouse(service, project_id, area_id, f"WH-{code}")


def _set_scope(service: BusinessService, project_id: str, user_id: str, warehouse_ids: list[str], area_ids: list[str]) -> None:
    service.db.execute(
        "UPDATE project_members SET scopes_json=? WHERE project_id=? AND user_id=?",
        (json.dumps({"warehouse_ids": warehouse_ids, "area_ids": area_ids}), project_id, user_id),
    )


def test_site_admin_scope_covers_both_transfer_ends_and_ppe_issue(
    seeded_service: BusinessService,
) -> None:
    ids = _project_ids(seeded_service)
    site = _context(seeded_service, "SITE_ADMIN", ids["project"])
    site_user = str(site["user_id"])
    same_area = _add_warehouse(seeded_service, ids["project"], ids["area"], "WH-SAME-AREA")
    other_area, other = _add_area_and_warehouse(seeded_service, ids["project"], "AREA-OTHER")

    # The seeded scope contains only WH-MAIN.  A transfer must authorize both
    # the source and destination, even when the destination shares its area.
    _set_scope(seeded_service, ids["project"], site_user, [ids["warehouse"]], [ids["area"]])
    with pytest.raises(DomainError) as error:
        seeded_service.transfer_material(
            {
                "project_id": ids["project"],
                "material_id": ids["material"],
                "batch_id": ids["batch"],
                "qty": "1",
                "unit": ids["unit"],
                "from_warehouse_id": ids["warehouse"],
                "to_warehouse_id": same_area,
                "reason": "scope test",
                "idempotency_key": "scope-transfer-same-area-denied",
            },
            site,
        )
    assert error.value.code == "FORBIDDEN"

    # Including the warehouse is still insufficient when its area is outside
    # the member's area scope.
    _set_scope(seeded_service, ids["project"], site_user, [ids["warehouse"], other], [ids["area"]])
    with pytest.raises(DomainError) as error:
        seeded_service.transfer_material(
            {
                "project_id": ids["project"],
                "material_id": ids["material"],
                "batch_id": ids["batch"],
                "qty": "1",
                "unit": ids["unit"],
                "from_warehouse_id": ids["warehouse"],
                "to_warehouse_id": other,
                "reason": "area scope test",
                "idempotency_key": "scope-transfer-area-denied",
            },
            site,
        )
    assert error.value.code == "FORBIDDEN"

    _set_scope(seeded_service, ids["project"], site_user, [ids["warehouse"], other], [ids["area"], other_area])
    moved = seeded_service.transfer_material(
        {
            "project_id": ids["project"],
            "material_id": ids["material"],
            "batch_id": ids["batch"],
            "qty": "1",
            "unit": ids["unit"],
            "from_warehouse_id": ids["warehouse"],
            "to_warehouse_id": other,
            "reason": "scope test allowed",
            "idempotency_key": "scope-transfer-allowed",
        },
        site,
    )
    assert len(moved["moves"]) == 2

    # PPE issuance uses the same warehouse scope through its nested ISSUE move.
    _set_scope(seeded_service, ids["project"], site_user, [ids["warehouse"]], [ids["area"], other_area])
    glove = seeded_service.db.query_one("SELECT id FROM materials WHERE sku='MAT-GLOVE'")
    glove_batch = seeded_service.db.query_one("SELECT id FROM batches WHERE material_id=?", (glove["id"],))
    crew = seeded_service.db.query_one("SELECT id FROM crews WHERE project_id=? LIMIT 1", (ids["project"],))
    assert glove and glove_batch and crew
    with pytest.raises(DomainError) as error:
        seeded_service.issue_ppe(
            {
                "project_id": ids["project"],
                "crew_id": crew["id"],
                "material_id": glove["id"],
                "batch_id": glove_batch["id"],
                "warehouse_id": other,
                "qty": "1",
                "idempotency_key": "scope-ppe-denied",
            },
            site,
        )
    assert error.value.code == "FORBIDDEN"
    issued = seeded_service.issue_ppe(
        {
            "project_id": ids["project"],
            "crew_id": crew["id"],
            "material_id": glove["id"],
            "batch_id": glove_batch["id"],
            "warehouse_id": ids["warehouse"],
            "qty": "1",
            "idempotency_key": "scope-ppe-allowed",
        },
        site,
    )
    assert issued["qty"] == "1"


def test_delivery_and_inspection_scope_is_checked_on_create_and_confirm(
    seeded_service: BusinessService,
) -> None:
    ids = _project_ids(seeded_service)
    owner = _context(seeded_service, "PROJECT_OWNER", ids["project"])
    supplier = _context(seeded_service, "SUPPLIER", ids["project"])
    site = _context(seeded_service, "SITE_ADMIN", ids["project"])
    inspector = _context(seeded_service, "INSPECTOR", ids["project"])
    other_area, other = _add_area_and_warehouse(seeded_service, ids["project"], "AREA-DELIVERY")
    _set_scope(seeded_service, ids["project"], str(site["user_id"]), [ids["warehouse"]], [ids["area"]])

    order = seeded_service.create_purchase_order(
        {
            "project_id": ids["project"],
            "supplier_id": ids["supplier"],
            "items": [{"material_id": ids["material"], "qty": "4", "unit": ids["unit"]}],
            "idempotency_key": "scope-delivery-order",
        },
        owner,
    )
    shipment = seeded_service.submit_shipment(
        {
            "project_id": ids["project"],
            "purchase_order_id": order["id"],
            "items": [{"material_id": ids["material"], "qty": "4", "unit": ids["unit"], "batch_no": "SCOPE-DELIVERY-BATCH"}],
            "idempotency_key": "scope-delivery-shipment",
        },
        supplier,
    )
    shipment_item_id = shipment["items"][0]["id"]

    with pytest.raises(DomainError) as error:
        seeded_service.record_delivery(
            {
                "project_id": ids["project"],
                "shipment_id": shipment["id"],
                "warehouse_id": other,
                "items": [{"shipment_item_id": shipment_item_id, "qty_arrived": "4"}],
                "idempotency_key": "scope-delivery-denied",
            },
            site,
        )
    assert error.value.code == "FORBIDDEN"

    delivery = seeded_service.record_delivery(
        {
            "project_id": ids["project"],
            "shipment_id": shipment["id"],
            "warehouse_id": ids["warehouse"],
            "items": [{"shipment_item_id": shipment_item_id, "qty_arrived": "4"}],
            "idempotency_key": "scope-delivery-allowed",
        },
        site,
    )
    # Existing delivery state is not enough to bypass a changed scope.
    _set_scope(seeded_service, ids["project"], str(site["user_id"]), [other], [other_area])
    with pytest.raises(DomainError) as error:
        seeded_service.confirm_delivery(delivery["id"], delivery["confirmation_token"], site)
    assert error.value.code == "FORBIDDEN"
    _set_scope(seeded_service, ids["project"], str(site["user_id"]), [ids["warehouse"]], [ids["area"]])
    confirmed_delivery = seeded_service.confirm_delivery(delivery["id"], delivery["confirmation_token"], site)
    assert confirmed_delivery["status"] == "ACCEPTED"

    inspection_command = {
        "delivery_id": delivery["id"],
        "result": "PASSED",
        "items": [{"delivery_item_id": delivery["items"][0]["id"], "qty_passed": "4", "qty_failed": "0"}],
        "idempotency_key": "scope-inspection",
    }
    _set_scope(seeded_service, ids["project"], str(inspector["user_id"]), [other], [other_area])
    with pytest.raises(DomainError) as error:
        seeded_service.submit_inspection(inspection_command, inspector)
    assert error.value.code == "FORBIDDEN"
    _set_scope(seeded_service, ids["project"], str(inspector["user_id"]), [ids["warehouse"]], [ids["area"]])
    inspection = seeded_service.submit_inspection(inspection_command, inspector)
    _set_scope(seeded_service, ids["project"], str(inspector["user_id"]), [other], [other_area])
    with pytest.raises(DomainError) as error:
        seeded_service.confirm_inspection(inspection["id"], inspection["confirmation_token"], inspector)
    assert error.value.code == "FORBIDDEN"
    _set_scope(seeded_service, ids["project"], str(inspector["user_id"]), [ids["warehouse"]], [ids["area"]])
    confirmed_inspection = seeded_service.confirm_inspection(inspection["id"], inspection["confirmation_token"], inspector)
    assert confirmed_inspection["status"] == "PASSED"


def test_handover_scope_is_checked_for_both_warehouses_and_confirmation(
    seeded_service: BusinessService,
) -> None:
    ids = _project_ids(seeded_service)
    owner = _context(seeded_service, "PROJECT_OWNER", ids["project"])
    site = _context(seeded_service, "SITE_ADMIN", ids["project"])
    project_org = str(owner["organization_id"])
    supplier_org = str(_context(seeded_service, "SUPPLIER", ids["project"])["organization_id"])
    other_area, other = _add_area_and_warehouse(seeded_service, ids["project"], "AREA-HANDOVER")
    _set_scope(seeded_service, ids["project"], str(site["user_id"]), [ids["warehouse"]], [ids["area"]])
    command = {
        "project_id": ids["project"],
        "source_org_id": project_org,
        "target_org_id": supplier_org,
        "source_warehouse_id": ids["warehouse"],
        "target_warehouse_id": other,
        "items": [{"material_id": ids["material"], "batch_id": ids["batch"], "qty": "1", "unit": ids["unit"]}],
        "idempotency_key": "scope-handover-denied",
    }
    with pytest.raises(DomainError) as error:
        seeded_service.create_handover(command, site)
    assert error.value.code == "FORBIDDEN"

    _set_scope(seeded_service, ids["project"], str(site["user_id"]), [ids["warehouse"], other], [ids["area"], other_area])
    handover = seeded_service.create_handover({**command, "idempotency_key": "scope-handover-allowed"}, site)
    _set_scope(seeded_service, ids["project"], str(site["user_id"]), [other], [other_area])
    with pytest.raises(DomainError) as error:
        seeded_service.issue_handover_confirmation_token(handover["id"], site)
    assert error.value.code == "FORBIDDEN"

    _set_scope(seeded_service, ids["project"], str(site["user_id"]), [ids["warehouse"], other], [ids["area"], other_area])
    token = seeded_service.issue_handover_confirmation_token(handover["id"], site)
    _set_scope(seeded_service, ids["project"], str(site["user_id"]), [other], [other_area])
    with pytest.raises(DomainError) as error:
        seeded_service.confirm_handover(handover["id"], token["confirmation_token"], site)
    assert error.value.code == "FORBIDDEN"

