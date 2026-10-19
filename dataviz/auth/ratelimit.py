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


class FailureLock:
    """Repeated wrong passwords lock (account, client address), not the account: someone guessing from one address
    cannot lock the real owner out from theirs. From the 5th failure in a row the pair waits 1, 2, 4, 8, then 15
    minutes; a success clears it."""

    def __init__(self, after=5, max_minutes=15, max_keys=50000, clock=time.monotonic):
        self.after, self.max_s, self.max_keys, self.clock = int(after), 60.0 * max_minutes, int(max_keys), clock
        self._state = OrderedDict()             # key -> (consecutive failures, locked until)
        self._lock = threading.Lock()

    def locked(self, key):
        with self._lock:
            hit = self._state.get(key)
        return hit is not None and hit[1] > self.clock()

    def fail(self, key):
        now = self.clock()
        with self._lock:
            n, _ = self._state.pop(key, (0, 0.0))
            n += 1
            until = now + min(self.max_s, 60.0 * 2 ** min(n - self.after, 4)) if n >= self.after else 0.0
            self._state[key] = (n, until)
            while len(self._state) > self.max_keys:
                self._state.popitem(last=False)

    def clear(self, key):
        with self._lock:
            self._state.pop(key, None)

    def reset(self):
        with self._lock:
            self._state.clear()
