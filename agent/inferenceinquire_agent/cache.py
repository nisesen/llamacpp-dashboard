"""Values refreshed off-thread, so an HTTP request never waits on a slow command."""

import threading
import time


class Cache:
    """Value + TTL, refreshed off-thread. Readers never block on a refresh.

    `key`, if given, is what the value describes; when it changes the value
    is stale at once, whatever the TTL says.
    """

    def __init__(self, fn, ttl, initial=None, key=None):
        self.fn, self.ttl, self.key_fn = fn, ttl, key
        self.value, self.ts, self.lock = initial, 0.0, threading.Lock()
        self.key = None

    def _current_key(self):
        try:
            return self.key_fn() if self.key_fn else None
        except Exception:
            return None

    def stale(self):
        if self.key_fn and self._current_key() != self.key:
            return True
        return time.time() - self.ts >= self.ttl

    def refresh(self):
        key = self._current_key()
        try:
            v = self.fn()
        except Exception as exc:
            v = {"error": str(exc)}
        with self.lock:
            self.value, self.ts, self.key = v, time.time(), key

    def get(self):
        with self.lock:
            return self.value, self.ts


# Every cached collector, by name. payload.py registers them; a collector that
# reads another's value (the GPU stream reads gpu_procs; the journal follower
# and the GGUF reader read guests) looks it up here when it runs.
CACHES: dict[str, Cache] = {}
