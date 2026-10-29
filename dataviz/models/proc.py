"""
Label-all inference in a separate process, so a long labeling run never stalls the web server.

Run in uvicorn's own process, a local model (torch / ultralytics) decodes images with PIL and runs the network
in threads that fight every request for the GIL and the CPUs: thumbnails, listings and job polling all crawl and
the page looks frozen. Here one spawned worker process holds the model instead:

    predict_files([(path, name)], conf) -> [predictions | ModelError]     (one per image, same as predict_batch)
    names()                                -> the model's class names     (loads the model in the worker)

The worker reads and decodes the files itself (no image bytes cross the pipe), JPEGs at draft scale for the
model input size, on a few decode threads that run ahead of the model, and runs the model at its own batch size.
It caps OMP / torch threads (the box reports 128 CPUs but is shared), keeps one model loaded and exits when the
server is gone. After `idle_s` without a call it is shut down (GPU memory back); a crashed worker is restarted
on the next call.

The visualizer's predict job keeps the in-process runner (runners.get_runner): one image at a time, latency first.
"""

import os
import time
import logging
import threading
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool

IDLE_S = 120.0                 # shut the worker down after this long without a call
DECODE_THREADS = 4             # image decode threads inside the worker (PIL releases the GIL while decoding)
DRAFT_FACTOR = 2               # as predict.py: a drafted JPEG keeps its long side >= 2 x the model input size
CHUNK = 32                     # images per call from label-all (the worker batches them for the model)
IN_FLIGHT = 2                  # calls queued at once: the worker always has the next chunk ready

_log = logging.getLogger('dataviz')


# ---------------------------------------------------------------- worker side

_W = {'key': None, 'runner': None, 'decode': None}


def _init(settings, parent):
    omp = max(1, int(settings.get('omp_threads') or 8))
    os.environ['OMP_NUM_THREADS'] = str(omp)
    from ..core import hardware
    if settings.get('hardware'):
        hardware.configure(settings['hardware'])
    try:
        import torch
        torch.set_num_threads(omp)
    except ImportError:
        pass
    _W['decode'] = ThreadPoolExecutor(DECODE_THREADS, thread_name_prefix='decode')

    def watchdog():                                       # exit when the server process is gone
        while True:
            time.sleep(2)
            if os.getppid() != parent:
                os._exit(0)
    threading.Thread(target=watchdog, daemon=True).start()


def _runner(m, fp, workdir):
    from . import runners
    key = (m['id'], fp)
    if _W['key'] != key:
        if _W['runner'] is not None:
            try:
                _W['runner'].close()
            except Exception:  # noqa: BLE001 - best effort
                pass
            _W['key'] = _W['runner'] = None
        _W['runner'] = runners.load_runner(m, workdir, check_owner=False)   # the server checked the owner
        _W['key'] = key
    return _W['runner']


def _decode(path, target):
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        size0 = im.size
        if target and im.format == 'JPEG' and max(size0) >= 2 * DRAFT_FACTOR * target:
            k = DRAFT_FACTOR * target / max(size0)
            im.draft('RGB', (max(1, int(size0[0] * k)), max(1, int(size0[1] * k))))
        return ImageOps.exif_transpose(im).convert('RGB')


def _names(m, fp, workdir):
    return list(_runner(m, fp, workdir).names or [])


def _predict(m, fp, workdir, paths, conf):
    """-> (names, [list of predictions | ('error', message)]) for every path, in order."""
    from ..core import hardware
    from ..labeling import models as lm
    runner = _runner(m, fp, workdir)
    names = list(runner.names or [])
    target = int(getattr(runner, 'draft_size', None) or 0)
    bs = max(1, int(runner.batch_size or 8))
    if runner.max_batch:
        bs = min(bs, runner.max_batch)
    thr = float(conf if conf is not None else m.get('conf') or 0.25)
    futs = [_W['decode'].submit(_decode, p, target) for p in paths]      # decoding runs ahead of the model
    out = [None] * len(paths)
    for start in range(0, len(paths), bs):
        ims, at = [], []
        for i in range(start, min(start + bs, len(paths))):
            try:
                ims.append(futs[i].result())
                at.append(i)
            except Exception as e:  # noqa: BLE001 - an unreadable image fails alone
                out[i] = ('error', f'cannot read the image: {e}')
        if not ims:
            continue
        try:
            dets = [d for part in hardware.with_fallback(runner.predict, ims) for d in part]
        except Exception as e:  # noqa: BLE001 - the model failed on the batch: every image of it
            for i in at:
                out[i] = ('error', f'{m.get("name")}: {type(e).__name__}: {e}')
            continue
        for i, im, det in zip(at, ims, dets):
            w, h = im.size
            preds = []
            for x1, y1, x2, y2, score, c in det.tolist():
                if score < thr:
                    continue
                c = int(c)
                preds.append({'class_name': names[c] if 0 <= c < len(names) else str(c), 'score': float(score),
                              'geometry_type': 'bbox',
                              'geometry': {'x': x1 / w, 'y': y1 / h, 'w': (x2 - x1) / w, 'h': (y2 - y1) / h}})
            out[i] = lm.clean_predictions(preds, None, 'norm')
    return names, out


# ---------------------------------------------------------------- server side

class ModelProcess:
    """The one worker process (one model loaded in it, like runners' single slot). Thread-safe."""

    def __init__(self, idle_s=IDLE_S):
        self.idle_s = float(idle_s)
        self._pool = None
        self._lock = threading.Lock()
        self._busy = 0
        self._last = time.monotonic()
        self._names = {}                                  # (id, fp) -> class names (cheap repeat info())
        threading.Thread(target=self._reaper, daemon=True, name='model-proc-reaper').start()

    def _settings(self):
        from ..core import hardware
        return {'omp_threads': hardware.workers('omp'), 'hardware': dict(getattr(hardware, '_SETTINGS', {}) or {})}

    def _get(self):
        with self._lock:
            if self._pool is None:
                self._pool = ProcessPoolExecutor(1, mp_context=mp.get_context('spawn'), initializer=_init,
                                                 initargs=(self._settings(), os.getpid()))
            self._busy += 1
            self._last = time.monotonic()
            return self._pool

    def _done(self):
        with self._lock:
            self._busy -= 1
            self._last = time.monotonic()

    def _call(self, fn, *args):
        for attempt in (0, 1):
            pool = self._get()
            try:
                return pool.submit(fn, *args).result()
            except BrokenProcessPool:
                with self._lock:                          # the worker died (out of memory, a crash): a fresh one
                    if self._pool is pool:
                        self._pool = None
                if attempt:
                    raise
                _log.warning('model worker process crashed: starting a new one')
            finally:
                self._done()

    def names(self, m, fp, workdir):
        key = (m['id'], fp)
        if key not in self._names:
            self._names[key] = self._call(_names, m, fp, workdir)
        return list(self._names[key])

    def predict_files(self, m, fp, workdir, paths, conf=None):
        from ..labeling import models as lm
        names, res = self._call(_predict, m, fp, workdir, list(paths), conf)
        self._names[(m['id'], fp)] = names
        return [lm.ModelError(r[1], 502) if isinstance(r, tuple) else r for r in res]

    def _reaper(self):
        while True:
            time.sleep(min(15.0, max(1.0, self.idle_s / 4)))
            with self._lock:
                if self._pool is None or self._busy or time.monotonic() - self._last < self.idle_s:
                    continue
                pool, self._pool = self._pool, None
            pool.shutdown(wait=False, cancel_futures=True)     # the model (and its GPU memory) goes with it

    def shutdown(self):
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


_proc = None
_proc_lock = threading.Lock()
_enabled = {'on': False, 'idle_s': IDLE_S}       # the server turns it on (cli.py); tests run in-process


def configure(enabled=True, idle_s=IDLE_S):
    _enabled.update(on=bool(enabled), idle_s=float(idle_s or IDLE_S))


def enabled():
    return _enabled['on']


def get():
    global _proc
    with _proc_lock:
        if _proc is None:
            _proc = ModelProcess(_enabled['idle_s'])
        return _proc
