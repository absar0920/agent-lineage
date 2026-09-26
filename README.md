# agent-lineage

Find where a wrong value entered an AI agent's run, in one click.

Multi-step agents fail silently: every call succeeds, but the answer is wrong because a
bad value slipped in a few steps earlier. Traces show *what was called*. agent-lineage
answers *where did this value come from?*, shows what alternatives existed at that moment,
and replays the run exactly as it happened, even after the underlying data has changed.

The repository ships two production components:

| component | what it is |
|---|---|
| **Lineage** (`packages/lineage_sdk`, `services/lineage`, `web/`) | Capture SDK, ingest/query/replay service, and operator UI |
| **Kestrel Support Agent** (`services/support_agent`) | A customer-support agent for an online store, connected to Shopify, Zendesk and Stripe through connectors, instrumented with Lineage |

See [SPEC.md](SPEC.md) for the full specification.

---

## Quick start

Requirements: Docker with Compose.

```sh
./scripts/init-env.sh            # creates .env with random secrets and prints the admin password
$EDITOR .env                     # set OPENROUTER_API_KEY (and optionally LLM_MODEL / LLM_FALLBACK_MODEL)
docker compose up -d --build
```

Open **http://localhost:8080** and sign in with `ADMIN_EMAIL` and the printed password.

On first start the services run their database migrations and load the synthetic store
dataset (1,000 customers, ~4,500 orders, tickets, payments, returns). Nothing else needs
to be run by hand.

| service | port | health |
|---|---|---|
| Web UI | 8080 | `/` |
| Lineage API | 8002 | `/healthz`, `/readyz`, `/metrics` |
| Support agent API | 8001 | `/healthz`, `/readyz`, `/metrics` |
| Postgres | 5433 | databases `lineage` and `store` |

Interactive API docs: `http://localhost:8002/docs` and `http://localhost:8001/docs`.

### Without a model key

Set `LLM_MODE=recorded`. Everything works except starting new conversations (the API
returns `503 llm_unavailable`); captured runs can still be inspected and replayed in
recorded mode.

---

## Using it

1. **Conversations:** pick a customer and send a message. The agent answers using its tools,
   and every answer links to its captured run.
2. **Run view:** the step timeline (model calls and tool calls with inputs and outputs).
   Every identifier is clickable.
3. **Lineage:** click a value (e.g. an order ID in a refund offer) to see the step where it
   **entered** the run, the alternatives it was chosen over, and every later step that used it.
4. **Replay:**
   - **Recorded:** tool and model outputs are served from the capture, so the run is reproduced exactly as it happened.
   - **Live:** tools are re-executed against current data (writes are dry-run), so you see what the agent would do *now*.

   The diff view highlights the first step where the two runs diverge.
5. **Connections:** Shopify, Zendesk and Stripe are connected. WooCommerce, Gorgias,
   Intercom, Freshdesk and ShipStation can be configured; their adapters are not in this release.
6. **Settings:** retention, SDK API keys, users.

### The reference incident

Customer `sara.qureshi@example.com` has two orders for the same shoes. The older one
(`ORD-7781`) was already returned and refunded; processing that return bumped its
`updated_at`. An order lookup that sorts by `updated_at` therefore picks the wrong order,
and every later step "succeeds" on it. The shipped agent sorts by `placed_at`, asks when
several orders match, and checks the returns ledger. The original defect is kept behind
`LEGACY_ORDER_LOOKUP=true`, which is used by the regression test (`tests/test_incident.py`)
to prove Lineage diagnoses this class of failure.

---

## Instrumenting your own agent

```python
from lineage_sdk import HttpTransport, Recorder, Redactor

rec = Recorder(
    service="my-agent",
    transport=HttpTransport("https://lineage.internal", api_key="lk_..."),
    redactor=Redactor(secret_key),             # PII is redacted before it leaves the process
)

with rec.run("refund-request", input={"message": msg}, attrs={"ticket_id": t}) as run:
    with run.step("llm", "chat.completions", input={"messages": messages}) as step:
        step.output(response)
    with run.step("tool", "lookup_order", input=args) as step:
        rows = db.find_orders(...)
        step.note_candidates(chosen=rows[0].id, alternatives=[r.id for r in rows[1:]],
                             reason="ORDER BY placed_at DESC")   # optional, powers "chosen over ..."
        step.output(result)
    run.output(answer)

rec.close()  # flush on shutdown (bounded to 5 s)
```

- **Fail-open:** events go through a bounded in-memory queue to a background exporter.
  `send()` never blocks or raises. If the Lineage service is down, events are retried with
  backoff, then dropped and counted (`lineage_dropped_events_total` on the agent's `/metrics`).
- **Redaction:** emails, phone numbers, Luhn-valid card numbers and PII-named fields
  (`email`, `phone`, `full_name`, `address`, …) become stable tokens like `<EMAIL:3fa1c2d9e0>`.
  The same value always gives the same token, so lineage still links across redacted values.
- **Recorded replay** with `RecordedRun` serves captured outputs back to your tool and model
  calls and raises `ReplayMissError` rather than falling through to live execution.

Create API keys in **Settings → API keys**. The key in `LINEAGE_API_KEY` is registered
automatically on start.

---

## Configuration

All configuration is through environment variables (`.env`). See `.env.example`.

| variable | service | default | notes |
|---|---|---|---|
| `OPENROUTER_API_KEY` | agent | none | required when `LLM_MODE=live` |
| `LLM_MODEL` | agent | `anthropic/claude-sonnet-5` | any OpenRouter model with tool calling |
| `LLM_FALLBACK_MODEL` | agent | none | used after the primary fails with 408/409/429/5xx/timeout |
| `LLM_MODE` | agent | `live` | `live` or `recorded` |
| `LLM_TIMEOUT_S` | agent | `30` | per request |
| `REFUND_APPROVAL_THRESHOLD` | agent | `200` | offers above this are `needs_approval` |
| `LINEAGE_API_KEY` | both | none | SDK → ingest key; registered by the lineage service on start |
| `LINEAGE_REDACTION_KEY` | agent | none (required) | HMAC secret for PII tokens; keep it stable |
| `LINEAGE_RETENTION_DAYS` | lineage | `30` | also editable in Settings; pinned runs are kept |
| `SERVICE_TOKEN` | both | none (required) | service-to-service calls and the carrier webhook |
| `SESSION_SECRET` | both | none (required) | signs operator sessions, shared so one login covers both APIs |
| `CONNECTOR_ENCRYPTION_KEY` | agent | derived from `SERVICE_TOKEN` | AES-256-GCM key material for stored connector credentials |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | lineage | none | first admin user, created on start if missing |
| `COOKIE_SECURE` | lineage | `false` | set `true` behind HTTPS |

Rotating `LINEAGE_REDACTION_KEY` changes all future tokens, so values captured before and
after the rotation won't link to each other.

---

## Operations

- **Migrations** run automatically on service start (Alembic). To run them by hand:
  `docker compose exec support-agent python -m support_agent.cli migrate`.
- **Store data** is seeded on first start only. To reset it:
  `docker compose exec support-agent python -m support_agent.cli reseed`.
- **Carrier webhook** (ShipStation-shaped): `POST /v1/webhooks/carrier` on the agent with
  `Authorization: Bearer $SERVICE_TOKEN` and
  `{"tracking_number", "status", "location", "occurred_at"}`.
- **Logs** are structured JSON on stdout with `request_id` (also returned as `X-Request-ID`).
- **Metrics:** Prometheus text format on `/metrics` for both APIs.
- **Retention** runs every 6 hours, deleting unpinned runs older than the configured number of days.
- **Backups:** the `pgdata` volume holds both databases; back up with `pg_dump` per database.

### Security notes

- Operator passwords are hashed with argon2, and API keys are stored as SHA-256 hashes (shown once on creation).
- Sessions are HMAC-signed, httpOnly, SameSite=Strict cookies. Set `COOKIE_SECURE=true` behind TLS.
- The model never controls identity or money: customer and ticket IDs are injected by the
  runtime, orders are checked against the current customer, and refund amounts come from the
  order, not the model.
- Replays run with dry-run payment and helpdesk connectors and never write to external systems.
- Recorded replays serve the *redacted* capture, so the model never sees raw PII during replay.

---

## Development

```sh
uv sync                                   # Python 3.12 workspace (SDK + both services)
uv run pytest                             # needs Docker: tests start a disposable Postgres
uv run ruff check . && uv run ruff format --check .
uv run mypy packages/lineage_sdk/src services/lineage/src services/support_agent/src

cd web && pnpm install && pnpm dev        # UI on :5173, proxies /api/lineage → :8002, /api/agent → :8001
```

Run the services locally against the Compose Postgres:

```sh
docker compose up -d postgres
set -a; source .env; set +a
DATABASE_URL=postgresql+asyncpg://lineage:lineage@localhost:5433/lineage \
  uv run uvicorn lineage_service.app:create_app --factory --port 8002
DATABASE_URL=postgresql+asyncpg://store:store@localhost:5433/store LINEAGE_URL=http://localhost:8002 \
  uv run uvicorn support_agent.app:create_app --factory --port 8001
```

### Layout

```
packages/lineage_sdk/     capture SDK (recorder, redaction, values, transport, replay)
services/lineage/         ingest, lineage query, diff, replay orchestration, auth, retention
services/support_agent/   agent loop, tools, connectors (mock adapters), synthetic data, API
web/                      React UI
tests/                    cross-service tests, incl. the reference-incident regression test
```
