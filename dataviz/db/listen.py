"""
ChangeListener: one thread per process holding a psycopg 3 autocommit connection that LISTENs on channel
'lbl_change' (the triggers of migration 0004 NOTIFY it with the project id on every annotation / status write)
and wakes the asyncio waiters of that project (data/live.py).

    listener()                     the process singleton (started on first use)
    listener().waiter(pid, loop)   a fresh asyncio.Event, set (on `loop`) by the next NOTIFY of project pid
    listener().available           False while the connection is down (callers fall back to polling)
    reset()                        stop the thread (db.init / db.dispose call it: tests re-init per database)

The connection is reconnected with a 1 -> 30 s backoff.
"""

import threading
import time

from ..core import logging as vlog

CHANNEL = 'lbl_change'
_lg = vlog.get_logger('listen')
_lock = threading.Lock()
_inst = None


def _dsn():
    import sys
    e = sys.modules[__package__ + '.engine']._engine       # (the package re-exports engine() under the same name)
    if e is None:
        return None
    return e.url.set(drivername='postgresql').render_as_string(hide_password=False)


class ChangeListener:
    def __init__(self, dsn):
        self.dsn = dsn
        self.available = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._waiters = {}            # pid -> [(loop, event)]
        self._conn = None
        self.n_notifies = 0
        self._thread = threading.Thread(target=self._run, name='lbl-listen', daemon=True)
        self._thread.start()

    def waiter(self, pid, loop):
        import asyncio
        ev = asyncio.Event()
        with self._lock:
            self._waiters.setdefault(str(pid), []).append((loop, ev))
        return ev

    def discard(self, pid, ev):
        with self._lock:
            lst = self._waiters.get(str(pid))
            if lst:
                lst[:] = [w for w in lst if w[1] is not ev]
                if not lst:
                    self._waiters.pop(str(pid), None)

    def _fire(self, pid):
        with self._lock:
            lst = self._waiters.pop(str(pid), [])
        for loop, ev in lst:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:          # the loop closed
                pass

    def _fire_all(self):
        with self._lock:
            pids = list(self._waiters)
        for p in pids:
            self._fire(p)

    def _run(self):
        import psycopg
        delay = 1.0
        while not self._stop.is_set():
            try:
                with psycopg.connect(self.dsn, autocommit=True, connect_timeout=10,
                                     application_name='dataviz-listen') as conn:
                    self._conn = conn
                    conn.execute(f'LISTEN {CHANNEL}')
                    self.available = True
                    delay = 1.0
                    while not self._stop.is_set():
                        for n in conn.notifies(timeout=5):
                            self.n_notifies += 1
                            self._fire(n.payload)
                            if self._stop.is_set():
                                break
                        conn.execute('SELECT 1')                 # a dead connection raises here
            except Exception as e:  # noqa: BLE001 - reconnect whatever went wrong
                if self._stop.is_set():
                    break
                _lg.warning(f'change listener down ({type(e).__name__}: {e}); retry in {delay:.0f} s')
            finally:
                self.available = False
                self._conn = None
            self._fire_all()                  # pollers re-check (they fall back to polling while down)
            if self._stop.wait(delay):
                break
            delay = min(30.0, delay * 2)

    def stop(self, timeout=1.0):
        self._stop.set()
        self.available = False
        self._fire_all()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)


def listener():
    """The process's ChangeListener (started lazily; None when the database is not initialised)."""
    global _inst
    with _lock:
        if _inst is None or not _inst._thread.is_alive():
            dsn = _dsn()
            if dsn is None:
                return None
            _inst = ChangeListener(dsn)
        return _inst


def reset():
    """Stop the listener thread (the next listener() starts a new one on the current database)."""
    global _inst
    with _lock:
        old, _inst = _inst, None
    if old is not None:
        old.stop()


def wait_available(timeout=10.0):
    """Test helper: True once the listener is connected."""
    end = time.time() + timeout
    lst = listener()
    while lst is not None and not lst.available and time.time() < end:
        time.sleep(0.05)
    return bool(lst and lst.available)
