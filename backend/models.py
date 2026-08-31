"""Shared domain types and enums.

Values are strings on purpose: they are persisted directly and are stable API
contracts for clients and evidence payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, TypedDict


class UserRole(str, Enum):
    PROJECT_OWNER = "PROJECT_OWNER"
    PROCUREMENT = "PROCUREMENT"
    SUPPLIER = "SUPPLIER"
    SITE_ADMIN = "SITE_ADMIN"
    INSPECTOR = "INSPECTOR"
    AUDITOR = "AUDITOR"


class MaterialCategory(str, Enum):
    CONSTRUCTION = "CONSTRUCTION"
    PPE = "PPE"
    LOGISTICS = "LOGISTICS"
    FIRST_AID = "FIRST_AID"
    OTHER = "OTHER"


class MaterialStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class BatchStatus(str, Enum):
    EXPECTED = "EXPECTED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    INSPECTED = "INSPECTED"
    REJECTED = "REJECTED"
    EXHAUSTED = "EXHAUSTED"
    QUARANTINED = "QUARANTINED"


class StockMoveType(str, Enum):
    RECEIPT = "RECEIPT"
    ISSUE = "ISSUE"
    RETURN = "RETURN"
    TRANSFER_OUT = "TRANSFER_OUT"
    TRANSFER_IN = "TRANSFER_IN"
    SCRAP = "SCRAP"
    ADJUSTMENT = "ADJUSTMENT"


class PurchaseRequestStatus(str, Enum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    CONVERTED = "CONVERTED"


class PurchaseOrderStatus(str, Enum):
    DRAFT = "DRAFT"
    SENT = "SENT"
    PARTIAL_SHIPPED = "PARTIAL_SHIPPED"
    SHIPPED = "SHIPPED"
    PARTIAL_RECEIVED = "PARTIAL_RECEIVED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


class ShipmentStatus(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    IN_TRANSIT = "IN_TRANSIT"
    ARRIVED = "ARRIVED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class InspectionStatus(str, Enum):
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class EvidenceEventType(str, Enum):
    SHIPMENT = "SHIPMENT"
    DELIVERY = "DELIVERY"
    INSPECTION = "INSPECTION"
    HANDOVER = "HANDOVER"
    PURCHASE_APPROVAL = "PURCHASE_APPROVAL"
    PPE_DELIVERY = "PPE_DELIVERY"


class EvidenceStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    SUPERSEDED = "SUPERSEDED"


class DraftStatus(str, Enum):
    DRAFT = "DRAFT"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class AIClaimType(str, Enum):
    FACT = "FACT"
    CALCULATION = "CALCULATION"
    HYPOTHESIS = "HYPOTHESIS"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RequestContext(TypedDict, total=False):
    request_id: str
    user_id: str
    organization_id: str
    project_id: str
    roles: list[str]
    locale: str
    now_utc: datetime


@dataclass(frozen=True)
class Result:
    ok: bool
    data: Any = None
    request_id: str | None = None
    trace_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "warnings": self.warnings,
            "source_refs": self.source_refs,
        }
