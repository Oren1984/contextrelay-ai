"""Lightweight observability: consistent key=value logs and in-process counters.

Logs carry identifiers, counts and durations only - never titles, notes or other
handover content.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field

LOGGER_NAME = "contextrelay"


def get_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(os.environ.get("CONTEXTRELAY_LOG_LEVEL", "INFO").upper())
        logger.propagate = False
    return logger


def log_event(event: str, level: int = logging.INFO, **fields: object) -> None:
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    get_logger().log(level, f"event={event} {details}".rstrip())


@dataclass
class Metrics:
    analyses_succeeded: int = 0
    analyses_failed: int = 0
    last_duration_ms: float = 0.0
    total_duration_ms: float = 0.0
    last_gap_count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_success(self, duration_ms: float, gap_count: int) -> None:
        with self._lock:
            self.analyses_succeeded += 1
            self.last_duration_ms = duration_ms
            self.total_duration_ms += duration_ms
            self.last_gap_count = gap_count

    def record_failure(self) -> None:
        with self._lock:
            self.analyses_failed += 1

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            runs = self.analyses_succeeded
            return {
                "analyses_succeeded": self.analyses_succeeded,
                "analyses_failed": self.analyses_failed,
                "last_duration_ms": round(self.last_duration_ms, 2),
                "average_duration_ms": round(self.total_duration_ms / runs, 2) if runs else 0.0,
                "last_gap_count": self.last_gap_count,
            }


METRICS = Metrics()
