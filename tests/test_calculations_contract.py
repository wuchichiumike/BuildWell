"""Executable contracts for the deterministic calculation services.

The calculation layer is intentionally pure: these tests pass controlled
snapshots instead of a database connection, which keeps the invariants easy to
exercise in CI and makes the same cases useful for a PostgreSQL adapter.
"""

from __future__ import annotations

from datetime import date, timedelta

from backend.calculations import (
    calculate_shortage,
    check_ppe_coverage,
    detect_consumption_anomaly,
    forecast_inventory,
)


def _shortage_command(**overrides: object) -> dict[str, object]:
    command: dict[str, object] = {
        "project_id": "project-1",
        "material_ids": ["m1"],
        "date_from": date(2026, 8, 27),
        "date_to": date(2026, 8, 29),
        "include_expected_receipts": True,
    }
    command.update(overrides)
    return command


def test_shortage_uses_available_stock_and_rounds_to_order_constraints() -> None:
    stock = [
        {
            "material_id": "m1",
            "qty_on_hand": "15.000",
            "qty_reserved": "2.000",
            "qty_quarantined": "1.000",
            "unit": "piece",
            "safety_stock": "10",
            "min_order_qty": "10",
            "order_multiple": "5",
        }
    ]
    plans = [
        {"material_id": "m1", "plan_date": "2026-08-27", "planned_qty": "3"},
        {"material_id": "m1", "plan_date": "2026-08-28", "planned_qty": "5"},
    ]
    receipts = [
        {
            "material_id": "m1",
            "eta": "2026-08-28",
            "status": "IN_TRANSIT",
            "qty": "4",
        }
    ]

    result = calculate_shortage(_shortage_command(), stock, plans, receipts)
    item = result["items"][0]

    # available0 = 15 - 2 - 1 = 12. The curve reaches 8 on 2026-08-28,
    # therefore the largest safety-stock deficit is 2 and the order is rounded
    # up to min_order_qty=10 (which is already a multiple of 5).
    assert item["available_now"] == "12"
    assert item["shortage_qty"] == "2"
    assert item["shortage_date"] == "2026-08-27"
    assert item["recommended_order_qty"] == "10"
    assert [point["projected_available"] for point in item["daily_curve"]] == ["9", "8", "8"]
    assert result["missing_data"] == []


def test_shortage_excludes_unconfirmed_expected_receipts_when_requested() -> None:
    stock = [{"material_id": "m1", "qty_on_hand": "10", "safety_stock": "5"}]
    plans = [{"material_id": "m1", "plan_date": "2026-08-27", "planned_qty": "8"}]
    receipts = [{"material_id": "m1", "eta": "2026-08-27", "status": "EXPECTED", "qty": "8"}]

    included = calculate_shortage(
        _shortage_command(include_expected_receipts=True), stock, plans, receipts
    )
    excluded = calculate_shortage(
        _shortage_command(include_expected_receipts=False), stock, plans, receipts
    )

    assert included["items"][0]["shortage_qty"] == "0"
    assert excluded["items"][0]["shortage_qty"] == "3"
    assert included["input_snapshot_hash"] != excluded["input_snapshot_hash"]
    assert included["calculation_id"] != excluded["calculation_id"]


def test_shortage_is_reproducible_for_an_identical_snapshot() -> None:
    command = _shortage_command()
    stock = [{"material_id": "m1", "qty_on_hand": "1", "safety_stock": "2"}]
    plans = [{"material_id": "m1", "plan_date": "2026-08-27", "planned_qty": "1"}]

    first = calculate_shortage(command, stock, plans)
    second = calculate_shortage(command, stock, plans)

    assert first == second
    assert first["input_snapshot_hash"]
    assert first["calculation_id"]


def test_forecast_falls_back_with_insufficient_history() -> None:
    history = [
        {"material_id": "m1", "date": f"2026-08-{day:02d}", "net_consumed_qty": "2"}
        for day in range(20, 26)
    ]
    result = forecast_inventory(
        _shortage_command(method="MOVING_AVERAGE", history_days=14),
        history=history,
        stock=[{"material_id": "m1", "qty_on_hand": "20", "safety_stock": "5"}],
        plans=[],
    )

    item = result["items"][0]
    assert item["method"] == "DETERMINISTIC"
    assert item["sample_count"] == 6
    assert item["values"] == []
    assert "m1:INSUFFICIENT_HISTORY" in result["warnings"]
    assert result["confidence"] < 1


def test_forecast_moving_average_reports_sample_count_and_curve() -> None:
    history = [
        {"material_id": "m1", "date": (date(2026, 8, 1) + timedelta(days=i)).isoformat(), "net_consumed_qty": str(i + 1)}
        for i in range(7)
    ]
    result = forecast_inventory(
        _shortage_command(
            method="MOVING_AVERAGE",
            date_from=date(2026, 8, 27),
            date_to=date(2026, 8, 29),
            history_days=14,
        ),
        history=history,
        stock=[{"material_id": "m1", "qty_on_hand": "20", "safety_stock": "5"}],
        plans=[],
    )

    item = result["items"][0]
    assert item["method"] == "MOVING_AVERAGE"
    assert item["sample_count"] == 7
    assert item["window"] == 7
    assert item["forecast_daily_qty"] == "4"
    assert len(item["curve"]) == 3


def test_anomaly_output_contains_review_clues_but_no_attribution() -> None:
    result = detect_consumption_anomaly(
        {
            "project_id": "project-1",
            "date_from": date(2026, 8, 27),
            "date_to": date(2026, 8, 29),
            "ratio_threshold": "0.20",
            "anomaly_min_qty": "1",
        },
        actual=[{"material_id": "m1", "date": "2026-08-27", "actual_net": "15"}],
        planned=[{"material_id": "m1", "date": "2026-08-27", "planned_qty": "10"}],
        repeat_issues=[{"material_id": "m1", "crew_id": "crew-1", "count": 3}],
    )

    assert len(result["items"]) == 2
    assert {"PLAN_VARIANCE"} <= set(result["items"][0]["types"])
    assert result["items"][0]["possible_causes"]
    assert result["items"][0]["review_steps"]
    for item in result["items"]:
        assert "responsible" not in item
        assert "person" not in item
        assert "penalty" not in item


def test_ppe_coverage_uses_headcount_and_hides_worker_identity() -> None:
    result = check_ppe_coverage(
        {
            "project_id": "project-1",
            "as_of": date(2026, 8, 27),
            "warning_days": 14,
        },
        crews=[{"id": "crew-1", "trade": "MASON", "planned_headcount": 10}],
        standards=[
            {
                "trade": "MASON",
                "material_id": "gloves",
                "required_qty_per_person": "1",
            }
        ],
        issues=[
            {
                "crew_id": "crew-1",
                "material_id": "gloves",
                "qty": "7",
                "replacement_due": "2026-09-01",
                "anonymous_worker_no": "worker-secret",
            }
        ],
    )

    item = result["items"][0]
    assert item["required_qty"] == "10"
    assert item["issued_qty"] == "7"
    assert item["shortage_qty"] == "3"
    assert result["anonymous_crews"] == ["crew-1"]
    assert result["expiring"] == [{"crew_id": "crew-1", "material_id": "gloves", "replacement_due": "2026-09-01"}]
    # Real worker identifiers must never leak through the calculation result.
    assert "worker-secret" not in repr(result)
