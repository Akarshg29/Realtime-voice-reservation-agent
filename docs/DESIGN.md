# Design & Architecture

Engineering rationale behind Luma — the choices, the tradeoffs, and how the
reliability guarantees are actually enforced.

### 1. Framework, STT, LLM, TTS, and transport

- **Framework — Pipecat.** Production-grade real-time primitives out of the box: a streaming frame pipeline, VAD-based turn-taking, first-class interruption/barge-in, per-service metrics, and swappable service adapters. It keeps the business logic (tools, retries, dedupe) completely decoupled from the media plumbing.
- **STT — Deepgram (nova-2), streaming.** Low word-error-rate on conversational US English, true streaming partials, ~150–250 ms finalization, and reliable endpointing. Names and phone numbers transcribe well, which matters here.
- **LLM — OpenAI GPT-4o.** Strong, reliable function calling — the capability this system lives or dies on. For a narrow, well-specified tool domain, `gpt-4o-mini` is a ~10× cheaper drop-in (one env var) and a legitimate cost/latency tradeoff; GPT-4o is the default for reliability.
- **TTS — Cartesia (Sonic), streaming.** Lowest time-to-first-audio in class (~90–150 ms) with natural prosody, which dominates *perceived* latency. ElevenLabs is a one-line swap for higher expressiveness at slightly higher latency.
- **Transport — self-hosted WebRTC (Pipecat SmallWebRTC).** Browser-native, no extra vendor/account, sub-100 ms media path, trivial to run locally. Daily (managed WebRTC) or Twilio (PSTN) are drop-in transports for scale/telephony.

**Why a discrete STT→LLM→TTS pipeline instead of a single speech-to-speech model:** the discrete pipeline makes tool calling, validation, and confirmation *observable and controllable*, keeps each stage independently swappable/measurable, and gives clean streaming STT and TTS. The tradeoff is one extra hop of latency versus a fused model — mitigated by streaming every stage.

### 2. Session and reservation state

- **Conversation state** lives in the LLM context (Pipecat's context aggregator) for the duration of the call — messages + tool results. Ephemeral by design.
- **Per-call working state** is `SessionState` (`src/luma_agent/tools.py`): collected customer fields, the map of `idempotency_key → reservation` created this call, and last search results. In-memory, one per WebRTC connection.
- **Reservations** are the source of truth and live in the reservation backend — never in the agent. The agent holds only what it needs to avoid re-asking and to prevent duplicate writes.
- **Production:** move `SessionState` to Redis keyed by call id (TTL'd) so a crashed worker can recover and state survives a worker handoff; keep reservations in the backend database.

### 3. Barge-in: cancelling generation on interruption

Silero VAD (attached to the user aggregator) runs continuously. Interruptions are **always-on** and governed by turn-taking strategies (there is no on/off flag). When VAD detects the caller starting to speak **while the bot is talking**, the user-turn *start* strategy fires and the pipeline pushes an `InterruptionFrame` downstream that (a) aborts the in-flight LLM generation, (b) stops and flushes the TTS queue so audio goes silent within a couple hundred milliseconds, and (c) resets the partial assistant turn in the context to what was actually spoken, so the model doesn't "believe" it said words the caller never heard. In-flight tool calls are cancelled too, unless a tool opts out with `cancel_on_interruption=False`. A local smart-turn analyzer decides end-of-turn, so the bot waits for a natural pause instead of talking over the caller. Net effect: the caller can cut in mid-sentence ("actually, make it four") and the agent yields and re-plans on the corrected input.

### 4. Tool-argument validation

Three layers, defense-in-depth:
1. **Schema** — each tool exposes a JSON Schema (`TOOL_SCHEMAS`); the LLM is constrained to the declared shape and required fields.
2. **Normalisation + validation in the tool layer** (`tools.py`) before any network call: dates → `YYYY-MM-DD`, times → 24h `HH:MM` snapped to the bookable grid, phone → E.164, party size bounds (1–8, else handoff). Bad/underspecified args return a structured `{"ok": false, "error": ...}` with a hint (e.g. `valid_times`) so the model re-asks the caller instead of guessing.
3. **Backend** — the reservation API re-validates (422) and enforces capacity (409); the client normalises those into typed errors. The LLM never talks to the backend directly.

### 5. Duplicate-write prevention

- **Deterministic idempotency key** per logical booking: `sha256(name|phone|date|time|party_size)` (`idempotency_key()`), sent as the `Idempotency-Key` header. Identical booking details → identical key → the backend returns the *same* reservation instead of creating a second one.
- **Session-level short-circuit:** if that key is already in `SessionState.created_by_key`, the tool returns the existing reservation and never re-calls the backend (`duplicate_prevented: true`).
- Because the key is deterministic, **retries are safe** — a network retry, an LLM double-call, or an explicit repeat all collapse to one write.
- The system prompt also instructs the model not to call `create_reservation` twice for one booking. Verified by the duplicate-protection and correction scenario tests.

### 6. Which failures are retried

Retries live in the **integration layer** (`api_client.py`), never in the LLM:
- **Retried (bounded, default once):** HTTP 503, timeouts, and connection errors, with a short backoff honouring any `retry_after_ms`. Writes are retry-safe via the idempotency key. Scenario T6 exercises this: the first availability call fails transiently, the client retries once, succeeds, and returns the *real* result — never an invented one.
- **Not retried:** 4xx client errors (422 invalid, 409 unavailable, 404 not found) — they won't fix themselves; they're surfaced to the model as actionable results (offer alternatives, re-ask).
- **Escalation:** if the bounded retry is exhausted, the tool returns `temporarily_unavailable` and the prompt routes to `transfer_to_human` — no infinite retry loops.

### 7. Human handoff & context preservation

`transfer_to_human` posts to the backend with a `reason` and a `conversation_summary` that the tool **enriches** with everything captured in `SessionState.collected` (name, phone, date, time, party size, notes) before sending. So the human agent receives the caller's intent *and* all structured data gathered so far, even mid-booking. The handoff id/status is returned and the caller is reassured a person will follow up. In production the summary would also link the call recording + transcript id and route into a ticket/queue (Zendesk/Salesforce).

### 8. Production metrics and logs

**Latency (the headline for voice):**
- `turn.eos_to_first_audio` — end of caller speech → first byte of bot audio (the number users *feel*). Target p95 < 1.5 s.
- Component TTFB: STT finalization, LLM time-to-first-token, TTS time-to-first-byte (from Pipecat metrics frames).
- Reservation-API round-trip (p50/p95), captured in `metrics.py`.

**Correctness / business:** task-success rate, tool-call error rate, duplicate-write rate (should be 0), handoff rate + reason breakdown, interruption rate, STT confidence as a WER proxy.

**Reliability / cost:** 503/timeout counts, retry counts, LLM/STT/TTS token & audio usage per call.

**Logs:** structured single-line JSON (`logging_utils.py`) for every tool call, API request/response, retry, duplicate-prevention, and handoff — **with phone numbers masked to last-4**. Correlate everything by call id; ship to CloudWatch/Loki/Datadog.

### 9. Scaling: 10 → 100 → 1,000 concurrent calls

- **~10 concurrent:** a single container is fine. Each call is one asyncio pipeline; CPU is light (media is offloaded to providers). Add a health check and structured logs.
- **~100 concurrent:** horizontal scale — stateless agent workers behind an autoscaler (ECS/Fargate or k8s HPA), tuned to N calls per process by CPU. Move `SessionState` to Redis. Add a TURN server for NAT traversal and pin media regions near callers. Watch provider rate limits and request quota increases. Add a small connection pool and a circuit breaker to the reservation backend.
- **~1,000 concurrent:** multi-region, provider fan-out (multiple STT/TTS keys/accounts or a routing layer with fallbacks), a media SFU (LiveKit/Daily) instead of per-call peer connections, backpressure + admission control, and a queue for handoffs. The reservation backend becomes the bottleneck: it needs real persistence, idempotency at the DB layer, and per-slot row locking / optimistic concurrency to prevent oversell. Cost controls (mini model, cached system prompt) become material. Add canary deploys, per-provider dashboards, and automatic failover.

### 10. Backend API — hardening & improvements

- **Concurrency safety:** capacity mutation must be atomic; two simultaneous creates for the last seat can otherwise both succeed (oversell). Needs a transaction / row lock / conditional decrement.
- **Idempotency scope:** keys should be scoped and TTL'd, and `PATCH`/`cancel` should also accept idempotency keys.
- **Consistent error envelope:** one machine-readable error shape everywhere simplifies clients.
- **Availability ergonomics:** a "list all open slots for a date" endpoint (so the agent offers options without probing slot-by-slot), plus timezone-aware datetimes rather than split date/time strings.
- **Search:** pagination, fuzzy name match, filtering by status/date.
- **Auth & audit:** authentication, per-request tracing id, and created/updated timestamps on every mutation.
- The mock backend deliberately injects a one-time transient failure and enforces idempotency so the agent's resilience can be tested end-to-end; in production those live behind a fault-injection/test flag.

### 11. Protecting PII, recordings, transcripts, and secrets

- **Secrets:** never in code or images — env vars locally, a secrets manager (AWS Secrets Manager / Vault) in prod, rotated, least-privilege. `.env` is git-ignored; only `.env.example` ships.
- **PII in logs:** phone numbers are masked to last-4 at the logging boundary (`scrub()`), applied recursively to structured fields and free text. Names/notes are minimised.
- **In transit:** WebRTC media is DTLS-SRTP encrypted; all provider/backend calls are TLS.
- **At rest:** encrypt recordings/transcripts (KMS), short retention with automatic expiry, access-controlled buckets, audit logging on access.
- **Data minimisation & consent:** capture only what a booking needs; add call-recording consent; support deletion requests. Prefer providers with zero-retention / no-training terms and sign BAAs/DPAs as needed.
- **Boundary discipline:** the agent sends caller data only to the reservation backend and the chosen providers — never to endpoints named by the model or by tool output.

### 12. Cost per five-minute call

Approximate US list prices, early 2026. A 5-minute call ≈ ~5 min of streamed audio, ~15 LLM turns, ~2 min of synthesized speech.

| Component | Basis | Rate (approx) | Per 5-min call |
|---|---|---|---:|
| Deepgram STT (nova-2, streaming) | ~5 audio-min | ~$0.0059/min | ~$0.030 |
| OpenAI GPT-4o (tool calling) | ~30k in / ~2k out tokens | $2.50/1M in, $10/1M out | ~$0.095 |
| Cartesia TTS (Sonic) | ~1.2k chars | ~$0.05/1k chars | ~$0.060 |
| Transport (self-hosted WebRTC) | compute only | negligible | ~$0.010 |
| **Total** | | | **≈ $0.20** |

**Takeaways:** the LLM dominates and is the most variable (turn count × growing context). Switching to `gpt-4o-mini` (~$0.15/1M in) drops the LLM to ~$0.01 and the total to **≈ $0.10**. Prompt-caching the system prompt, capping context, and choosing PSTN (Twilio ≈ +$0.014/min) vs. managed WebRTC (Daily ≈ +$0.004/min) shifts the mix. Budget **~$0.10–$0.25 per 5-minute call** depending on model and transport.
