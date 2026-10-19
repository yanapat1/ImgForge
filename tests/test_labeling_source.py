"""LabelingSource (a labeling task read live as a YOLO dataset) and the kind=labeling visualize session."""

import hashlib

import pytest
from PIL import Image

from conftest import make_jpeg, make_user, wait_job

from dataviz.data.labeling_source import LabelConflict, LabelingSource
from dataviz.labeling import formats
from dataviz.labeling.projects import ProjectRegistry
from dataviz.labeling.store import new_id

TASKS = [{'name': 'Detect', 'task_type': 'object_detection', 'classes': ['cat', 'dog']},
         {'name': 'Segment', 'task_type': 'segmentation', 'classes': ['house']},
         {'name': 'Pose', 'task_type': 'keypoints', 'config': {'keypoints': ['a', 'b']}, 'classes': ['person']},
         {'name': 'Tag', 'task_type': 'classification', 'classes': ['day', 'night']}]


def sha1(text):
    return hashlib.sha1(text.encode()).hexdigest()


def box(cid, x, y, w, h, **kw):
    return {'id': new_id(), 'class_id': cid, 'geometry_type': 'bbox', 'geometry': {'x': x, 'y': y, 'w': w, 'h': h}, **kw}


def make_project(reg, names=('a.jpg', 'b.jpg', 'c.jpg'), sizes=None, owner_id=None):
    """A labeling session with uploaded images (no import job: media rows written directly)."""
    owner_id = owner_id or getattr(reg, 'owner_id', None) or make_user('labeler')['id']
    entry = reg.create({'name': 'Proj', 'tasks': TASKS, 'source': {'kind': 'upload'}}, owner_id)
    pid = entry['id']
    images = reg.project_dir(pid) / 'images'
    rows = []
    for i, n in enumerate(names):
        w, h = (sizes or {}).get(n, (64, 48))
        make_jpeg(images / n, w, h, color=(40 * i, 90, 160))
        rows.append({'file_name': n, 'uri': n, 'width': w, 'height': h})
    st = reg.store(pid)
    st.add_media(rows)
    tasks = {t['task_type']: t for t in st.tasks()}
    mids = {m['file_name']: m['id'] for m in st.media_page(0, 100)['items']}
    return pid, st, tasks, mids


def put(st, mid, tid, rows):
    v = st.get_annotations(mid)['versions'].get(tid, 0)
    return st.put_annotations(mid, tid, rows, v)


@pytest.fixture
def lab(pg, tmp_path):
    reg = ProjectRegistry(tmp_path / 'labeling')
    reg.owner_id = make_user('labeler')['id']
    return reg


def test_read_label_matches_yolo_export(lab, monkeypatch):
    pid, st, tasks, mids = make_project(lab)
    det = tasks['object_detection']
    cat, dog = (c['id'] for c in det['classes'])
    put(st, mids['a.jpg'], det['id'], [box(cat, .1, .1, .2, .3), box(dog, .5, .4, .25, .25)])
    put(st, mids['c.jpg'], det['id'], [box(dog, 0, 0, 1, 1)])
    src = LabelingSource(lab, pid, det['id'])
    src.check()
    assert src.name == 'Proj · Detect' and src.kind == 'labeling' and src.root is None
    assert src.list_images() == ['a.jpg', 'b.jpg', 'c.jpg']
    assert src.list_labels() == ['a.txt', 'c.txt']                 # b has no shapes: not a label file
    assert src.read_classes() == ['cat', 'dog']
    cmap, _ = formats.class_map(st.task(det['id'])['classes'])
    exported = {m['file_name']: formats.yolo_detect(rows, cmap) for m, rows in st.iter_task_export(det['id'])}
    assert src.read_label('a.txt') == exported['a.jpg'] and src.read_label('c.txt') == exported['c.jpg']
    assert src.read_label('a.txt').splitlines()[1].startswith('1 ')
    # batched: one pass over the task, reused while nothing changes (no query per image)
    calls = []
    real = type(st).iter_task_export
    monkeypatch.setattr(type(st), 'iter_task_export', lambda self, *a, **k: (calls.append(1), real(self, *a, **k))[1])
    src2 = LabelingSource(lab, pid, det['id'])
    assert src2.read_labels(['a.txt', 'c.txt']) == [exported['a.jpg'], exported['c.jpg']]
    src2.read_labels(['a.txt']), src2.list_labels(), src2.read_label('c.txt')
    assert len(calls) == 1
    assert src.image_ref('a.jpg').startswith(f'label://{pid}/{det["id"]}/')


def test_fingerprint_follows_edits(lab):
    pid, st, tasks, mids = make_project(lab)
    det = tasks['object_detection']
    cat, dog = (c['id'] for c in det['classes'])
    b1 = box(cat, .1, .1, .2, .2)
    put(st, mids['a.jpg'], det['id'], [b1])
    src = LabelingSource(lab, pid, det['id'])
    fp0 = src.fingerprint()
    assert src.fingerprint() == fp0                                # stable
    put(st, mids['a.jpg'], det['id'], [{**b1, 'class_id': dog}])   # reclassify
    fp1 = src.fingerprint()
    assert fp1 != fp0 and src.read_label('a.txt').startswith('1 ')
    put(st, mids['a.jpg'], det['id'], [])                          # delete
    fp2 = src.fingerprint()
    assert fp2 not in (fp0, fp1) and src.list_labels() == []
    st.update_class(cat, name='kitten')                            # class rename
    assert src.fingerprint() != fp2 and src.read_classes() == ['kitten', 'dog']
    # another task's edits do not touch this one
    fp3 = src.fingerprint()
    seg = tasks['segmentation']
    put(st, mids['b.jpg'], seg['id'], [{'id': new_id(), 'class_id': seg['classes'][0]['id'], 'geometry_type': 'polygon',
                                        'geometry': {'points': [[.1, .1], [.5, .1], [.3, .6]]}}])
    assert src.fingerprint() == fp3
    # only_done: a commit changes the label set
    put(st, mids['c.jpg'], det['id'], [box(dog, .2, .2, .3, .3)])
    done = LabelingSource(lab, pid, det['id'], only_done=True)
    fp = done.fingerprint()
    assert done.list_labels() == []
    st.set_status(mids['c.jpg'], [det['id']], 'commit')
    assert done.fingerprint() != fp and done.list_labels() == ['c.txt']
    assert done.uri != src.uri


def test_segmentation_keypoints_and_classification(lab):
    pid, st, tasks, mids = make_project(lab)
    seg, pose, tag = tasks['segmentation'], tasks['keypoints'], tasks['classification']
    put(st, mids['a.jpg'], seg['id'], [{'id': new_id(), 'class_id': seg['classes'][0]['id'], 'geometry_type': 'polygon',
                                        'geometry': {'points': [[.1, .2], [.5, .2], [.3, .6]]}}])
    assert LabelingSource(lab, pid, seg['id']).read_label('a.txt') == '0 0.300000 0.400000 0.400000 0.400000\n'
    person = pose['classes'][0]['id']
    parent = box(person, .2, .2, .4, .4)
    put(st, mids['a.jpg'], pose['id'], [parent, {'id': new_id(), 'class_id': person, 'geometry_type': 'keypoints',
                                                 'parent_id': parent['id'],
                                                 'geometry': {'points': [[.3, .3, 2], [.5, .5, 2]]}}])
    # one line per instance: its parent box (not the box again on its own line)
    assert LabelingSource(lab, pid, pose['id']).read_label('a.txt') == '0 0.400000 0.400000 0.400000 0.400000\n'
    with pytest.raises(ValueError, match='classification'):
        LabelingSource(lab, pid, tag['id']).check()
    with pytest.raises(ValueError):
        LabelingSource(lab, pid, 'f' * 32).check()


def test_exif_rotated_image_is_transposed(lab):
    pid, st, tasks, mids = make_project(lab, names=('r.jpg',))
    path = lab.project_dir(pid) / 'images' / 'r.jpg'
    im = Image.new('RGB', (64, 32), (200, 10, 10))
    im.paste((10, 200, 10), (0, 0, 32, 32))                        # left half green in the stored pixels
    exif = im.getexif()
    exif[0x0112] = 6                                               # "rotate 90 CW to display"
    im.save(path, 'JPEG', exif=exif.tobytes(), quality=95)
    src = LabelingSource(lab, pid, tasks['object_detection']['id'])
    out = src.open_image('r.jpg')
    assert out.size == (32, 64)                                    # the orientation the labels were drawn on
    assert out.convert('RGB').getpixel((16, 8))[1] > 150           # stored left half is the displayed top
    make_jpeg(lab.project_dir(pid) / 'images' / 'r.jpg', 50, 20)
    assert src.open_image('r.jpg').size == (50, 20)                # no EXIF: untouched


def test_write_label_round_trip_and_conflict(lab):
    pid, st, tasks, mids = make_project(lab)
    det = tasks['object_detection']
    cat, dog = (c['id'] for c in det['classes'])
    put(st, mids['a.jpg'], det['id'], [box(cat, .1, .1, .2, .2), box(cat, .5, .5, .2, .2),
                                       box(dog, .3, .3, .1, .1, source='model', score=.9)])
    src = LabelingSource(lab, pid, det['id'])
    assert src.label_writable
    old = src.read_label('a.txt')
    lines = old.splitlines()
    v0 = st.get_annotations(mids['a.jpg'])['versions'][det['id']]
    # line 0 -> class 1, line 1 removed, line 2 kept, one box added
    new = '\n'.join(['1' + lines[0][1:], lines[2], '0 0.500000 0.500000 0.200000 0.200000']) + '\n'
    h = src.write_label('a.txt', new, sha1(old))
    assert h == sha1(new)
    assert src.read_label('a.txt') == new
    anns = st.get_annotations(mids['a.jpg'])
    assert anns['versions'][det['id']] == v0 + 1                   # one save through the store's edit API
    live = [a for a in anns['annotations'] if a['task_id'] == det['id']]
    assert len(live) == 3 and sorted(a['source'] for a in live) == ['manual', 'manual', 'model']
    # the label changed since it was read: refused, nothing written
    with pytest.raises(LabelConflict):
        src.write_label('a.txt', old, sha1(old))
    assert src.read_label('a.txt') == new
    # clamp: geometry of a box line changes in place
    fixed = new.replace(lines[2], '1 0.350000 0.350000 0.100000 0.050000')
    assert src.write_label('a.txt', fixed, h) == sha1(fixed)
    with pytest.raises(ValueError):
        src.write_label('a.txt', '7 0.5 0.5 0.1 0.1\n', None)      # no class 7
    assert src.write_label('a.txt', '', None) == sha1('')          # delete all
    assert src.list_labels() == []


def test_api_create_open_index(client):
    ctx = client.ctx
    pid, st, tasks, mids = make_project(ctx.labeling, owner_id=client.user['id'])
    det = tasks['object_detection']
    cat, dog = (c['id'] for c in det['classes'])
    put(st, mids['a.jpg'], det['id'], [box(cat, .1, .1, .2, .3), box(dog, .5, .4, .25, .25)])
    put(st, mids['b.jpg'], det['id'], [box(dog, .2, .2, .5, .5)])
    bad = client.post('/api/datasets', json={'kind': 'labeling', 'project_id': pid, 'task_id': tasks['classification']['id']})
    assert bad.status_code == 400 and 'classification' in bad.json()['detail']
    assert client.post('/api/datasets', json={'kind': 'labeling', 'project_id': pid, 'task_id': 'a' * 32}).status_code == 400
    assert client.post('/api/datasets', json={'kind': 'labeling'}).status_code == 400
    assert client.get('/api/datasets').json()['datasets'] == []           # nothing saved by the failures
    r = client.post('/api/datasets', json={'kind': 'labeling', 'project_id': pid, 'task_id': det['id'], 'name': ''})
    assert r.status_code == 200, r.text
    ds = r.json()
    assert ds['name'] == 'Proj · Detect' and ds['uri'] == f'label://{pid}/{det["id"]}'
    assert ds['labeling'] == {'project': 'Proj', 'task': 'Detect', 'task_type': 'object_detection'}
    assert ds['only_done'] is False

    from dataviz.state import State

    def open_dataset(d):                         # = cli.open_dataset without the feature registry / projections
        src = ctx.datasets.source(d, None, ctx.labeling)
        src.check()
        s = State(src, None, {'none': None}, jobs=ctx.jobs, cfg=ctx.cfg, workspace_root=ctx.datasets.root,
                  dataset_id=d['id'])
        s.index_job = ctx.jobs.submit(f'index:{s.tag}', 'Open', s._index_job, lane='io')
        ctx.state = s
        return s

    ctx.open_dataset = open_dataset
    job = client.post(f'/api/datasets/{ds["id"]}/open').json()['job_id']
    assert wait_job(client, job)['status'] == 'done'
    s = ctx.state
    assert len(s.samples) == 3 and s.classes == ['cat', 'dog']
    assert sorted(x['image'] for x in s.samples) == ['a.jpg', 'a.jpg', 'b.jpg']
    a0 = next(x for x in s.samples if x['image'] == 'a.jpg' and x['cls'] == 0)
    assert a0['bbox'] == [6, 5, 19, 19] and (a0['img_w'], a0['img_h']) == (64, 48)
    card = next(d for d in client.get('/api/datasets').json()['datasets'] if d['id'] == ds['id'])
    assert card['active'] and card['stats']['n_boxes'] == 3 and card['stats']['n_images'] == 2
    # the labeling session goes away: the card says so, opening fails cleanly
    ctx.labeling.delete(pid)
    card = next(d for d in client.get('/api/datasets').json()['datasets'] if d['id'] == ds['id'])
    assert 'deleted' in card['error']
    assert client.post(f'/api/datasets/{ds["id"]}/open').status_code == 400


def _rows(st, mid, tid):
    keys = ('id', 'class_id', 'geometry_type', 'geometry', 'attributes', 'parent_id', 'score', 'source')
    return sorted((tuple(repr(a[k]) for k in keys) for a in st.get_annotations(mid)['annotations'] if a['task_id'] == tid))


def test_restore_rows_brings_back_polygons_keypoints_and_attributes(lab):
    """The curation undo of a labeling source restores the store rows (same ids, polygons, keypoints, attributes,
    source, score), not a re-add of the text lines as new plain boxes."""
    pid, st, tasks, mids = make_project(lab)
    seg, pose = tasks['segmentation'], tasks['keypoints']
    house, person = seg['classes'][0]['id'], pose['classes'][0]['id']
    mid = mids['a.jpg']
    put(st, mid, seg['id'], [
        {'id': new_id(), 'class_id': house, 'geometry_type': 'polygon', 'attributes': {'occluded': True},
         'geometry': {'points': [[.1, .2], [.5, .2], [.3, .6]]}},
        {'id': new_id(), 'class_id': house, 'geometry_type': 'polygon', 'source': 'model', 'score': 0.9,
         'geometry': {'points': [[.6, .6], [.9, .6], [.8, .9]]}}])
    parent = box(person, .2, .2, .4, .4)
    put(st, mid, pose['id'], [parent, {'id': new_id(), 'class_id': person, 'geometry_type': 'keypoints',
                                       'parent_id': parent['id'], 'geometry': {'points': [[.3, .3, 2], [.5, .5, 1]]}}])
    for task in (seg, pose):
        src = LabelingSource(lab, pid, task['id'])
        orig = _rows(st, mid, task['id'])
        before = src.read_label('a.txt')
        after, rows = src.write_label_rows('a.txt', before.splitlines(keepends=True)[-1] if task is seg else '', sha1(before))
        assert _rows(st, mid, task['id']) != orig
        with pytest.raises(LabelConflict):
            src.restore_rows('a.txt', rows, sha1(before))           # not the label the fix left
        assert src.restore_rows('a.txt', rows, after) == sha1(before)
        assert _rows(st, mid, task['id']) == orig and src.read_label('a.txt') == before


def test_curation_fix_undo_on_a_labeling_session(lab, tmp_path):
    from dataviz.data import curation as cur
    from dataviz.state import State
    pid, st, tasks, mids = make_project(lab)
    seg = tasks['segmentation']
    house = seg['classes'][0]['id']
    poly = {'id': new_id(), 'class_id': house, 'geometry_type': 'polygon', 'attributes': {'note': 'x'},
            'geometry': {'points': [[.1, .2], [.5, .2], [.3, .6]]}}
    put(st, mids['a.jpg'], seg['id'], [poly, dict(poly, id=new_id(), attributes={})])     # the same polygon twice
    src = LabelingSource(lab, pid, seg['id'])

    class _Job:
        def progress(self, *a, **k):
            pass

    state = State(src, tmp_path / 'ws', {'none': None}, default_feature='none')
    state._index_job(_Job())
    c = cur.Curation(state)
    c.scan()
    orig = _rows(st, mids['a.jpg'], seg['id'])
    dup = next(it for it in c.all_items() if it['type'] == 'dup_box')
    res = c.fix([{'label': 'a.txt', 'expect_sha1': c._public(dup)['sha1'], 'ops': [{'op': 'delete', 'line': 0}]}])
    assert res['applied'] == 1 and res['errors'] == []
    assert len(_rows(st, mids['a.jpg'], seg['id'])) == 1
    u = c.undo(res['undo_id'])
    assert u['restored'] == 1 and u['errors'] == [] and u['conflicts'] == []
    assert _rows(st, mids['a.jpg'], seg['id']) == orig                  # the polygon with its attributes is back
    assert not state.deleted.any()


def test_image_editor_on_a_labeling_session(lab, tmp_path):
    """The visualizer's image editor on a labeling task: a moved box keeps its row, an added box becomes a new
    detection row (even on an image with no shapes yet), and undo puts the rows back."""
    from dataviz.data import curation as cur
    from dataviz.state import State
    pid, st, tasks, mids = make_project(lab)
    det = tasks['object_detection']
    cat, dog = (c['id'] for c in det['classes'])
    put(st, mids['a.jpg'], det['id'], [box(cat, .1, .1, .2, .2)])
    src = LabelingSource(lab, pid, det['id'])

    class _Job:
        def progress(self, *a, **k):
            pass

    state = State(src, tmp_path / 'ws', {'none': None}, default_feature='none')
    state._index_job(_Job())
    c = cur.Curation(state)                              # no scan: the editor works on one file
    b = c.image_boxes('a.jpg')
    assert [x['sid'] for x in b['boxes']] == [0]
    def live(mid):
        return [a for a in st.get_annotations(mid)['annotations'] if a['task_id'] == det['id']]
    before = _rows(st, mids['a.jpg'], det['id'])
    first_id = live(mids['a.jpg'])[0]['id']
    res = c.edit('a.jpg', b['sha1'], [{'op': 'set_box', 'line': 0, 'cx': 0.5, 'cy': 0.5, 'w': 0.2, 'h': 0.2},
                                      {'op': 'add', 'cls': 1, 'cx': 0.8, 'cy': 0.8, 'w': 0.2, 'h': 0.2}])
    assert res['applied'] == 2 and res['errors'] == [] and res['patch']['moved'] == [0]
    rows = live(mids['a.jpg'])
    assert len(rows) == 2 and first_id in [r['id'] for r in rows]                 # moved in place, same row
    assert sorted(r['class_id'] for r in rows) == sorted([cat, dog])
    g = next(r for r in rows if r['class_id'] == cat)['geometry']
    assert g['x'] == pytest.approx(0.4) and g['y'] == pytest.approx(0.4)
    # an image with no shapes (no label file in the YOLO view): the first box creates it
    e = c.image_boxes('b.jpg')
    assert e['boxes'] == []
    r2 = c.edit('b.jpg', e['sha1'], [{'op': 'add', 'cls': 0, 'cx': 0.5, 'cy': 0.5, 'w': 0.5, 'h': 0.5}])
    assert r2['errors'] == [] and len(live(mids['b.jpg'])) == 1
    assert state.pending_new == {'a.jpg': 1, 'b.jpg': 1}
    c.undo(res['undo_id'])
    assert _rows(st, mids['a.jpg'], det['id']) == before
