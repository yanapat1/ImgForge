"""The Compare tab's API over a combined dataset with a known answer: one image identical in A and B (renamed in
B, matched by content), one with a moved + a reclassed box, one only in A, one only in B; binary columns; image
detail; edits keep the diff current; the routes refuse a plain session."""

import shutil

import numpy as np
import pytest

from authutil import account, client, login
from conftest import make_jpeg
from dataviz.image import diff
from dataviz.server.binary import unpack_columns
from vizutil import make_viz_app, make_yolo, union_session, viz_session


@pytest.fixture
def world(pg, tmp_path):
    a = make_yolo(tmp_path / 'dsA', classes=('cat', 'dog'), boxes=(
        ('same', '0 0.5 0.5 0.4 0.4\n'),
        ('edit', '0 0.3 0.3 0.2 0.2\n1 0.7 0.7 0.2 0.2\n'),
        ('onlya', '1 0.5 0.5 0.2 0.2\n')))
    b = make_yolo(tmp_path / 'dsB', classes=('cat', 'dog'), boxes=(
        ('renamed', '0 0.5 0.5 0.4 0.4\n'),
        ('edit', '0 0.32 0.3 0.2 0.2\n0 0.7 0.7 0.2 0.2\n'),
        ('onlyb', '0 0.5 0.5 0.3 0.3\n')))
    shutil.copy(a / 'images' / 'same.jpg', b / 'images' / 'renamed.jpg')       # same bytes, other name
    shutil.copy(a / 'images' / 'edit.jpg', b / 'images' / 'edit.jpg')
    make_jpeg(b / 'images' / 'onlyb.jpg', 64, 48, color=(200, 10, 200))          # make_yolo colours by position
    alice = account('alice')
    A = viz_session(alice['id'], 'A', a)
    B = viz_session(alice['id'], 'B', b)
    plain = viz_session(alice['id'], 'plain', a)
    U = union_session(alice['id'], 'A vs B', [(A, 'A'), (B, 'B')])
    ctx, app = make_viz_app(tmp_path / 'ws')
    c = client(app)
    assert login(c, 'alice').status_code == 200
    return {'c': c, 'U': U, 'plain': plain, 'ctx': ctx, 'alice': alice}


def test_known_answer(world):
    c, U = world['c'], world['U']
    r = c.post(f'/s/{U}/api/compare/run', json={'a': 'A', 'b': 'B'})
    assert r.status_code == 200, r.text
    body = r.json()
    sm = body['summary']
    assert sm['images'] == {'only_a': 1, 'only_b': 1, 'same': 1, 'labels_changed': 1, 'pixels_changed': 0,
                            'dup_a': 0, 'dup_b': 0}
    assert sm['boxes']['same'] == 1 and sm['boxes']['moved'] == 1 and sm['boxes']['reclass'] == 1
    assert sm['boxes']['image_only_a'] == 1 and sm['boxes']['image_only_b'] == 1
    key = body['key']
    res = c.get(f'/s/{U}/api/compare/result', params={'key': key}).json()
    assert res['image_lists']['only_a'] == ['A/onlya.jpg'] and res['image_lists']['only_b'] == ['B/onlyb.jpg']
    assert res['image_lists']['same'] == ['A/same.jpg']
    col = c.get(f'/s/{U}/api/compare/columns', params={'key': key})
    assert col.headers['content-type'] == 'application/octet-stream'
    meta, cols = unpack_columns(col.content)
    pts = c.get(f'/s/{U}/api/points').json()
    st = dict(zip(zip(pts['image'], range(len(pts['id']))), cols['status']))
    by = {}
    for (img, sid), s in st.items():
        by.setdefault(img, []).append(int(s))
    assert by['A/same.jpg'] == [diff.BOX_SAME] and by['B/renamed.jpg'] == [diff.BOX_SAME]
    assert sorted(by['A/edit.jpg']) == [diff.BOX_MOVED, diff.BOX_RECLASS]
    assert by['A/onlya.jpg'] == [diff.BOX_IMAGE_ONLY]
    assert c.get(f'/s/{U}/api/compare/columns', params={'key': key},
                 headers={'If-None-Match': col.headers['etag']}).status_code == 304
    d = c.get(f'/s/{U}/api/compare/image', params={'key': key, 'name': 'B/edit.jpg'}).json()
    assert d['a']['name'] == 'A/edit.jpg' and d['b']['name'] == 'B/edit.jpg' and len(d['b']['boxes']) == 2
    again = c.post(f'/s/{U}/api/compare/run', json={'a': 'A', 'b': 'B'}).json()
    assert again['key'] == key                                          # cached


def test_patch_keeps_diff_current(world):
    c, U, ctx = world['c'], world['U'], world['ctx']
    key = c.post(f'/s/{U}/api/compare/run', json={'a': 'A', 'b': 'B'}).json()['key']
    st = ctx.state_for(U)
    sid = st.by_image['B/edit.jpg'][1]                               # B's reclassed box: make it a dog again
    st.labels[sid] = 1
    st.samples[sid]['cls'] = 1
    out = st.on_boxes_patched(['B/edit.jpg'])
    assert out and list(out.values())[0]['summary']['boxes']['reclass'] == 0
    new = c.post(f'/s/{U}/api/compare/run', json={'a': 'A', 'b': 'B'}).json()
    assert new['key'] != key and new['summary']['boxes']['reclass'] == 0
    fresh = __import__('dataviz.image.compare', fromlist=['x']).CompareService(st).run('A', 'B', {})[1]
    assert np.array_equal(fresh.status, st._compare.cached('A', 'B', {})[1].status)


def test_errors(world):
    c = world['c']
    assert c.post(f'/s/{world["plain"]}/api/compare/run', json={'a': 'A', 'b': 'B'}).status_code == 409
    assert c.post(f'/s/{world["U"]}/api/compare/run', json={'a': 'A', 'b': 'A'}).status_code == 400
    assert c.post(f'/s/{world["U"]}/api/compare/run', json={'a': 'A', 'b': 'Z'}).status_code == 400
    s = c.get(f'/s/{world["U"]}/api/union/status').json()
    assert s['union'] and not s['stale'] and [m['alias'] for m in s['members']] == ['A', 'B']


def test_deleting_a_box_updates_the_diff(world):
    """A box deleted through the patch path (curation / live feed) is no longer SAME in the diff (review finding:
    patched() walked by_image, which no longer lists deleted boxes)."""
    from dataviz.image import patching
    c, U, ctx = world['c'], world['U'], world['ctx']
    before = c.post(f'/s/{U}/api/compare/run', json={'a': 'A', 'b': 'B'}).json()['summary']['boxes']
    st = ctx.state_for(U)
    sid = st.by_image['B/renamed.jpg'][0]                            # B's copy of the identical image
    patching.apply_patch(st, {'deleted': [sid]})
    after = c.post(f'/s/{U}/api/compare/run', json={'a': 'A', 'b': 'B'}).json()['summary']
    assert after['boxes']['same'] == before['same'] - 1 and after['boxes']['removed'] == before['removed'] + 1
    assert after['images']['labels_changed'] == 2


def test_image_conditions(world, tmp_path):
    """POST /api/compare/shift: a job measures A's and B's images, the re-POST returns the report (B's three images
    are darker than A's: brightness flagged), the image lists and the simulated preview; a plain session gets 409."""
    from PIL import Image

    from conftest import wait_job
    c, U = world['c'], world['U']
    for p in sorted((tmp_path / 'dsB' / 'images').glob('*.jpg')):       # B: a darker camera (same names)
        Image.open(p).convert('RGB').point(lambda v: v // 3).save(p, 'JPEG', quality=95)
    body = {'a': 'A', 'b': 'B', 'sample': 0}
    r = c.post(f'/s/{U}/api/compare/shift', json=body)
    assert r.status_code == 200, r.text
    first = r.json()
    if not first['ready']:
        wait_job(c, first['job_id'])
        first = c.post(f'/s/{U}/api/compare/shift', json=body).json()
    assert first['ready'], first
    rep = first['report']
    assert rep['n']['a'] == 3 and rep['n']['b'] == 3
    by = {m['key']: m for m in rep['metrics']}
    assert by['brightness']['b']['med'] < by['brightness']['a']['med']
    assert rep['verdict']['level'] in ('fits', 'check', 'shifted') and rep['hyp_yaml'].startswith('#')
    key = first['key']
    names = c.get(f'/s/{U}/api/compare/shift/images', params={'key': key, 'which': 'low', 'metric': 'brightness'}).json()
    assert sorted(names['names']) == ['B/edit.jpg', 'B/onlyb.jpg', 'B/renamed.jpg']
    a_low = c.get(f'/s/{U}/api/compare/shift/images',
                  params={'key': key, 'which': 'high', 'metric': 'brightness', 'side': 'a'}).json()['names']
    assert len(a_low) == 3 and all(n.startswith('A/') for n in a_low)
    from types import SimpleNamespace

    from dataviz.llm.tools import visual
    out, summary = visual._compare_shift(SimpleNamespace(state=world['ctx'].state_for(U)), {})   # the assistant's view
    assert out['report']['a'] == 'A' and out['report']['verdict']['level'] == rep['verdict']['level']
    assert out['report']['metrics'] and 'hyp_yaml' in out['report'] and summary.startswith('→ A vs B')
    assert out['report']['light_map']['b']['mean'] < out['report']['light_map']['a']['mean']
    assert set(out['report']['exposure_mix']['a']) >= {'dark', 'normal', 'flat'} and 'after_lut' in out['report']['correction']
    assert 'q' in out['report']['metrics'][0] and 'delta_ci95' in out['report']['metrics'][0]
    assert visual._compare_shift(SimpleNamespace(state=world['ctx'].state_for(U)), {'a': 'B'})[0]['report'] is None
    pv = c.get(f'/s/{U}/api/compare/shift/preview', params={'key': key, 'name': 'A/same.jpg'})
    assert pv.status_code == 200 and pv.headers['content-type'] == 'image/jpeg'
    assert pv.content[:2] == b'\xff\xd8' and 'brightness_gain' in pv.headers['x-shift-params']
    pc = c.get(f'/s/{U}/api/compare/shift/preview', params={'key': key, 'name': 'B/renamed.jpg', 'mode': 'correct'})
    assert pc.status_code == 200 and pc.content[:2] == b'\xff\xd8' and 'residual' in pc.headers['x-shift-params']
    lut = c.get(f'/s/{U}/api/compare/shift/lut', params={'key': key})
    assert lut.status_code == 200 and 'attachment' in lut.headers['content-disposition']
    lj = lut.json()
    assert len(lj['lut']) == 3 and all(len(ch) == 256 for ch in lj['lut']) and lj['usage']
    assert rep['light_map'] and len(rep['light_map']['diff']) == 4 and rep['exposure_mix']['a']['n'] >= 1
    assert 'ci' in rep['metrics'][0] and 'q' in rep['metrics'][0]
    assert c.get(f'/s/{U}/api/compare/shift/images', params={'key': key, 'which': 'uncovered'}).json()['names'] == []
    assert c.get(f'/s/{U}/api/compare/shift/images', params={'key': 'nope', 'which': 'most_b'}).status_code == 404
    assert c.post(f'/s/{U}/api/compare/shift', json={'a': 'A', 'b': 'A'}).status_code == 400
    assert c.post(f'/s/{world["plain"]}/api/compare/shift', json=body).status_code == 409


def test_stress_test_route(world, monkeypatch):
    """POST /api/compare/stress: a model (faked: finds every label unless the image is dark) under the brightness
    axis on A's labelled images: a job, then the cached result + summary with B placed from the shift report; the
    preview of one level; unknown model 404, bad axis 400."""
    from types import SimpleNamespace

    import numpy as np
    from conftest import wait_job

    from dataviz.models import predict, runners
    from dataviz.models.registry import ModelRegistry
    from dataviz.models.service import ModelService
    c, U, ctx = world['c'], world['U'], world['ctx']
    st = ctx.state_for(U)
    monkeypatch.setattr(ctx, 'models', ModelRegistry(ctx.datasets.root), raising=False)
    monkeypatch.setattr(ctx, 'model_service', ModelService(ctx.models, ctx.cfg), raising=False)
    m = ctx.models.upsert({'name': 'det', 'family': 'yolo', 'format': 'api', 'api': {'url': 'http://fake'}},
                          owner_id=world['alice']['id'], admin=True)
    gt = {nm: [st.samples[i] for i in ids] for nm, ids in st.by_image.items()}
    runner = SimpleNamespace(names=['cat', 'dog'], draft_size=None, imgsz=640, batch_size=4, max_batch=None)
    monkeypatch.setattr(runners, 'get_runner', lambda *a: runner)
    order = []

    def fake(mm, fp, wd, batch):
        out = []
        for im in batch:
            nm = order.pop(0) if order else None
            dark = float(np.asarray(im.convert('L')).mean()) < 40
            boxes = [] if dark or nm is None else [list(s['bbox']) + [0.9, s['cls']] for s in gt.get(nm, [])]
            out.append(np.array(boxes, np.float32).reshape(-1, 6))
        return out
    monkeypatch.setattr(predict, 'predict_batch', fake)
    from dataviz.image import stress as stresslib
    real_run = stresslib.run

    def run(state, mm, fp, wd, names, axes, conf, job=None, labelled=True):
        # each predict pass walks the names in order: feed the fake its image names
        n_pass = 1 + sum(len([lv for lv in stresslib.AXES[a][1] if lv != stresslib.AXES[a][2]]) for a in axes)
        order.extend(names * n_pass)
        return real_run(state, mm, fp, wd, names, axes, conf, job, labelled)
    monkeypatch.setattr(stresslib, 'run', run)
    body = {'member': 'A', 'model': m['id'], 'n': 50, 'axes': ['brightness']}
    r = c.post(f'/s/{U}/api/compare/stress', json=body)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first['ready'] is False
    wait_job(c, first['job_id'])
    got = c.post(f'/s/{U}/api/compare/stress', json=body).json()
    assert got['ready'] and got['result']['labelled'] and got['result']['n'] == 3
    rows = {x['level']: x['map50'] for x in got['result']['axes'][0]['rows']}
    assert rows[1.0] > 0.95 and rows[0.3] < rows[1.0]
    assert got['summary']['axes'][0]['b'] is None                       # no shift report key given
    from dataviz.llm.tools import visual
    out, summary = visual._compare_stress(SimpleNamespace(state=st), {})     # the assistant's view
    sv = out['stress']
    assert sv['model'] == 'det' and sv['axes'][0]['axis'] == 'brightness' and sv['axes'][0]['scores']['1.0'] > 0.95
    assert sv['b_report'] is None and 'Analyse conditions' in sv['note'] and summary.startswith('→ det on A')
    assert visual._compare_stress(SimpleNamespace(state=st), {'model': 'other'})[0]['stress'] is None
    assert c.get('/s/' + U + '/api/compare/stress/axes').json()['axes'][0]['key'] == 'brightness'
    pv = c.get(f'/s/{U}/api/compare/stress/preview', params={'name': 'A/same.jpg', 'axis': 'blur', 'level': 2})
    assert pv.status_code == 200 and pv.content[:2] == b'\xff\xd8'
    assert c.get(f'/s/{U}/api/compare/stress/preview', params={'name': 'A/same.jpg', 'axis': 'x', 'level': 2}).status_code == 400
    assert c.post(f'/s/{U}/api/compare/stress', json={**body, 'model': 'nope'}).status_code == 404
    assert c.post(f'/s/{U}/api/compare/stress', json={**body, 'member': 'Z'}).status_code == 400


def test_compare_tests_route(world):
    """GET /api/compare/tests: the class mix (chi-square), class changes (Bowker) and box statistics of a run,
    cached, unknown key 404, unknown feature 400; no feature computed -> no embedding test."""
    c, U = world['c'], world['U']
    key = c.post(f'/s/{U}/api/compare/run', json={'a': 'A', 'b': 'B'}).json()['key']
    t = c.get(f'/s/{U}/api/compare/tests', params={'key': key}).json()
    assert t['classes']['test'] == 'chi-square' and t['classes']['n_a'] == 4 and t['classes']['n_b'] == 4
    assert len(t['classes']['per']) == 2
    assert t['class_changes']['test'] == 'Bowker symmetry' and t['class_changes']['df'] == 1   # dog -> cat once
    assert set(t['box_stats']) == {'area', 'aspect', 'brightness', 'blur'}
    assert t['box_stats']['area']['n_a'] == 4 and t['embedding'] is None
    assert c.get(f'/s/{U}/api/compare/tests', params={'key': key}).json() == t
    assert c.get(f'/s/{U}/api/compare/tests', params={'key': 'nope'}).status_code == 404
    assert c.get(f'/s/{U}/api/compare/tests', params={'key': key, 'feature': 'nope'}).status_code == 400
    from types import SimpleNamespace
    from dataviz.llm.tools import visual
    out, summary = visual._compare_tests(SimpleNamespace(state=world['ctx'].state_for(U)), {})   # the assistant's view
    assert out['tests']['a'] == 'A' and out['tests']['class_changes']['df'] == 1 and summary.startswith('→ A vs B')
