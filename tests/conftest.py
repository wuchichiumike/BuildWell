"""Shared fixtures for backend contract and API tests."""

from __future__ import annotations

import pytest

from backend.db import Database
from backend.service import BusinessService


@pytest.fixture()
def seeded_service() -> BusinessService:
    """Create a fresh deterministic P001 repository for one test."""

    database = Database()
    database.seed_demo()
    service = BusinessService(database)
    try:
        yield service
    finally:
        database.close()


@pytest.fixture()
def demo_ids(seeded_service: BusinessService) -> dict[str, str]:
    rows = seeded_service.db.query_all(
        "SELECT id, username, role FROM users ORDER BY username"
    )
    materials = seeded_service.db.query_all(
        "SELECT id, sku FROM materials WHERE project_id=(SELECT id FROM projects WHERE code='P001')"
    )
    warehouse = seeded_service.db.query_one(
        "SELECT id FROM warehouses WHERE project_id=(SELECT id FROM projects WHERE code='P001') LIMIT 1"
    )
    return {
        "project": str(seeded_service.db.query_one("SELECT id FROM projects WHERE code='P001'")["id"]),
        "owner": str(next(row["id"] for row in rows if row["role"] == "PROJECT_OWNER")),
        "supplier_user": str(next(row["id"] for row in rows if row["role"] == "SUPPLIER")),
        "material": str(next(row["id"] for row in materials if row["sku"] == "MAT-BLOCK")),
        "gloves": str(next(row["id"] for row in materials if row["sku"] == "MAT-GLOVE")),
        "warehouse": str(warehouse["id"]),
        "batch": str(
            seeded_service.db.query_one(
                "SELECT b.id FROM batches b JOIN materials m ON m.id=b.material_id WHERE m.sku='MAT-BLOCK'"
            )["id"]
        ),
    }


@pytest.fixture()
def owner_context(demo_ids: dict[str, str]) -> dict[str, object]:
    return {
        "user_id": demo_ids["owner"],
        "project_id": demo_ids["project"],
        "roles": ["PROJECT_OWNER"],
        "request_id": "00000000-0000-4000-8000-000000000099",
    }
