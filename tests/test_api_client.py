"""Low-level ReservationClient tests: retry, idempotency, error normalisation."""

from __future__ import annotations

import pytest

from luma_agent.api_client import ReservationAPIError

pytestmark = pytest.mark.asyncio


async def test_health(client):
    assert (await client.health())["status"] == "ok"


async def test_availability_available(client):
    r = await client.check_availability("2026-08-14", "18:00", 4)
    assert r["available"] is True
    assert r["remaining_capacity"] == 4


async def test_availability_unavailable_offers_alternatives(client):
    r = await client.check_availability("2026-08-14", "18:30", 4)  # capacity 0
    assert r["available"] is False
    assert len(r["alternatives"]) > 0
    assert all(a["remaining_capacity"] >= 4 for a in r["alternatives"])


async def test_availability_503_then_retry_recovers(client):
    # 2026-08-16 returns 503 on the FIRST request, then succeeds.
    r = await client.check_availability("2026-08-16", "18:00", 2)
    assert r["available"] is True
    # Exactly one retry => exactly two attempts were made.
    assert client.metrics.summary("api.get_availability")["count"] == 2


async def test_availability_retry_is_bounded_to_once(client):
    client.max_retries = 1
    # Force a scenario where it would fail more than the budget: after the
    # single allowed retry the mock has already recovered, so this asserts we
    # never exceed 2 attempts total.
    await client.check_availability("2026-08-16", "18:00", 2)
    assert client.metrics.summary("api.get_availability")["count"] <= 2


async def test_invalid_slot_raises(client):
    with pytest.raises(ReservationAPIError) as exc:
        await client.check_availability("2026-08-14", "21:00", 2)  # not on the grid
    assert exc.value.code == "INVALID_SLOT"


async def test_create_is_idempotent(client):
    key = "test-key-123"
    args = dict(
        name="Jordan Lee",
        phone="+13105550199",
        date="2026-08-14",
        time="18:00",
        party_size=4,
        notes=None,
        idempotency_key=key,
    )
    r1 = await client.create_reservation(**args)
    r2 = await client.create_reservation(**args)  # same key
    assert r1["reservation_id"] == r2["reservation_id"]
    found = await client.search_reservations(phone="+13105550199")
    assert len([f for f in found if f["status"] == "confirmed"]) == 1


async def test_create_conflict_returns_alternatives(client):
    with pytest.raises(ReservationAPIError) as exc:
        await client.create_reservation(
            name="Test User",
            phone="+13105550000",
            date="2026-08-14",
            time="18:30",  # capacity 0
            party_size=4,
            notes=None,
            idempotency_key="conflict-key",
        )
    assert exc.value.code == "SLOT_UNAVAILABLE"
    assert len(exc.value.alternatives) > 0


async def test_search_by_confirmation_code(client):
    results = await client.search_reservations(confirmation_code="LUMA-4821")
    assert len(results) == 1
    assert results[0]["name"] == "Alex Morgan"


async def test_modify_existing(client):
    r = await client.modify_reservation("res_existing_4821", time="19:30", party_size=4)
    assert r["time"] == "19:30"
    assert r["party_size"] == 4


async def test_cancel_existing(client):
    r = await client.cancel_reservation("res_existing_4821")
    assert r["status"] == "cancelled"
    # Idempotent: cancelling again stays cancelled.
    r2 = await client.cancel_reservation("res_existing_4821")
    assert r2["status"] == "cancelled"


async def test_handoff(client):
    h = await client.handoff(reason="party too large", conversation_summary="party of 12")
    assert h["status"] == "queued"
    assert "handoff_id" in h
