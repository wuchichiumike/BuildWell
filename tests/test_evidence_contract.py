"""Contracts for canonical evidence payloads and the development adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backend.evidence import (
    ChainUnavailable,
    MockEvidenceAdapter,
    build_evidence_payload,
    canonical_json,
    hash_document,
    hash_payload,
)


def test_canonical_json_is_stable_and_normalises_decimal_dates_and_document_hashes() -> None:
    payload = {
        "z": Decimal("1.2300"),
        "a": {"when": datetime(2026, 8, 27, 1, 2, 3, tzinfo=timezone.utc)},
        "document_hashes": ["b", "a"],
    }

    assert canonical_json(payload) == '{"a":{"when":"2026-08-27T01:02:03Z"},"document_hashes":["a","b"],"z":"1.23"}'
    assert hash_payload(payload) == hash_payload(
        {
            "document_hashes": ["a", "b"],
            "z": "1.23",
            "a": {"when": "2026-08-27T01:02:03Z"},
        }
    )


def test_canonical_evidence_rejects_floats() -> None:
    with pytest.raises(TypeError, match="floats"):
        canonical_json({"qty": 1.5})


def test_mock_adapter_is_idempotent_and_has_deterministic_transaction_ids() -> None:
    adapter = MockEvidenceAdapter()
    first = adapter.submit("event-1", {"project_id": "P001"})
    second = adapter.submit("event-1", {"project_id": "P001", "other": "ignored by adapter key"})

    assert first == second
    assert first.tx_id == "mocktx_event-1"
    assert first.block_no == 1
    assert adapter.get("event-1") == first


def test_mock_adapter_failure_injection_can_be_recovered() -> None:
    adapter = MockEvidenceAdapter()
    adapter.fail_next = 1
    with pytest.raises(ChainUnavailable):
        adapter.submit("event-1", {"project_id": "P001"})
    assert adapter.get("event-1") is None
    assert adapter.submit("event-1", {"project_id": "P001"}).tx_id == "mocktx_event-1"


def test_build_evidence_payload_only_contains_chain_safe_fields() -> None:
    payload = build_evidence_payload(
        "INSPECTION",
        {
            "id": "inspection-1",
            "project_id": "project-1",
            "version": 2,
            "occurred_at": datetime(2026, 8, 27, 1, 2, 3),
            "inspection_no": "INSP-1",
            "status": "PASSED",
            "download_url": "https://private.example/file.pdf",
            "raw_file": "private contents",
        },
        [
            {"sha256": "b", "object_key": "private/b"},
            {"sha256": "a", "object_key": "private/a"},
        ],
    )

    assert payload["business_document_id"] == "inspection-1"
    assert payload["document_hashes"] == ["a", "b"]
    assert payload["inspection_no"] == "INSP-1"
    assert "download_url" not in payload
    assert "raw_file" not in payload


def test_hash_document_is_sha256_hex() -> None:
    assert hash_document(b"hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
