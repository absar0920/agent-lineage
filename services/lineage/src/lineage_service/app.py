"""Lineage service HTTP API (SPEC §6)."""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import String, cast, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from lineage_service import retention, security
from lineage_service.config import Settings, get_settings
from lineage_service.db import Database
from lineage_service.ingest import ingest
from lineage_service.logging import configure_logging, request_id_var
from lineage_service.migrate import upgrade_async
from lineage_service.models import ApiKey, AppSetting, Candidate, Run, Step, StepValue, User
from lineage_service.query import diff_steps, lineage

log = logging.getLogger("lineage")
COOKIE = "lineage_session"

HTTP_REQUESTS = Counter("lineage_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram("lineage_http_request_seconds", "HTTP latency", ["route"])
INGESTED = Counter("lineage_ingested_events_total", "Events applied", ["result"])


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    db = Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await upgrade_async(settings.database_url)
        async with db.sessionmaker() as s:
            if settings.admin_email and settings.admin_password:
                await security.ensure_user(s, settings.admin_email, settings.admin_password.get_secret_value())
            if settings.lineage_api_key:
                raw = settings.lineage_api_key.get_secret_value()
                if await security.verify_api_key(s, raw) is None:
                    await security.create_api_key(s, "bootstrap (LINEAGE_API_KEY)", raw)
        task = asyncio.create_task(retention.retention_loop(db.sessionmaker, settings.lineage_retention_days))
        app.state.http = httpx.AsyncClient(timeout=120)
        log.info("lineage service started")
        yield
        task.cancel()
        await app.state.http.aclose()
        await db.dispose()

    app = FastAPI(title="agent-lineage", version="1.0.0", lifespan=lifespan)
    app.state.db = db
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- middleware & errors ------------------------------------------------

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        token = request_id_var.set(rid)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
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

    # --- dependencies -------------------------------------------------------

    async def session() -> AsyncIterator[AsyncSession]:
        async with db.sessionmaker() as s:
            yield s

    Session = Annotated[AsyncSession, Depends(session)]

    async def current_user(request: Request, s: Session) -> User:
        token = request.cookies.get(COOKIE)
        data = security.read_session(token, settings.session_secret.get_secret_value()) if token else None
        user = await s.get(User, data["uid"]) if data else None
        if user is None:
            raise ApiError(401, "unauthenticated", "Login required")
        return user

    async def api_key(request: Request, s: Session) -> ApiKey:
        header = request.headers.get("authorization", "")
        raw = header.removeprefix("Bearer ").strip()
        key = await security.verify_api_key(s, raw) if raw else None
        if key is None:
            raise ApiError(401, "invalid_api_key", "Missing or invalid API key")
        return key

    UserDep = Annotated[User, Depends(current_user)]

    # --- health & metrics ---------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(s: Session) -> dict[str, str]:
        try:
            await s.execute(text("SELECT 1"))
        except Exception as exc:
            raise ApiError(503, "db_unavailable", "Database not reachable") from exc
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # --- auth ---------------------------------------------------------------

    class LoginBody(BaseModel):
        email: EmailStr
        password: str = Field(min_length=1, max_length=200)

    @app.post("/v1/auth/login")
    async def login(body: LoginBody, response: Response, s: Session) -> dict[str, Any]:
        user = await security.authenticate(s, body.email, body.password)
        if user is None:
            raise ApiError(401, "invalid_credentials", "Invalid email or password")
        token = security.sign_session(
            user.id, settings.session_secret.get_secret_value(), settings.session_ttl_hours * 3600
        )
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="strict",
            secure=settings.cookie_secure,
            max_age=settings.session_ttl_hours * 3600,
        )
        return {"email": user.email, "role": user.role}

    @app.post("/v1/auth/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/v1/auth/me")
    async def me(user: UserDep) -> dict[str, str]:
        return {"email": user.email, "role": user.role}

    # --- ingest -------------------------------------------------------------

    class IngestBody(BaseModel):
        events: list[dict[str, Any]] = Field(max_length=2000)

    @app.post("/v1/ingest", status_code=202)
    async def ingest_events(body: IngestBody, s: Session, _: Annotated[ApiKey, Depends(api_key)]) -> dict[str, int]:
        for e in body.events:
            if not isinstance(e.get("event_id"), str) or not isinstance(e.get("run_id"), str):
                raise ApiError(422, "invalid_event", "every event needs string event_id and run_id")
        try:
            result = await ingest(s, body.events)
        except ValueError as exc:
            raise ApiError(422, "invalid_event", str(exc)) from exc
        INGESTED.labels("applied").inc(result["applied"])
        INGESTED.labels("duplicate").inc(result["skipped"])
        return result

    # --- runs ---------------------------------------------------------------

    @app.get("/v1/runs")
    async def list_runs(
        s: Session,
        _: UserDep,
        service: str | None = None,
        status: str | None = None,
        q: str | None = Query(None, max_length=200),
        since: datetime | None = None,
        replay_of: str | None = None,
        include_replays: bool = True,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        stmt = select(Run)
        if service:
            stmt = stmt.where(Run.service == service)
        if status:
            stmt = stmt.where(Run.status == status)
        if since:
            stmt = stmt.where(Run.started_at >= since)
        if replay_of:
            stmt = stmt.where(Run.replay_of == replay_of)
        elif not include_replays:
            stmt = stmt.where(Run.replay_of.is_(None))
        if q:
            like = f"%{q}%"
            stmt = stmt.where(
                or_(
                    Run.id == q,
                    Run.name.ilike(like),
                    cast(Run.input, String).ilike(like),
                    cast(Run.output, String).ilike(like),
                    cast(Run.attrs, String).ilike(like),
                )
            )
        total = (await s.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        runs = (await s.execute(stmt.order_by(Run.started_at.desc()).limit(limit).offset(offset))).scalars().all()
        counts = dict(
            (
                await s.execute(
                    select(Step.run_id, func.count())
                    .where(Step.run_id.in_([r.id for r in runs]), Step.kind.in_(["llm", "tool"]))
                    .group_by(Step.run_id)
                )
            ).all()
        )
        return {"total": total, "runs": [_run_summary(r, counts.get(r.id, 0)) for r in runs]}

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str, s: Session, _: UserDep) -> dict[str, Any]:
        run = await _load_run(s, run_id)
        values = (await s.execute(select(StepValue).where(StepValue.run_id == run_id))).scalars().all()
        cands = (await s.execute(select(Candidate).where(Candidate.run_id == run_id))).scalars().all()
        by_step: dict[str, list[dict[str, Any]]] = {}
        for v in values:
            by_step.setdefault(v.step_id, []).append(
                {"direction": v.direction, "path": v.path, "fingerprint": v.fingerprint, "display": v.display}
            )
        cand_by_step: dict[str, list[dict[str, Any]]] = {}
        for c in cands:
            cand_by_step.setdefault(c.step_id, []).append(
                {"chosen": c.chosen, "alternatives": c.alternatives, "reason": c.reason}
            )
        replays = (await s.execute(select(Run).where(Run.replay_of == run_id).order_by(Run.started_at))).scalars().all()
        return {
            **_run_summary(run, sum(1 for st in run.steps if st.kind in ("llm", "tool"))),
            "input": run.input,
            "output": run.output,
            "error": run.error,
            "replays": [_run_summary(r, 0) for r in replays],
            "steps": [
                {
                    "id": st.id,
                    "seq": st.seq,
                    "kind": st.kind,
                    "name": st.name,
                    "status": st.status,
                    "input": st.input,
                    "output": st.output,
                    "error": st.error,
                    "started_at": st.started_at,
                    "duration_ms": st.duration_ms,
                    "values": by_step.get(st.id, []),
                    "candidates": cand_by_step.get(st.id, []),
                }
                for st in run.steps
            ],
        }

    @app.get("/v1/runs/{run_id}/lineage")
    async def get_lineage(
        run_id: str, s: Session, _: UserDep, fingerprint: str = Query(pattern=r"^[0-9a-f]{16}$")
    ) -> dict[str, Any]:
        await _load_run(s, run_id, with_steps=False)
        return (await lineage(s, run_id, fingerprint)).to_dict()

    @app.get("/v1/runs/{a}/diff/{b}")
    async def diff(a: str, b: str, s: Session, _: UserDep) -> dict[str, Any]:
        ra, rb = await _load_run(s, a), await _load_run(s, b)
        return {"a": _run_summary(ra, 0), "b": _run_summary(rb, 0), **diff_steps(ra.steps, rb.steps)}

    class ReplayBody(BaseModel):
        mode: Literal["live", "recorded"]

    @app.post("/v1/runs/{run_id}/replay")
    async def replay(run_id: str, body: ReplayBody, request: Request, s: Session, _: UserDep) -> dict[str, Any]:
        run = await _load_run(s, run_id)
        if run.replay_of:
            raise ApiError(409, "replay_of_replay", "Replay the original run instead")
        payload = {
            "mode": body.mode,
            "run": {
                "id": run.id,
                "name": run.name,
                "service": run.service,
                "input": run.input,
                "attrs": run.attrs,
                "steps": [
                    {
                        "seq": st.seq,
                        "kind": st.kind,
                        "name": st.name,
                        "status": st.status,
                        "input": st.input,
                        "output": st.output,
                        "error": st.error,
                    }
                    for st in run.steps
                    if st.kind in ("llm", "tool")
                ],
            },
        }
        try:
            resp = await request.app.state.http.post(
                f"{settings.support_agent_url}/v1/replay",
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.service_token.get_secret_value()}",
                    "X-Request-ID": request_id_var.get() or "",
                },
            )
        except httpx.HTTPError as exc:
            raise ApiError(502, "agent_unreachable", f"Support agent unreachable: {exc}") from exc
        if resp.status_code >= 400:
            detail = resp.json().get("error", {}).get("message", resp.text) if resp.content else resp.text
            raise ApiError(502, "replay_failed", f"Replay failed: {detail}")
        new_id = resp.json()["run_id"]
        # The replayed run arrives through ingest; wait briefly so the UI can open it immediately.
        for _attempt in range(50):
            if (await s.execute(select(Run.status).where(Run.id == new_id))).scalar_one_or_none() not in (
                None,
                "running",
            ):
                break
            await asyncio.sleep(0.1)
        return {"run_id": new_id, "replay_of": run_id, "mode": body.mode}

    class PinBody(BaseModel):
        pinned: bool

    @app.post("/v1/runs/{run_id}/pin")
    async def pin(run_id: str, body: PinBody, s: Session, _: UserDep) -> dict[str, Any]:
        await _load_run(s, run_id, with_steps=False)
        await s.execute(update(Run).where(Run.id == run_id).values(pinned=body.pinned))
        await s.commit()
        return {"run_id": run_id, "pinned": body.pinned}

    # --- settings, API keys, users -----------------------------------------

    @app.get("/v1/settings")
    async def get_app_settings(s: Session, _: UserDep) -> dict[str, int]:
        return await retention.list_settings(s, settings.lineage_retention_days)

    class SettingsBody(BaseModel):
        retention_days: int = Field(ge=1, le=3650)

    @app.put("/v1/settings")
    async def put_app_settings(body: SettingsBody, s: Session, user: UserDep) -> dict[str, int]:
        _require_admin(user)
        row = await s.get(AppSetting, retention.SETTING_KEY)
        if row is None:
            s.add(AppSetting(key=retention.SETTING_KEY, value=body.retention_days))
        else:
            row.value = body.retention_days
        await s.commit()
        return {"retention_days": body.retention_days}

    @app.get("/v1/api-keys")
    async def list_keys(s: Session, _: UserDep) -> list[dict[str, Any]]:
        keys = (await s.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))).scalars().all()
        return [
            {
                "id": k.id,
                "name": k.name,
                "prefix": k.prefix,
                "created_at": k.created_at,
                "last_used_at": k.last_used_at,
                "revoked_at": k.revoked_at,
            }
            for k in keys
        ]

    class KeyBody(BaseModel):
        name: str = Field(min_length=1, max_length=80)

    @app.post("/v1/api-keys", status_code=201)
    async def create_key(body: KeyBody, s: Session, user: UserDep) -> dict[str, Any]:
        _require_admin(user)
        key, raw = await security.create_api_key(s, body.name)
        return {"id": key.id, "name": key.name, "prefix": key.prefix, "key": raw}

    @app.delete("/v1/api-keys/{key_id}")
    async def revoke_key(key_id: str, s: Session, user: UserDep) -> dict[str, bool]:
        _require_admin(user)
        key = await s.get(ApiKey, key_id)
        if key is None:
            raise ApiError(404, "not_found", "API key not found")
        key.revoked_at = func.now()
        await s.commit()
        return {"revoked": True}

    @app.get("/v1/users")
    async def list_users(s: Session, _: UserDep) -> list[dict[str, Any]]:
        users = (await s.execute(select(User).order_by(User.created_at))).scalars().all()
        return [{"id": u.id, "email": u.email, "role": u.role, "created_at": u.created_at} for u in users]

    class UserBody(BaseModel):
        email: EmailStr
        password: str = Field(min_length=12, max_length=200)
        role: Literal["admin", "viewer"] = "viewer"

    @app.post("/v1/users", status_code=201)
    async def create_user(body: UserBody, s: Session, user: UserDep) -> dict[str, str]:
        _require_admin(user)
        if (await s.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none():
            raise ApiError(409, "exists", "A user with this email already exists")
        u = await security.ensure_user(s, body.email, body.password, body.role)
        return {"id": u.id, "email": u.email, "role": u.role}

    return app


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise ApiError(403, "forbidden", "Admin role required")


async def _load_run(s: AsyncSession, run_id: str, with_steps: bool = True) -> Run:
    stmt = select(Run).where(Run.id == run_id)
    if with_steps:
        stmt = stmt.options(selectinload(Run.steps))
    run = (await s.execute(stmt)).scalar_one_or_none()
    if run is None:
        raise ApiError(404, "not_found", f"Run {run_id} not found")
    return run


def _run_summary(r: Run, step_count: int) -> dict[str, Any]:
    return {
        "id": r.id,
        "service": r.service,
        "name": r.name,
        "status": r.status,
        "started_at": r.started_at,
        "ended_at": r.ended_at,
        "attrs": r.attrs,
        "replay_of": r.replay_of,
        "replay_mode": r.replay_mode,
        "pinned": r.pinned,
        "step_count": step_count,
        "duration_ms": int((r.ended_at - r.started_at).total_seconds() * 1000) if r.ended_at else None,
    }
