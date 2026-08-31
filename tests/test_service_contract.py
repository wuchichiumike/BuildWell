"""BusinessService integration checks for authorization and inventory rules."""

from __future__ import annotations

import pytest

from backend.service import BusinessService, DomainError


def _issue_command(ids: dict[str, str], key: str = "issue-1") -> dict[str, object]:
    return {
        "project_id": ids["project"],
        "material_id": ids["material"],
        "batch_id": ids["batch"],
        "move_type": "ISSUE",
        "qty": "3",
        "unit": "piece",
        "from_warehouse_id": ids["warehouse"],
        "to_warehouse_id": None,
        "crew_id": None,
        "plan_id": None,
        "reference_type": "PLAN",
        "reference_id": "plan-1",
        "reason": "contract test",
        "idempotency_key": key,
    }


def test_stock_issue_never_makes_available_negative_and_is_idempotent(
    seeded_service: BusinessService,
    demo_ids: dict[str, str],
    owner_context: dict[str, object],
) -> None:
    command = _issue_command(demo_ids)
    first = seeded_service.create_stock_move(command, owner_context)
    second = seeded_service.create_stock_move(command, owner_context)

    assert first == second
    assert first["qty"] == "3"
    assert first["balance"]["available"] == "27"
    assert seeded_service.db.query_one("SELECT COUNT(*) AS count FROM stock_moves")["count"] == 1
    assert seeded_service.db.query_one("SELECT COUNT(*) AS count FROM audit_logs")["count"] == 1

    with pytest.raises(DomainError) as error:
        seeded_service.create_stock_move(
            {**command, "qty": "100", "idempotency_key": "issue-too-large"}, owner_context
        )
    assert error.value.code == "INSUFFICIENT_STOCK"


def test_same_idempotency_key_with_different_payload_is_rejected(
    seeded_service: BusinessService,
    demo_ids: dict[str, str],
    owner_context: dict[str, object],
) -> None:
    command = _issue_command(demo_ids, "issue-reused")
    seeded_service.create_stock_move(command, owner_context)
    with pytest.raises(DomainError) as error:
        seeded_service.create_stock_move({**command, "qty": "4"}, owner_context)
    assert error.value.code == "DUPLICATE_IDEMPOTENCY"


def test_only_project_owner_can_reverse_stock_moves(
    seeded_service: BusinessService,
    demo_ids: dict[str, str],
    owner_context: dict[str, object],
) -> None:
    original = seeded_service.create_stock_move(
        _issue_command(demo_ids, "reverse-original"), owner_context
    )

    for role in ("PROCUREMENT", "SITE_ADMIN"):
        user = seeded_service.db.query_one("SELECT id FROM users WHERE role=?", (role,))
        assert user
        with pytest.raises(DomainError) as error:
            seeded_service.reverse_stock_move(
                original["id"],
                "未经负责人授权的更正",
                {"user_id": user["id"], "project_id": demo_ids["project"], "roles": [role]},
                f"reverse-{role.lower()}",
            )
        assert error.value.code == "FORBIDDEN"

    reversed_move = seeded_service.reverse_stock_move(
        original["id"], "项目负责人更正", owner_context, "reverse-owner"
    )
    assert reversed_move["reverses_move_id"] == original["id"]

    scrapped = seeded_service.create_stock_move(
        {
            **_issue_command(demo_ids, "reverse-scrap-original"),
            "move_type": "SCRAP",
            "reference_type": None,
            "reference_id": None,
            "reason": "误报废",
        },
        owner_context,
    )
    restored = seeded_service.reverse_stock_move(
        scrapped["id"], "项目负责人恢复报废库存", owner_context, "reverse-scrap-owner"
    )
    assert restored["move_type"] == "RETURN"
    assert restored["reverses_move_id"] == scrapped["id"]


def test_supplier_cannot_write_project_inventory(
    seeded_service: BusinessService,
    demo_ids: dict[str, str],
) -> None:
    supplier_context = {
        "user_id": demo_ids["supplier_user"],
        "project_id": demo_ids["project"],
        "roles": ["SUPPLIER"],
    }
    with pytest.raises(DomainError) as error:
        seeded_service.create_stock_move(_issue_command(demo_ids, "supplier-issue"), supplier_context)
    assert error.value.code == "FORBIDDEN"
    assert seeded_service.db.query_one("SELECT COUNT(*) AS count FROM stock_moves")["count"] == 0


def test_service_uses_persisted_role_over_context_role_hint(
    seeded_service: BusinessService,
    demo_ids: dict[str, str],
) -> None:
    forged_supplier_context = {
        "user_id": demo_ids["supplier_user"],
        "project_id": demo_ids["project"],
        "roles": ["PROJECT_OWNER"],
    }
    with pytest.raises(DomainError) as error:
        seeded_service.create_stock_move(
            _issue_command(demo_ids, "forged-service-role"), forged_supplier_context
        )
    assert error.value.code == "FORBIDDEN"


def test_unknown_stock_command_fields_are_rejected_without_side_effect(
    seeded_service: BusinessService,
    demo_ids: dict[str, str],
    owner_context: dict[str, object],
) -> None:
    command = _issue_command(demo_ids, "unknown-field")
    command["user_id"] = "attacker"
    with pytest.raises(DomainError) as error:
        seeded_service.create_stock_move(command, owner_context)
    assert error.value.code == "VALIDATION_ERROR"
    assert seeded_service.db.query_one("SELECT COUNT(*) AS count FROM stock_moves")["count"] == 0


def test_wrong_unit_is_rejected_before_mutating_inventory(
    seeded_service: BusinessService,
    demo_ids: dict[str, str],
    owner_context: dict[str, object],
) -> None:
    with pytest.raises(DomainError) as error:
        seeded_service.create_stock_move(
            {**_issue_command(demo_ids, "bad-unit"), "unit": "kg"}, owner_context
        )
    assert error.value.code == "UNIT_MISMATCH"
    assert seeded_service.db.query_one("SELECT COUNT(*) AS count FROM stock_moves")["count"] == 0


def test_large_stocktake_difference_requires_confirmation(
    seeded_service: BusinessService,
    demo_ids: dict[str, str],
    owner_context: dict[str, object],
) -> None:
    with pytest.raises(DomainError) as error:
        seeded_service.reconcile_stock(
            demo_ids["project"],
            demo_ids["warehouse"],
            demo_ids["material"],
            "20",
            "stocktake",
            owner_context,
            idempotency_key="stocktake-large",
        )
    assert error.value.code == "VERSION_CONFLICT"
    assert seeded_service.db.query_one("SELECT COUNT(*) AS count FROM stock_moves")["count"] == 0


def test_daily_report_draft_contains_real_sources_and_is_idempotent(
    seeded_service: BusinessService,
    demo_ids: dict[str, str],
    owner_context: dict[str, object],
) -> None:
    command = {"project_id": demo_ids["project"], "report_date": "2026-08-26", "idempotency_key": "daily-report-contract"}
    first = seeded_service.create_daily_report_draft(command, owner_context)
    second = seeded_service.create_daily_report_draft(command, owner_context)
    assert first == second
    payload = first["payload"]
    assert payload["arrivals"]
    assert payload["shortage_risks"]
    assert payload["ppe_coverage"]
    assert payload["anomalies"]
    assert payload["todo"]
    assert first["confirmation_token"]


def test_daily_report_publish_is_idempotent_and_token_bound(
    seeded_service: BusinessService,
    demo_ids: dict[str, str],
    owner_context: dict[str, object],
) -> None:
    draft = seeded_service.create_daily_report_draft(
        {"project_id": demo_ids["project"], "report_date": "2026-08-26", "idempotency_key": "daily-publish-draft"},
        owner_context,
    )
    published = seeded_service.publish_daily_report(
        draft["draft_id"], draft["confirmation_token"], owner_context, "daily-publish-command"
    )
    replay = seeded_service.publish_daily_report(
        draft["draft_id"], draft["confirmation_token"], owner_context, "daily-publish-command"
    )

    assert replay == published
    assert seeded_service.db.query_one(
        "SELECT COUNT(*) AS count FROM daily_reports WHERE project_id=? AND report_date=?",
        (demo_ids["project"], "2026-08-26"),
    )["count"] == 2
    assert published["supersedes_report_id"] == seeded_service.db.query_one(
        "SELECT id FROM daily_reports WHERE project_id=? AND report_date=? AND version=1",
        (demo_ids["project"], "2026-08-26"),
    )["id"]
    assert seeded_service.db.query_one(
        "SELECT COUNT(*) AS count FROM audit_logs WHERE action='PUBLISH_DAILY_REPORT' AND entity_id=?",
        (published["id"],),
    )["count"] == 1

    # A consumed token with a new idempotency key still follows the existing
    # one-time confirmation semantics and cannot create another version.
    with pytest.raises(DomainError) as error:
        seeded_service.publish_daily_report(
            draft["draft_id"], draft["confirmation_token"], owner_context, "daily-publish-replay"
        )
    assert error.value.code == "INVALID_STATE"

    # Reusing a key for a different token/payload is rejected before any
    # confirmation-token lookup or database mutation.
    with pytest.raises(DomainError) as error:
        seeded_service.publish_daily_report(
            draft["draft_id"], "different-token", owner_context, "daily-publish-command"
        )
    assert error.value.code == "DUPLICATE_IDEMPOTENCY"
