"""
Per-IP token bucket for sign-in and sign-up attempts (in memory, per process): `rate` attempts per `per` seconds
with bursts up to `burst`. Memory is bounded: past `max_keys` addresses the least recently seen is dropped.
The per-account lockout lives with the account (auth/users.py record_failure).
"""

import threading
import time
from collections import OrderedDict


class RateLimiter:
    def __init__(self, rate=20, per=60.0, burst=20, max_keys=10000, clock=time.monotonic):
        self.rate, self.per, self.burst, self.max_keys = float(rate), float(per), float(burst), int(max_keys)
        self.clock = clock
        self._buckets = OrderedDict()           # key -> (tokens, last refill time)
        self._lock = threading.Lock()

    def allow(self, key):
        """Take one token for `key`; False when its bucket is empty (answer 429)."""
        now = self.clock()
        with self._lock:
            tokens, last = self._buckets.pop(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate / self.per)
            ok = tokens >= 1.0
            if ok:
                tokens -= 1.0
            self._buckets[key] = (tokens, now)
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
        return ok

    def reset(self):
        with self._lock:
            self._buckets.clear()
