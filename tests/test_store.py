import uuid

import pytest

from conftest import make_user, new_store
from dataviz.labeling.store import Conflict, InUse


def hid():
    return uuid.uuid4().hex


def sql(q, **a):
    """Raw rows straight from the test database (what the store wrote)."""
    from sqlalchemy import text
    from dataviz import db
    with db.connect() as c:
        return c.execute(text(q), a).mappings().all()


@pytest.fixture
def proj(store):
    det = store.add_task('Detect', 'object_detection', None, ['cat', 'dog'])
    seg = store.add_task('Segment', 'segmentation', None, [{'name': 'house', 'color': '#123456'}])
    kp = store.add_task('Pose', 'keypoints', {'keypoints': ['a', 'b']}, ['person'])
    n = store.add_media([{'file_name': f'{i}.jpg', 'uri': f'{i}.jpg', 'width': 10, 'height': 10} for i in range(5)])
    assert n == 5
    return store, det, seg, kp, store.media_ids()


def box(cls, x=0.1, y=0.1, w=0.2, h=0.2, i=None):
    return {'id': i or hid(), 'class_id': cls, 'geometry_type': 'bbox', 'geometry': {'x': x, 'y': y, 'w': w, 'h': h}}


def test_tasks_classes_colors(proj):
    s, det, seg, kp, _ = proj
    assert [t['name'] for t in s.tasks()] == ['Detect', 'Segment', 'Pose']
    cat, dog = det['classes']
    assert cat['color'] != dog['color'] and cat['position'] == 0 and dog['position'] == 1
    assert seg['classes'][0]['color'] == '#123456'
    new = s.add_classes(det['id'], ['dog', 'horse', ''])
    assert [c['name'] for c in new] == ['horse']
    with pytest.raises(ValueError):
        s.add_task('bad', 'keypoints', {}, [])
    with pytest.raises(ValueError):
        s.add_task('bad', 'nope', None, [])
    c = s.update_class(dog['id'], name='doggo', hotkey='ctrl+2', position=0)
    assert c['name'] == 'doggo' and c['hotkey'] == 'ctrl+2'
    assert [x['name'] for x in s.task(det['id'])['classes']] == ['doggo', 'cat', 'horse']
    with pytest.raises(ValueError):
        s.update_class(dog['id'], name='CAT')


def test_put_diff_versions_and_bbox_extent(proj):
    s, det, _, _, ids = proj
    cat = det['classes'][0]['id']
    a, b = box(cat), box(cat, x=0.9, w=0.5)                          # b is clamped to the right edge
    r = s.put_annotations(ids[0], det['id'], [a, b], 0)
    assert r == {'version': 1, 'status': 'in_progress', 'n': 2}
    got = s.get_annotations(ids[0])
    assert got['versions'][det['id']] == 1
    gb = next(x for x in got['annotations'] if x['id'] == b['id'])
    assert gb['geometry'] == {'x': 0.9, 'y': 0.1, 'w': 0.1, 'h': 0.2}
    x, _, w, _ = s.bbox_extents([b['id']])[b['id']]
    assert (x, w) == (0.9, 0.1)
    # same set again: no version bump
    assert s.put_annotations(ids[0], det['id'], [a, b], 1)['version'] == 1
    # change a, drop b
    a2 = dict(a, geometry={'x': 0.2, 'y': 0.2, 'w': 0.1, 'h': 0.1})
    r = s.put_annotations(ids[0], det['id'], [a2], 1)
    assert r['version'] == 2 and r['n'] == 1
    rows = {x['id']: x for x in sql('SELECT id, version, is_deleted FROM lbl.annotations WHERE project_id = :p', p=s.pid)}
    assert rows[a['id']]['version'] == 2 and rows[b['id']]['is_deleted'] is True and rows[b['id']]['version'] == 2
    # undo the delete: the same id comes back
    s.put_annotations(ids[0], det['id'], [a2, b], 2)
    assert len(s.get_annotations(ids[0])['annotations']) == 2


def test_conflict_carries_server_rows(proj):
    s, det, _, _, ids = proj
    cat = det['classes'][0]['id']
    s.put_annotations(ids[1], det['id'], [box(cat)], 0)
    with pytest.raises(Conflict) as e:
        s.put_annotations(ids[1], det['id'], [], 0)
    assert e.value.version == 1 and len(e.value.annotations) == 1


def test_validation(proj):
    s, det, seg, kp, ids = proj
    cat = det['classes'][0]['id']
    with pytest.raises(ValueError, match='class'):
        s.put_annotations(ids[0], det['id'], [box(seg['classes'][0]['id'])], 0)
    with pytest.raises(ValueError, match='no area'):
        s.put_annotations(ids[0], det['id'], [box(cat, x=1.5)], 0)
    with pytest.raises(ValueError, match='cannot hold'):
        s.put_annotations(ids[0], det['id'], [dict(box(cat), geometry_type='polygon')], 0)
    house = seg['classes'][0]['id']
    poly = {'id': hid(), 'class_id': house, 'geometry_type': 'polygon', 'geometry': {'points': [[0, 0], [1, 1]]}}
    with pytest.raises(ValueError, match='at least 3'):
        s.put_annotations(ids[0], seg['id'], [poly], 0)
    poly['geometry']['points'] = [[-1, 0], [0.5, 2], [0.7, 0.2]]
    s.put_annotations(ids[0], seg['id'], [poly], 0)
    g = s.get_annotations(ids[0])['annotations'][0]['geometry']
    assert g['points'] == [[0.0, 0.0], [0.5, 1.0], [0.7, 0.2]]
    person = kp['classes'][0]['id']
    k = {'id': hid(), 'class_id': person, 'geometry_type': 'keypoints', 'geometry': {'points': [[0.1, 0.1, 2]]}}
    with pytest.raises(ValueError, match='2 keypoints'):
        s.put_annotations(ids[0], kp['id'], [k], 0)
    with pytest.raises(ValueError, match='listed twice'):
        b = box(cat)
        s.put_annotations(ids[0], det['id'], [b, b], 0)
    with pytest.raises(KeyError):
        s.put_annotations('nope', det['id'], [], 0)


def test_status_commit_skip_reopen_and_stats(proj):
    s, det, seg, _, ids = proj
    cat = det['classes'][0]['id']
    s.put_annotations(ids[0], det['id'], [box(cat)], 0)
    assert s.set_status(ids[0], [det['id'], seg['id']], 'commit') == {det['id']: 'done', seg['id']: 'done'}
    # editing a done image keeps it done
    assert s.put_annotations(ids[0], det['id'], [box(cat), box(cat)], 1)['status'] == 'done'
    s.set_status(ids[1], [det['id']], 'skip')
    st = s.stats()
    assert st['n_media'] == 5
    assert st['tasks'][det['id']] == {'done': 1, 'in_progress': 0, 'skipped': 1, 'todo': 3}
    assert s.set_status(ids[0], [det['id']], 'reopen') == {det['id']: 'in_progress'}
    assert s.set_status(ids[1], [det['id']], 'reopen') == {det['id']: 'todo'}
    page = s.media_page(0, 2, det['id'], 'in_progress')
    assert page['total'] == 1 and page['items'][0]['id'] == ids[0] and page['items'][0]['n'][det['id']] == 2
    assert s.media_page(0, 10)['total'] == 5
    assert s.media_page(0, 10, det['id'], 'todo')['total'] == 4


def test_class_delete_needs_reassign_or_delete(proj):
    s, det, _, _, ids = proj
    cat, dog = (c['id'] for c in det['classes'])
    s.put_annotations(ids[0], det['id'], [box(cat), box(dog)], 0)
    with pytest.raises(InUse):
        s.delete_class(cat)
    s.delete_class(cat, reassign_to=dog)
    anns = s.get_annotations(ids[0])
    assert {a['class_id'] for a in anns['annotations']} == {dog}
    assert anns['versions'][det['id']] == 2                     # open tabs must reload
    s.delete_class(dog, delete_annotations=True)
    assert s.get_annotations(ids[0])['annotations'] == []
    assert s.stats()['tasks'][det['id']]['in_progress'] == 0              # nothing left: back to todo
    assert sql('SELECT n_annotations FROM lbl.media_task_status WHERE media_id = :m', m=ids[0])[0]['n_annotations'] == 0


def test_add_media_skips_existing_and_iter_export(proj):
    s, det, _, _, ids = proj
    assert s.add_media([{'file_name': '0.jpg', 'uri': 'x/0.jpg'}, {'file_name': 'new.jpg', 'uri': 'new.jpg'}]) == 1
    assert len(s.media_ids()) == 6
    cat = det['classes'][0]['id']
    s.put_annotations(ids[2], det['id'], [box(cat), box(cat)], 0)
    s.put_annotations(ids[4], det['id'], [box(cat)], 0)
    s.set_status(ids[3], [det['id']], 'commit')
    out = list(s.iter_task_export(det['id']))
    assert [m['id'] for m, _ in out] == s.media_ids()
    assert [len(r) for _, r in out] == [0, 0, 2, 0, 1, 0]
    assert out[2][1][0]['geometry']['w'] == 0.2
    done = list(s.iter_task_export(det['id'], only_done=True))
    assert [(m['id'], len(r)) for m, r in done] == [(ids[3], 0)]
    rows = [r for b in s.iter_rows('annotations', {'media_id': [ids[2], ids[4]]}, batch=2) for r in b]
    assert len(rows) == 3 and isinstance(rows[0]['geometry'], dict) and rows[0]['is_deleted'] is False
    assert rows[0]['created_at'].tzinfo is not None


def test_task_soft_delete_and_reorder(proj):
    s, det, seg, kp, ids = proj
    s.reorder_tasks([kp['id'], det['id'], seg['id']])
    assert [t['name'] for t in s.tasks()] == ['Pose', 'Detect', 'Segment']
    s.delete_task(seg['id'])
    assert [t['name'] for t in s.tasks()] == ['Pose', 'Detect']
    with pytest.raises(ValueError):
        s.reorder_tasks([kp['id']])
    assert seg['id'] not in s.get_annotations(ids[0])['versions']


# ---------------------------------------------------------------- v2: model predictions, bulk delete, duplicate
def pred(cls, x=0.1, score=0.9):
    return {'class_id': cls, 'score': score, 'geometry_type': 'bbox', 'geometry': {'x': x, 'y': 0.1, 'w': 0.2, 'h': 0.2}}


def test_apply_predictions_keeps_manual_rows(proj):
    s, det, seg, kp, ids = proj
    cat, dog = (c['id'] for c in det['classes'])
    s.put_annotations(ids[0], det['id'], [box(dog, i='a' * 32)], 0)
    r = s.apply_predictions(ids[0], det['id'], [pred(cat), pred(cat, 0.5, 0.4), pred('x' * 32),
                                                {**pred(cat), 'geometry': {'x': 0.5, 'y': 0.5, 'w': 0, 'h': 0}}])
    assert r['version'] == 2 and r['n_model'] == 2
    by = {a['source']: [] for a in r['annotations']}
    for a in r['annotations']:
        by[a['source']].append(a)
    assert [a['id'] for a in by['manual']] == ['a' * 32]
    assert sorted(a['score'] for a in by['model']) == [0.4, 0.9]
    r = s.apply_predictions(ids[0], det['id'], [pred(dog, 0.3, 0.7)])          # replaces the model rows only
    assert r['version'] == 3 and len(r['annotations']) == 2
    m = s.media_item(ids[0])
    assert m['n'][det['id']] == 2 and m['status'][det['id']] == 'in_progress' and m['model_run'][det['id']] > 0
    # a fresh image goes todo -> in_progress; an empty prediction on a fresh image stays todo but is marked as run
    assert s.apply_predictions(ids[1], det['id'], [pred(cat)])['version'] == 1
    assert s.media_item(ids[1])['status'][det['id']] == 'in_progress'
    s.apply_predictions(ids[2], det['id'], [])
    m = s.media_item(ids[2])
    assert m['status'][det['id']] == 'todo' and m['model_run'][det['id']] and m['model_run'][seg['id']] is None
    # done stays done
    s.set_status(ids[3], [det['id']], 'commit')
    s.apply_predictions(ids[3], det['id'], [pred(cat)])
    assert s.media_item(ids[3])['status'][det['id']] == 'done'
    assert s.apply_predictions_many(det['id'], [(ids[3], [pred(cat)])], skip_done=True) == {}
    # remove: model rows of every image; manual rows and counts follow
    assert s.remove_model_annotations(det['id']) == {'removed': 3, 'images': 3}
    assert [a['source'] for a in s.get_annotations(ids[0])['annotations']] == ['manual']
    assert s.media_item(ids[1])['status'][det['id']] == 'todo' and s.media_item(ids[1])['n'][det['id']] == 0
    assert s.remove_model_annotations(det['id']) == {'removed': 0, 'images': 0}


def test_classification_single_tag_from_model(proj):
    s, *_ = proj
    t = s.add_task('Cls', 'classification', None, ['day', 'night'])
    day, night = (c['id'] for c in t['classes'])
    tag = lambda c, sc: {'class_id': c, 'score': sc, 'geometry_type': 'tag', 'geometry': {}}  # noqa: E731
    mid = s.media_ids()[0]
    r = s.apply_predictions(mid, t['id'], [tag(day, 0.3), tag(night, 0.8)])
    assert [(a['class_id'], a['score']) for a in r['annotations']] == [(night, 0.8)]
    s.put_annotations(mid, t['id'], [{'id': 'b' * 32, 'class_id': day, 'geometry_type': 'tag', 'geometry': {}}],
                      r['version'])
    r = s.apply_predictions(mid, t['id'], [tag(night, 0.9)])             # the user already picked one
    assert [a['class_id'] for a in r['annotations']] == [day]


def test_edited_model_shape_becomes_manual(proj):
    s, det, *_ , ids = proj
    cat, dog = (c['id'] for c in det['classes'])
    r = s.apply_predictions(ids[0], det['id'], [pred(cat), pred(cat, 0.5)])
    a, b = r['annotations']
    moved = dict(a, geometry={'x': 0.3, 'y': 0.3, 'w': 0.1, 'h': 0.1})
    v = s.put_annotations(ids[0], det['id'], [moved, b], r['version'])['version']
    rows = {x['id']: x for x in s.get_annotations(ids[0])['annotations']}
    assert (rows[a['id']]['source'], rows[a['id']]['score']) == ('manual', None)
    assert (rows[b['id']]['source'], rows[b['id']]['score']) == ('model', 0.9)
    # reclassified -> manual too
    v = s.put_annotations(ids[0], det['id'], [rows[a['id']], dict(rows[b['id']], class_id=dog)], v)['version']
    rows = {x['id']: x for x in s.get_annotations(ids[0])['annotations']}
    assert rows[b['id']]['source'] == 'manual'
    # the page's undo restores the model state (stored manual, sent back as model): kept as sent
    s.put_annotations(ids[0], det['id'], [a, rows[b['id']]], v)
    rows = {x['id']: x for x in s.get_annotations(ids[0])['annotations']}
    assert (rows[a['id']]['source'], rows[a['id']]['score']) == ('model', 0.9)


def test_class_stats_and_delete_classes_all_or_nothing(proj):
    s, det, seg, kp, ids = proj
    cat, dog = (c['id'] for c in det['classes'])
    s.put_annotations(ids[0], det['id'], [box(cat), box(cat), box(dog)], 0)
    s.put_annotations(ids[1], det['id'], [box(cat)], 0)
    st = s.class_stats()
    assert st[cat] == {'n_annotations': 3, 'n_images': 2} and st[dog] == {'n_annotations': 1, 'n_images': 1}
    assert st[seg['classes'][0]['id']] == {'n_annotations': 0, 'n_images': 0}
    with pytest.raises(KeyError):
        s.delete_classes([cat, 'f' * 32])
    assert s.class_stats()[cat]['n_annotations'] == 3 and len(s.task(det['id'])['classes']) == 2   # nothing changed
    with pytest.raises(ValueError):
        s.delete_classes([])
    v0 = s.get_annotations(ids[0])['versions'][det['id']]
    assert s.delete_classes([cat, dog, cat]) == {'deleted': 2, 'annotations': 4, 'images': 2}
    assert s.task(det['id'])['classes'] == [] and cat not in s.class_stats()
    got = s.get_annotations(ids[0])
    assert got['annotations'] == [] and got['versions'][det['id']] > v0 and got['media']['n'][det['id']] == 0


def test_duplicate_renews_every_id(pg, tmp_path):
    from dataviz.labeling.projects import ProjectRegistry
    reg = ProjectRegistry(tmp_path / 'labeling')
    owner = make_user('orig')
    e = reg.create({'name': 'Orig', 'tasks': [{'name': 'Det', 'task_type': 'object_detection', 'classes': ['a', 'b']},
                                              {'name': 'Pose', 'task_type': 'keypoints', 'config': {'keypoints': ['l']},
                                               'classes': ['p']}]}, owner['id'])
    s = reg.store(e['id'])
    det, pose = s.tasks()
    a, b = (c['id'] for c in det['classes'])
    s.update_task(det['id'], config={'model': {'connection_id': 'c1', 'model_id': 'm', 'class_map': {'x': a, 'y': 'ignore'}}})
    (reg.project_dir(e['id']) / 'images' / 'sub').mkdir(parents=True)
    for i in range(3):
        (reg.project_dir(e['id']) / 'images' / 'sub' / f'{i}.jpg').write_bytes(b'img%d' % i)
    s.add_media([{'file_name': f'{i}.jpg', 'uri': f'sub/{i}.jpg', 'width': 10, 'height': 10} for i in range(3)])
    ids = s.media_ids()
    (reg.project_dir(e['id']) / 'thumbs' / f'{ids[0]}.jpg').write_bytes(b't')
    (reg.project_dir(e['id']) / 'thumbs' / f'{ids[0]}_320.jpg').write_bytes(b't2')
    bx = box(a, i='1' * 32)
    s.put_annotations(ids[0], det['id'], [bx, box(b)], 0)
    s.put_annotations(ids[0], pose['id'], [{'id': hid(), 'class_id': pose['classes'][0]['id'], 'geometry_type': 'keypoints',
                                            'geometry': {'points': [[.1, .1, 2]]}, 'parent_id': bx['id']}], 0)
    s.apply_predictions(ids[1], det['id'], [pred(b)])
    s.set_status(ids[2], [det['id']], 'skip')
    s.add_export_run('r' * 32, det['id'], 'zip', 'x', {})
    reg.update(e['id'], export_default={'target': {'kind': 'zip'}, 'task_ids': [det['id']], 'classes': {det['id']: [a]}})

    before = {t: sql(f'SELECT * FROM lbl.{t} WHERE ' + ('id' if t == 'projects' else 'project_id') + ' = :p ORDER BY 1',
                     p=e['id'])
              for t in ('projects', 'tasks', 'label_classes', 'media', 'annotations', 'media_task_status', 'export_runs')}
    new = reg.duplicate(e['id'])
    assert new['name'] == 'Orig (copy)' and new['n_media'] == 3 and new['id'] != e['id']
    assert new['owner_id'] == owner['id'] and new['visibility'] == 'private'
    assert reg.duplicate(e['id'])['name'] == 'Orig (copy 2)'
    # another user's copy is theirs, and the name is free for them
    other = make_user('other')
    theirs = reg.duplicate(e['id'], owner_id=other['id'])
    assert theirs['owner_id'] == other['id'] and theirs['name'] == 'Orig (copy)'
    with pytest.raises(ValueError):
        reg.duplicate(e['id'], name='orig (COPY)')                     # typed + taken by this owner
    after = {t: sql(f'SELECT * FROM lbl.{t} WHERE ' + ('id' if t == 'projects' else 'project_id') + ' = :p ORDER BY 1',
                    p=e['id']) for t in before}
    assert after == before                                             # the original is untouched
    c = reg.store(new['id'])
    old_ids, new_ids = set(), set()
    for st, acc in ((s, old_ids), (c, new_ids)):
        for t in ('projects', 'tasks', 'label_classes', 'media', 'annotations', 'export_runs'):
            for rows in st.iter_rows(t):
                acc.update(r['id'] for r in rows)
    assert len(new_ids) == len(old_ids) and not old_ids & new_ids
    assert c.stats()['tasks'] != s.stats()["tasks"] and \
        list(c.stats()['tasks'].values()) == list(s.stats()['tasks'].values())     # same counts, new task ids
    ndet, npose = c.tasks()
    na, nb = (x['id'] for x in ndet['classes'])
    assert ndet['config']['model']['class_map'] == {'x': na, 'y': 'ignore'}
    nids = c.media_ids()
    got = c.get_annotations(nids[0])['annotations']
    kpt = [x for x in got if x['geometry_type'] == 'keypoints'][0]
    nbox = [x for x in got if x['class_id'] == na][0]
    assert kpt['parent_id'] == nbox['id'] and kpt['task_id'] == npose['id']
    m1 = [x for x in c.get_annotations(nids[1])['annotations']]
    assert [(x['source'], x['score'], x['class_id']) for x in m1] == [('model', 0.9, nb)]
    assert c.media_item(nids[1])['model_run'][ndet['id']] and c.media_item(nids[2])['status'][ndet['id']] == 'skipped'
    assert c.export_runs()[0]['task_id'] == ndet['id']
    nd = reg.project_dir(new['id'])
    assert (nd / 'images' / 'sub' / '1.jpg').read_bytes() == b'img1'
    assert (nd / 'images' / 'sub' / '1.jpg').stat().st_ino == (reg.project_dir(e['id']) / 'images' / 'sub' / '1.jpg').stat().st_ino
    assert sorted(p.name for p in (nd / 'thumbs').iterdir()) == sorted([f'{nids[0]}.jpg', f'{nids[0]}_320.jpg'])
    assert reg.get(new['id'])['export_default'] == {'target': {'kind': 'zip'}, 'task_ids': [ndet['id']],
                                                     'classes': {ndet['id']: [na]}}
    # the original is untouched and both are independent
    assert s.get_annotations(ids[0])['annotations'][0]['class_id'] in (a, b)
    c.put_annotations(nids[2], ndet['id'], [box(na)], 0)
    assert s.media_item(ids[2])['n'][det['id']] == 0


def test_apply_predictions_rechecks_scope_at_write(proj):
    """A label-all job lists its images first; ones the user labeled / skipped / committed since then are left
    alone by the write (the scope is applied again inside the write transaction)."""
    s, det, _, _, ids = proj
    cat, dog = (c['id'] for c in det['classes'])
    s.put_annotations(ids[0], det['id'], [box(dog, i='c' * 32)], 0)           # labeled after the listing
    s.set_status(ids[1], [det['id']], 'skip')
    s.set_status(ids[2], [det['id']], 'commit')
    items = [(m, [pred(cat)]) for m in ids[:4]]
    assert set(s.apply_predictions_many(det['id'], items, scope='unlabeled')) == {ids[3]}
    assert [a['source'] for a in s.get_annotations(ids[0])['annotations']] == ['manual']
    assert set(s.apply_predictions_many(det['id'], items, scope='not_done')) == {ids[0], ids[1], ids[3]}
    assert set(s.apply_predictions_many(det['id'], items, scope='all')) == set(ids[:4])
    assert set(s.apply_predictions_many(det['id'], items, skip_done=True)) == {ids[0], ids[1], ids[3]}
    with pytest.raises(ValueError):
        s.apply_predictions_many(det['id'], items, scope='nope')


def test_server_deletes_unlink_children(proj):
    s, det, _, _, ids = proj
    pose = s.add_task('Pose2', 'keypoints', {'keypoints': ['a', 'b']}, ['person', 'crowd'])
    person, crowd = (c['id'] for c in pose['classes'])
    cat = det['classes'][0]['id']
    kp = lambda parent, i: {'id': i, 'class_id': person, 'geometry_type': 'keypoints', 'parent_id': parent,  # noqa: E731
                            'geometry': {'points': [[0.2, 0.2, 2], [0.3, 0.3, 2]]}}
    # a pose set linked to a model box of ANOTHER task: removing the model rows unlinks it, its tab reloads
    mbox = s.apply_predictions(ids[0], det['id'], [pred(cat)])['annotations'][0]['id']
    s.put_annotations(ids[0], pose['id'], [kp(mbox, 'd' * 32)], 0)
    v = s.get_annotations(ids[0])['versions'][pose['id']]
    s.remove_model_annotations(det['id'])
    got = s.get_annotations(ids[0])
    k = next(a for a in got['annotations'] if a['id'] == 'd' * 32)
    assert k['parent_id'] is None and k['version'] == 2 and got['versions'][pose['id']] == v + 1
    # replaced model rows (a new prediction run) unlink too
    mbox = s.apply_predictions(ids[1], det['id'], [pred(cat)])['annotations'][0]['id']
    s.put_annotations(ids[1], pose['id'], [kp(mbox, 'e' * 32)], 0)
    s.apply_predictions(ids[1], det['id'], [pred(cat, 0.5)])
    assert next(a for a in s.get_annotations(ids[1])['annotations'] if a['id'] == 'e' * 32)['parent_id'] is None
    # deleting a class with its annotations unlinks the sets of the boxes it took along (same task)
    pbox = {**box(crowd, i='f' * 32)}
    s.put_annotations(ids[2], pose['id'], [pbox, kp('f' * 32, '9' * 32)], 0)
    s.delete_class(crowd, delete_annotations=True)
    rows = s.get_annotations(ids[2])['annotations']
    assert [(a['id'], a['parent_id']) for a in rows] == [('9' * 32, None)]


def test_delete_class_resets_status_and_prunes_model_map(proj):
    s, det, _, _, ids = proj
    cat, dog = (c['id'] for c in det['classes'])
    s.put_annotations(ids[0], det['id'], [box(cat)], 0)                        # only cat: back to todo
    s.put_annotations(ids[1], det['id'], [box(cat), box(dog)], 0)              # a dog is left: in_progress
    s.update_task(det['id'], config={'model': {'connection_id': 'c', 'model_id': 'm',
                                               'class_map': {'kitty': cat, 'puppy': dog, 'x': 'ignore'}}})
    s.delete_classes([cat])
    assert s.media_item(ids[0])['status'][det['id']] == 'todo' and s.media_item(ids[0])['n'][det['id']] == 0
    assert s.media_item(ids[1])['status'][det['id']] == 'in_progress'
    assert s.task(det['id'])['config']['model']['class_map'] == {'kitty': 'ignore', 'puppy': dog, 'x': 'ignore'}
    assert s.stats()['tasks'][det['id']]['in_progress'] == 1


def test_review_manual_bulk_and_clearing(proj):
    s, det, seg, _, ids = proj
    cat = det['classes'][0]['id']
    d = det['id']
    for m in ids[:3]:
        s.put_annotations(m, d, [box(cat)], 0)
        s.set_status(m, [d], 'commit')
    s.put_annotations(ids[3], d, [box(cat)], 0)            # in progress: never reviewed
    # bulk: only done images, only the ones without a review
    assert s.set_review([d], 'manual', [ids[0]]) == {d: 1}
    assert s.set_review([d], 'bulk') == {d: 2}
    assert s.set_review([d], 'bulk') == {d: 0}              # again: nothing left, a manual mark is never downgraded
    assert s.stats()['review'][d] == {'manual': 1, 'bulk': 2}
    assert s.stats()['tasks'][d] == {'done': 3, 'in_progress': 1, 'skipped': 0, 'todo': 1}
    # a person re-checking a bulk image upgrades it
    assert s.set_review([d], 'manual', [ids[1], ids[3]]) == {d: 1}
    assert s.stats()['review'][d] == {'manual': 2, 'bulk': 1}
    # the media list carries it, and the filter finds each group
    items = {x['id']: x for x in s.media_page(0, 10)['items']}
    assert items[ids[0]]['review'] == {d: 'manual'} and items[ids[2]]['review'] == {d: 'bulk'}
    assert items[ids[3]]['review'] == {}
    assert s.media_page(0, 10, d, review='manual')['total'] == 2
    assert s.media_page(0, 10, d, review='bulk')['total'] == 1
    assert s.media_page(0, 10, d, review='none')['total'] == 0
    with pytest.raises(ValueError):
        s.media_page(0, 10, d, review='nope')
    # a label edit, a reopen and a class delete each put the image back to not reviewed
    s.put_annotations(ids[0], d, [box(cat), box(cat)], 1)
    assert s.media_page(0, 10, d, review='none')['total'] == 1
    s.set_status(ids[1], [d], 'reopen')
    s.set_status(ids[1], [d], 'commit')
    assert s.media_page(0, 10, d, review='none')['total'] == 2
    # committing again an image that is done and reviewed keeps the review
    s.set_status(ids[2], [d], 'commit')
    assert s.stats()['review'][d] == {'manual': 0, 'bulk': 1}
    assert s.set_review([d], 'clear') == {d: 1}
    assert s.stats()['review'][d] == {'manual': 0, 'bulk': 0}
    with pytest.raises(ValueError):
        s.set_review([d], 'nope')
    with pytest.raises(KeyError):
        s.set_review(['x' * 32], 'bulk')


def test_review_class_delete_clears(proj):
    s, det, _, _, ids = proj
    cat, dog = (c['id'] for c in det['classes'])
    d = det['id']
    s.put_annotations(ids[0], d, [box(cat)], 0)
    s.set_status(ids[0], [d], 'commit')
    s.set_review([d], 'bulk')
    s.delete_class(cat, reassign_to=dog)
    assert s.stats()['review'][d] == {'manual': 0, 'bulk': 0}


# ---------------------------------------------------------------- PostgreSQL: one shared schema for every project
def _fill(s, prefix, n=4):
    det = s.add_task('Detect', 'object_detection', None, ['cat', 'dog'])
    s.add_media([{'file_name': f'{prefix}{i}.jpg', 'uri': f'{prefix}{i}.jpg', 'width': 10, 'height': 10}
                 for i in range(n)])
    ids = s.media_ids()
    cat, dog = (c['id'] for c in det['classes'])
    s.put_annotations(ids[0], det['id'], [box(cat), box(dog)], 0)
    s.put_annotations(ids[1], det['id'], [box(cat)], 0)
    s.set_status(ids[1], [det['id']], 'commit')
    s.set_review([det['id']], 'bulk')
    s.add_export_run(hid(), det['id'], 'zip', 'x', {})
    return det, ids


def test_projects_never_see_each_other(store):
    """Two projects in the same database: every read is scoped to its own project_id."""
    a = store
    b = new_store(a.owner['id'], 'other')
    adet, aids = _fill(a, 'shared_', 4)
    bdet, bids = _fill(b, 'shared_', 6)                  # the same file names: unique per project only
    assert set(aids).isdisjoint(bids)
    for s, det, ids, n in ((a, adet, aids, 4), (b, bdet, bids, 6)):
        other = bdet if s is a else adet
        assert [t['id'] for t in s.tasks()] == [det['id']] and s.media_count() == n and len(ids) == n
        assert s.media_page(0, 100)['total'] == n
        assert {x['id'] for x in s.media_page(0, 100)['items']} == set(ids)
        assert s.media_page(0, 100, det['id'], 'done')['total'] == 1
        assert s.media_page(0, 100, det['id'], 'todo')['total'] == n - 2
        # strip filters: name, wildcard, regex, classes, counts, review
        assert s.media_page(0, 100, q='shared')['total'] == n
        assert s.media_page(0, 100, q='SHARED_*.jpg')['total'] == n
        assert s.media_page(0, 100, q='/^shared_\\d/')['total'] == n
        cat = det['classes'][0]['id']
        ocat = other['classes'][0]['id']
        assert s.media_page(0, 100, cls=[cat])['total'] == 2
        assert s.media_page(0, 100, cls=[ocat])['total'] == 0
        assert s.media_page(0, 100, cls=[ocat], cls_mode='none')['total'] == n
        assert s.media_page(0, 100, min_ann=1)['total'] == 2
        assert s.media_page(0, 100, task_id=det['id'], max_ann=0)['total'] == n - 2
        assert s.media_page(0, 100, task_id=det['id'], review='bulk')['total'] == 1
        st = s.stats()
        assert st['n_media'] == n and list(st['tasks']) == [det['id']]
        assert st['tasks'][det['id']] == {'done': 1, 'in_progress': 1, 'skipped': 0, 'todo': n - 2}
        assert set(s.class_stats()) == {c['id'] for c in det['classes']}
        # export iterators and raw rows
        out = list(s.iter_task_export(det['id']))
        assert [m['id'] for m, _ in out] == ids and sum(len(r) for _, r in out) == 3
        assert all(not r for _, r in s.iter_task_export(other['id']))      # another project's task: nothing
        for t in ('tasks', 'label_classes', 'media', 'annotations', 'media_task_status', 'export_runs'):
            rows = [r for bt in s.iter_rows(t) for r in bt]
            assert rows and all(r['project_id'] == s.pid for r in rows), t
        assert [r['id'] for bt in s.iter_rows('projects') for r in bt] == [s.pid]
        assert len(s.export_runs()) == 1 and len(s.media_names()) == n and len(s.media_for_import()) == n
        cnt, gen = s.predict_targets(det['id'], 'all')
        assert cnt == n and [r[0] for batch in gen for r in batch] == ids
        # another project's ids are unknown here
        with pytest.raises(KeyError):
            s.media_row((bids if s is a else aids)[0])
        with pytest.raises(KeyError):
            s.task(other['id'])
        with pytest.raises(KeyError):
            s.put_annotations((bids if s is a else aids)[0], det['id'], [], 0)
        assert s.media_rows([(bids if s is a else aids)[0]]) == {}
        assert s.parent_boxes([x['id'] for x in (b if s is a else a).get_annotations(
            (bids if s is a else aids)[0])['annotations']]) == []
    # writes stay scoped: a review / model removal / class delete in one project leaves the other alone
    a.set_review([adet['id']], 'clear')
    assert b.stats()['review'][bdet['id']] == {'manual': 0, 'bulk': 1}
    a.delete_class(adet['classes'][0]['id'], delete_annotations=True)
    assert b.class_stats()[bdet['classes'][0]['id']]['n_annotations'] == 2


def test_created_by_and_committed_by(store):
    s = store
    det = s.add_task('Detect', 'object_detection', None, ['cat'])
    s.add_media([{'file_name': '0.jpg', 'uri': '0.jpg', 'width': 10, 'height': 10}])
    mid = s.media_ids()[0]
    uid = s.owner['id']
    b = box(det['classes'][0]['id'])
    s.put_annotations(mid, det['id'], [b], 0, user=uid)
    s.set_status(mid, [det['id']], 'commit', user=uid)
    assert sql('SELECT created_by FROM lbl.annotations WHERE id = :i', i=b['id'])[0]['created_by'] == uid
    row = sql('SELECT committed_by, committed_at, project_id FROM lbl.media_task_status WHERE media_id = :m', m=mid)[0]
    assert row['committed_by'] == uid and row['committed_at'] > 0 and row['project_id'] == s.pid
    # a reopen keeps who committed it last; a skip does not change it either
    s.set_status(mid, [det['id']], 'reopen', user=None)
    assert sql('SELECT committed_by FROM lbl.media_task_status WHERE media_id = :m', m=mid)[0]['committed_by'] == uid


def test_concurrent_puts_one_conflict(store):
    """Two writers with the same base_version (two tabs, two server processes): exactly one wins."""
    import threading
    from dataviz.labeling.store import ProjectStore
    det = store.add_task('Detect', 'object_detection', None, ['cat'])
    store.add_media([{'file_name': '0.jpg', 'uri': '0.jpg', 'width': 10, 'height': 10}])
    mid = store.media_ids()[0]
    cat = det['classes'][0]['id']
    for rnd in range(5):
        base = store.get_annotations(mid)['versions'][det['id']]
        go = threading.Barrier(2)
        res = []

        def put(k):
            st = ProjectStore(store.pid)                 # its own object, like another process
            go.wait()
            try:
                res.append(st.put_annotations(mid, det['id'], [box(cat, x=0.1 * (k + 1))], base))
            except Conflict as e:
                res.append(e)
        ts = [threading.Thread(target=put, args=(k,)) for k in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert sum(isinstance(r, Conflict) for r in res) == 1, rnd
        ok = next(r for r in res if not isinstance(r, Conflict))
        assert ok['version'] == base + 1
        assert next(r for r in res if isinstance(r, Conflict)).version == base + 1


def test_nested_write_joins_and_rolls_back(store):
    s = store
    det = s.add_task('Detect', 'object_detection', None, ['cat'])
    with s.write():
        s.add_classes(det['id'], ['dog'])
        assert [c['name'] for c in s.task(det['id'])['classes']] == ['cat', 'dog']   # its own uncommitted row
    assert len(s.task(det['id'])['classes']) == 2
    with pytest.raises(RuntimeError):
        with s.write():
            s.add_classes(det['id'], ['horse'])
            raise RuntimeError('boom')
    assert [c['name'] for c in s.task(det['id'])['classes']] == ['cat', 'dog']


def test_delete_cascades_every_row(pg, tmp_path):
    from dataviz.labeling.projects import ProjectRegistry
    reg = ProjectRegistry(tmp_path / 'labeling')
    owner = make_user('del')
    keep = reg.create({'name': 'Keep', 'tasks': [{'name': 'D', 'task_type': 'object_detection', 'classes': ['a']}]},
                      owner['id'])
    e = reg.create({'name': 'Gone', 'tasks': [{'name': 'D', 'task_type': 'object_detection', 'classes': ['a']}]},
                   owner['id'])
    for pid in (keep['id'], e['id']):
        s = reg.store(pid)
        _fill(s, 'x', 3)
    reg.delete(e['id'], delete_files=True)
    for t in ('tasks', 'label_classes', 'media', 'annotations', 'annotation_relations', 'media_task_status',
              'export_runs'):
        assert sql(f'SELECT 1 FROM lbl.{t} WHERE project_id = :p', p=e['id']) == [], t
    assert sql('SELECT 1 FROM lbl.projects WHERE id = :p', p=e['id']) == []
    assert sql('SELECT 1 FROM app.label_projects WHERE session_id = :p', p=e['id']) == []
    assert not reg.project_dir(e['id']).exists()
    with pytest.raises(KeyError):
        reg.store(e['id'])
    with pytest.raises(KeyError):
        reg.delete(e['id'])
    assert reg.store(keep['id']).media_count() == 3 and len(sql('SELECT 1 FROM lbl.annotations')) == 3


def test_registry_entry_list_and_names(pg, tmp_path):
    from dataviz.data import sessions
    from dataviz.labeling.projects import ProjectRegistry
    reg = ProjectRegistry(tmp_path / 'labeling')
    alice, bob = make_user('alice'), make_user('bob')
    body = {'name': 'Cars', 'tags': ['x'], 'review': True, 'source': {'kind': 'upload'},
            'tasks': [{'name': 'D', 'task_type': 'object_detection', 'classes': ['a']}]}
    e = reg.create(body, alice['id'])
    assert set(e) >= {'id', 'name', 'description', 'created', 'updated', 'source', 'export_default', 'n_media',
                      'task_ids', 'model_connections', 'tags', 'review', 'owner_id', 'visibility'}
    assert e['owner_id'] == alice['id'] and e['tags'] == ['x'] and e['review'] is True and len(e['task_ids']) == 1
    with pytest.raises(ValueError):
        reg.create(body, alice['id'])                     # unique per owner
    assert reg.create(body, bob['id'])['owner_id'] == bob['id']
    assert [p['name'] for p in reg.list(alice)] == ['Cars'] and reg.list(alice)[0]['my_role'] == 'owner'
    assert reg.list(bob)[0]['owner']['id'] == bob['id']
    sessions.set_sharing(e['id'], 'private', [{'user_id': bob['id'], 'role': 'labeler'}], alice['id'])
    assert len(reg.list(bob)) == 2 and len(reg.list(bob, 'shared')) == 1 and len(reg.list(None)) == 2
    up = reg.update(e['id'], name='Trucks', description='d', tags=['y'], export_default={'target': {'kind': 'zip'}})
    assert (up['name'], up['description'], up['tags']) == ('Trucks', 'd', ['y'])
    assert reg.store(e['id']).project_row()['name'] == 'Trucks'
    assert reg.set_counts(e['id'])['n_media'] == 0
    reg.update(e['id'], source={'kind': 's3', 'connection_id': 'c9', 'bucket': 'b'})
    assert reg.uses_connection('c9') == ['Trucks'] and reg.uses_connection('nope') == []
    assert sorted(x['name'] for x in reg.entries()) == ['Cars', 'Trucks']
