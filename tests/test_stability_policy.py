import unittest

from stability_policy import FailureCircuitBreaker


class FailureCircuitBreakerTest(unittest.TestCase):
    def test_same_failure_trips_at_three(self):
        breaker = FailureCircuitBreaker()
        self.assertFalse(breaker.record(False, "红米:品牌型号").tripped)
        self.assertFalse(breaker.record(False, "红米:品牌型号").tripped)
        self.assertTrue(breaker.record(False, "红米:品牌型号").tripped)

    def test_rolling_rate_trips_only_after_full_window(self):
        breaker = FailureCircuitBreaker(window=20, failure_rate=.4, consecutive_same=99)
        for index in range(19):
            self.assertFalse(breaker.record(index >= 9, "stage-%d" % index).tripped)
        self.assertTrue(breaker.record(True).tripped)

    def test_exactly_forty_percent_does_not_trip(self):
        breaker = FailureCircuitBreaker(window=20, failure_rate=.4, consecutive_same=99)
        result = None
        for index in range(20):
            result = breaker.record(index >= 8, "stage-%d" % index)
        self.assertFalse(result.tripped)
