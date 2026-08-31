"""Deliver a finished turn to its callback URL, retrying.

Delivery is best-effort by design: the result is ALWAYS also retrievable
from GET /v1/messages/{turnId}, so a dropped callback costs a poll, not a
turn.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from .metrics import Metrics
from .turns import Record

log = logging.getLogger("wrapper.webhook")


class Deliverer:
    def __init__(
        self,
        metrics: Metrics,
        *,
        retries: int = 3,
        backoff: float = 0.5,
        timeout: float = 10.0,
        default_url: str = "",
    ) -> None:
        self.metrics = metrics
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout
        self.default_url = default_url

    def _post(self, url: str, body: dict) -> None:
        httpx.post(url, json=body, timeout=self.timeout).raise_for_status()

    def deliver(self, record: Record) -> None:
        url = record.callback_url or self.default_url
        if not url:
            return
        body = record.full()
        for attempt in range(self.retries):
            if attempt:
                time.sleep(self.backoff * (2 ** (attempt - 1)))
            try:
                self._post(url, body)
                self.metrics.webhook_delivery_total.labels(result="ok").inc()
                return
            except Exception as exc:  # noqa: BLE001 - every failure retries alike
                log.warning(
                    "webhook attempt %d/%d to %s failed: %s",
                    attempt + 1,
                    self.retries,
                    url,
                    exc,
                )
        self.metrics.webhook_delivery_total.labels(result="dropped").inc()
        log.error("webhook dropped turn_id=%s url=%s", record.id, url)

    def deliver_async(self, record: Record) -> None:
        threading.Thread(target=self.deliver, args=(record,), daemon=True).start()
