"""Evidence payload hashing and chain adapters.

Only hashes and stable event metadata are sent to a chain.  The mock adapter
implements the same boundary as a Fabric Gateway adapter and is deterministic
for repeatable demos and tests.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Protocol

from .config import decimal_string


def _normalise(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, Enum):
        return _normalise(value.value, key=key)
    if dataclasses.is_dataclass(value):
        return _normalise(dataclasses.asdict(value), key=key)
    if isinstance(value, Decimal):
        return decimal_string(value)
    if isinstance(value, float):
        raise TypeError("floats are not permitted in canonical evidence payloads")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _normalise(value[k], key=str(k)) for k in sorted(value, key=lambda item: str(item))}
    if isinstance(value, (list, tuple)):
        values = [_normalise(item) for item in value]
        # Document hashes are unordered business metadata.  Sorting here keeps
        # equivalent payloads hash-identical while preserving all other array
        # order (which may be meaningful to the event).
        if key == "document_hashes":
            values = sorted(values)
        return values
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise TypeError(f"unsupported canonical payload value: {type(value).__name__}")


def canonical_json(payload: Mapping[str, Any] | Any) -> str:
    """Return canonical UTF-8 JSON (no whitespace, sorted object keys)."""

    return json.dumps(_normalise(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def hash_payload(payload: Mapping[str, Any] | Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def hash_document(data: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(bytes(data)).hexdigest()


@dataclass(frozen=True)
class EvidenceSubmission:
    tx_id: str
    block_no: int | None = None
    payload_hash: str | None = None


class EvidenceAdapter(Protocol):
    def submit(self, event_id: str, payload: Mapping[str, Any]) -> EvidenceSubmission:
        ...

    def get(self, event_id: str) -> EvidenceSubmission | None:
        ...


class ChainUnavailable(RuntimeError):
    """Raised when an adapter cannot currently accept an event."""


class MockEvidenceAdapter:
    """Deterministic in-memory evidence adapter for development.

    ``fail_next`` and ``available`` are explicit fault-injection switches;
    callers can verify retry behavior without pretending this is Fabric.
    """

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.fail_next = 0
        self._events: dict[str, EvidenceSubmission] = {}

    def submit(self, event_id: str, payload: Mapping[str, Any]) -> EvidenceSubmission:
        if not self.available or self.fail_next > 0:
            if self.fail_next > 0:
                self.fail_next -= 1
            raise ChainUnavailable("mock evidence adapter unavailable")
        existing = self._events.get(str(event_id))
        if existing:
            return existing
        submission = EvidenceSubmission(
            tx_id=f"mocktx_{event_id}",
            block_no=len(self._events) + 1,
            payload_hash=hash_payload(payload),
        )
        self._events[str(event_id)] = submission
        return submission

    def get(self, event_id: str) -> EvidenceSubmission | None:
        return self._events.get(str(event_id))

    def clear(self) -> None:
        self._events.clear()

    def restore(self, event_id: str, submission: EvidenceSubmission) -> None:
        """Restore persisted submissions when the demo API process restarts."""

        self._events[str(event_id)] = submission


def build_evidence_payload(event_type: str, business_record: Mapping[str, Any], documents: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Build a chain-safe payload from a validated business record.

    Document records are reduced to SHA-256 values; URLs and original file
    contents are intentionally excluded.
    """

    hashes = sorted({str(doc.get("sha256")) for doc in (documents or []) if doc.get("sha256")})
    payload: dict[str, Any] = {
        "event_type": str(event_type),
        "business_document_id": business_record.get("business_document_id", business_record.get("id")),
        "project_id": business_record.get("project_id"),
        "version": business_record.get("version", 1),
        "occurred_at": business_record.get("occurred_at"),
        "document_hashes": hashes,
        "schema_version": "1",
    }
    # Include only explicitly whitelisted scalar/structured evidence fields.
    allowed = {"shipment_no", "delivery_no", "inspection_no", "handover_no", "batch_id", "status", "qty", "unit", "material_id"}
    for key in allowed:
        if key in business_record:
            payload[key] = business_record[key]
    return payload


__all__ = [
    "ChainUnavailable",
    "EvidenceAdapter",
    "EvidenceSubmission",
    "MockEvidenceAdapter",
    "build_evidence_payload",
    "canonical_json",
    "hash_document",
    "hash_payload",
]
