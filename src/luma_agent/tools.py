"""Framework-agnostic tool layer.

These handlers are the ONLY way the agent touches the reservation API. They are
pure ``async`` functions with no dependency on Pipecat or OpenAI, so they can be:

  * driven by the live voice pipeline (bot.py),
  * driven by a text LLM in the eval harness (eval/run_eval.py),
  * unit-tested directly against the mock API (tests/).

Responsibilities that live here (not in the LLM, not in the raw client):
  * validate + normalise arguments (dates, 24h times, phone numbers, party size);
  * compute a DETERMINISTIC idempotency key per booking and cache it per session,
    so a repeated / duplicated create call can never double-book (test T7);
  * translate API errors into compact, LLM-actionable result dicts with hints
    ("offer these alternatives", "transfer to a human");
  * never raise into the LLM — every failure comes back as a structured result.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .api_client import ReservationAPIError, ReservationClient
from .config import MAX_STANDARD_PARTY_SIZE, VALID_SLOT_TIMES
from .logging_utils import log_event, mask_phone
from .metrics import LatencyRecorder

# --------------------------------------------------------------------------
# Normalisation / validation helpers (deterministic, unit-testable)
# --------------------------------------------------------------------------

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def normalize_phone(raw: str | None) -> Optional[str]:
    """Best-effort E.164 normalisation matching the mock API's storage.

    US 10-digit -> +1XXXXXXXXXX, 11-digit leading 1 -> +1..., keeps existing +.
    Returns None if there are too few digits to be a phone number.
    """
    if not raw:
        return None
    has_plus = raw.strip().startswith("+")
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 7:
        return None
    if has_plus:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def normalize_time(raw: str | None) -> Optional[str]:
    """Parse a spoken/typed time into 24h 'HH:MM'.

    Handles '18:00', '6 PM', '6:00 pm', '6pm', '1800', '18'. For a dinner
    service, a bare hour of 1-9 with no am/pm is read as PM.
    """
    if not raw:
        return None
    s = raw.strip().lower().replace(".", "")
    m = re.match(r"^(\d{1,2})(?::?(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ampm = m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    elif ampm is None and 1 <= hour <= 9:
        # Dinner service: a bare "6" or "6:30" means the evening slot.
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def normalize_date(raw: str | None, *, today: Optional[datetime] = None) -> Optional[str]:
    """Parse a date into ISO 'YYYY-MM-DD'. ISO is the fast path; a few spoken
    forms ('August 14', 'Aug 14 2026', '8/14/2026') are handled as a safety net.
    """
    if not raw:
        return None
    s = raw.strip()
    # Fast path: already ISO.
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return s
        except ValueError:
            return None

    cleaned = s.lower()
    for wd in _WEEKDAYS:
        cleaned = cleaned.replace(wd, "")
    cleaned = cleaned.replace(",", " ")
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    year = (today or datetime.utcnow()).year
    candidates = [cleaned, f"{cleaned} {year}"]
    fmts = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d %Y", "%b %d %Y", "%m/%d"]
    for cand in candidates:
        for fmt in fmts:
            try:
                dt = datetime.strptime(cand, fmt)
                if dt.year == 1900:  # format without a year
                    dt = dt.replace(year=year)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def idempotency_key(*, name: str, phone: str, date: str, time: str, party_size: int) -> str:
    """Deterministic key for a logical booking.

    Same booking details -> same key -> the API returns the same reservation
    instead of creating a duplicate, regardless of how many times we (or the
    LLM) call create.
    """
    norm_phone = normalize_phone(phone) or phone
    basis = f"{name.strip().lower()}|{norm_phone}|{date}|{time}|{int(party_size)}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
    return f"luma-{digest}"


def _compact_reservation(r: dict) -> dict:
    return {
        "reservation_id": r.get("reservation_id"),
        "confirmation_code": r.get("confirmation_code"),
        "name": r.get("name"),
        "phone": r.get("phone"),
        "date": r.get("date"),
        "time": r.get("time"),
        "party_size": r.get("party_size"),
        "notes": r.get("notes"),
        "status": r.get("status"),
    }


# --------------------------------------------------------------------------
# Session state + tool context
# --------------------------------------------------------------------------


@dataclass
class SessionState:
    call_id: str = "local"
    collected: dict = field(default_factory=dict)
    created_by_key: dict[str, dict] = field(default_factory=dict)
    last_search: list[dict] = field(default_factory=list)
    handoff: Optional[dict] = None
    tool_call_count: int = 0

    def remember(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            if v is not None:
                self.collected[k] = v


@dataclass
class ToolContext:
    client: ReservationClient
    state: SessionState
    logger: Any = None
    metrics: LatencyRecorder = field(default_factory=LatencyRecorder)


# --------------------------------------------------------------------------
# Result helpers
# --------------------------------------------------------------------------


def _err(error: str, message: str, **extra: Any) -> dict:
    return {"ok": False, "error": error, "message": message, **extra}


def _handoff_hint(message: str) -> dict:
    return _err(
        "party_too_large" if "8" in message else "needs_human",
        message,
        action="transfer_to_human",
    )


# --------------------------------------------------------------------------
# Tool handlers
# --------------------------------------------------------------------------


async def check_availability(
    ctx: ToolContext, *, date: str, time: str, party_size: int, **_: Any
) -> dict:
    nd, nt = normalize_date(date), normalize_time(time)
    try:
        ps = int(party_size)
    except (TypeError, ValueError):
        return _err("invalid_arguments", "Party size must be a whole number.")
    if nd is None:
        return _err("invalid_arguments", f"Couldn't understand the date '{date}'. Use YYYY-MM-DD.")
    if nt is None:
        return _err(
            "invalid_time",
            f"Couldn't understand the time '{time}'.",
            valid_times=VALID_SLOT_TIMES,
        )
    if ps < 1:
        return _err("invalid_arguments", "Party size must be at least 1.")
    if ps > MAX_STANDARD_PARTY_SIZE:
        return _handoff_hint(
            f"Parties larger than {MAX_STANDARD_PARTY_SIZE} need a team member to arrange."
        )
    if nt not in VALID_SLOT_TIMES:
        return _err(
            "invalid_time",
            f"{nt} isn't a bookable slot.",
            valid_times=VALID_SLOT_TIMES,
        )

    try:
        result = await ctx.client.check_availability(nd, nt, ps)
    except ReservationAPIError as e:
        return _availability_error(e)

    return {
        "ok": True,
        "available": result.get("available", False),
        "date": nd,
        "time": nt,
        "party_size": ps,
        "remaining_capacity": result.get("remaining_capacity"),
        "alternatives": result.get("alternatives", []),
    }


def _availability_error(e: ReservationAPIError) -> dict:
    if e.code in {"INVALID_SLOT", "VALIDATION_ERROR"}:
        return _err("invalid_time", e.message or "Invalid slot.", valid_times=VALID_SLOT_TIMES)
    if e.code in {"TEMPORARY_UPSTREAM_FAILURE", "NETWORK_ERROR", "HTTP_503"}:
        return _err(
            "temporarily_unavailable",
            "The booking system is temporarily unavailable.",
            action="retry_once_then_transfer_to_human",
        )
    return _err("api_error", e.message, code=e.code)


async def create_reservation(
    ctx: ToolContext,
    *,
    name: str,
    phone: str,
    date: str,
    time: str,
    party_size: int,
    notes: Optional[str] = None,
    **_: Any,
) -> dict:
    nd, nt, np_ = normalize_date(date), normalize_time(time), None
    try:
        np_ = int(party_size)
    except (TypeError, ValueError):
        return _err("invalid_arguments", "Party size must be a whole number.")
    problems = []
    if not name or len(name.strip()) < 2:
        problems.append("a name (at least 2 characters)")
    nphone = normalize_phone(phone)
    if not nphone:
        problems.append("a valid phone number")
    if nd is None:
        problems.append("a valid date (YYYY-MM-DD)")
    if nt is None or nt not in VALID_SLOT_TIMES:
        return _err(
            "invalid_time",
            f"'{time}' isn't a bookable slot.",
            valid_times=VALID_SLOT_TIMES,
        )
    if np_ < 1:
        problems.append("a party size of at least 1")
    if problems:
        return _err("invalid_arguments", "Still need: " + ", ".join(problems) + ".")
    if np_ > MAX_STANDARD_PARTY_SIZE:
        return _handoff_hint(
            f"Parties larger than {MAX_STANDARD_PARTY_SIZE} need a team member to arrange."
        )

    key = idempotency_key(name=name, phone=nphone, date=nd, time=nt, party_size=np_)

    # Session-level dedupe: identical booking already created on this call.
    if key in ctx.state.created_by_key:
        cached = ctx.state.created_by_key[key]
        if ctx.logger:
            log_event(
                ctx.logger,
                "duplicate_create_prevented",
                level="WARNING",
                idempotency_key=key,
                confirmation_code=cached.get("confirmation_code"),
            )
        return {
            "ok": True,
            "duplicate_prevented": True,
            "reservation": _compact_reservation(cached),
            "message": "This reservation was already created; returning the existing one.",
        }

    try:
        r = await ctx.client.create_reservation(
            name=name.strip(),
            phone=nphone,
            date=nd,
            time=nt,
            party_size=np_,
            notes=(notes or None),
            idempotency_key=key,
        )
    except ReservationAPIError as e:
        if e.code == "SLOT_UNAVAILABLE":
            return _err(
                "slot_unavailable",
                "That time just filled up.",
                alternatives=e.alternatives,
            )
        if e.code in {"INVALID_SLOT", "VALIDATION_ERROR"}:
            return _err("invalid_arguments", e.message, valid_times=VALID_SLOT_TIMES)
        if e.code in {"TEMPORARY_UPSTREAM_FAILURE", "NETWORK_ERROR", "HTTP_503"}:
            return _err(
                "temporarily_unavailable",
                "The booking system is temporarily unavailable.",
                action="retry_once_then_transfer_to_human",
            )
        return _err("api_error", e.message, code=e.code)

    ctx.state.created_by_key[key] = r
    ctx.state.remember(name=name.strip(), phone=nphone, date=nd, time=nt, party_size=np_, notes=notes)
    return {
        "ok": True,
        "reservation": _compact_reservation(r),
        "confirmation_code": r.get("confirmation_code"),
        "message": "Reservation created.",
    }


async def find_reservation(
    ctx: ToolContext,
    *,
    phone: Optional[str] = None,
    confirmation_code: Optional[str] = None,
    **_: Any,
) -> dict:
    if not phone and not confirmation_code:
        return _err(
            "need_criteria",
            "Ask the caller for their confirmation code or the phone number on the booking.",
        )
    nphone = normalize_phone(phone) if phone else None
    code = confirmation_code.strip().upper() if confirmation_code else None
    if code and not code.startswith("LUMA-") and re.fullmatch(r"[0-9]{4}", code):
        code = f"LUMA-{code}"
    try:
        results = await ctx.client.search_reservations(phone=nphone, confirmation_code=code)
    except ReservationAPIError as e:
        return _err("api_error", e.message, code=e.code)

    ctx.state.last_search = results
    compact = [_compact_reservation(r) for r in results]
    if not compact:
        return {"ok": True, "count": 0, "reservations": [], "message": "No reservation found with those details."}
    return {"ok": True, "count": len(compact), "reservations": compact}


async def modify_reservation(
    ctx: ToolContext,
    *,
    reservation_id: str,
    date: Optional[str] = None,
    time: Optional[str] = None,
    party_size: Optional[int] = None,
    notes: Optional[str] = None,
    **_: Any,
) -> dict:
    if not reservation_id:
        return _err(
            "need_reservation",
            "Find the reservation first (by confirmation code or phone), then modify it.",
        )
    changes: dict[str, Any] = {}
    if date is not None:
        nd = normalize_date(date)
        if nd is None:
            return _err("invalid_arguments", f"Couldn't understand the date '{date}'.")
        changes["date"] = nd
    if time is not None:
        nt = normalize_time(time)
        if nt is None or nt not in VALID_SLOT_TIMES:
            return _err("invalid_time", f"'{time}' isn't a bookable slot.", valid_times=VALID_SLOT_TIMES)
        changes["time"] = nt
    if party_size is not None:
        try:
            ps = int(party_size)
        except (TypeError, ValueError):
            return _err("invalid_arguments", "Party size must be a whole number.")
        if ps < 1:
            return _err("invalid_arguments", "Party size must be at least 1.")
        if ps > MAX_STANDARD_PARTY_SIZE:
            return _handoff_hint(
                f"Parties larger than {MAX_STANDARD_PARTY_SIZE} need a team member to arrange."
            )
        changes["party_size"] = ps
    if notes is not None:
        changes["notes"] = notes
    if not changes:
        return _err("no_changes", "Nothing to change — ask what they'd like to update.")

    try:
        r = await ctx.client.modify_reservation(reservation_id, **changes)
    except ReservationAPIError as e:
        if e.code == "NOT_FOUND":
            return _err("not_found", "Couldn't find that reservation. Re-confirm the code.")
        if e.code == "ALREADY_CANCELLED":
            return _err("already_cancelled", "That reservation was already cancelled.")
        if e.code == "SLOT_UNAVAILABLE":
            return _err("slot_unavailable", "That new time isn't available.", alternatives=e.alternatives)
        if e.code in {"TEMPORARY_UPSTREAM_FAILURE", "NETWORK_ERROR", "HTTP_503"}:
            return _err(
                "temporarily_unavailable",
                "The booking system is temporarily unavailable.",
                action="retry_once_then_transfer_to_human",
            )
        return _err("api_error", e.message, code=e.code)

    return {"ok": True, "reservation": _compact_reservation(r), "message": "Reservation updated."}


async def cancel_reservation(
    ctx: ToolContext, *, reservation_id: str, **_: Any
) -> dict:
    if not reservation_id:
        return _err(
            "need_reservation",
            "Find the reservation first (by confirmation code or phone), then cancel it.",
        )
    try:
        r = await ctx.client.cancel_reservation(reservation_id)
    except ReservationAPIError as e:
        if e.code == "NOT_FOUND":
            return _err("not_found", "Couldn't find that reservation. Re-confirm the code.")
        if e.code in {"TEMPORARY_UPSTREAM_FAILURE", "NETWORK_ERROR", "HTTP_503"}:
            return _err(
                "temporarily_unavailable",
                "The booking system is temporarily unavailable.",
                action="retry_once_then_transfer_to_human",
            )
        return _err("api_error", e.message, code=e.code)
    return {
        "ok": True,
        "status": r.get("status"),
        "reservation": _compact_reservation(r),
        "message": "Reservation cancelled.",
    }


async def transfer_to_human(
    ctx: ToolContext,
    *,
    reason: str,
    conversation_summary: str,
    customer_phone: Optional[str] = None,
    **_: Any,
) -> dict:
    phone = normalize_phone(customer_phone) or ctx.state.collected.get("phone")
    # Enrich the summary with anything collected so the human has full context.
    collected = {k: v for k, v in ctx.state.collected.items() if k != "phone"}
    enriched = conversation_summary
    if collected:
        details = ", ".join(f"{k}={v}" for k, v in collected.items())
        enriched = f"{conversation_summary}\n\nCollected so far: {details}"
    try:
        h = await ctx.client.handoff(
            reason=reason, conversation_summary=enriched, customer_phone=phone
        )
    except ReservationAPIError as e:
        # Even if logging the handoff fails, tell the caller a human will follow up.
        ctx.state.handoff = {"reason": reason, "summary": enriched, "phone": phone}
        return {
            "ok": True,
            "queued": False,
            "message": "I'm connecting you with a team member who will follow up shortly.",
            "note": f"handoff_log_failed: {e.code}",
        }
    ctx.state.handoff = h
    return {
        "ok": True,
        "queued": True,
        "handoff_id": h.get("handoff_id"),
        "status": h.get("status"),
        "message": "I've passed your details to a team member who will follow up shortly.",
    }


# --------------------------------------------------------------------------
# Dispatch + schemas
# --------------------------------------------------------------------------

_HANDLERS = {
    "check_availability": check_availability,
    "create_reservation": create_reservation,
    "find_reservation": find_reservation,
    "modify_reservation": modify_reservation,
    "cancel_reservation": cancel_reservation,
    "transfer_to_human": transfer_to_human,
}


def _mask_args(args: dict) -> dict:
    out = dict(args)
    for k in ("phone", "customer_phone", "phone_number"):
        if k in out and isinstance(out[k], str):
            out[k] = mask_phone(out[k])
    return out


async def dispatch(ctx: ToolContext, name: str, arguments: dict) -> dict:
    """Route a tool call by name. Never raises — always returns a result dict."""
    ctx.state.tool_call_count += 1
    handler = _HANDLERS.get(name)
    start = time.perf_counter()
    if handler is None:
        result = _err("unknown_tool", f"No such tool: {name}")
    else:
        try:
            result = await handler(ctx, **(arguments or {}))
        except TypeError as e:
            result = _err("invalid_arguments", f"Bad arguments for {name}: {e}")
        except Exception as e:  # defensive: a tool bug must not kill the call
            result = _err("internal_error", f"Unexpected error in {name}.", detail=str(e))
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    ctx.metrics.record(f"tool.{name}", elapsed_ms)
    if ctx.logger:
        log_event(
            ctx.logger,
            "tool_call",
            tool=name,
            args=_mask_args(arguments or {}),
            ok=result.get("ok"),
            error=result.get("error"),
            latency_ms=round(elapsed_ms, 1),
        )
    return result


# OpenAI-style tool/function schemas. bot.py converts these to Pipecat
# FunctionSchema objects; the eval harness passes them straight to OpenAI.
TOOL_SCHEMAS: list[dict] = [
    {
        "name": "check_availability",
        "description": (
            "Check whether a table is available for a given date, time and party "
            "size. ALWAYS call this before creating a reservation. Never guess "
            "availability. If unavailable, the result includes real alternatives "
            "to offer the caller."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Reservation date as YYYY-MM-DD."},
                "time": {"type": "string", "description": "Reservation time as 24h HH:MM (e.g. 18:30)."},
                "party_size": {"type": "integer", "description": "Number of guests (1-8)."},
            },
            "required": ["date", "time", "party_size"],
        },
    },
    {
        "name": "create_reservation",
        "description": (
            "Create a reservation. Only call AFTER checking availability AND after "
            "the caller has explicitly confirmed name, phone, date, time and party "
            "size. Safe to retry — duplicate calls with the same details will not "
            "double-book."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Caller's full name."},
                "phone": {"type": "string", "description": "Caller's phone number."},
                "date": {"type": "string", "description": "Date as YYYY-MM-DD."},
                "time": {"type": "string", "description": "Time as 24h HH:MM."},
                "party_size": {"type": "integer", "description": "Number of guests (1-8)."},
                "notes": {"type": "string", "description": "Optional special requests."},
            },
            "required": ["name", "phone", "date", "time", "party_size"],
        },
    },
    {
        "name": "find_reservation",
        "description": (
            "Look up an existing reservation by confirmation code (e.g. LUMA-4821) "
            "or phone number. Call this first before modifying or cancelling."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "confirmation_code": {"type": "string", "description": "Confirmation code, e.g. LUMA-4821."},
                "phone": {"type": "string", "description": "Phone number on the reservation."},
            },
            "required": [],
        },
    },
    {
        "name": "modify_reservation",
        "description": (
            "Change an existing reservation's date, time, party size or notes. "
            "Requires the reservation_id from find_reservation. Confirm the change "
            "with the caller before calling."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reservation_id": {"type": "string", "description": "reservation_id from find_reservation."},
                "date": {"type": "string", "description": "New date YYYY-MM-DD (optional)."},
                "time": {"type": "string", "description": "New time 24h HH:MM (optional)."},
                "party_size": {"type": "integer", "description": "New party size 1-8 (optional)."},
                "notes": {"type": "string", "description": "New notes (optional)."},
            },
            "required": ["reservation_id"],
        },
    },
    {
        "name": "cancel_reservation",
        "description": (
            "Cancel an existing reservation. Requires the reservation_id from "
            "find_reservation. Confirm with the caller before calling."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reservation_id": {"type": "string", "description": "reservation_id from find_reservation."},
            },
            "required": ["reservation_id"],
        },
    },
    {
        "name": "transfer_to_human",
        "description": (
            "Hand off to a human team member when the request cannot be completed "
            "by the agent: party larger than 8, repeated system failures, or an "
            "explicit request for a person. Always pass a concise conversation "
            "summary so context is preserved."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Short reason for the handoff."},
                "conversation_summary": {
                    "type": "string",
                    "description": "Summary of the request and everything collected so far.",
                },
                "customer_phone": {"type": "string", "description": "Caller's phone number, if known."},
            },
            "required": ["reason", "conversation_summary"],
        },
    },
]


def openai_tools() -> list[dict]:
    """TOOL_SCHEMAS in OpenAI Chat Completions 'tools' format."""
    return [{"type": "function", "function": s} for s in TOOL_SCHEMAS]
