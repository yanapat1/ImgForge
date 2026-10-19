"""
Background job queue with progress reporting (live rich bar in the terminal + JSON for the UI).

A single worker thread runs jobs one at a time: the GPU models and numba (UMAP)
must not run concurrently anyway, and it keeps progress easy to follow. A second
"io" lane runs network work (S3 sync) so a long download never waits behind a UMAP,
and a third "model" lane runs the labeling page's model jobs (remote inference), so
a long label-all never blocks an export or a feature job.

Cancel: JobManager.cancel(jid) sets job.cancelled (a threading.Event). A queued job
never starts; a running one sees it only if its fn checks job.cancelled (between
batches) and returns early, keeping what it already wrote.

Ownership: every job records who queued it (owner = user id) and the visualizer session it belongs to
(session_id). Both default to the request's context (auth/context.py) when the caller does not pass them, so the
page lists only jobs of sessions the user can open (server/routes/system.py) and only the owner of the job or of
its session may cancel it. A finished job is written to app.jobs_log (best effort, never raises).
"""

import time
import queue
import threading
import itertools
import uuid
from datetime import datetime, timezone

from rich.progress import (Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn,
                           TimeElapsedColumn, TimeRemainingColumn)

from . import logging as vlog

_log = vlog.get_logger('jobs')

# one live progress display shared by every job: log lines print above it
_progress = Progress(SpinnerColumn(style='cyan'), TextColumn('[bold]{task.fields[label]}[/] [dim]{task.fields[stage]}[/]'),
                     BarColumn(bar_width=32, complete_style='cyan', finished_style='green', pulse_style='blue'),
                     MofNCompleteColumn(), TextColumn('[dim]{task.fields[extra]}[/]'), TimeElapsedColumn(),
                     TextColumn('[dim]eta[/]'), TimeRemainingColumn(), console=vlog.console, transient=True,
                     refresh_per_second=8)
_progress_lock = threading.Lock()
_progress_users = 0


def _progress_acquire():
    global _progress_users
    with _progress_lock:
        _progress_users += 1
        if _progress_users == 1:
            _progress.start()


def _progress_release():
    global _progress_users
    with _progress_lock:
        _progress_users -= 1
        if _progress_users == 0:
            _progress.stop()


class Job:
    def __init__(self, jid, key, label, fn, lane='main', public_result=False, owner=None, session_id=None):
        self.id = jid
        self.owner = owner              # user id of who queued it (None: the server itself)
        self.session_id = session_id    # the visualizer session it works on (None: not tied to one)
        self.key = key
        self.label = label
        self.fn = fn
        self.lane = lane
        self.status = 'queued'
        self.stage = 'waiting'
        self.done = 0
        self.total = None
        self.extra = ''             # free text shown next to the count (e.g. "12.4 MB/s")
        self.created = time.time()
        self.started = None
        self.finished = None
        self.error = None
        self.result = None
        self.public_result = public_result     # result is small JSON the page reads from GET /api/jobs/{id}
        self.cancelled = threading.Event()
        self._task = None
        self._stage_start = None
        self._event = threading.Event()

    def progress(self, done=None, total=None, stage=None, extra=None):
        """Report progress. total=None means indeterminate (pulsing bar)."""
        new_stage = stage is not None and stage != self.stage
        if new_stage:
            if self.status == 'running' and self.stage not in ('starting', 'waiting'):
                _log.debug(f'{vlog.esc(self.label)}: {vlog.esc(stage)}')
            self.stage = stage
            self._stage_start = time.time()
            self.done, self.total = 0, None
            self.extra = ''
        if total is not None:
            self.total = total
        if done is not None:
            self.done = done
        if extra is not None:
            self.extra = extra
        if new_stage:
            self._logged_pct = -1
        if not vlog.console.is_terminal and self.total and self.status == 'running':
            pct = int(100 * self.done / self.total)
            if pct // 10 > getattr(self, '_logged_pct', -1) // 10 or (pct >= 100 and self._logged_pct < 100):
                self._logged_pct = pct
                eta = self.eta()
                _log.info(f'{vlog.text_bar(self.done / self.total)} {vlog.esc(self.label)} [dim]{vlog.esc(self.stage)} · '
                          f'{self.done:,}/{self.total:,}' + (f' · eta {eta:.0f}s' if eta else '') + '[/]')
        task = self._task
        if task is not None:
            try:
                if new_stage:                  # new stage: restart the bar (rich's API cannot set total back to None)
                    _progress.reset(task, completed=0)
                    with _progress._lock:
                        _progress._tasks[task].total = self.total
                _progress.update(task, total=self.total, completed=self.done, stage=vlog.esc(self.stage),
                                 extra=vlog.esc(self.extra))
            except KeyError:                   # the job finished concurrently
                pass

    def eta(self):
        if not self.total or not self.done or self._stage_start is None:
            return None
        elapsed = time.time() - self._stage_start
        return elapsed / self.done * (self.total - self.done)

    def to_dict(self):
        eta = self.eta()
        return {
            'id': self.id, 'key': self.key, 'label': self.label, 'status': self.status, 'stage': self.stage,
            'done': self.done, 'total': self.total, 'extra': self.extra,
            'pct': round(100 * self.done / self.total, 1) if self.total else None,
            'eta': round(eta, 1) if eta is not None else None,
            'elapsed': round((self.finished or time.time()) - self.started, 1) if self.started else 0,
            'error': self.error, 'cancelled': self.cancelled.is_set(), 'owner': self.owner,
            'session_id': self.session_id,
            **({'result': self.result} if self.public_result and self.status == 'done' else {}),
        }

    def wait(self, timeout=None):
        return self._event.wait(timeout)


class JobManager:
    def __init__(self, log=None, keep=30):
        self.keep = keep
        self._ids = itertools.count(1)
        self._jobs = {}            # id -> Job (recent history)
        self._active = {}          # key -> Job (queued/running)
        self._lock = threading.Lock()
        self._queues = {'main': queue.Queue(), 'io': queue.Queue(), 'model': queue.Queue()}
        for lane, q in self._queues.items():
            threading.Thread(target=self._worker, args=(q,), daemon=True, name=f'dataviz-jobs-{lane}').start()

    def submit(self, key, label, fn, lane='main', public_result=False, owner=None, session_id=None):
        """Queue fn(job) -> result. Returns the existing job if `key` is already queued/running.
        public_result: GET /api/jobs/{id} carries the (small, JSON) result once done.
        owner / session_id: default to the signed-in user and the /s/<id>/ session of the current request."""
        if owner is None or session_id is None:
            from ..auth import context
            if owner is None:
                owner = context.user_id()
            if session_id is None:
                b = context.bound()
                session_id = b.session_id if b is not None and b.session_id else None
        with self._lock:
            if key in self._active and not self._active[key].cancelled.is_set():
                return self._active[key]
            job = Job(next(self._ids), key, label, fn, lane, public_result, owner, session_id)
            self._jobs[job.id] = job
            self._active[key] = job
            old = sorted(j for j, x in self._jobs.items() if x.status in ('done', 'error'))
            for j in old[:max(0, len(old) - self.keep)]:
                del self._jobs[j]
        _log.info(f'[dim]▶ queued[/]  {vlog.esc(label)}')
        self._queues[lane].put(job)
        return job

    def get(self, jid):
        return self._jobs.get(jid)

    def cancel(self, jid):
        """Ask job `jid` to stop. False when there is no such job or it already finished."""
        job = self._jobs.get(jid)
        if job is None or job.status in ('done', 'error'):
            return False
        job.cancelled.set()
        _log.info(f'[yellow]■ cancel[/]  {vlog.esc(job.label)}')
        return True

    def list(self, visible=None):
        """Recent jobs as dicts; visible(job) -> bool keeps only the ones a user may see."""
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.id)
        return [j.to_dict() for j in jobs if visible is None or visible(j)]

    def active(self, visible=None):
        with self._lock:
            jobs = sorted(self._active.values(), key=lambda j: j.id)
        return [j.to_dict() for j in jobs if visible is None or visible(j)]

    def busy(self, tag):
        """True while a queued / running job's key mentions `tag` (a State's tag): that State must stay loaded."""
        with self._lock:
            return any(tag in k for k in self._active)

    def _log_finished(self, job):
        """One app.jobs_log row (best effort: no database, a broken connection, ... never fail the job)."""
        try:
            from .. import db
            from ..db import schema
            ts = lambda t: datetime.fromtimestamp(t, timezone.utc) if t else None     # noqa: E731
            with db.begin() as c:
                c.execute(schema.jobs_log.insert().values(
                    id=uuid.uuid4().hex, user_id=job.owner, session_id=job.session_id, label=str(job.label)[:300],
                    status='cancelled' if job.cancelled.is_set() and job.status == 'done' else job.status,
                    started_at=ts(job.started), ended_at=ts(job.finished), error=job.error))
        except Exception as e:  # noqa: BLE001 - the log is a nicety
            _log.debug(f'jobs_log: {vlog.esc(e)}')

    def _worker(self, q):
        while True:
            job = q.get()
            if job.cancelled.is_set():             # cancelled while queued: never started
                job.status, job.stage, job.result = 'done', 'cancelled', {'cancelled': True}
                job.started = job.finished = time.time()
                with self._lock:
                    if self._active.get(job.key) is job:     # a cancelled job may have been replaced
                        self._active.pop(job.key)
                job._event.set()
                self._log_finished(job)
                continue
            job.status = 'running'
            job.started = time.time()
            job._stage_start = job.started
            job.stage = 'starting'
            _log.info(f'[cyan]● started[/] {vlog.esc(job.label)}')
            _progress_acquire()
            job._task = _progress.add_task('', total=None, label=vlog.esc(job.label), stage='starting', extra='')
            try:
                job.result = job.fn(job)
                job.status = 'done'
                job.stage = 'done'
                if job.total is not None:
                    job.done = job.total
            except Exception as e:  # noqa: BLE001 - surface any failure to the UI
                job.status = 'error'
                job.error = f'{type(e).__name__}: {e}'
                _log.exception(f'[bold red]✖ failed[/]  {vlog.esc(job.label)} — {vlog.esc(job.error)}')
            finally:
                job.finished = time.time()
                task, job._task = job._task, None
                _progress.remove_task(task)
                _progress_release()
                took = job.finished - job.started
                if job.status == 'done':
                    _log.info(f'[green]✔ done[/]    {vlog.esc(job.label)} [dim]in {took:.1f}s[/]')
                with self._lock:
                    if self._active.get(job.key) is job:
                        self._active.pop(job.key)
                job._event.set()
                self._log_finished(job)
