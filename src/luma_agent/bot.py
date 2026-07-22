"""Real-time voice bot: Deepgram STT -> OpenAI LLM (tools) -> Cartesia TTS.

Built on Pipecat 1.6.0. The runner (`pipecat.runner.run.main`) serves the
prebuilt browser UI, handles the WebRTC signaling at ``/api/offer``, and calls
``bot(runner_args)`` once per connection.

Design notes:
  * The three reservation tools are the SAME `tools.dispatch` used by the tests
    and the eval harness — the voice layer adds nothing to the business logic.
  * The per-call `ToolContext` (API client + session state + metrics + logger) is
    injected via `PipelineWorker(app_resources=...)` and read back in each tool
    handler as `params.app_resources` — no globals.
  * Barge-in is native: Silero VAD (on the user aggregator) drives Pipecat's
    interruption handling, which flushes TTS and cancels the in-flight LLM turn.
  * A small observer records `turn.eos_to_first_audio` (end of caller speech ->
    first bot audio) and per-service TTFB from Pipecat metrics frames.

Run:
    python -m luma_agent.bot            # -> http://localhost:7860
"""

from __future__ import annotations

import time

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    LLMRunFrame,
    MetricsFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.observers.base_observer import BaseObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import WorkerRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams

from .api_client import ReservationClient
from .config import RESTAURANT_NAME, get_settings
from .logging_utils import get_logger, log_event
from .metrics import LatencyRecorder
from .prompts import build_system_prompt
from .tools import TOOL_SCHEMAS, SessionState, ToolContext, dispatch

settings = get_settings()
logger = get_logger("luma.bot", level=settings.log_level, log_file=settings.log_file)


# --------------------------------------------------------------------------
# Latency instrumentation
# --------------------------------------------------------------------------


class LatencyObserver(BaseObserver):
    """Records end-of-speech -> first-bot-audio and per-service TTFB."""

    def __init__(self, metrics: LatencyRecorder) -> None:
        super().__init__()
        self._metrics = metrics
        self._eos_t: float | None = None

    async def on_push_frame(self, data) -> None:  # noqa: ANN001 (pipecat FramePushed)
        try:
            frame = getattr(data, "frame", None)
            if isinstance(frame, UserStoppedSpeakingFrame):
                self._eos_t = time.perf_counter()
            elif isinstance(frame, BotStartedSpeakingFrame):
                if self._eos_t is not None:
                    ms = (time.perf_counter() - self._eos_t) * 1000.0
                    self._metrics.record("turn.eos_to_first_audio", ms)
                    log_event(logger, "turn_latency", eos_to_first_audio_ms=round(ms, 1))
                    self._eos_t = None
            elif isinstance(frame, MetricsFrame):
                for item in getattr(frame, "data", []) or []:
                    if isinstance(item, TTFBMetricsData) and item.value is not None:
                        self._metrics.record(f"ttfb.{item.processor}", item.value * 1000.0)
        except Exception:  # never let instrumentation break the call
            pass


# --------------------------------------------------------------------------
# Tools wiring
# --------------------------------------------------------------------------


def _tools_schema() -> ToolsSchema:
    return ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name=s["name"],
                description=s["description"],
                properties=s["parameters"]["properties"],
                required=s["parameters"]["required"],
            )
            for s in TOOL_SCHEMAS
        ]
    )


def _make_handler(tool_name: str):
    async def handler(params):  # params: FunctionCallParams
        ctx: ToolContext = params.app_resources
        result = await dispatch(ctx, tool_name, params.arguments or {})
        await params.result_callback(result)

    return handler


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


async def run_bot(transport: BaseTransport, runner_args) -> None:
    metrics = LatencyRecorder()
    client = ReservationClient(
        settings.reservation_api_url,
        timeout_s=settings.api_timeout_s,
        max_retries=settings.api_max_retries,
        retry_backoff_ms=settings.api_retry_backoff_ms,
        logger=logger,
        metrics=metrics,
    )
    session_id = getattr(runner_args, "session_id", None) or "call"
    tool_ctx = ToolContext(client=client, state=SessionState(call_id=session_id), logger=logger, metrics=metrics)
    log_event(logger, "call_started", call_id=session_id)

    # Services (streaming).
    stt = DeepgramSTTService(
        api_key=settings.deepgram_api_key,
        live_options=LiveOptions(
            model=settings.deepgram_model, language="en-US", smart_format=True
        ),
    )
    tts = CartesiaTTSService(
        api_key=settings.cartesia_api_key, voice_id=settings.cartesia_voice_id
    )
    llm = OpenAILLMService(api_key=settings.openai_api_key, model=settings.openai_model)

    for schema in TOOL_SCHEMAS:
        llm.register_function(schema["name"], _make_handler(schema["name"]))

    context = LLMContext(
        messages=[{"role": "system", "content": build_system_prompt()}],
        tools=_tools_schema(),
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),      # caller audio in
            stt,                    # -> transcript
            user_aggregator,        # -> context (with VAD turn-taking)
            llm,                    # -> response + tool calls
            tts,                    # -> speech
            transport.output(),     # bot audio out
            assistant_aggregator,   # -> context (assistant turn)
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        app_resources=tool_ctx,           # <- injected into every tool handler
        observers=[LatencyObserver(metrics)],
        idle_timeout_secs=getattr(runner_args, "pipeline_idle_timeout_secs", 300),
    )

    greeted = {"done": False}

    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, client_info):  # noqa: ANN001
        log_event(logger, "client_connected", call_id=session_id)
        if greeted["done"]:
            return
        greeted["done"] = True
        # Let the LLM speak first, in its own voice, per the system prompt.
        context.add_message(
            {
                "role": "developer",
                "content": f"Greet the caller now — warmly and briefly, as {RESTAURANT_NAME} "
                "reservations — and ask how you can help.",
            }
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, client_info):  # noqa: ANN001
        log_event(
            logger,
            "client_disconnected",
            call_id=session_id,
            tool_calls=tool_ctx.state.tool_call_count,
            latency=metrics.report(),
        )
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=getattr(runner_args, "handle_sigint", False))
    try:
        await runner.add_workers(worker)
        await runner.run()
    finally:
        log_event(logger, "call_ended", call_id=session_id, latency=metrics.report())
        await client.aclose()


async def bot(runner_args) -> None:
    """Runner entry point (discovered by pipecat.runner.run.main)."""
    missing = settings.require_voice_keys()
    if missing:
        log_event(logger, "missing_keys", level="ERROR", missing=missing)
        raise RuntimeError(
            f"Missing required env keys for the voice stack: {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in."
        )
    transport_params = {
        "webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    }
    from pipecat.runner.utils import create_transport

    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


def main() -> None:
    from pipecat.runner.run import main as runner_main

    runner_main()


if __name__ == "__main__":
    main()
