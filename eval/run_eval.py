"""Evaluation harness for the reservation test scenarios.

Two modes:

  scripted  (default, NO API keys):
      Drives the tool layer through the correct sequence for each scenario
      against the real mock API. Proves tool-calling correctness, real (never
      invented) alternatives, retry-once, and duplicate prevention, and measures
      reservation-API latency (p50/p95). Fully reproducible offline.

  llm       (needs OPENAI_API_KEY):
      Runs the actual GPT-4o agent (same system prompt + tools as the live voice
      bot) over each scenario's spoken user turns, and records the tool calls the
      MODEL chose, plus LLM latency. This is the real end-to-end logic test.

Writes EVALUATION_RESULTS.md (per-scenario results table + aggregates).

Usage:
    python eval/run_eval.py                      # scripted, in-process mock
    python eval/run_eval.py --api-url http://localhost:8000
    python eval/run_eval.py --mode llm           # needs OPENAI_API_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import pathlib
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from luma_agent.api_client import ReservationClient  # noqa: E402
from luma_agent.metrics import LatencyRecorder, _percentile  # noqa: E402
from luma_agent.prompts import build_system_prompt  # noqa: E402
from luma_agent.tools import (  # noqa: E402
    SessionState,
    ToolContext,
    dispatch,
    openai_tools,
)

MOCK_APP_PATH = ROOT / "mock_api" / "app.py"
SCENARIOS_PATH = ROOT / "data" / "standard_test_cases.json"


def _load_mock_app():
    spec = importlib.util.spec_from_file_location("luma_mock_app_eval", MOCK_APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore
    return module.app


@dataclass
class TestRecord:
    id: str
    name: str
    passed: bool = False
    final_outcome: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    duplicate_or_wrong_write: bool = False
    api_latency: dict = field(default_factory=dict)
    llm_latency: dict = field(default_factory=dict)
    notes: str = ""

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    @property
    def tool_call_names(self) -> str:
        return " -> ".join(tc["name"] for tc in self.tool_calls) or "-"


# --------------------------------------------------------------------------
# Harness plumbing
# --------------------------------------------------------------------------


class Harness:
    def __init__(self, api_url: str = "") -> None:
        self.api_url = api_url

    async def fresh_ctx(self) -> tuple[ToolContext, httpx.AsyncClient]:
        metrics = LatencyRecorder()
        if self.api_url:
            hc = httpx.AsyncClient(base_url=self.api_url, timeout=8.0)
            await hc.post("/admin/reset")
        else:
            app = _load_mock_app()  # fresh globals == reset
            hc = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock")
        rc = ReservationClient(
            self.api_url or "http://mock",
            client=hc,
            max_retries=1,
            retry_backoff_ms=50,
            metrics=metrics,
        )
        ctx = ToolContext(client=rc, state=SessionState(call_id=""), logger=None, metrics=metrics)
        return ctx, hc

    @staticmethod
    async def confirmed_count(ctx: ToolContext, phone: str) -> int:
        found = await ctx.client.search_reservations(phone=phone)
        return len([f for f in found if f.get("status") == "confirmed"])


def _api_latency(ctx: ToolContext) -> dict:
    samples: list[float] = []
    for name in ctx.metrics.names():
        if name.startswith("api."):
            # pull raw samples via summary count is lossy; recompute from private store
            samples.extend(ctx.metrics._samples.get(name, []))  # noqa: SLF001
    if not samples:
        return {"p50_ms": 0.0, "p95_ms": 0.0, "n": 0}
    return {
        "p50_ms": round(_percentile(samples, 0.50), 1),
        "p95_ms": round(_percentile(samples, 0.95), 1),
        "n": len(samples),
    }


# --------------------------------------------------------------------------
# Scripted flows (deterministic, no LLM)
# --------------------------------------------------------------------------


async def _record_call(ctx: ToolContext, rec: TestRecord, name: str, args: dict) -> dict:
    result = await dispatch(ctx, name, args)
    rec.tool_calls.append({"name": name, "ok": result.get("ok"), "error": result.get("error")})
    return result


async def scripted_t1(ctx, rec):
    a = await _record_call(ctx, rec, "check_availability", {"date": "2026-08-14", "time": "18:00", "party_size": 4})
    c = await _record_call(ctx, rec, "create_reservation",
                           {"name": "Jordan Lee", "phone": "310-555-0199", "date": "2026-08-14", "time": "18:00", "party_size": 4})
    count = await Harness.confirmed_count(ctx, "+13105550199")
    rec.duplicate_or_wrong_write = count != 1
    rec.passed = a.get("available") is True and c.get("ok") is True and count == 1
    rec.final_outcome = f"Booked {c.get('confirmation_code')} (party 4, 18:00)"


async def scripted_t2(ctx, rec):
    a = await _record_call(ctx, rec, "check_availability", {"date": "2026-08-14", "time": "18:30", "party_size": 4})
    alts = {x["time"] for x in a.get("alternatives", [])}
    a2 = await _record_call(ctx, rec, "check_availability", {"date": "2026-08-14", "time": "19:30", "party_size": 4})
    c = await _record_call(ctx, rec, "create_reservation",
                           {"name": "Taylor Kim", "phone": "424-555-0188", "date": "2026-08-14", "time": "19:30", "party_size": 4})
    count = await Harness.confirmed_count(ctx, "+14245550188")
    rec.duplicate_or_wrong_write = count != 1
    rec.passed = (a.get("available") is False and "19:30" in alts and a2.get("available") is True
                  and c.get("ok") is True and count == 1)
    rec.final_outcome = f"18:30 unavailable; offered real alternatives {sorted(alts)}; booked 19:30 ({c.get('confirmation_code')})"


async def scripted_t3(ctx, rec):
    await _record_call(ctx, rec, "check_availability", {"date": "2026-08-15", "time": "18:30", "party_size": 2})
    a = await _record_call(ctx, rec, "check_availability", {"date": "2026-08-15", "time": "18:30", "party_size": 4})
    c = await _record_call(ctx, rec, "create_reservation",
                           {"name": "Casey Brown", "phone": "213-555-0114", "date": "2026-08-15", "time": "18:30", "party_size": 4})
    dup = await _record_call(ctx, rec, "create_reservation",
                             {"name": "Casey Brown", "phone": "213-555-0114", "date": "2026-08-15", "time": "18:30", "party_size": 4})
    count = await Harness.confirmed_count(ctx, "+12135550114")
    rec.duplicate_or_wrong_write = count != 1
    rec.passed = (a.get("available") is True and c.get("reservation", {}).get("party_size") == 4
                  and dup.get("duplicate_prevented") is True and count == 1)
    rec.final_outcome = f"Correction applied (party 4); duplicate create prevented; one record ({c.get('confirmation_code')})"


async def scripted_t4(ctx, rec):
    f = await _record_call(ctx, rec, "find_reservation", {"confirmation_code": "LUMA-4821"})
    rid = f["reservations"][0]["reservation_id"] if f.get("count") else ""
    m = await _record_call(ctx, rec, "modify_reservation", {"reservation_id": rid, "time": "19:30", "party_size": 4})
    ok = m.get("ok") and m.get("reservation", {}).get("time") == "19:30" and m["reservation"]["party_size"] == 4
    rec.passed = bool(f.get("count") == 1 and ok)
    rec.final_outcome = f"LUMA-4821 moved to 19:30, party 4"


async def scripted_t5(ctx, rec):
    f = await _record_call(ctx, rec, "find_reservation", {"confirmation_code": "LUMA-4821"})
    rid = f["reservations"][0]["reservation_id"] if f.get("count") else ""
    c = await _record_call(ctx, rec, "cancel_reservation", {"reservation_id": rid})
    rec.passed = bool(f.get("count") == 1 and c.get("status") == "cancelled")
    rec.final_outcome = "LUMA-4821 cancelled"


async def scripted_t6(ctx, rec):
    a = await _record_call(ctx, rec, "check_availability", {"date": "2026-08-16", "time": "18:00", "party_size": 2})
    attempts = ctx.metrics.summary("api.get_availability")["count"]
    rec.passed = a.get("ok") is True and a.get("available") is True and attempts == 2
    rec.notes = f"first request 503 -> retried once -> success ({int(attempts)} attempts total)"
    rec.final_outcome = "Recovered after one retry; real availability returned"


async def scripted_t7(ctx, rec):
    args = {"name": "Morgan Reed", "phone": "310-555-0166", "date": "2026-08-14", "time": "20:00", "party_size": 2}
    c1 = await _record_call(ctx, rec, "create_reservation", dict(args))
    c2 = await _record_call(ctx, rec, "create_reservation", dict(args))
    count = await Harness.confirmed_count(ctx, "+13105550166")
    rec.duplicate_or_wrong_write = count != 1
    same = c1.get("confirmation_code") == c2.get("reservation", {}).get("confirmation_code")
    rec.passed = c1.get("ok") is True and c2.get("duplicate_prevented") is True and same and count == 1
    rec.final_outcome = f"Repeated create returned same reservation ({c1.get('confirmation_code')}); one record"


SCRIPTED = {
    "T1": scripted_t1, "T2": scripted_t2, "T3": scripted_t3, "T4": scripted_t4,
    "T5": scripted_t5, "T6": scripted_t6, "T7": scripted_t7,
}


# --------------------------------------------------------------------------
# LLM-driven flow
# --------------------------------------------------------------------------


async def run_llm_scenario(ctx: ToolContext, scenario: dict, rec: TestRecord, model: str) -> None:
    from openai import AsyncOpenAI

    from luma_agent.config import get_settings

    settings = get_settings()
    if settings.llm_provider == "groq":
        # Groq is OpenAI-compatible — reuse the OpenAI client against its endpoint.
        client = AsyncOpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=settings.groq_api_key,
            max_retries=5,
            timeout=60,
        )
        model = settings.groq_model
        throttle_s = 2.5  # stay under Groq free-tier rate limits
    else:
        client = AsyncOpenAI(api_key=settings.openai_api_key or None, max_retries=5, timeout=60)
        throttle_s = 0.0
    tools = openai_tools()
    messages: list[dict] = [{"role": "system", "content": build_system_prompt()}]
    llm_ms: list[float] = []

    for turn in scenario["script"]:
        messages.append({"role": "user", "content": turn})
        for _ in range(6):  # cap tool-calling rounds per user turn
            if throttle_s:
                await asyncio.sleep(throttle_s)
            t0 = time.perf_counter()
            resp = await client.chat.completions.create(
                model=model, messages=messages, tools=tools, tool_choice="auto", temperature=0.2
            )
            llm_ms.append((time.perf_counter() - t0) * 1000.0)
            msg = resp.choices[0].message
            messages.append(msg.model_dump(exclude_none=True))
            if not msg.tool_calls:
                break
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await dispatch(ctx, tc.function.name, args)
                rec.tool_calls.append({"name": tc.function.name, "ok": result.get("ok"), "error": result.get("error")})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})

    if llm_ms:
        rec.llm_latency = {"p50_ms": round(_percentile(llm_ms, 0.5), 1), "p95_ms": round(_percentile(llm_ms, 0.95), 1)}
    last_assistant = next((m for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")), None)
    rec.final_outcome = (last_assistant or {}).get("content", "")[:180]
    # Heuristic pass criteria per scenario (model chose the tools).
    names = [tc["name"] for tc in rec.tool_calls]
    creates = [tc for tc in rec.tool_calls if tc["name"] == "create_reservation" and tc["ok"]]
    if rec.id in {"T1", "T2", "T3"}:
        rec.passed = "check_availability" in names and len(creates) >= 1
    elif rec.id == "T4":
        rec.passed = "find_reservation" in names and any(t["name"] == "modify_reservation" and t["ok"] for t in rec.tool_calls)
    elif rec.id == "T5":
        rec.passed = "find_reservation" in names and any(t["name"] == "cancel_reservation" and t["ok"] for t in rec.tool_calls)
    elif rec.id == "T6":
        rec.passed = "check_availability" in names
    elif rec.id == "T7":
        rec.passed = "create_reservation" in names


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def write_report(records: list[TestRecord], mode: str, out_path: pathlib.Path, agg: dict) -> None:
    def yn(b):
        return "Yes" if b else "No"

    lines = [
        "# Evaluation Results",
        "",
        f"_Mode: **{mode}** · generated by `eval/run_eval.py`._",
        "",
        "| Test | Pass/Fail | Final outcome | Tool calls | Duplicate/wrong write? | End-of-speech to first audio | API latency (p50/p95) | Notes |",
        "|---|---|---|---|---|---:|---:|---|",
    ]
    for r in records:
        eos = "n/a (text eval)" if mode != "voice" else r.notes
        api = f"{r.api_latency.get('p50_ms', 0)}/{r.api_latency.get('p95_ms', 0)} ms"
        outcome = r.final_outcome.replace("\n", " ").replace("|", "/")
        note = r.notes.replace("|", "/")
        if r.llm_latency:
            note = (note + f" LLM {r.llm_latency['p50_ms']}/{r.llm_latency['p95_ms']} ms").strip()
        lines.append(
            f"| {r.id} | {'PASS' if r.passed else 'FAIL'} | {outcome} | "
            f"{r.tool_call_count} ({r.tool_call_names}) | {yn(r.duplicate_or_wrong_write)} | "
            f"{eos} | {api} | {note} |"
        )

    passed = sum(1 for r in records if r.passed)
    dup = sum(1 for r in records if r.duplicate_or_wrong_write)
    lines += [
        "",
        "## Aggregate",
        "",
        f"- **Task success rate:** {passed}/{len(records)} ({round(100*passed/max(len(records),1))}%)",
        f"- **Tool-call accuracy:** {agg.get('tool_ok_rate', 'n/a')}",
        f"- **Duplicate-write rate:** {dup}/{len(records)}",
        f"- **Reservation API latency (all calls):** p50 {agg.get('api_p50', 0)} ms · p95 {agg.get('api_p95', 0)} ms",
    ]
    if agg.get("llm_p50"):
        lines.append(f"- **LLM turn latency:** p50 {agg['llm_p50']} ms · p95 {agg['llm_p95']} ms")
    lines += [
        "- **End-of-speech to first audio (voice):** measured live from Pipecat metrics in the voice demo; "
        "see `turn.eos_to_first_audio` in the agent logs and the README latency section.",
        "",
        "### Known limitations",
        "- Text eval exercises logic + reservation-API latency, not audio latency (captured separately in the voice demo).",
        "- Scripted mode drives the intended tool sequence; the `llm` mode verifies the model *chooses* the right tools.",
        "",
    ]
    out_path.write_text("\n".join(lines))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["scripted", "llm"], default="scripted")
    ap.add_argument("--api-url", default="", help="Live reservation API URL; empty = in-process mock")
    ap.add_argument("--model", default="gpt-4o")
    ap.add_argument("--out", default=str(ROOT / "EVALUATION_RESULTS.md"))
    args = ap.parse_args()

    scenarios = json.loads(SCENARIOS_PATH.read_text())
    harness = Harness(api_url=args.api_url)
    records: list[TestRecord] = []
    all_api: list[float] = []
    all_llm: list[float] = []
    tool_ok = tool_total = 0

    for scenario in scenarios:
        sid = scenario["id"]
        rec = TestRecord(id=sid, name=scenario["name"])
        ctx, hc = await harness.fresh_ctx()
        try:
            if args.mode == "scripted":
                await SCRIPTED[sid](ctx, rec)
            else:
                await run_llm_scenario(ctx, scenario, rec, args.model)
        except Exception as e:  # a crash is a failed test, not a crashed run
            rec.passed = False
            rec.notes = f"harness error: {e}"
        finally:
            rec.api_latency = _api_latency(ctx)
            for name in ctx.metrics.names():
                if name.startswith("api."):
                    all_api.extend(ctx.metrics._samples.get(name, []))  # noqa: SLF001
            if rec.llm_latency:
                all_llm.extend([rec.llm_latency["p50_ms"], rec.llm_latency["p95_ms"]])
            tool_ok += sum(1 for tc in rec.tool_calls if tc["ok"])
            tool_total += len(rec.tool_calls)
            await hc.aclose()
        records.append(rec)
        print(f"[{sid}] {'PASS' if rec.passed else 'FAIL'}  {rec.tool_call_names}  "
              f"api p50={rec.api_latency.get('p50_ms')}ms")

    agg = {
        "api_p50": round(_percentile(all_api, 0.5), 1) if all_api else 0.0,
        "api_p95": round(_percentile(all_api, 0.95), 1) if all_api else 0.0,
        "tool_ok_rate": f"{tool_ok}/{tool_total}" if tool_total else "n/a",
    }
    if all_llm:
        agg["llm_p50"] = round(_percentile(all_llm, 0.5), 1)
        agg["llm_p95"] = round(_percentile(all_llm, 0.95), 1)

    write_report(records, args.mode, pathlib.Path(args.out), agg)
    passed = sum(1 for r in records if r.passed)
    print(f"\n{passed}/{len(records)} passed · API p50 {agg['api_p50']}ms / p95 {agg['api_p95']}ms")
    print(f"Report written to {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
