"""Run both services as real HTTP servers (uvicorn in threads) for end-to-end tests."""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import uvicorn
from lineage_service.app import create_app as create_lineage
from lineage_service.config import Settings as LineageSettings
from support_agent.app import build_recorder
from support_agent.app import create_app as create_agent
from support_agent.config import Settings as AgentSettings

API_KEY = "lk_e2e_key_0123456789abcdefghijklmn"
SERVICE_TOKEN = "e2e-service-token-0123456789abcdefgh"
SESSION_SECRET = "e2e-session-secret-0123456789abcdefg"
REDACTION_KEY = "e2e-redaction-key-0123456789abcdefgh"
ADMIN = {"email": "admin@example.com", "password": "correct horse battery staple"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def serve(app: Any, port: int) -> Iterator[str]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("server failed to start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)


@dataclass
class Stack:
    lineage: httpx.Client  # logged-in operator session
    agent: httpx.Client  # service-token client
    agent_app: Any

    def wait_for_run(self, run_id: str, timeout: float = 10) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = self.lineage.get(f"/v1/runs/{run_id}")
            if r.status_code == 200 and r.json()["status"] != "running":
                return dict(r.json())
            time.sleep(0.1)
        raise TimeoutError(f"run {run_id} not ingested")


@contextmanager
def stack(
    pg_urls: dict[str, str], *, llm: Any, now: datetime, legacy: bool = False, lineage_up: bool = True
) -> Iterator[Stack]:
    lport, aport = free_port(), free_port()
    lineage_url, agent_url = f"http://127.0.0.1:{lport}", f"http://127.0.0.1:{aport}"
    lset = LineageSettings(
        database_url=pg_urls["lineage"],
        support_agent_url=agent_url,
        admin_email=ADMIN["email"],
        admin_password=ADMIN["password"],
        lineage_api_key=API_KEY,
        service_token=SERVICE_TOKEN,
        session_secret=SESSION_SECRET,
    )
    aset = AgentSettings(
        database_url=pg_urls["store"],
        llm_mode="recorded",
        legacy_order_lookup=legacy,
        lineage_url=lineage_url,
        lineage_api_key=API_KEY,
        lineage_redaction_key=REDACTION_KEY,
        service_token=SERVICE_TOKEN,
        session_secret=SESSION_SECRET,
        connector_latency_ms=(0, 0),
    )
    recorder = build_recorder(aset)
    if not lineage_up:
        recorder.transport._backoff = 0.01  # type: ignore[attr-defined]
    agent_app = create_agent(aset, recorder=recorder, llm=llm, now=now)
    agent_app.state.recorder = recorder
    with serve(agent_app, aport):
        if lineage_up:
            with (
                serve(create_lineage(lset), lport),
                httpx.Client(base_url=lineage_url, timeout=30) as lc,
                httpx.Client(
                    base_url=agent_url, timeout=30, headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}
                ) as ac,
            ):
                assert lc.post("/v1/auth/login", json=ADMIN).status_code == 200
                yield Stack(lineage=lc, agent=ac, agent_app=agent_app)
        else:
            with httpx.Client(
                base_url=agent_url, timeout=30, headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}
            ) as ac:
                yield Stack(lineage=None, agent=ac, agent_app=agent_app)  # type: ignore[arg-type]
