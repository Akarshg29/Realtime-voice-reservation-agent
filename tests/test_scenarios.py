"""End-to-end scenario tests, exercised at the TOOL layer via dispatch().

These prove the deterministic behaviour that matters — correct tool
calling, real (never invented) alternatives, retry-once, duplicate prevention —
without needing an LLM or audio. The LLM-driven versions live in eval/run_eval.py.
"""

from __future__ import annotations

import pytest

from luma_agent.tools import dispatch

pytestmark = pytest.mark.asyncio


async def _count_confirmed(client, phone):
    found = await client.search_reservations(phone=phone)
    return len([f for f in found if f["status"] == "confirmed"])


async def test_t1_create_available(ctx):
    avail = await dispatch(ctx, "check_availability", {"date": "2026-08-14", "time": "18:00", "party_size": 4})
    assert avail["ok"] and avail["available"] is True

    created = await dispatch(
        ctx,
        "create_reservation",
        {"name": "Jordan Lee", "phone": "310-555-0199", "date": "2026-08-14", "time": "18:00", "party_size": 4},
    )
    assert created["ok"] is True
    assert created["confirmation_code"].startswith("LUMA-")
    assert await _count_confirmed(ctx.client, "+13105550199") == 1


async def test_t2_unavailable_offers_real_alternatives(ctx):
    avail = await dispatch(ctx, "check_availability", {"date": "2026-08-14", "time": "18:30", "party_size": 4})
    assert avail["ok"] and avail["available"] is False
    alt_times = {a["time"] for a in avail["alternatives"]}
    assert "19:30" in alt_times  # the alternative the caller picks in the script

    # Caller picks 7:30 PM; agent re-checks then books it.
    avail2 = await dispatch(ctx, "check_availability", {"date": "2026-08-14", "time": "19:30", "party_size": 4})
    assert avail2["available"] is True
    created = await dispatch(
        ctx,
        "create_reservation",
        {"name": "Taylor Kim", "phone": "424-555-0188", "date": "2026-08-14", "time": "19:30", "party_size": 4},
    )
    assert created["ok"] and created["reservation"]["time"] == "19:30"


async def test_t3_correction_uses_final_party_size(ctx):
    # Original intent: party of 2. Correction (barge-in): make it 4.
    await dispatch(ctx, "check_availability", {"date": "2026-08-15", "time": "18:30", "party_size": 2})
    avail = await dispatch(ctx, "check_availability", {"date": "2026-08-15", "time": "18:30", "party_size": 4})
    assert avail["available"] is True
    created = await dispatch(
        ctx,
        "create_reservation",
        {"name": "Casey Brown", "phone": "213-555-0114", "date": "2026-08-15", "time": "18:30", "party_size": 4},
    )
    assert created["ok"] and created["reservation"]["party_size"] == 4
    # A duplicate create of the same final booking must not double-book.
    dup = await dispatch(
        ctx,
        "create_reservation",
        {"name": "Casey Brown", "phone": "213-555-0114", "date": "2026-08-15", "time": "18:30", "party_size": 4},
    )
    assert dup.get("duplicate_prevented") is True
    assert await _count_confirmed(ctx.client, "+12135550114") == 1


async def test_t4_modify_existing(ctx):
    found = await dispatch(ctx, "find_reservation", {"confirmation_code": "LUMA-4821"})
    assert found["count"] == 1
    rid = found["reservations"][0]["reservation_id"]

    modified = await dispatch(
        ctx, "modify_reservation", {"reservation_id": rid, "time": "19:30", "party_size": 4}
    )
    assert modified["ok"] is True
    assert modified["reservation"]["time"] == "19:30"
    assert modified["reservation"]["party_size"] == 4


async def test_t5_cancel_existing(ctx):
    found = await dispatch(ctx, "find_reservation", {"confirmation_code": "LUMA-4821"})
    rid = found["reservations"][0]["reservation_id"]
    cancelled = await dispatch(ctx, "cancel_reservation", {"reservation_id": rid})
    assert cancelled["ok"] and cancelled["status"] == "cancelled"


async def test_t6_temporary_failure_retries_once(ctx):
    # 2026-08-16 503s once, then recovers. The tool must return a real result.
    result = await dispatch(ctx, "check_availability", {"date": "2026-08-16", "time": "18:00", "party_size": 2})
    assert result["ok"] is True
    assert result["available"] is True
    # Bounded to a single retry => 2 attempts total.
    assert ctx.client.metrics.summary("api.get_availability")["count"] == 2


async def test_t7_duplicate_protection(ctx):
    args = {"name": "Morgan Reed", "phone": "310-555-0166", "date": "2026-08-14", "time": "20:00", "party_size": 2}
    first = await dispatch(ctx, "create_reservation", dict(args))
    second = await dispatch(ctx, "create_reservation", dict(args))  # same idempotency basis
    assert first["ok"] is True
    assert second.get("duplicate_prevented") is True
    assert first["confirmation_code"] == second["reservation"]["confirmation_code"]
    assert await _count_confirmed(ctx.client, "+13105550166") == 1


async def test_party_too_large_signals_handoff(ctx):
    result = await dispatch(ctx, "check_availability", {"date": "2026-08-14", "time": "18:00", "party_size": 12})
    assert result["ok"] is False
    assert result["action"] == "transfer_to_human"
