"""Access log + simple per-key rate limiting."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any

log = logging.getLogger("zen.access")


class AccessLog:
    def __init__(self, path: str, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self._lock = threading.Lock()
        if self.enabled:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            except OSError:
                pass

    def write(self, **fields: Any) -> None:
        if not self.enabled:
            return
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fields}
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:
                log.warning("cannot write access log %s: %s", self.path, exc)
        log.info(
            "%s key=%s model=%s stream=%s status=%s %.1fs tokens=%s",
            record.get("path"),
            record.get("key"),
            record.get("model"),
            record.get("stream"),
            record.get("status"),
            record.get("duration_ms", 0) / 1000.0,
            record.get("total_tokens"),
        )


class RateLimiter:
    """Sliding-window limiter, one window per API key name. 0 disables it."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, name: str) -> int | None:
        """Return None when allowed, else seconds to wait."""
        if self.per_minute <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            window = self._hits.setdefault(name, deque())
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= self.per_minute:
                return max(1, int(60 - (now - window[0])) + 1)
            window.append(now)
        return None
