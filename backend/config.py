"""Runtime configuration and serialization helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_decimal(value: Any, *, field: str = "quantity") -> Decimal:
    """Parse a decimal without silently accepting floats or scientific values.

    JSON clients must send decimal strings.  ``Decimal`` is accepted for
    internal callers; floats are rejected to prevent binary rounding leaking
    into inventory balances.
    """

    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field} must be a decimal string")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        if not value.strip() or "e" in value.lower():
            raise ValueError(f"{field} must be a fixed decimal string")
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{field} must be a decimal string") from exc
    else:
        raise ValueError(f"{field} must be a decimal string")
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def decimal_string(value: Any) -> str:
    value = parse_decimal(value)
    # ``f`` avoids exponent notation and retains the exact decimal value.
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".") or "0"
    return text


def iso_utc(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Settings:
    app_timezone: str = "Asia/Shanghai"
    shortage_include_expected_receipts: bool = True
    forecast_moving_avg_window: int = 7
    anomaly_plan_variance_ratio: Decimal = Decimal("0.20")
    anomaly_zscore: Decimal = Decimal("2.0")
    anomaly_min_abs_qty: Decimal = Decimal("1")
    ppe_expiry_warning_days: int = 14
    approval_token_ttl_minutes: int = 30
    ai_read_timeout_seconds: int = 5
    ai_calc_timeout_seconds: int = 15
    ai_max_tool_calls: int = 8
    evidence_max_retries: int = 8
    evidence_backoff_seconds: tuple[int, ...] = (60, 300, 900, 3600)
    audit_retention_days: int = 180
    max_adjustment_ratio: Decimal = Decimal("0.20")

    @classmethod
    def from_env(cls) -> "Settings":
        def integer(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, default))
            except (TypeError, ValueError):
                return default

        def boolean(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        def decimal(name: str, default: Decimal) -> Decimal:
            try:
                return parse_decimal(os.getenv(name, str(default)), field=name)
            except ValueError:
                return default

        backoff_raw = os.getenv("EVIDENCE_BACKOFF_SECONDS", "60,300,900,3600")
        try:
            backoff = tuple(int(item.strip()) for item in backoff_raw.split(",") if item.strip())
        except ValueError:
            backoff = cls.evidence_backoff_seconds
        return cls(
            app_timezone=os.getenv("APP_TIMEZONE", "Asia/Shanghai"),
            shortage_include_expected_receipts=boolean("SHORTAGE_INCLUDE_EXPECTED_RECEIPTS", True),
            forecast_moving_avg_window=integer("FORECAST_MOVING_AVG_WINDOW", 7),
            anomaly_plan_variance_ratio=decimal("ANOMALY_PLAN_VARIANCE_RATIO", Decimal("0.20")),
            anomaly_zscore=decimal("ANOMALY_ZSCORE", Decimal("2.0")),
            anomaly_min_abs_qty=decimal("ANOMALY_MIN_ABS_QTY", Decimal("1")),
            ppe_expiry_warning_days=integer("PPE_EXPIRY_WARNING_DAYS", 14),
            approval_token_ttl_minutes=integer("APPROVAL_TOKEN_TTL_MINUTES", 30),
            ai_read_timeout_seconds=integer("AI_READ_TIMEOUT_SECONDS", 5),
            ai_calc_timeout_seconds=integer("AI_CALC_TIMEOUT_SECONDS", 15),
            ai_max_tool_calls=integer("AI_MAX_TOOL_CALLS", 8),
            evidence_max_retries=integer("EVIDENCE_MAX_RETRIES", 8),
            evidence_backoff_seconds=backoff,
            audit_retention_days=integer("AUDIT_RETENTION_DAYS", 180),
            max_adjustment_ratio=decimal("MAX_ADJUSTMENT_RATIO", Decimal("0.20")),
        )

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.app_timezone)
        except Exception:
            return ZoneInfo("Asia/Shanghai")
