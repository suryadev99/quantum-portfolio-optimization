"""Lightweight per-stage timing (NFR9)."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager


class StageTimer:
    """Accumulates wall-clock seconds per named stage."""

    def __init__(self) -> None:
        self.seconds: dict[str, float] = defaultdict(float)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.seconds[name] += time.perf_counter() - t0

    def as_dict(self) -> dict[str, float]:
        return {k: round(v, 6) for k, v in self.seconds.items()}

    @property
    def total(self) -> float:
        return float(sum(self.seconds.values()))
