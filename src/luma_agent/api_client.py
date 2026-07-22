"""Async client for the Luma Bistro reservation API.

This is the reliability boundary between the LLM and the outside world:

  * transient failures (HTTP 503, timeouts, connection errors) are retried a
    bounded number of times (default: once) with a short backoff;
  * writes carry an Idempotency-Key, so retrying a create is always safe and
    can never double-book;
  * every error is normalised into a single ``ReservationAPIError`` carrying a
    machine code + any alternatives the API offered, so the tool layer can react
    without parsing HTTP internals;
  * 4xx client errors are never retried (they will not fix themselves).

The client is deliberately dumb about business logic — validation, dedupe and
conversational reactions live in tools.py.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from .logging_utils import log_event, mask_phone
from .metrics import LatencyRecorder


class ReservationAPIError(Exception):
    """Normalised error from the reservation API."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        status_code: Optional[int] = None,
        alternatives: Optional[list[dict]] = None,
        retry_after_ms: Optional[int] = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status_code = status_code
        self.alternatives = alternatives or []
        self.retry_after_ms = retry_after_ms
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "status_code": self.status_code,
            "alternatives": self.alternatives,
        }


def _parse_error(resp: httpx.Response) -> ReservationAPIError:
    """Turn an error response into a ReservationAPIError.

    FastAPI emits two shapes:
      * app-raised: ``{"detail": {"code": ..., "alternatives": [...]}}``
      * validation: ``{"detail": [{"loc": ..., "msg": ...}, ...]}``
    """
    code = f"HTTP_{resp.status_code}"
    message = ""
    alternatives: list[dict] = []
    retry_after_ms: Optional[int] = None
    detail: Any = None
    try:
        body = resp.json()
        detail = body.get("detail") if isinstance(body, dict) else body
    except Exception:
        detail = resp.text

    if isinstance(detail, dict):
        code = detail.get("code", code)
        alternatives = detail.get("alternatives", []) or []
        retry_after_ms = detail.get("retry_after_ms")
        message = detail.get("message", "") or code
    elif isinstance(detail, list):  # pydantic validation errors
        code = "VALIDATION_ERROR"
        parts = []
        for err in detail:
            loc = ".".join(str(x) for x in err.get("loc", []) if x != "body")
            parts.append(f"{loc}: {err.get('msg', '')}".strip(": "))
        message = "; ".join(p for p in parts if p) or "validation error"
    elif isinstance(detail, str) and detail:
        message = detail

    return ReservationAPIError(
        code,
        message,
        status_code=resp.status_code,
        alternatives=alternatives,
        retry_after_ms=retry_after_ms,
        detail=detail,
    )


class ReservationClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 8.0,
        max_retries: int = 1,
        retry_backoff_ms: int = 250,
        logger=None,
        metrics: Optional[LatencyRecorder] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.retry_backoff_ms = retry_backoff_ms
        self.logger = logger
        self.metrics = metrics or LatencyRecorder()
        # An injected client lets tests run in-process against the ASGI app.
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout_s)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "ReservationClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # -- core request with bounded retry -----------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        retry: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        attempts = (self.max_retries + 1) if retry else 1
        metric_name = f"api.{method.lower()}{path.split('?')[0].rstrip('/').replace('/', '_') or '_root'}"
        last_exc: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                with self.metrics.timer(metric_name):
                    resp = await self._client.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if self.logger:
                    log_event(
                        self.logger,
                        "api_network_error",
                        level="WARNING",
                        method=method,
                        path=path,
                        attempt=attempt,
                        error=str(exc),
                    )
                if attempt < attempts:
                    await asyncio.sleep(self.retry_backoff_ms / 1000.0)
                    continue
                raise ReservationAPIError(
                    "NETWORK_ERROR", str(exc), status_code=None
                ) from exc

            # Retry only transient 503s, and only while budget remains.
            if resp.status_code == 503 and attempt < attempts:
                err = _parse_error(resp)
                backoff = (err.retry_after_ms or self.retry_backoff_ms) / 1000.0
                if self.logger:
                    log_event(
                        self.logger,
                        "api_retrying",
                        level="WARNING",
                        method=method,
                        path=path,
                        attempt=attempt,
                        status=503,
                        code=err.code,
                        backoff_ms=int(backoff * 1000),
                    )
                await asyncio.sleep(backoff)
                continue

            if self.logger:
                log_event(
                    self.logger,
                    "api_response",
                    method=method,
                    path=path,
                    attempt=attempt,
                    status=resp.status_code,
                )
            return resp

        # Unreachable, but keeps type-checkers happy.
        assert last_exc is not None
        raise ReservationAPIError("NETWORK_ERROR", str(last_exc))

    @staticmethod
    def _ok_or_raise(resp: httpx.Response) -> dict:
        if resp.is_success:
            return resp.json()
        raise _parse_error(resp)

    # -- endpoints ----------------------------------------------------------
    async def health(self) -> dict:
        return self._ok_or_raise(await self._request("GET", "/health"))

    async def get_restaurant(self) -> dict:
        return self._ok_or_raise(await self._request("GET", "/restaurant"))

    async def check_availability(self, date: str, time: str, party_size: int) -> dict:
        resp = await self._request(
            "GET",
            "/availability",
            params={"date": date, "time": time, "party_size": party_size},
        )
        return self._ok_or_raise(resp)

    async def create_reservation(
        self,
        *,
        name: str,
        phone: str,
        date: str,
        time: str,
        party_size: int,
        notes: Optional[str],
        idempotency_key: str,
    ) -> dict:
        # Retrying is safe: the Idempotency-Key guarantees the API returns the
        # same reservation instead of creating a second one.
        resp = await self._request(
            "POST",
            "/reservations",
            headers={"Idempotency-Key": idempotency_key},
            json={
                "name": name,
                "phone": phone,
                "date": date,
                "time": time,
                "party_size": party_size,
                "notes": notes,
            },
        )
        return self._ok_or_raise(resp)

    async def search_reservations(
        self,
        *,
        phone: Optional[str] = None,
        confirmation_code: Optional[str] = None,
    ) -> list[dict]:
        params: dict[str, str] = {}
        if phone:
            params["phone"] = phone
        if confirmation_code:
            params["confirmation_code"] = confirmation_code
        resp = await self._request("GET", "/reservations/search", params=params)
        return self._ok_or_raise(resp).get("results", [])

    async def modify_reservation(self, reservation_id: str, **changes: Any) -> dict:
        payload = {k: v for k, v in changes.items() if v is not None}
        resp = await self._request(
            "PATCH", f"/reservations/{reservation_id}", json=payload
        )
        return self._ok_or_raise(resp)

    async def cancel_reservation(self, reservation_id: str) -> dict:
        resp = await self._request("POST", f"/reservations/{reservation_id}/cancel")
        return self._ok_or_raise(resp)

    async def handoff(
        self, *, reason: str, conversation_summary: str, customer_phone: Optional[str] = None
    ) -> dict:
        resp = await self._request(
            "POST",
            "/handoff",
            json={
                "reason": reason,
                "conversation_summary": conversation_summary,
                "customer_phone": customer_phone,
            },
        )
        return self._ok_or_raise(resp)
