"""Event export. Fail-open by design (SPEC §5.1).

`HttpTransport` hands events to a background thread through a bounded queue:
`send()` never blocks and never raises. Batches are POSTed to the lineage
ingest API with retries; when retries are exhausted or the queue is full, events
are dropped and counted. The instrumented agent is never slowed or broken.
"""

import logging
import queue
import threading
import time
from typing import Any, Protocol

import httpx

log = logging.getLogger("lineage_sdk")


class Transport(Protocol):
    def send(self, event: dict[str, Any]) -> None: ...
    def flush(self, timeout: float = 5.0) -> bool: ...
    def close(self, timeout: float = 5.0) -> None: ...


class Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sent = 0
        self.dropped = 0
        self.failed_batches = 0

    def add(self, **kw: int) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)


class HttpTransport:
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        max_queue: int = 10_000,
        batch_size: int = 500,
        flush_interval: float = 1.0,
        max_retries: int = 3,
        backoff: float = 0.5,
        timeout: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.stats = Stats()
        self._url = endpoint.rstrip("/") + "/v1/ingest"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue)
        self._batch_size = batch_size
        self._interval = flush_interval
        self._max_retries = max_retries
        self._backoff = backoff
        self._client = client or httpx.Client(timeout=timeout)
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._thread = threading.Thread(target=self._loop, name="lineage-exporter", daemon=True)
        self._thread.start()

    def send(self, event: dict[str, Any]) -> None:
        try:
            self._idle.clear()
            self._queue.put_nowait(event)
        except queue.Full:
            self.stats.add(dropped=1)

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty() and self._idle.is_set():
                return True
            time.sleep(0.02)
        return False

    def close(self, timeout: float = 5.0) -> None:
        self.flush(timeout)
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._client.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            batch = self._take_batch()
            if batch:
                self._ship(batch)
            if self._queue.empty():
                self._idle.set()

    def _take_batch(self) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        deadline = time.monotonic() + self._interval
        while len(batch) < self._batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(self._queue.get(timeout=min(remaining, 0.05) if batch else remaining))
            except queue.Empty:
                if batch or self._stop.is_set():
                    break
        return batch

    def _ship(self, batch: list[dict[str, Any]]) -> None:
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.post(self._url, json={"events": batch}, headers=self._headers)
                if resp.status_code < 300:
                    self.stats.add(sent=len(batch))
                    return
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    log.error("lineage ingest rejected batch: %s %s", resp.status_code, resp.text[:200])
                    break  # not retryable
            except Exception as exc:  # network errors, timeouts
                log.warning("lineage ingest failed (attempt %d): %s", attempt + 1, exc)
            if attempt < self._max_retries and not self._stop.is_set():
                time.sleep(self._backoff * 2**attempt)
        self.stats.add(dropped=len(batch), failed_batches=1)


class MemoryTransport:
    """Collects events in memory (tests, and in-process consumers)."""

    def __init__(self) -> None:
        self.stats = Stats()
        self.events: list[dict[str, Any]] = []

    def send(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        self.stats.add(sent=1)

    def flush(self, timeout: float = 5.0) -> bool:
        return True

    def close(self, timeout: float = 5.0) -> None:
        pass


class NullTransport(MemoryTransport):
    """Capture disabled."""

    def send(self, event: dict[str, Any]) -> None:
        pass
