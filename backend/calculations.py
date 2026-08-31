"""Deterministic planning, forecasting, anomaly and PPE calculations.

These functions accept plain mappings so they can be called by an API, an AI
tool adapter, or a test without granting either caller database access.  A
``BusinessService`` supplies the controlled snapshots from SQLite.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from typing import Any, Iterable, Mapping, Sequence

from .config import decimal_string, parse_decimal


def _canonical(value: Any) -> str:
    def normal(item: Any) -> Any:
        if isinstance(item, Decimal):
            return decimal_string(item)
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, Mapping):
            return {str(key): normal(item[key]) for key in sorted(item, key=lambda x: str(x))}
        if isinstance(item, (list, tuple)):
            return [normal(x) for x in item]
        return item

    return json.dumps(normal(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def snapshot_hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise ValueError("date must be ISO-8601")


def _money(value: Any, field: str = "quantity") -> Decimal:
    try:
        return parse_decimal(value, field=field)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _rows_for(records: Any, material_id: str | None = None) -> list[Mapping[str, Any]]:
    if records is None:
        return []
    if isinstance(records, Mapping):
        # A map keyed by material id is a convenient service snapshot shape.
        if material_id is not None and material_id in records:
            value = records[material_id]
            if isinstance(value, Mapping):
                return [value]
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return [x for x in value if isinstance(x, Mapping)]
        return [records]
    rows = [x for x in records if isinstance(x, Mapping)]
    if material_id is None:
        return rows
    # Service snapshots are commonly lists.  Material rows identify the
    # record by ``id`` while stock/plan/receipt rows use ``material_id``;
    # accept both without allowing another material's first row to bleed into
    # this calculation.
    return [
        row
        for row in rows
        if str(row.get("material_id", row.get("id", ""))) == str(material_id)
    ]


def _round_order(shortage: Decimal, minimum: Decimal, multiple: Decimal) -> Decimal:
    if shortage <= 0:
        return Decimal("0")
    minimum = max(Decimal("0"), minimum)
    multiple = multiple if multiple > 0 else Decimal("1")
    value = max(shortage, minimum)
    units = (value / multiple).to_integral_value(rounding=ROUND_CEILING)
    return units * multiple


def calculate_shortage(
    cmd: Mapping[str, Any],
    stock: Any = None,
    plans: Any = None,
    receipts: Any = None,
    materials: Any = None,
) -> dict[str, Any]:
    """Calculate projected inventory and order quantities for each material.

    ``stock``, ``plans`` and ``receipts`` are optional controlled snapshots.
    They may also be embedded in ``cmd`` under the same names, which keeps the
    function easy to use from JSON API tests.
    """

    project_id = str(cmd.get("project_id", ""))
    material_ids = [str(item) for item in cmd.get("material_ids", [])]
    if not material_ids:
        # A single material record is accepted for convenience, while the
        # public command remains list-based.
        one = cmd.get("material_id")
        material_ids = [str(one)] if one else []
    if not material_ids:
        raise ValueError("material_ids must not be empty")
    date_from = _as_date(cmd.get("date_from"))
    date_to = _as_date(cmd.get("date_to"))
    if date_to < date_from:
        raise ValueError("date_to must be on or after date_from")
    if (date_to - date_from).days > 365:
        raise ValueError("date range must not exceed 366 days")
    stock = cmd.get("stock", stock)
    plans = cmd.get("plans", plans)
    receipts = cmd.get("receipts", receipts)
    materials = cmd.get("materials", materials)
    include_expected = bool(cmd.get("include_expected_receipts", True))
    if "include_expected_receipts" not in cmd:
        include_expected = bool(cmd.get("include_expected_receipts", True))

    source_snapshot = {
        "project_id": project_id,
        "material_ids": material_ids,
        "date_from": date_from,
        "date_to": date_to,
        "area_ids": cmd.get("area_ids"),
        "warehouse_ids": cmd.get("warehouse_ids"),
        "include_expected_receipts": include_expected,
        "plan_version": cmd.get("plan_version"),
        "stock": stock,
        "plans": plans,
        "receipts": receipts,
        "materials": materials,
    }
    input_hash = snapshot_hash(source_snapshot)
    calc_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "shortage:" + input_hash))
    results: list[dict[str, Any]] = []
    missing_data: list[str] = []
    all_dates = [date_from + timedelta(days=n) for n in range((date_to - date_from).days + 1)]

    for material_id in material_ids:
        stock_rows = _rows_for(stock, material_id)
        stock_row = stock_rows[0] if stock_rows else {}
        material_rows = _rows_for(materials, material_id)
        material_row = material_rows[0] if material_rows else {}
        # Material metadata can be supplied alongside a stock row.
        unit = str(stock_row.get("unit") or material_row.get("unit") or "piece")
        try:
            on_hand = _money(stock_row.get("qty_on_hand", stock_row.get("on_hand", "0")))
            reserved = _money(stock_row.get("qty_reserved", stock_row.get("reserved", "0")))
            quarantined = _money(stock_row.get("qty_quarantined", stock_row.get("quarantined", "0")))
            safety = _money(stock_row.get("safety_stock", material_row.get("safety_stock", "0")))
            minimum = _money(stock_row.get("min_order_qty", material_row.get("min_order_qty", "0")))
            multiple = _money(stock_row.get("order_multiple", material_row.get("order_multiple", "1")))
        except ValueError as exc:
            missing_data.append(f"{material_id}:invalid_numeric_snapshot")
            raise exc
        available = on_hand - reserved - quarantined
        if not stock_rows:
            missing_data.append(f"{material_id}:stock")
        if available < 0:
            # The invariant is enforced by the write path; calculations expose
            # a warning rather than hiding an inconsistent source snapshot.
            missing_data.append(f"{material_id}:negative_available")

        plan_rows = _rows_for(plans, material_id)
        receipt_rows = _rows_for(receipts, material_id)
        plan_by_day: defaultdict[date, Decimal] = defaultdict(Decimal)
        receipt_by_day: defaultdict[date, Decimal] = defaultdict(Decimal)
        for row in plan_rows:
            if not row.get("plan_date") and not row.get("date"):
                continue
            day = _as_date(row.get("plan_date", row.get("date")))
            if date_from <= day <= date_to:
                plan_by_day[day] += _money(row.get("planned_qty", row.get("qty", "0")))
        for row in receipt_rows:
            eta = row.get("eta", row.get("expected_date", row.get("date")))
            if not eta:
                continue
            day = _as_date(eta)
            status = str(row.get("status", "IN_TRANSIT")).upper()
            # In-transit/SENT receipts are part of the default policy.  Mere
            # expected records are included only when explicitly enabled.
            allowed = status in {"SENT", "IN_TRANSIT", "PARTIAL_SHIPPED", "ACCEPTED", "ARRIVED", "DELIVERED", "EXPECTED"}
            if status == "EXPECTED" and not include_expected:
                allowed = False
            if allowed and date_from <= day <= date_to:
                receipt_by_day[day] += _money(row.get("qty", row.get("qty_expected", row.get("qty_shipped", "0"))))
        if not plan_rows:
            missing_data.append(f"{material_id}:plan")
        curve: list[dict[str, Any]] = []
        projected = available
        shortage_qty = Decimal("0")
        shortage_date: date | None = None
        for day in all_dates:
            projected += receipt_by_day[day] - plan_by_day[day]
            deficit = max(Decimal("0"), safety - projected)
            if deficit > shortage_qty:
                shortage_qty = deficit
            if shortage_date is None and projected < safety:
                shortage_date = day
            curve.append({
                "date": day.isoformat(),
                "planned_qty": decimal_string(plan_by_day[day]),
                "receipt_qty": decimal_string(receipt_by_day[day]),
                "projected_available": decimal_string(projected),
                "safety_stock": decimal_string(safety),
                "source_refs": [f"plan:{material_id}:{day.isoformat()}"],
            })
        results.append({
            "material_id": material_id,
            "unit": unit,
            "available_now": decimal_string(available),
            "safety_stock": decimal_string(safety),
            "shortage_qty": decimal_string(shortage_qty),
            "shortage_date": shortage_date.isoformat() if shortage_date else None,
            "recommended_order_qty": decimal_string(_round_order(shortage_qty, minimum, multiple)),
            "daily_curve": curve,
            "source_refs": [f"stock:{material_id}", f"plans:{material_id}"],
        })
    completeness = 1.0 if not missing_data else max(0.0, 1.0 - len(set(missing_data)) / max(1, len(material_ids) * 2))
    return {
        "calculation_id": calc_id,
        "calculation_version": str(cmd.get("calculation_version") or "shortage-v1"),
        "items": results,
        "input_snapshot_hash": input_hash,
        "missing_data": sorted(set(missing_data)),
        "warnings": ["MISSING_DATA" if missing_data else ""].copy() if missing_data else [],
        "confidence": round(completeness, 4),
    }


def forecast_inventory(
    cmd: Mapping[str, Any],
    history: Any = None,
    stock: Any = None,
    plans: Any = None,
    receipts: Any = None,
    materials: Any = None,
) -> dict[str, Any]:
    """Forecast daily net consumption with deterministic fallbacks."""

    history = cmd.get("history", history)
    method = str(cmd.get("method", "DETERMINISTIC")).upper()
    history_days = int(cmd.get("history_days", 14))
    if not 1 <= history_days <= 365:
        raise ValueError("history_days must be between 1 and 365")
    shortage = calculate_shortage(cmd, stock=stock, plans=plans, receipts=receipts, materials=materials)
    forecasts: list[dict[str, Any]] = []
    warnings = list(shortage["warnings"])
    for material_id in [str(item) for item in cmd.get("material_ids", [])]:
        rows = _rows_for(history, material_id)
        values: list[Decimal] = []
        for row in rows[-history_days:]:
            try:
                values.append(_money(row.get("net_consumed_qty", row.get("actual_net", row.get("qty", "0")))))
            except ValueError:
                continue
        dates = []
        try:
            start, end = _as_date(cmd["date_from"]), _as_date(cmd["date_to"])
            dates = [start + timedelta(days=n) for n in range((end - start).days + 1)]
        except (KeyError, ValueError):
            dates = []
        if len(values) < 7:
            warnings.append(f"{material_id}:INSUFFICIENT_HISTORY")
            fallback = next((item for item in shortage["items"] if item["material_id"] == material_id), None)
            forecasts.append({"material_id": material_id, "method": "DETERMINISTIC", "sample_count": len(values), "values": [], "shortage": fallback})
            continue
        window = min(7, len(values))
        if method == "EXP_SMOOTHING" and len(values) >= 14:
            alpha = Decimal("0.3")
            level = values[0]
            for value in values[1:]:
                level = alpha * value + (Decimal("1") - alpha) * level
            forecast_value = level
        elif method in {"MOVING_AVERAGE", "EXP_SMOOTHING"}:
            forecast_value = sum(values[-window:], Decimal("0")) / Decimal(window)
            if method == "EXP_SMOOTHING":
                warnings.append(f"{material_id}:EXP_SMOOTHING_FALLBACK_MOVING_AVERAGE")
        else:
            forecast_value = sum(values[-window:], Decimal("0")) / Decimal(window)
        forecasts.append({
            "material_id": material_id,
            "method": method if method in {"MOVING_AVERAGE", "EXP_SMOOTHING"} else "DETERMINISTIC",
            "window": window,
            "sample_count": len(values),
            "forecast_daily_qty": decimal_string(forecast_value),
            "curve": [{"date": day.isoformat(), "forecast_qty": decimal_string(forecast_value)} for day in dates],
        })
    return {
        "calculation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "forecast:" + snapshot_hash({"cmd": cmd, "history": history}))),
        "calculation_version": "forecast-v1",
        "method": method,
        "items": forecasts,
        "source_refs": ["consumption_snapshots"],
        "warnings": sorted(set(warnings)),
        "confidence": 1.0 if all(item.get("sample_count", 0) >= 7 for item in forecasts) else 0.5,
    }


def detect_consumption_anomaly(
    cmd: Mapping[str, Any],
    actual: Any = None,
    planned: Any = None,
    repeat_issues: Any = None,
) -> dict[str, Any]:
    """Return review clues, never responsibility attribution."""

    actual = cmd.get("actual", actual)
    planned = cmd.get("planned", planned)
    repeat_issues = cmd.get("repeat_issues", repeat_issues)
    ratio_threshold = _money(cmd.get("ratio_threshold", "0.20"))
    min_abs = _money(cmd.get("anomaly_min_qty", "1"))
    z_threshold = _money(cmd.get("zscore_threshold", "2.0"))
    by_key: defaultdict[tuple[str, str], dict[str, Decimal]] = defaultdict(lambda: {"actual": Decimal("0"), "planned": Decimal("0")})
    for row in _rows_for(actual):
        key = (str(row.get("material_id", "")), str(row.get("date", row.get("plan_date", ""))))
        by_key[key]["actual"] += _money(row.get("actual_net", row.get("net_consumed_qty", row.get("qty", "0"))))
    for row in _rows_for(planned):
        key = (str(row.get("material_id", "")), str(row.get("date", row.get("plan_date", ""))))
        by_key[key]["planned"] += _money(row.get("planned_qty", row.get("qty", "0")))
    clues: list[dict[str, Any]] = []
    values = [item["actual"] - item["planned"] for item in by_key.values()]
    mean = sum(values, Decimal("0")) / Decimal(len(values)) if values else Decimal("0")
    variance = sum((v - mean) ** 2 for v in values) / Decimal(len(values)) if values else Decimal("0")
    std = Decimal(str(math.sqrt(float(variance)))) if variance > 0 else Decimal("0")
    for (material_id, day), item in sorted(by_key.items()):
        difference = item["actual"] - item["planned"]
        denominator = max(item["planned"], Decimal("0.000001"))
        ratio = difference / denominator
        z_score = (difference - mean) / std if std else Decimal("0")
        kinds: list[str] = []
        if abs(difference) >= min_abs and abs(ratio) >= ratio_threshold:
            kinds.append("PLAN_VARIANCE")
        if abs(z_score) >= z_threshold:
            kinds.append("STATISTICAL")
        if kinds:
            clues.append({
                "material_id": material_id,
                "date": day,
                "types": kinds,
                "actual_net": decimal_string(item["actual"]),
                "planned_qty": decimal_string(item["planned"]),
                "plan_variance_ratio": decimal_string(ratio),
                "z_score": decimal_string(z_score),
                "possible_causes": ["计划或现场记录需要复核"],
                "review_steps": ["核对领用流水、班组记录和计划版本"],
            })
    for row in _rows_for(repeat_issues):
        count = int(row.get("count", row.get("repeat_count", 0)))
        if count >= 3:
            clues.append({
                "material_id": str(row.get("material_id", "")),
                "crew_id": str(row.get("crew_id", "")),
                "types": ["PROCESS_REPEAT"],
                "repeat_count_24h": count,
                "possible_causes": ["短时间重复领用或录入流程需要复核"],
                "review_steps": ["核对原始领用单和退库记录"],
            })
    return {
        "calculation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "anomaly:" + snapshot_hash({"cmd": cmd, "actual": actual, "planned": planned, "repeat_issues": repeat_issues}))),
        "calculation_version": "anomaly-v1",
        "items": clues,
        "warnings": [],
        "source_refs": ["stock_moves", "material_plans"],
        "confidence": 1.0,
    }


def check_ppe_coverage(
    cmd: Mapping[str, Any],
    crews: Any = None,
    standards: Any = None,
    issues: Any = None,
) -> dict[str, Any]:
    """Calculate PPE requirements using anonymous crew/member references."""

    crews = cmd.get("crews", crews)
    standards = cmd.get("standards", standards)
    issues = cmd.get("issues", issues)
    as_of = _as_date(cmd.get("as_of", date.today()))
    warning_days = int(cmd.get("warning_days", 14))
    if not 0 <= warning_days <= 365:
        raise ValueError("warning_days must be between 0 and 365")
    crew_rows = _rows_for(crews)
    standard_rows = _rows_for(standards)
    issue_rows = _rows_for(issues)
    issue_by_crew_material: defaultdict[tuple[str, str], Decimal] = defaultdict(Decimal)
    expiring: list[dict[str, Any]] = []
    for row in issue_rows:
        crew = str(row.get("crew_id", ""))
        material = str(row.get("material_id", ""))
        due = row.get("replacement_due")
        if due:
            due_date = _as_date(due)
            if as_of <= due_date <= as_of + timedelta(days=warning_days):
                expiring.append({"crew_id": crew, "material_id": material, "replacement_due": due_date.isoformat()})
        if not row.get("voided", False) and (not due or _as_date(due) >= as_of):
            issue_by_crew_material[(crew, material)] += _money(row.get("qty", "0"))
    result: list[dict[str, Any]] = []
    for crew in crew_rows:
        crew_id = str(crew.get("id", crew.get("crew_id", "")))
        trade = str(crew.get("trade", ""))
        headcount = _money(crew.get("headcount", crew.get("planned_headcount", 0)))
        for standard in standard_rows:
            if standard.get("trade") and str(standard["trade"]) != trade:
                continue
            material_id = str(standard.get("material_id", ""))
            per_person = _money(standard.get("required_qty_per_person", standard.get("per_person", "0")))
            required = headcount * per_person
            issued = issue_by_crew_material[(crew_id, material_id)]
            result.append({
                "crew_id": crew_id,
                "material_id": material_id,
                "required_qty": decimal_string(required),
                "issued_qty": decimal_string(issued),
                "shortage_qty": decimal_string(max(Decimal("0"), required - issued)),
                "anonymous": True,
            })
    missing = [] if standard_rows else ["ppe_standards"]
    return {
        "calculation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "ppe:" + snapshot_hash({"cmd": cmd, "crews": crews, "standards": standards, "issues": issues}))),
        "calculation_version": "ppe-v1",
        "as_of": as_of.isoformat(),
        "items": result,
        "expiring": expiring,
        "anonymous_crews": sorted({item["crew_id"] for item in result}),
        "missing_data": missing,
        "warnings": ["MISSING_DATA"] if missing else [],
        "confidence": 1.0 if not missing else 0.5,
    }


__all__ = [
    "calculate_shortage",
    "check_ppe_coverage",
    "detect_consumption_anomaly",
    "forecast_inventory",
    "snapshot_hash",
]
