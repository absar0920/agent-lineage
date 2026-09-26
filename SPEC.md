# agent-lineage — Spec

**Status:** Draft for review · **Author:** Claude (for Muhammad Absar) · **Date:** 2026-09-26
**Target:** Production-ready v1.0 by end of day today (2026-09-26)
**Deployment model:** Self-hosted, single team, inside the operator's own infrastructure

---

## 1. Problem

Multi-step AI agents fail **silently**. Every call succeeds, but the result is wrong.

Reference incident (real, client-confidential; recreated here with a fictional
store and synthetic data):

1. A support agent looks up a customer's order and gets an **older order** belonging to the same customer.
2. The next two steps run correctly on that wrong data.
3. The agent offers a **refund on an item that was already returned**.
4. The traces show every call, but:
   - the root cause is **two steps before** the visible failure,
   - each step **looks correct on its own**,
   - **replay does not reproduce** the failure.
5. It took **~6 days and 3–4 engineers**, mostly spent walking back through steps by hand.

Traces answer *"what was called?"*. Nobody could quickly answer
*"where did this wrong value come from?"*

## 2. Product

Two production components in one repository:

| component | what it is |
|---|---|
| **Lineage** | Capture SDK, ingest API, storage, and UI that answer *"where did this value come from?"*, show what alternatives existed at that moment, and replay a run exactly as it happened. |
| **Kestrel Support Agent** | A customer-support agent for an online store (fictional brand *Kestrel Outdoor Co.*), connected to store, helpdesk, and payments systems via **connectors**. It is the first system instrumented with Lineage. |

### v1.0 acceptance criteria

- [ ] The support agent handles refund, order-status, and return requests end to end through its connectors, and every run is captured by Lineage.
- [ ] **Lineage query:** clicking any identifier in a run's output (e.g. an order ID in a refund offer) shows the step where it **entered** the run, every step that consumed it, and any **candidate alternatives** at the origin, in under 1 s.
- [ ] **Replay:** any run can be replayed in `live` mode (tools re-executed) or `recorded` mode (tool and LLM outputs served from capture), with a step-by-step diff that points to the **first divergence**.
- [ ] **Reference incident** (§4.4) is reproduced as an automated regression test and diagnosed by the Lineage query in one click.
- [ ] Capture **never breaks or blocks the agent**: if Lineage is down, the agent keeps working and the loss is counted in metrics.
- [ ] No raw PII is stored by Lineage.
- [ ] `docker compose up` starts the full stack; migrations run automatically; health checks pass.
- [ ] The UI requires login.

### Out of scope for v1.0

- Multi-tenancy, billing, hosted SaaS
- CI pipelines
- OpenTelemetry export
- Real third-party connector credentials (v1.0 ships **mock connectors** with a stable interface; real adapters are drop-in later, see §5)
- Automatic detection of wrong results (rules/evals). This is v1.1, built on the same data
- Anything copied from the client's real agent, prompts, schema, or data

---

## 3. Architecture

```
                    ┌────────────────────────────────────────┐
  customer ───────▶ │ support-agent service (FastAPI)        │
  (chat UI / API)   │  agent loop ─ OpenRouter (LLM)         │
                    │  connectors: shopify* zendesk* stripe* │
                    │  lineage SDK (async, fail-open)        │
                    └───────┬───────────────────┬────────────┘
                            │ SQL               │ HTTPS + API key
                 ┌──────────▼─────────┐  ┌──────▼──────────────────────┐
                 │ Postgres: `store`  │  │ lineage service (FastAPI)   │
                 │ (mock connector    │  │  ingest · query · replay    │
                 │  backing data)     │  │  retention job · auth       │
                 └────────────────────┘  └──────┬──────────────────────┘
                                                │ SQL
                                         ┌──────▼──────────────────────┐
                                         │ Postgres: `lineage`         │
                                         └─────────────────────────────┘
                    ┌────────────────────────────────────────┐
                    │ web UI (React + Vite + TS, nginx)      │
                    │ Conversations · Runs · Lineage ·       │
                    │ Replay · Connections · Settings        │
                    └────────────────────────────────────────┘
       * mock connectors in v1.0
```

**Design rules**

1. The `lineage` SDK and service know **nothing** about the support agent. Any Python agent can be instrumented with them.
2. The agent talks to external systems **only through connector interfaces**. Swapping a mock for a real adapter changes no agent code.
3. Capture is **fail-open**: SDK errors are logged and counted, never raised into the agent.

### Tech stack

| layer | choice |
|---|---|
| Language | Python 3.12 (backend), TypeScript (UI) |
| Web | FastAPI, uvicorn, pydantic v2, pydantic-settings |
| Database | Postgres 16; SQLAlchemy 2.0 (async) + Alembic migrations |
| LLM | OpenRouter (OpenAI-compatible Chat Completions) via `httpx` |
| UI | React 18 + Vite + TypeScript, React Flow, TanStack Query |
| Packaging | `uv`; the SDK is a separate installable package `agent-lineage-sdk` |
| Runtime | Docker images, `docker compose` with health checks |
| Tests | pytest, pytest-asyncio, testcontainers (Postgres); vitest for UI logic |
| Quality | ruff, mypy (strict on `lineage_sdk` and `lineage_service`), `tsc --noEmit` |

### Repository layout

```
agent-lineage/
├── SPEC.md
├── README.md                    # install, configure, operate
├── docker-compose.yml
├── .env.example
├── packages/
│   └── lineage_sdk/             # pip-installable capture SDK
│       ├── recorder.py          # Recorder, Run, Step, @tool decorator
│       ├── values.py            # value extraction + fingerprinting
│       ├── redact.py            # PII redaction (stable tokens)
│       ├── transport.py         # async batched HTTP exporter, fail-open
│       └── replay.py            # recorded-mode tool/LLM stubs
├── services/
│   ├── lineage/                 # ingest, query, replay orchestration, retention, auth
│   │   ├── api/  models/  query/  migrations/
│   └── support_agent/
│       ├── agent/               # loop, prompts, policies
│       ├── connectors/          # interfaces + mock adapters
│       ├── llm/                 # OpenRouter client, fallback, recorded mode
│       ├── data/                # schema, migrations, synthetic data generator
│       └── api/
├── web/                         # React UI
└── tests/                       # cross-service integration + incident regression
```

---

## 4. Kestrel Support Agent

### 4.1 Capabilities (tools)

| tool | connector | purpose |
|---|---|---|
| `find_customer(email\|id)` | commerce | Resolve the customer |
| `lookup_recent_order(customer_id, product_query)` | commerce | Find the order a request refers to |
| `get_order(order_id)` | commerce | Order details, items, fulfilment status |
| `get_shipment_status(order_id)` | commerce | Carrier status |
| `check_refund_eligibility(order_id, sku)` | commerce | Policy check: 45-day window, refundable category |
| `create_refund_offer(order_id, sku, amount)` | payments | Create a pending refund offer |
| `add_ticket_note(ticket_id, note)` | helpdesk | Write the outcome to the ticket |

After the loop, the reply is posted to the helpdesk ticket as a captured
`post_ticket_reply` step, so it is replayable like any tool call.

Loop: plain tool-use loop over OpenRouter, `temperature=0`, at most 8 iterations,
30 s LLM timeout, automatic switch to `LLM_FALLBACK_MODEL` on 429/5xx/timeout.
Refund offers above a configurable amount (`REFUND_APPROVAL_THRESHOLD`, default $200)
are created as `needs_approval`, not auto-issued.

### 4.2 Connectors

A `Connector` interface per domain (`CommerceConnector`, `HelpdeskConnector`,
`PaymentsConnector`) with `health()`, `describe()`, and domain methods.
v1.0 ships **mock adapters** backed by the `store` Postgres database. They behave
like the real APIs: realistic IDs, latency (20–150 ms), pagination, and
occasional retryable errors (configurable, default 0% in tests).

**Connections page (UI):**

| connector | status in v1.0 |
|---|---|
| Shopify (commerce) | **Connected** (mock) |
| Zendesk (helpdesk) | **Connected** (mock) |
| Stripe (payments) | **Connected** (mock) |
| WooCommerce, Gorgias, Intercom, Freshdesk, ShipStation | Available, not connected |

Each card shows status, last sync, record counts, and a **Test connection** button
(calls `health()`). Connecting an "available" connector opens a config form that is
stored but disabled until a real adapter exists.

### 4.3 Data (synthetic, realistic)

Generated by a **deterministic** generator (fixed seed, Faker) so every install
and every test sees the same data. All names, emails, phones, and addresses are fake.

| entity | volume |
|---|---|
| customers | 1,000 |
| products | 120 SKUs across 8 categories (footwear, jackets, packs, tents, …) |
| orders | ~4,500 over 18 months, with seasonal peaks |
| order items | ~9,000 |
| shipments | one per order; carrier events (label → in transit → delivered, some delays and lost parcels) |
| returns | ~8% of items; mix of `requested`, `received`, `refunded` |
| helpdesk tickets | ~1,200 with realistic subjects, messages, tags, statuses |
| payments / refunds | one charge per order; refunds linked to returns |

Repeat purchases are common (same product bought twice by ~6% of customers), so
the "which order did they mean?" problem shows up **naturally** across the dataset,
not only in the reference incident.

### 4.4 Reference incident (regression case)

Included in the synthetic data as a named case:

| order | placed_at | updated_at | item | state |
|---|---|---|---|---|
| **ORD-7781** | 2026-08-18 | **2026-09-20** | Ridgeline Trail Shoes, $129 | Returned 09-19 and refunded. Return processing bumped `updated_at`. |
| **ORD-8124** | 2026-09-12 | 2026-09-15 | Ridgeline Trail Shoes, $129 | Delivered. **The order the customer means.** |

Customer C-1042 (synthetic) writes: *"the trail shoes from my latest order are too small, can I get a refund?"*

**The defect:** `lookup_recent_order` uses `ORDER BY updated_at DESC LIMIT 1`.
It returns ORD-7781. Eligibility passes (policy check, not the returns ledger), and
the agent offers a refund on already-refunded shoes. Every step returns successfully.

**Why live replay doesn't reproduce it:** a carrier delivery event later updates
`ORD-8124.updated_at`. Re-executing tool 1 now returns ORD-8124 and gives the
correct answer. Recorded replay serves the captured output and reproduces ORD-7781
exactly; the diff shows the first divergence at step 1's output.

**Decision on the defect:** it ships **fixed** in the agent, with three layers:
`ORDER BY placed_at DESC`, other matching orders reported so the agent asks the customer
which one they mean, and the eligibility check also consults the returns ledger. The
original behavior (both the sort and the ledger-blind eligibility check) is kept behind
the test-only `LEGACY_ORDER_LOOKUP` flag, so the regression test (§8) keeps proving that
Lineage diagnoses this class of failure.

---

## 5. Lineage

### 5.1 SDK public API

```python
from lineage_sdk import HttpTransport, Recorder, Redactor

rec = Recorder(service="kestrel-support",
               transport=HttpTransport(LINEAGE_URL, LINEAGE_API_KEY),
               redactor=Redactor(LINEAGE_REDACTION_KEY))

with rec.run("refund-request", input=user_msg, attrs={"ticket_id": t}) as run:
    with run.step("tool", "lookup_recent_order", input=args) as step:
        rows = commerce.find_orders(...)
        step.note_candidates(chosen=rows[0].id,
                             alternatives=[r.id for r in rows[1:]],
                             reason="ORDER BY placed_at DESC")
        step.output(result)
    run.output(final_answer)
```

- `@run.tool` decorator wraps a tool function (kwargs in, return value out, exceptions become failed steps).
- `note_candidates` is optional. Without it, the step is still traced, but the "chose 1 of N" insight is lost.
- **Transport:** events go into an in-memory bounded queue (default 10,000) and are flushed in batches by a background task (500 events or 1 s). On failure: exponential backoff, then drop, incrementing `lineage_dropped_events_total`. Never blocks and never raises.
- **Flush on shutdown** with a 5 s cap.

### 5.2 Value extraction and fingerprinting

- Walk each step's input and output JSON. For every leaf scalar (strings of 3 or more characters, numbers; booleans and a stop-list of common tokens excluded), record `(direction, json_path, fingerprint, display)`.
- LLM text: also extract identifier-like tokens with a regex (`[A-Z]{2,5}-\d{3,}`, UUIDs, emails, currency amounts), so values the model copies into prose still link.
- `fingerprint = sha256(normalize(value))[:16]`.

### 5.3 PII redaction

- Applied **in the SDK, before data leaves the process**.
- Detectors: email, phone, Luhn-valid card numbers, and configurable field names (`name`, `email`, `phone`, `address`).
- Stable tokens: `<EMAIL:3fa1c2>`, where the suffix is `HMAC(LINEAGE_REDACTION_KEY, value)`. The same value always gives the same token, so lineage still links across redacted values.
- Business identifiers (order IDs, SKUs, amounts) are kept.

### 5.4 Storage (`lineage` database)

| table | columns |
|---|---|
| `runs` | `id`, `service`, `name`, `started_at`, `ended_at`, `status`, `input`, `output`, `attrs`, `replay_of`, `replay_mode` |
| `steps` | `id`, `run_id`, `seq`, `kind` (`llm`/`tool`/`input`/`output`), `name`, `input`, `output`, `started_at`, `duration_ms`, `status`, `error` |
| `step_values` | `step_id`, `direction`, `path`, `fingerprint`, `display`; indexed on `(run_id, fingerprint)` |
| `candidates` | `step_id`, `chosen`, `alternatives`, `reason` |
| `api_keys` | `id`, `name`, `hash`, `created_at`, `revoked_at` |
| `users` | `id`, `email`, `password_hash` (argon2), `role` |

Ingest is **idempotent** (client-generated event IDs, upsert).

### 5.5 Lineage query

`GET /runs/{id}/lineage?fingerprint=`

1. Find every step where the value appears, ordered by `seq`.
2. **Origin:** the earliest step where the value appears in an **output** (or the run input) but not in that step's input.
3. **Path:** each later step that consumed it, through to the final output.
4. Attach the origin's `candidates`, e.g. "chose ORD-7781 over [ORD-8124] because ORDER BY updated_at DESC".

Returns graph nodes and edges. Target p95 under 200 ms for runs of up to 500 steps.

### 5.6 Replay and diff

| mode | tools | LLM |
|---|---|---|
| `live` | re-executed through the connectors | live |
| `recorded` | served from capture, matched by `(seq, name, args fingerprint)` | served from capture |

- A recorded replay that asks for an uncaptured call fails with `ReplayMissError(seq, name, args)`. It never falls through to live execution.
- Replay runs are stored with `replay_of` and `replay_mode`.
- **Replay never writes to external systems.** In both modes, write tools (`create_refund_offer`, `add_ticket_note`) go through a dry-run connector.
- `GET /runs/{a}/diff/{b}` aligns steps and returns the first divergence plus the steps downstream of it.

### 5.7 Retention

A daily job deletes runs older than `LINEAGE_RETENTION_DAYS` (default 30). Runs can
be pinned to exempt them.

---

## 6. APIs

### Support agent service (`:8001`)

| method | path | purpose |
|---|---|---|
| `POST` | `/v1/conversations` | Start a conversation `{customer_email, message}` |
| `POST` | `/v1/conversations/{id}/messages` | Continue it |
| `GET` | `/v1/conversations` | List (with ticket and run IDs) |
| `GET` | `/v1/connectors` | Connector status |
| `POST` | `/v1/connectors/{name}/test` | Run the health check |
| `POST` | `/v1/replay` | Internal: execute a replay for Lineage (service-to-service key) |
| `GET` | `/healthz`, `/readyz` | Liveness and readiness (DB, LLM config) |

### Lineage service (`:8002`)

| method | path | purpose |
|---|---|---|
| `POST` | `/v1/ingest` | Batched events (API key) |
| `GET` | `/v1/runs` | Filter by service, status, time, text |
| `GET` | `/v1/runs/{id}` | Run with steps and values |
| `GET` | `/v1/runs/{id}/lineage?fingerprint=` | Origin and path graph |
| `POST` | `/v1/runs/{id}/replay` | `{mode}` → new run |
| `GET` | `/v1/runs/{a}/diff/{b}` | Step diff |
| `POST` | `/v1/runs/{id}/pin` | Exempt from retention |
| `POST` | `/v1/auth/login`, `/v1/auth/logout` | UI session (httpOnly cookie) |
| `GET` | `/healthz`, `/readyz`, `/metrics` | Health checks and Prometheus metrics |

Errors: `{error: {code, message, request_id}}` with correct status codes. Every
request gets an `X-Request-ID` that is propagated to logs.

---

## 7. Web UI

Login page, then a sidebar with:

1. **Conversations:** send customer messages to the agent and see replies, each linked to its Lineage run.
2. **Runs:** filterable list with status and replay badges.
3. **Run view:** a step timeline (input/output JSON, durations). **Every identifier in any output is clickable** and opens the **lineage graph** (React Flow): origin node highlighted, candidates box, consumer chain.
4. **Replay:** choose a mode, then a side-by-side diff with the first divergence highlighted.
5. **Connections:** connector cards (§4.2).
6. **Settings:** API keys (create/revoke), retention, users.

---

## 8. Quality bar

| area | requirement |
|---|---|
| Tests | Unit tests: fingerprinting, redaction, origin query, diff, transport backoff/drop. Integration tests against real Postgres (testcontainers): ingest → query → replay. API tests for both services. |
| **Incident regression test** | With the original defect flag on and recorded LLM responses: run → refund on ORD-7781 → `lineage(ORD-7781)` origin is step `lookup_recent_order` with 2 candidates → carrier event → live replay gives ORD-8124 → recorded replay gives ORD-7781 → diff's first divergence is that step. With the flag off: the agent picks ORD-8124 or asks the customer. |
| Fail-open test | Lineage service down: the agent still answers correctly, and the drop counter increases. |
| PII test | The synthetic customer's raw email and phone never appear in the `lineage` database. |
| Security | Argon2 password hashes; API keys stored hashed; CORS locked to the UI origin; secrets only from env; no secrets in logs. |
| Ops | Structured JSON logs with `request_id` and `run_id`; Prometheus metrics; health checks; graceful shutdown; migrations on start. |
| Config | pydantic-settings with fail-fast validation. `LLM_MODE=recorded` needs no API key (tests, offline replay). |

---

## 9. Configuration (`.env.example`)

```
OPENROUTER_API_KEY=
LLM_MODEL=anthropic/claude-sonnet-5        # verified against OpenRouter at build time
LLM_FALLBACK_MODEL=
LLM_MODE=live                             # live | recorded
REFUND_APPROVAL_THRESHOLD=200
DATABASE_URL_STORE=postgresql+asyncpg://...
DATABASE_URL_LINEAGE=postgresql+asyncpg://...
LINEAGE_URL=http://lineage:8002
LINEAGE_API_KEY=
LINEAGE_REDACTION_KEY=
LINEAGE_RETENTION_DAYS=30
ADMIN_EMAIL=
ADMIN_PASSWORD=                           # hashed on first start
```

---

## 10. Build plan (today, PKT)

Spec approval target: **12:15**. Each slice ends with tests passing and a short walkthrough for you.

| # | slice | time | done when | priority |
|---|---|---|---|---|
| S1 | Repo scaffold, Docker Compose (one Postgres server, `store` and `lineage` databases), settings, logging, Alembic | 12:15–13:00 | `docker compose up` gives healthy databases | must |
| S2 | Store schema, synthetic data generator, mock connectors | 13:00–14:15 | 1,000 customers loaded; connector tests green | must |
| S3 | Lineage SDK: recorder, values, redaction, fail-open transport | 14:15–15:30 | SDK unit tests green | must |
| S4 | Lineage service: ingest, storage, lineage query, auth, API keys | 15:30–17:00 | ingest → query integration test green | must |
| S5 | Support agent: loop, OpenRouter client, fallback, recorded mode, API | 17:00–18:15 | live conversation captured end to end | must |
| S6 | Replay, diff, incident regression test | 18:15–19:15 | **regression test green** | must |
| S7 | Web UI: login, conversations, runs, lineage graph, replay diff, connections | 19:15–21:30 | full flow works in the browser | must |
| S8 | Settings UI, retention job, `/metrics`, README | 21:30–22:30 | docs complete | cut-if-behind |

Order is chosen so the most valuable parts (capture, query, replay) are solid first.
If time runs short, S8 moves to tomorrow. **Nothing in S1–S7 is cut.**

---

## 11. v1.1+

- Real connector adapters (Shopify, Zendesk, Stripe first)
- Detection rules on lineage data (e.g. "a value from an ambiguous selection reached a write tool")
- Auto-candidates at the DB-driver or HTTP-client level (no `note_candidates` needed)
- Additional agent framework adapters
- OpenTelemetry export, CI (deferred by decision)

---

## 12. Open items for your review

1. The store name *Kestrel Outdoor Co.* and the connector list (§4.2). Should Shopify, Zendesk, and Stripe be the three shown as connected?
2. Data volumes (§4.3). Are 1,000 customers and ~4,500 orders enough?
3. §4.4: the defect ships **fixed**, with the original kept behind a test-only flag. Agree?
4. Anything in §4 too close to your client's real flow?
5. I need `OPENROUTER_API_KEY` in `.env` before S5 (~17:00).
