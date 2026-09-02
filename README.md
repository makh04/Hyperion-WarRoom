# SentinelVoice — Backend

A FastAPI backend for the SentinelVoice SRE incident co-pilot: it bridges Google Meet call
audio to [AssemblyAI's Voice Agent API](https://www.assemblyai.com/docs/voice-agents/voice-agent-api),
with **Groq** wired in as the agent's reasoning LLM, and exposes REST/WebSocket APIs for a
live incident dashboard.

## What's here vs. what's stubbed

This delivers the full **backend**, built and verified against AssemblyAI's current API
(events, tool-calling contract, and the Agents REST API were all checked live against their
docs while building this — see "Design notes" below).

Not included (out of scope for a backend deliverable, but the interfaces they'd talk to are
fully defined below):
- The **Playwright Google Meet bot** (`bot/meet_client.js` in the original spec) — the
  `/ws/bot/{incident_id}` contract it needs to speak is documented below, and
  `scripts/mic_test_client.py` is a working stand-in you can use today.
- The **Next.js dashboard UI** — `/ws/dashboard/{incident_id}` and the REST endpoints it
  needs are implemented and documented below.

## Architecture

```
Meet bot (audio + speaker names)          Dashboard (live timeline + approvals)
        │  ws://.../ws/bot/{id}                   │  ws://.../ws/dashboard/{id}
        ▼                                          ▼
┌─────────────────────────── FastAPI backend (this repo) ───────────────────────────┐
│  bridge.py  ──audio in/out──▶  AgentSession  ──tool.call/result──▶  dispatcher.py  │
│                                     │                                    │         │
│                              wss://agents.assemblyai.com/v1/ws     tools/infra.py  │
│                              (STT + turn-taking + TTS,             (mocked cloud   │
│                               reasoning via Groq)                   actions)       │
└──────────────────────────────────────────────────────────────────────────────────┘
```

Nothing CPU- or memory-heavy runs locally: speech recognition, reasoning, and speech
synthesis all happen on AssemblyAI's and Groq's infrastructure. This backend's only job is
moving JSON events and base64 audio chunks between three WebSockets, so it's cheap to run
and scales mostly on network I/O, not compute.

## Reasoning LLM: Groq

AssemblyAI's Voice Agent API normally reasons with its own managed model. To point it at
Groq instead, AssemblyAI requires you to [connect your own LLM](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/connect-your-own-llm) —
and that's only configurable on a **stored agent** (via their Agents REST API), not inline
per-session. So when `GROQ_API_KEY` is set, this backend:

1. On startup (and lazily on first use if that fails), calls `POST /v1/agents` on
   `https://agents.assemblyai.com` with `llm: [{base_url: "https://api.groq.com/openai/v1",
   model: GROQ_MODEL, api_key: GROQ_API_KEY}]`, plus our system prompt, voice, tools, and
   turn-detection tuning (`app/voice/provisioning.py`).
2. Caches the returned `agent_id` in `.assemblyai_agent_cache.json`, keyed by a hash of the
   config (minus the secret). Restarts reuse the cached agent instead of creating a new one
   every time; if you change the system prompt, tools, or model, the hash changes and the
   backend `PUT`s the existing agent in place rather than creating an orphan.
3. Each incident's `session.update` then just sends `{"agent_id": "<id>"}` — the actual
   voice session is otherwise identical to the managed-model path.

If `GROQ_API_KEY` is left blank, none of the above happens: sessions configure everything
inline per-connection and AssemblyAI's own managed model reasons instead. Same code path,
zero extra latency, easiest way to sanity-check the pipeline before adding Groq.

Run `python scripts/provision_agent.py` any time to provision/inspect the agent standalone
and confirm your keys are wired correctly before starting the full server.

**Model choice:** defaults to `llama-3.3-70b-versatile` — Groq's recommended pick for
tool-calling reliability plus speed. Override with `GROQ_MODEL` if you'd rather use
`llama-3.1-8b-instant` (faster, lighter) or another Groq-hosted model.

## Setup

```bash
cd sentinelvoice-backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: at minimum set ASSEMBLYAI_API_KEY (https://www.assemblyai.com/dashboard/home)
# and GROQ_API_KEY (https://console.groq.com/keys) if you want Groq reasoning
```

Run it:

```bash
uvicorn app.main:app --reload --port 8000
```

Verify the core logic (no external services needed — pure Python, runs in a second):

```bash
python3 tests/test_dispatcher.py
```

Try it end-to-end with your own voice, no Meet bot required:

```bash
pip install sounddevice numpy requests
python scripts/mic_test_client.py
# say: "Agent, check checkout-db CPU load"
```

## REST API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/incidents` | Create an incident. Body: `{"name": str, "meet_url"?: str}` |
| `GET` | `/incidents` | List incidents |
| `GET` | `/incidents/{id}` | Incident status, including whether the voice session is live |
| `GET` | `/incidents/{id}/timeline` | Full post-mortem timeline (transcript + decisions + tool calls) |
| `GET` | `/incidents/{id}/pending-actions` | Actions awaiting confirmation |
| `POST` | `/incidents/{id}/pending-actions/{action_id}/approve` | Dashboard-driven approval — runs the action immediately, no spoken phrase needed |
| `POST` | `/incidents/{id}/resolve` | Mark an incident resolved |
| `GET` | `/health` | Liveness + which reasoning LLM is active |

## WebSocket contracts

### `/ws/bot/{incident_id}` — for the Meet browser bot

**Bot → backend:**
```json
{"type": "audio.chunk", "audio": "<base64 PCM16, 24kHz, mono>"}
{"type": "speaker.active", "name": "Alex"}
{"type": "meet.left"}
```

**Backend → bot** (play into the virtual mic feeding the Meet call):
```json
{"type": "reply.audio", "data": "<base64 PCM16, 24kHz, mono>"}
```

### `/ws/dashboard/{incident_id}` — for the live dashboard

Push-only. On connect you get a snapshot; after that, events stream as they happen:
```json
{"type": "timeline.snapshot", "entries": [...], "pending_actions": [...]}
{"type": "timeline.entry", "entry": {...}}
{"type": "pending_action.created", "action": {...}}
{"type": "pending_action.executed", "action": {...}}
{"type": "transcript.delta", "text": "partial..."}
{"type": "agent.error", "code": "...", "message": "..."}
```

## Tools & the two-phase confirmation protocol

Five tools are registered with the agent (`app/tools/schemas.py`), dispatched by
`app/tools/dispatcher.py`:

- `get_service_health`, `fetch_recent_deployments` — read-only, mocked in
  `app/tools/infra.py` with a couple of realistic baselines (`checkout-db` looks unhealthy
  out of the box, good for a demo).
- `log_incident_decision` — appends to the incident timeline.
- `request_action_confirmation` → `execute_confirmed_action` — the safety-critical pair.
  `request_action_confirmation` never executes anything; it registers a pending action and
  returns a phrase like `"Confirm action ACT-102"`. `execute_confirmed_action` refuses to
  run unless either that exact phrase comes back, *or* the action was approved via
  `POST /incidents/{id}/pending-actions/{action_id}/approve` from the dashboard. Wrong
  phrases and double-execution are both rejected — see `tests/test_dispatcher.py` for the
  exact behavior, verified against the actual state machine.

`execute_confirmed_action` uses AssemblyAI's `"hold"` execution mode, so the agent stays
silent while the (mocked, ~1s) action runs instead of narrating filler, then delivers the
result directly.

Wire real infrastructure in by setting `USE_MOCK_CLOUD=false` and filling in
`_execute_infra_action_live()` in `app/tools/infra.py` — it lazily imports `boto3` only on
that path, so the default mock-only footprint stays light.

## Design notes: "fast and low resource"

- **No local models.** STT, turn detection, barge-in, LLM reasoning, and TTS all run on
  AssemblyAI's and Groq's servers. This process never loads a model, so it needs no GPU and
  barely any RAM.
- **Async all the way down**, single process, no worker pools — the backend is just
  shuttling JSON/bytes between three WebSockets, which is cheap even at scale.
- **In-memory state**, no database. Fine for a hackathon-scale deployment; if you need
  durability, `app/state.py` is the one place to swap in persistence.
- **One AssemblyAI connection per incident**, reused for the whole call, not per utterance
  or per speaker.
- **Groq for reasoning** — beyond being the model the person building this asked for, Groq's
  LPU inference is genuinely fast, which matters directly for the "under ~1s response" feel
  a war-room co-pilot needs.
- **Minimal dependencies** (`requirements.txt`): six packages, no ML/audio libraries in the
  core server. `boto3`, `sounddevice`, and `numpy` are called out as optional, only needed
  for the live-cloud path or the manual mic test client.

## Known limitations (being upfront about the hard parts)

- **Multi-speaker turn detection.** AssemblyAI's turn detection is tuned for one-speaker-at-
  a-time conversation. War-room audio is many people talking over each other. The system
  prompt handles this by instructing the agent to stay silent unless explicitly addressed
  ("Agent, ..."), and turn-detection silence thresholds are loosened in
  `provisioning.py`/`agent_session.py` — but this is a deliberate simplification, not a
  solved problem. A production version would likely want a lightweight wake-word gate in
  the bot itself, only forwarding audio to AssemblyAI once "Agent" is detected.
- **Speaker attribution is best-effort.** `speaker.active` events from the bot's DOM
  observer are matched to transcript lines by recency (`SPEAKER_STALENESS_SECONDS` in
  `app/state.py`), not by hard timestamp alignment with AssemblyAI's transcript events
  (which the API doesn't currently expose at the word level).
- **Real cloud actions are stubbed**, not implemented — see `_execute_infra_action_live()`.
