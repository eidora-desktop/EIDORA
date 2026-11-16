"""Choose inference batch sizes from useful work, independently of checkpoints."""
from __future__ import annotations

from statistics import median
from dataclasses import dataclass


@dataclass(frozen=True)
class BatchPolicy:
    candidates: tuple[int, ...]
    near_best: float
    batch_seconds: float
    memory_budget: float
    available_reserve: float
    resident_reserve: float


BATCH_POLICIES = {
    'auto': BatchPolicy((8, 16, 32), .98, 2.0, .70, .10, .85),
    'low': BatchPolicy((4, 8, 16), .90, 1.0, .50, .20, .70),
    'high': BatchPolicy((16, 32, 64, 128), .98, 3.0, .80, .075, .90),
}


class BatchThrottle:
    """Apply live caps without forgetting the preferred inference size.

    Reductions are immediate. Recovery doubles at most once per ten seconds of
    stable headroom, limiting CoreML shape changes and repeated compilation.
    """

    RECOVERY_SECONDS = 10.0

    def __init__(self, initial: int) -> None:
        self.size = initial
        self.active = False
        self._healthy_since: float | None = None

    def select(self, preferred: int, limit: int, now: float, *, healthy: bool = True) -> int:
        permitted = max(1, min(preferred, limit))
        if permitted < self.size:
            self.size = permitted
            self.active = True
            self._healthy_since = None
        elif not self.active and permitted == preferred:
            self.size = preferred
        else:
            self.active = True
            if not healthy or permitted <= self.size:
                self._healthy_since = None
            elif self._healthy_since is None:
                self._healthy_since = now
            elif now - self._healthy_since >= self.RECOVERY_SECONDS:
                self.size = min(permitted, self.size * 2)
                self._healthy_since = now
        if self.size == preferred:
            self.active = False
            self._healthy_since = None
        return self.size


class AutomaticBatchSizer:
    """Compare every permitted candidate using useful preparation/inference work.

    Two warmup batches precede at least eight measurements, targeting two seconds
    per size. Bound the window to 64 batches and reserve inputs for all candidates.
    Small collections keep their starting size without extra calibration work.
    """

    WARMUP_BATCHES = 2
    MIN_SAMPLES = 8
    MAX_SAMPLES = 64
    WINDOW_SECONDS = 2.0

    def __init__(self, total: int, limit: int, initial: int | None = None, policy: str = 'auto') -> None:
        self.policy = BATCH_POLICIES[policy]
        self.label = policy.capitalize()
        self.limit = max(1, min(512, limit, total))
        self._candidates = sorted({min(size, self.limit) for size in self.policy.candidates})
        self._max_samples = min(self.MAX_SAMPLES, total // sum(self._candidates) - self.WARMUP_BATCHES)
        self.size = min(max(1, initial or self.policy.candidates[0]), self.limit, self.policy.candidates[-1])
        self.tuning = len(self._candidates) > 1 and self._max_samples >= self.MIN_SAMPLES
        self.reason = (f'model limit caps batches at {self.limit}' if len(self._candidates) == 1 and self.limit == limit
                       else 'too few remaining inputs for a full comparison; using the starting size')
        if self.tuning:
            self.size = self._candidates[0]
            self.reason = 'comparing batch sizes'
        self._warmup = self.WARMUP_BATCHES
        self._samples: list[float] = []
        self.measurements: dict[int, float] = {}
        self._suspended = False
        self._memory_failure = False

    @property
    def profile_size(self) -> int | None:
        """Only persist a completed comparison or a validated allocation ceiling."""
        compared_all = all(size in self.measurements for size in self._candidates)
        if not self.tuning and (compared_all or self._memory_failure):
            return self.size
        return None

    def suspend(self) -> None:
        """Discard an interrupted timing window, retaining completed candidates."""
        self._suspended = True
        self._samples.clear()
        self._warmup = self.WARMUP_BATCHES

    @property
    def report(self) -> str:
        if self.tuning:
            text = self.label + ': comparing batches ' + ', '.join(map(str, self._candidates)) + '.'
        else:
            text = f'{self.label} batch {self.size}: {self.reason}.'
        if self.measurements:
            text += ' Measured: ' + '; '.join(
                f'{size} = {rate:,.1f} items/s' for size, rate in self.measurements.items()
            ) + '.'
        return text

    def _finish(self, reason: str = '') -> None:
        eligible = {size: rate for size, rate in self.measurements.items() if size <= self.limit}
        if eligible:
            fastest = max(eligible.values())
            close = [size for size, rate in eligible.items() if rate >= fastest * self.policy.near_best]
            self.size = min(close)
            self.reason = (f'within {round(100 * (1 - self.policy.near_best))}% of the fastest; smaller batch preferred' if len(close) > 1
                           else 'fastest measured throughput')
        if reason:
            self.reason = f'{self.reason}; {reason}' if eligible else reason
        self.tuning = False
        self._samples.clear()

    def observe(self, count: int, seconds: float, remaining: int) -> None:
        if not self.tuning or count != self.size:
            return
        if self._suspended:
            # Throttled work consumed inputs reserved for calibration. Rebudget
            # every remaining candidate before restarting with fresh warmups.
            pending = sum(size for size in self._candidates if size >= self.size)
            self._max_samples = min(self.MAX_SAMPLES, (remaining + count) // pending - self.WARMUP_BATCHES)
            self._suspended = False
            if self._max_samples < self.MIN_SAMPLES:
                self._finish('too few remaining inputs to resume the comparison')
                return
        if self._warmup:
            self._warmup -= 1
            return
        self._samples.append(max(seconds, 1e-6))
        if (len(self._samples) < self.MIN_SAMPLES or
                (sum(self._samples) < self.WINDOW_SECONDS and len(self._samples) < self._max_samples)):
            return
        duration = median(self._samples)
        self.measurements[self.size] = self.size / duration
        self._samples.clear()
        following = [size for size in self._candidates if size > self.size]
        if duration >= self.policy.batch_seconds:
            self._finish(f'comparison stopped at the {self.policy.batch_seconds:g}-second batch-time limit')
        elif not following:
            self._finish()
        elif remaining < (self.WARMUP_BATCHES + self.MIN_SAMPLES) * following[0]:
            self._finish('too few remaining inputs to measure the next size')
        else:
            self.size = following[0]
            self._warmup = self.WARMUP_BATCHES

    def reduce_after_memory_failure(self, attempted: int) -> bool:
        if attempted <= 1:
            return False
        self.size = max(1, attempted // 2)
        self.limit = min(self.limit, self.size)
        self.tuning = False
        self._memory_failure = True
        self.reason = 'reduced after a memory allocation failure'
        self._samples.clear()
        return True
