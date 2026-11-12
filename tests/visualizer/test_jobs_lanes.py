"""Job lanes (core/jobs.py): opening a session never waits behind Bucket work, and the Bucket page lane is
fair per account (one running job per owner, several owners at once)."""

import threading

import pytest

from dataviz.core.jobs import LANES, JobManager

T = 10


def gated(jm, key, lane, owner=None):
    """A job that runs until its gate is set -> (job, started event, gate)."""
    started, gate = threading.Event(), threading.Event()

    def fn(job):
        started.set()
        assert gate.wait(T)
        return key
    return jm.submit(key, key, fn, lane=lane, owner=owner, session_id='s'), started, gate


@pytest.fixture
def jm():
    return JobManager()


def test_open_lane_does_not_wait_behind_dc_or_io(jm):
    dc, dc_on, dc_gate = gated(jm, 'bk-frames:b:x', 'bucket', owner='alice')
    io, io_on, io_gate = gated(jm, 'sync:d', 'io', owner='alice')
    try:
        assert dc_on.wait(T) and io_on.wait(T)
        opened = jm.submit('index:A', 'Open dataset · A', lambda j: 'ok', lane='open', owner='bob', session_id='A')
        assert opened.wait(T) and opened.result == 'ok' and dc.status == 'running' and io.status == 'running'
    finally:
        dc_gate.set()
        io_gate.set()
    assert dc.wait(T) and io.wait(T)


def test_open_lane_runs_sessions_side_by_side(jm):
    jobs = [gated(jm, f'index:{i}', 'open', owner=f'u{i}') for i in range(LANES['open'][0])]
    try:
        assert all(on.wait(T) for _, on, _ in jobs)            # all running at once
    finally:
        for _, _, g in jobs:
            g.set()
    assert all(j.wait(T) and j.status == 'done' for j, _, _ in jobs)


def test_bucket_lane_is_fair_per_account(jm):
    a1, a1_on, a1_gate = gated(jm, 'bk-frames:b:1', 'bucket', owner='alice')
    assert a1_on.wait(T)
    a2, a2_on, a2_gate = gated(jm, 'bk-import:b', 'bucket', owner='alice')      # alice's second job waits for her first
    b1, b1_on, b1_gate = gated(jm, 'bk-import:c', 'bucket', owner='bob')        # bob's starts at once
    try:
        assert b1_on.wait(T)
        assert not a2_on.wait(0.3) and a2.status == 'queued'
        a1_gate.set()
        assert a2_on.wait(T)                                                 # her own jobs keep their order
    finally:
        for g in (a1_gate, a2_gate, b1_gate):
            g.set()
    assert all(j.wait(T) and j.status == 'done' for j in (a1, a2, b1))


def test_bucket_lane_caps_parallel_accounts_and_frees_on_error(jm):
    workers = LANES['bucket'][0]
    running = [gated(jm, f'bk-sync:{i}', 'bucket', owner=f'u{i}') for i in range(workers)]
    assert all(on.wait(T) for _, on, _ in running)
    extra, extra_on, extra_gate = gated(jm, 'bk-sync:extra', 'bucket', owner='late')
    extra_gate.set()
    assert not extra_on.wait(0.3)                                            # every worker is busy
    boom = jm.submit('bk-check:x', 'boom', lambda j: 1 / 0, lane='bucket', owner='u0')
    running[1][2].set()                                                      # a worker frees: 'late' goes first
    assert extra.wait(T) and extra.status == 'done'
    running[0][2].set()                                                      # u0's slot frees, then its failing job
    assert boom.wait(T) and boom.status == 'error'
    again = jm.submit('bk-check:y', 'after the error', lambda j: 'ok', lane='bucket', owner='u0')
    assert again.wait(T) and again.result == 'ok'                            # the error freed u0's slot
    for _, _, g in running:
        g.set()


def test_same_key_dedups_and_cancel_while_queued(jm):
    a1, a1_on, a1_gate = gated(jm, 'bk-frames:b:1', 'bucket', owner='alice')
    assert a1_on.wait(T)
    q = jm.submit('bk-import:b', 'import', lambda j: 'ran', lane='bucket', owner='alice')
    assert jm.submit('bk-import:b', 'import again', lambda j: 'other', lane='bucket', owner='bob') is q
    assert jm.cancel(q.id)
    a1_gate.set()
    assert q.wait(T) and q.result == {'cancelled': True}
    after = jm.submit('bk-sync:b', 'next', lambda j: 'ok', lane='bucket', owner='alice')
    assert after.wait(T) and after.result == 'ok'                            # the cancelled job freed the slot


def test_unknown_lane_raises(jm):
    with pytest.raises(ValueError, match='unknown job lane'):
        jm.submit('x', 'x', lambda j: None, lane='nope')


def test_session_opens_while_a_bucket_job_runs(jm, tmp_path):
    """The real index job of State.start goes to the 'open' lane: a running frame extraction does not hold it."""
    from vizutil import geom_registry, make_yolo
    from dataviz.core import config as vconfig
    from dataviz.image.sources import LocalSource
    from dataviz.state import State
    make_yolo(tmp_path / 'ds')
    cfg = vconfig.load_config(str(vconfig.DEFAULT_CONFIG))
    dc, dc_on, dc_gate = gated(jm, 'bk-frames:b:x', 'bucket', owner='alice')
    try:
        assert dc_on.wait(T)
        st = State(LocalSource(str(tmp_path / 'ds'), name='ds'), None, geom_registry(), default_feature='geom',
                   jobs=jm, cfg=cfg, workspace_root=tmp_path / 'ws', dataset_id='A')
        st.start()
        assert st.index_job.lane == 'open'
        assert st.index_job.wait(30) and st.index_job.status == 'done', st.index_job.error
        assert st.ready and dc.status == 'running'
    finally:
        dc_gate.set()
    assert dc.wait(T)
