"""Support agent HTTP API (SPEC §6)."""

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from lineage_sdk import (
    HttpTransport,
    NullTransport,
    RecordedRun,
    Recorder,
    Redactor,
    ReplayMissError,
)
from lineage_sdk.transport import Transport
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.agent.loop import SERVICE, Turn, run_turn
from support_agent.agent.tools import ToolContext
from support_agent.config import Settings, get_settings
from support_agent.connectors.base import CommerceConnector, HelpdeskConnector, PaymentsConnector
from support_agent.connectors.registry import (
    ADAPTERS,
    CATALOG,
    apply_carrier_event,
    build_connectors,
    connector_statuses,
    save_connector_config,
)
from support_agent.data import models as m
from support_agent.db import Database
from support_agent.llm.client import (
    LLM,
    LLMUnavailable,
    OpenRouterLLM,
    RecordedLLM,
    UnconfiguredLLM,
)
from support_agent.logging import configure_logging, request_id_var
from support_agent.migrate import upgrade_async

log = logging.getLogger("support_agent")
SESSION_COOKIE = "lineage_session"

HTTP_REQUESTS = Counter("agent_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram("agent_http_request_seconds", "HTTP latency", ["route"])
TURNS = Counter("agent_turns_total", "Agent turns", ["status", "mode"])
CAPTURE_SENT = Gauge("lineage_sent_events_total", "Lineage events delivered")
CAPTURE_DROPPED = Gauge("lineage_dropped_events_total", "Lineage events dropped (fail-open)")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


def build_recorder(settings: Settings) -> Recorder:
    redactor = Redactor(settings.lineage_redaction_key.get_secret_value())
    transport: Transport
    if settings.lineage_url and settings.lineage_api_key:
        transport = HttpTransport(settings.lineage_url, settings.lineage_api_key.get_secret_value())
    else:
        log.warning("LINEAGE_URL/LINEAGE_API_KEY not set: runs will not be captured")
        transport = NullTransport()
    return Recorder(service=SERVICE, transport=transport, redactor=redactor)


def build_llm(settings: Settings) -> LLM:
    if settings.llm_mode == "recorded" or settings.openrouter_api_key is None:
        return UnconfiguredLLM()
    return OpenRouterLLM(
        api_key=settings.openrouter_api_key.get_secret_value(),
        model=settings.llm_model,
        fallback_model=settings.llm_fallback_model,
        base_url=settings.openrouter_base_url,
        timeout_s=settings.llm_timeout_s,
    )


def create_app(
    settings: Settings | None = None,
    *,
    recorder: Recorder | None = None,
    llm: LLM | None = None,
    now: datetime | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    db = Database(settings.database_url)
    rec = recorder or build_recorder(settings)
    model = llm or build_llm(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await upgrade_async(settings.database_url)
        from support_agent.cli import seed

        counts = await seed(settings.database_url)
        if counts:
            log.info("seeded synthetic store data", extra={"event": "seed"})
        yield
        rec.close()
        if hasattr(model, "aclose"):
            await model.aclose()
        await db.dispose()

    app = FastAPI(title="Kestrel Support Agent", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        token = request_id_var.set(rid)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        path = getattr(request.scope.get("route"), "path", "unmatched")
        HTTP_REQUESTS.labels(request.method, path, response.status_code).inc()
        HTTP_LATENCY.labels(path).observe(time.perf_counter() - t0)
        response.headers["X-Request-ID"] = rid
        if path not in ("/healthz", "/readyz", "/metrics"):
            log.info(
                "%s %s",
                request.method,
                request.url.path,
                extra={
                    "status_code": response.status_code,
                    "path": path,
                    "duration_ms": int((time.perf_counter() - t0) * 1000),
                },
            )
        return response

    def error(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": code, "message": message, "request_id": request_id_var.get()}}, status_code=status
        )

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error(exc.status, exc.code, exc.message)

    @app.exception_handler(LLMUnavailable)
    async def _llm_error(_: Request, exc: LLMUnavailable) -> JSONResponse:
        log.error("LLM unavailable: %s", exc)
        return error(503, "llm_unavailable", f"The language model is unavailable: {exc}")

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return error(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error(
            422, "validation_error", "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return error(500, "internal_error", "Internal server error")

    # --- auth ---------------------------------------------------------------

    async def session() -> AsyncIterator[AsyncSession]:
        async with db.sessionmaker() as s:
            yield s

    Session = Annotated[AsyncSession, Depends(session)]

    def _bearer_ok(request: Request) -> bool:
        raw = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        return bool(raw) and hmac.compare_digest(raw.encode(), settings.service_token.get_secret_value().encode())

    def _session_ok(request: Request) -> bool:
        """Operators log in on the lineage service; the signed session cookie is shared (same secret)."""
        token = request.cookies.get(SESSION_COOKIE)
        if not token or not settings.session_secret:
            return False
        try:
            payload, sig = token.rsplit(".", 1)
            expected = hmac.new(
                settings.session_secret.get_secret_value().encode(), payload.encode(), hashlib.sha256
            ).hexdigest()
            data = json.loads(base64.urlsafe_b64decode(payload))
        except ValueError:
            return False
        return hmac.compare_digest(sig, expected) and data.get("exp", 0) > time.time()

    async def operator(request: Request) -> None:
        if not (_session_ok(request) or _bearer_ok(request)):
            raise ApiError(401, "unauthenticated", "Login required")

    async def service(request: Request) -> None:
        if not _bearer_ok(request):
            raise ApiError(401, "unauthenticated", "Service token required")

    Operator = Annotated[None, Depends(operator)]
    Service = Annotated[None, Depends(service)]

    # --- health & metrics ---------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(s: Session) -> dict[str, Any]:
        try:
            await s.execute(text("SELECT 1"))
        except Exception as exc:
            raise ApiError(503, "db_unavailable", "Database not reachable") from exc
        return {
            "status": "ready",
            "llm_mode": settings.llm_mode,
            "model": settings.llm_model,
            "capture": type(rec.transport).__name__,
        }

    @app.get("/metrics")
    async def metrics() -> Response:
        stats = getattr(rec.transport, "stats", None)
        if stats:
            CAPTURE_SENT.set(stats.sent)
            CAPTURE_DROPPED.set(stats.dropped)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # --- conversations ------------------------------------------------------

    class StartBody(BaseModel):
        customer_email: EmailStr
        message: str = Field(min_length=1, max_length=4000)

    class MessageBody(BaseModel):
        message: str = Field(min_length=1, max_length=4000)

    def tool_ctx(conv: m.Conversation, *, dry_run: bool = False) -> ToolContext:
        return ToolContext(
            customer_id=conv.customer_id,
            ticket_id=conv.ticket_id,
            conversation_id=conv.id,
            connectors=build_connectors(db.sessionmaker, settings, dry_run=dry_run),
            refund_approval_threshold=settings.refund_approval_threshold,
            legacy_order_lookup=settings.legacy_order_lookup,
            now=now,
        )

    async def take_turn(s: AsyncSession, conv: m.Conversation, message: str) -> dict[str, Any]:
        history = [{"role": msg.role, "content": msg.content} for msg in conv.messages]
        s.add(m.ConversationMessage(conversation_id=conv.id, role="customer", content=message))
        await s.commit()
        try:
            result = await run_turn(
                Turn(message=message, history=history, ctx=tool_ctx(conv)),
                llm=model,
                recorder=rec,
                max_iterations=settings.llm_max_iterations,
            )
        except LLMUnavailable:
            TURNS.labels("llm_unavailable", "live").inc()
            raise
        TURNS.labels(result.status, "live").inc()
        s.add(
            m.ConversationMessage(conversation_id=conv.id, role="assistant", content=result.reply, run_id=result.run_id)
        )
        await s.commit()
        return {
            "conversation_id": conv.id,
            "ticket_id": conv.ticket_id,
            "reply": result.reply,
            "run_id": result.run_id,
            "status": result.status,
        }

    @app.post("/v1/conversations", status_code=201)
    async def start_conversation(body: StartBody, s: Session, _: Operator) -> dict[str, Any]:
        connectors = build_connectors(db.sessionmaker, settings)
        customer = await connectors.commerce.find_customer(email=body.customer_email)
        if customer is None:
            raise ApiError(404, "customer_not_found", "No customer with that email")
        ticket = await connectors.helpdesk.create_ticket(
            customer_id=customer["customer_id"], subject=body.message[:120], body=body.message
        )
        conv = m.Conversation(id=str(uuid.uuid4()), customer_id=customer["customer_id"], ticket_id=ticket["ticket_id"])
        s.add(conv)
        await s.commit()
        await s.refresh(conv, ["messages"])
        return await take_turn(s, conv, body.message)

    @app.post("/v1/conversations/{conversation_id}/messages")
    async def continue_conversation(conversation_id: str, body: MessageBody, s: Session, _: Operator) -> dict[str, Any]:
        conv = await s.get(m.Conversation, conversation_id)
        if conv is None:
            raise ApiError(404, "not_found", "Conversation not found")
        return await take_turn(s, conv, body.message)

    @app.get("/v1/conversations")
    async def list_conversations(s: Session, _: Operator, limit: int = 50) -> list[dict[str, Any]]:
        rows = (
            await s.execute(
                select(m.Conversation, m.Customer)
                .join(m.Customer)
                .order_by(m.Conversation.created_at.desc())
                .limit(min(limit, 200))
            )
        ).all()
        return [
            {
                "id": c.id,
                "customer_id": c.customer_id,
                "customer_name": cu.name,
                "customer_email": cu.email,
                "ticket_id": c.ticket_id,
                "created_at": c.created_at,
                "messages": [
                    {"role": x.role, "content": x.content, "run_id": x.run_id, "created_at": x.created_at}
                    for x in c.messages
                ],
            }
            for c, cu in rows
        ]

    @app.get("/v1/customers")
    async def search_customers(s: Session, _: Operator, q: str = "", limit: int = 20) -> list[dict[str, Any]]:
        stmt = select(m.Customer)
        if q:
            like = f"%{q.lower()}%"
            stmt = stmt.where(
                func.lower(m.Customer.name).like(like)
                | func.lower(m.Customer.email).like(like)
                | (m.Customer.id == q.upper())
            )
        rows = (await s.execute(stmt.order_by(m.Customer.id).limit(min(limit, 100)))).scalars().all()
        return [{"id": c.id, "name": c.name, "email": c.email} for c in rows]

    # --- connectors ---------------------------------------------------------

    @app.get("/v1/connectors")
    async def connectors_list(s: Session, _: Operator) -> list[dict[str, Any]]:
        return [asdict(c) for c in await connector_statuses(s)]

    @app.post("/v1/connectors/{name}/test")
    async def connector_test(name: str, _: Operator) -> dict[str, Any]:
        if name not in CATALOG:
            raise ApiError(404, "not_found", "Unknown connector")
        if name not in ADAPTERS:
            return {"name": name, "ok": False, "message": "Not connected: no adapter installed for this connector yet"}
        c = build_connectors(db.sessionmaker, settings)
        target: CommerceConnector | HelpdeskConnector | PaymentsConnector
        if name == "shopify":
            target = c.commerce
        elif name == "zendesk":
            target = c.helpdesk
        else:
            target = c.payments
        t0 = time.perf_counter()
        try:
            ok = await target.health()
        except Exception as exc:
            return {"name": name, "ok": False, "message": str(exc)}
        return {"name": name, "ok": ok, "latency_ms": int((time.perf_counter() - t0) * 1000), "message": "Connected"}

    class ConnectorConfigBody(BaseModel):
        settings: dict[str, str] = Field(default_factory=dict)

    @app.put("/v1/connectors/{name}")
    async def connector_configure(name: str, body: ConnectorConfigBody, s: Session, _: Operator) -> dict[str, Any]:
        if name not in CATALOG:
            raise ApiError(404, "not_found", "Unknown connector")
        # Never echo secrets back; store only non-empty keys.
        await save_connector_config(s, name, {k: v for k, v in body.settings.items() if v})
        return {"name": name, "saved": True, "enabled": name in ADAPTERS}

    # --- carrier webhook (ShipStation-shaped) --------------------------------

    class CarrierEvent(BaseModel):
        tracking_number: str = Field(min_length=4, max_length=40)
        status: Literal["label_created", "in_transit", "delayed", "delivered", "lost"]
        location: str = Field(default="", max_length=80)
        occurred_at: datetime | None = None

    @app.post("/v1/webhooks/carrier")
    async def carrier_webhook(body: CarrierEvent, s: Session, _: Service) -> dict[str, Any]:
        out = await apply_carrier_event(
            s,
            tracking_number=body.tracking_number,
            status=body.status,
            location=body.location,
            occurred_at=body.occurred_at or datetime.now(UTC),
        )
        if out is None:
            raise ApiError(404, "unknown_tracking_number", "No shipment with that tracking number")
        return out

    # --- replay (called by the lineage service) -----------------------------

    class ReplayBody(BaseModel):
        mode: Literal["live", "recorded"]
        run: dict[str, Any]

    @app.post("/v1/replay")
    async def replay(body: ReplayBody, s: Session, _: Service) -> dict[str, Any]:
        run, attrs = body.run, body.run.get("attrs") or {}
        conv = await s.get(m.Conversation, attrs.get("conversation_id", ""))
        if conv is None:
            raise ApiError(404, "not_found", "The run's conversation no longer exists")
        ctx = tool_ctx(conv, dry_run=True)  # replays never write to external systems
        ctx.legacy_order_lookup = bool(attrs.get("legacy_order_lookup", ctx.legacy_order_lookup))
        run_input = run.get("input") or {}
        turn = Turn(message=run_input.get("message", ""), history=run_input.get("history") or [], ctx=ctx)
        recording = RecordedRun(run, redact=rec.redact) if body.mode == "recorded" else None
        try:
            result = await run_turn(
                turn,
                llm=RecordedLLM(recording) if recording else model,
                recorder=rec,
                max_iterations=settings.llm_max_iterations,
                recording=recording,
                replay_of=run["id"],
                replay_mode=body.mode,
            )
        except ReplayMissError as exc:
            raise ApiError(409, "replay_miss", str(exc)) from exc
        finally:
            rec.transport.flush(5)
        TURNS.labels(result.status, f"replay:{body.mode}").inc()
        return {"run_id": result.run_id, "reply": result.reply, "status": result.status}

    return app
