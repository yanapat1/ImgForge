"""
CPU-heavy work (projections, k-NN scores, group finding) in separate worker processes.

UMAP / PaCMAP (numba), t-SNE and HDBSCAN hold the GIL for long stretches. Run in the web
server's own process they stall every request (thumbnails, job polling, hovers), so they
run here instead, in persistent single-worker process pools (spawn) that stay warm:

  heavy  projections + scores, used by the job thread
  fast   groups (bubbles), used directly by the API so it never waits behind a long UMAP

Workers send progress (done, total, stage) back over a queue, so long work shows a real
progress bar in the terminal (tqdm) and the page instead of looking frozen.
"""

import os
import time
import queue
import logging
import threading
import multiprocessing as mp
from collections import deque
from concurrent.futures import ProcessPoolExecutor, Future
from concurrent.futures.process import BrokenProcessPool

import numpy as np

from ..ml import analysis
from . import hardware
from . import logging as vlog

WARMUP_ESTIMATE_S = 22.0            # typical numba / UMAP / HDBSCAN compile time on this box (only drives the progress bar)


def _noop(*_, **__):
    pass


# ---------------------------------------------------------------- pure functions (run in a worker)

def do_project(emb, method, params, metric, progress=_noop):
    return analysis.project(emb, method, params, metric, progress)


def do_scores(emb, labels, metric, k, progress=_noop):
    nbr_idx, nbr_sim = analysis.knn(emb, k=k, progress=progress)
    progress(stage='label suspicion + distance to class centre')
    suspicion, suggested = analysis.label_suspicion(labels, nbr_idx)
    outlier = analysis.outlier_scores(emb, labels, metric) if len(emb) else np.zeros(0, np.float32)
    return {'nbr_idx': nbr_idx, 'nbr_sim': nbr_sim, 'suspicion': suspicion, 'suggested': suggested,
            'outlier': outlier}


def do_groups(xy, labels, min_cluster_size, per_class, pad_frac=0.015, progress=_noop):
    return analysis.find_groups(xy, labels, min_cluster_size, per_class, pad_frac)


def warmup(kind, method='umap', progress=_noop):
    """Import and JIT-compile the startup method once, so the first real request is fast.

    Only that one method: the others compile on first use instead of slowing every start.
    """
    rng = np.random.default_rng(0)
    x = rng.normal(size=(200, 16)).astype(np.float32)
    if kind == 'heavy':
        if method in ('umap', 'pacmap', 'localmap'):        # numba JIT; t-SNE / PCA need no warm-up
            try:
                analysis.project(x, method, {'n_neighbors': 5})
            except Exception:  # noqa: BLE001 - optional packages
                pass
        do_scores(x, np.arange(200) % 3, 'cosine', 5)
    else:
        do_groups(rng.normal(size=(200, 2)), np.arange(200) % 2, 10, True)
    return kind


# ---------------------------------------------------------------- worker side

_Q = None          # progress queue to the server process
_TOKEN = None      # id of the task currently running in this worker


def _report(done=None, total=None, stage=None):
    if _Q is not None and _TOKEN is not None:
        try:
            _Q.put_nowait((_TOKEN, done, total, stage))
        except Exception:  # noqa: BLE001 - progress is best effort
            pass


def _run_task(token, fn, args):
    global _TOKEN
    _TOKEN = token
    try:
        return fn(*args, progress=_report)
    finally:
        _TOKEN = None


def _worker_init(q, settings):
    global _Q
    _Q = q
    omp = settings.get('omp_threads', 8)
    try:
        omp = max(1, int(omp))
    except (TypeError, ValueError):                       # 'auto' / None: size from the CPUs this process may use
        omp = hardware.workers('omp')
    os.environ['OMP_NUM_THREADS'] = str(omp)              # the box shows 128 CPUs but is shared
    if settings.get('hardware'):
        hardware.configure(settings['hardware'])          # device choice; the GPU itself is only touched lazily
    analysis.configure(settings.get('projections'))
    for name in ('pacmap', 'pacmap.pacmap', 'umap', 'pynndescent', 'numba'):
        logging.getLogger(name).setLevel(logging.ERROR)  # warm-up / random_state notices are noise
    parent = os.getppid()

    def watchdog():                                       # exit when the server process is gone
        while True:
            time.sleep(2)
            if os.getppid() != parent:
                os._exit(0)
    threading.Thread(target=watchdog, daemon=True).start()


# ---------------------------------------------------------------- server side

class Pools:
    """Two persistent single-process pools. run() blocks the calling thread only, never the server."""

    def __init__(self, warm=True, log=print, settings=None):
        self._ctx = mp.get_context('spawn')
        self.settings = settings or {}          # {'omp_threads': .., 'projections': {..}} applied in each worker
        self.log = log
        self._token = 0
        self._lock = threading.Lock()
        self._inbox = {}                        # token -> progress messages of a run() waiting on that task
        self._readers = {'heavy': threading.Lock(), 'fast': threading.Lock()}   # one thread reads a kind's queue at a time
        self.queues = {}
        self.warm = {}
        for kind in ('heavy', 'fast'):
            self._start(kind)
        if warm:
            t0 = time.time()
            method = self.settings.get('warm_method', 'umap')
            log(f'[workers] starting 2 worker processes and compiling {method} / HDBSCAN (first ~20 s) ...')
            _wlog = vlog.get_logger('workers')
            for kind in ('heavy', 'fast'):
                fut = self._submit(kind, warmup, kind, method)
                self.warm[kind] = fut
                threading.Thread(target=self._warm_bar, args=(kind, fut, t0, _wlog), daemon=True).start()
                fut.add_done_callback(lambda f, k=kind: log(
                    f'[workers] {k} worker ready in {time.time() - t0:.0f}s'
                    + (f' (warm-up failed: {f.exception()})' if f.exception() else '')))

    @staticmethod
    def _warm_bar(kind, fut, t0, wlog, every=4.0):
        """Log an estimated progress bar every few seconds until the warm-up finishes (the real end is the 'ready' line)."""
        while not fut.done():
            time.sleep(every)
            if fut.done():
                break
            frac = min((time.time() - t0) / WARMUP_ESTIMATE_S, 0.95)
            wlog.info(f'{vlog.text_bar(frac)} [dim]{kind} worker warming up · {time.time() - t0:.0f}s[/]', stacklevel=1)

    def _start(self, kind):
        q = self._ctx.Queue()
        self.queues[kind] = q
        setattr(self, kind, ProcessPoolExecutor(max_workers=1, mp_context=self._ctx,
                                                initializer=_worker_init, initargs=(q, self.settings)))

    def _submit(self, kind, fn, *args, track=False):
        with self._lock:
            self._token += 1
            token = self._token
            if track:
                self._inbox[token] = deque()
        try:
            fut = getattr(self, kind).submit(_run_task, token, fn, args)
        except BrokenProcessPool:
            self._start(kind)                 # a worker died (e.g. out of memory): start a fresh one
            fut = getattr(self, kind).submit(_run_task, token, fn, args)
        fut.token = token
        return fut

    def submit(self, kind, fn, *args):
        return self._submit(kind, fn, *args)

    def run(self, kind, fn, *args, progress=None):
        """Run fn in the worker; forward its progress reports to `progress` while waiting."""
        progress = progress or _noop
        warm = self.warm.get(kind)
        if warm is not None and not warm.done():
            progress(stage='worker process is still warming up (first start only, ~30 s)')
        fut = self._submit(kind, fn, *args, track=True)
        q = self.queues[kind]
        box = self._inbox[fut.token]
        try:
            while True:
                self._pump(kind, q, 0.2)
                while box:
                    progress(*box.popleft())
                if fut.done():
                    self._pump(kind, q, 0)            # flush the last messages
                    while box:
                        progress(*box.popleft())
                    return fut.result()
        except BrokenProcessPool as e:
            self._start(kind)
            raise RuntimeError(f'worker process crashed ({e}); it was restarted, try again') from e
        finally:
            self._inbox.pop(fut.token, None)

    def _pump(self, kind, q, timeout):
        """Move the kind's queued progress messages into the inbox of the run() they belong to.

        Concurrent run() calls on one pool share a queue; whoever holds the reader lock reads for all of them,
        so no run loses (or drops) another's messages."""
        if not self._readers[kind].acquire(blocking=False):
            time.sleep(timeout)                       # another thread is reading right now
            return
        try:
            while True:
                try:
                    token, done, total, stage = q.get(timeout=timeout)
                except queue.Empty:
                    return
                timeout = 0
                box = self._inbox.get(token)
                if box is not None:
                    box.append((done, total, stage))
        finally:
            self._readers[kind].release()

    def shutdown(self):
        for kind in ('heavy', 'fast'):
            getattr(self, kind).shutdown(wait=False, cancel_futures=True)


class InlinePools:
    """Same interface, runs in the calling thread (tests, debugging)."""

    def submit(self, kind, fn, *args):
        f = Future()
        try:
            f.set_result(fn(*args))
        except Exception as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def run(self, kind, fn, *args, progress=None):
        return fn(*args, progress=progress or _noop)

    def shutdown(self):
        pass
