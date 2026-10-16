"""Rate limiter a finestra scorrente usato dalle sorgenti HTTP/SFTP."""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Consente al massimo `max_calls` richieste ogni `period` secondi.

    Thread-safe: più job concorrenti che condividono la stessa istanza
    rispettano il stesso tetto. `acquire()` blocca fino a libero slot.
    """

    def __init__(self, max_calls: int, period: float = 60.0) -> None:
        self.max_calls = max(1, int(max_calls))
        self.period = float(period)
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, timeout: float | None = None) -> bool:
        """Attende fino a che non c'è slot libero. Restituisce False se scade."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= self.period:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_calls:
                    self._timestamps.append(now)
                    return True
                wait = self.period - (now - self._timestamps[0])
            if deadline is not None and time.monotonic() + wait > deadline:
                return False
            time.sleep(max(wait, 0.01))

    def reset(self) -> None:
        with self._lock:
            self._timestamps.clear()
