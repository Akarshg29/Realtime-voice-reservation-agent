"""Test fixtures: run the mock reservation API in-process (no network, no keys).

Each test loads a fresh copy of mock_api/app.py, so every test starts from the
the INITIAL data — equivalent to POST /admin/reset before each test.
"""

from __future__ import annotations

import importlib.util
import pathlib

import httpx
import pytest_asyncio

from luma_agent.api_client import ReservationClient
from luma_agent.metrics import LatencyRecorder
from luma_agent.tools import SessionState, ToolContext

MOCK_APP_PATH = pathlib.Path(__file__).resolve().parents[1] / "mock_api" / "app.py"


def _load_mock_app():
    spec = importlib.util.spec_from_file_location("luma_mock_app", MOCK_APP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)  # fresh globals => fresh INITIAL data
    return module.app


@pytest_asyncio.fixture
async def client():
    app = _load_mock_app()
    transport = httpx.ASGITransport(app=app)
    httpx_client = httpx.AsyncClient(transport=transport, base_url="http://mock")
    rc = ReservationClient(
        "http://mock",
        client=httpx_client,
        max_retries=1,
        retry_backoff_ms=1,  # keep tests fast
        metrics=LatencyRecorder(),
    )
    try:
        yield rc
    finally:
        await httpx_client.aclose()


@pytest_asyncio.fixture
async def ctx(client):
    return ToolContext(
        client=client,
        state=SessionState(call_id="test"),
        logger=None,
        metrics=client.metrics,
    )
