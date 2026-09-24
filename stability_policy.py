"""Pure, offline-testable policies for stopping unsafe batch runs."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class BreakerDecision:
    tripped: bool
    reason: str = ""


class FailureCircuitBreaker:
    def __init__(self, window: int = 20, failure_rate: float = 0.40,
                 consecutive_same: int = 3):
        if window < 1 or not 0 <= failure_rate <= 1 or consecutive_same < 1:
            raise ValueError("invalid circuit-breaker settings")
        self.window = window
        self.failure_rate = failure_rate
        self.consecutive_same = consecutive_same
        self.results = deque(maxlen=window)
        self.last_signature = ""
        self.same_count = 0

    def record(self, success: bool, signature: str = "") -> BreakerDecision:
        self.results.append(bool(success))
        if success:
            self.last_signature, self.same_count = "", 0
        elif signature == self.last_signature:
            self.same_count += 1
        else:
            self.last_signature, self.same_count = signature, 1

        if not success and self.same_count >= self.consecutive_same:
            return BreakerDecision(True, "同类故障连续%d件: %s" %
                                   (self.same_count, signature or "未分类"))
        if len(self.results) == self.window:
            failures = sum(not item for item in self.results)
            rate = failures / float(self.window)
            if rate > self.failure_rate:
                return BreakerDecision(True, "最近%d件失败%d件（%.0f%%），超过阈值%.0f%%" %
                                       (self.window, failures, rate * 100,
                                        self.failure_rate * 100))
        return BreakerDecision(False)
