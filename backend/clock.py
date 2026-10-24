"""One source of time for the whole backend.

Live mode: real UTC. Replay mode: a simulated clock that starts at a chosen moment and
runs `speed` times faster than real time. Anything created while the clock is simulated
is tagged simulated=True by the modules, so replayed data never passes for live data.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional


class Clock:
    def __init__(self, simulated: bool = False, start: Optional[datetime] = None, speed: float = 1.0):
        self.simulated = simulated
        self.speed = speed
        self._lock = threading.Lock()
        self._start = start or datetime.now(timezone.utc)
        self._t0 = time.monotonic()
        self._frozen: Optional[datetime] = None

    def now(self) -> datetime:
        if not self.simulated:
            return datetime.now(timezone.utc)
        with self._lock:
            if self._frozen is not None:
                return self._frozen
            return self._start + timedelta(seconds=(time.monotonic() - self._t0) * self.speed)

    @property
    def frozen(self) -> bool:
        return self._frozen is not None

    # ---- for tests and step-by-step replays
    def freeze(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("clock times must be timezone-aware UTC")
        with self._lock:
            self.simulated = True
            self._frozen = at

    def advance(self, seconds: float) -> datetime:
        with self._lock:
            if self._frozen is None:
                raise RuntimeError("advance() needs a frozen clock")
            self._frozen += timedelta(seconds=seconds)
            return self._frozen

    def sleep(self, sim_seconds: float, stop: Optional[threading.Event] = None) -> None:
        """Wait the real time that corresponds to sim_seconds on this clock."""
        real = sim_seconds / self.speed if self.simulated and self.speed > 0 else sim_seconds
        if stop is not None:
            stop.wait(real)
        else:
            time.sleep(real)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
