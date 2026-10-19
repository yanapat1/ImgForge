"""Annotation import into labeling sessions: the parsers (YOLO detect / seg / pose, COCO boxes / polygons / RLE /
keypoints, CSV in pixels and normalised, headerless lists), image matching (relative path, file name, stem), the
three modes, class mapping / creation, uploads (a .zip of YOLO labels), a single file named by path, a database
(SQLite through the same SELECT path), and the job started right after a session is created."""

import io
import json
import zipfile

import orjson
import pytest

from conftest import make_jpeg, wait_job

from dataviz.labeling import importers as imp

pytest.importorskip('dataviz.labeling.media')

TASKS = [{'name': 'Detect', 'task_type': 'object_detection', 'classes': [{'name': 'cat'}]},
         {'name': 'Segment', 'task_type': 'segmentation', 'classes': ['house']},
         {'name': 'Pose', 'task_type': 'keypoints', 'config': {'keypoints': ['a', 'b']}, 'classes': ['person']}]


@pytest.fixture
def images(tmp_path):
    d = tmp_path / 'imgs'
    make_jpeg(d / 'train' / 'a.jpg', 100, 50)
    make_jpeg(d / 'train' / 'b.jpg', 100, 50)
    make_jpeg(d / 'val' / 'c.png', 200, 100)
    return d


def session(client, images):
    r = client.post('/api/label/projects', json={'name': 'Imp', 'source': {'kind': 'local', 'path': str(images)},
                                                 'tasks': TASKS})
    assert r.status_code == 200, r.text
    out = r.json()
    return out['project'], out['job_id']


def run(client, pid, body):
    r = client.post(f'/api/label/projects/{pid}/import-annotations', json=body)
    assert r.status_code == 200, r.text
    j = wait_job(client, r.json()['job_id'])
    assert j['status'] == 'done', j
    return j['result']


def anns(client, pid, name, tid):
    mid = next(m['id'] for m in client.get(f'/api/label/projects/{pid}/media').json()['items'] if m['file_name'] == name)
    return [a for a in client.get(f'/api/label/projects/{pid}/annotations/{mid}').json()['annotations'] if a['task_id'] == tid]


# ---------------------------------------------------------------- parsers
def test_yolo_lines_detect_seg_pose():
    rep = imp.Report()
    recs = list(imp.yolo_lines('a', '0 0.5 0.5 0.2 0.4\n\nx y\n1 0.1 0.1 0.3 0.1 0.2 0.3\n0 0.5 0.5 0 0.1\n',
                               'object_detection', 0, rep))
    assert [r.gtype for r in recs] == ['bbox', 'bbox']
    assert recs[0].geom == pytest.approx({'x': 0.4, 'y': 0.3, 'w': 0.2, 'h': 0.4})
    assert recs[1].geom == pytest.approx({'x': 0.1, 'y': 0.1, 'w': 0.2, 'h': 0.2})      # a polygon line: its extent
    assert rep.skipped == {'bad_line': 1, 'degenerate': 1}
    seg = list(imp.yolo_lines('a', '0 0.1 0.1 0.5 0.1 0.3 0.4\n0 0.5 0.5 0.2 0.2\n', 'segmentation', 0, imp.Report()))
    assert len(seg) == 2 and len(seg[1].geom['points']) == 4                           # a box line becomes a polygon
    pose = list(imp.yolo_lines('a', '0 0.5 0.5 0.2 0.2 0.4 0.4 2 0.6 0.6 1\n', 'keypoints', 2, imp.Report()))
    assert pose[0].geom == {'points': [[0.4, 0.4, 2], [0.6, 0.6, 1]]}


def test_coco_and_tables():
    doc = {'images': [{'id': 1, 'file_name': 'train/a.jpg', 'width': 100, 'height': 50}],
           'categories': [{'id': 7, 'name': 'dog'}],
           'annotations': [{'id': 1, 'image_id': 1, 'category_id': 7, 'bbox': [10, 5, 20, 10],
                            'segmentation': [[10, 5, 30, 5, 30, 15]], 'keypoints': [10, 5, 2, 0, 0, 0]},
                           {'id': 2, 'image_id': 1, 'category_id': 7, 'segmentation': {'counts': 'x', 'size': [50, 100]}},
                           {'id': 3, 'image_id': 9, 'category_id': 7, 'bbox': [0, 0, 1, 1]}]}
    rep = imp.Report()
    recs, names = imp.coco(orjson.dumps(doc), 'object_detection', rep)
    assert names == ['dog'] and len(recs) == 1 and recs[0].geom == pytest.approx({'x': 0.1, 'y': 0.1, 'w': 0.2, 'h': 0.2})
    assert rep.skipped == {'other_type': 1, 'no_image': 1}
    rep = imp.Report()
    seg, _ = imp.coco(orjson.dumps(doc), 'segmentation', rep)
    assert len(seg) == 1 and rep.skipped['rle'] == 1 and rep.skipped['no_image'] == 1
    kp, _ = imp.coco(orjson.dumps(doc), 'keypoints', imp.Report())
    assert kp[0].geom['points'] == [[0.1, 0.1, 2], [0.0, 0.0, 0]]
    with pytest.raises(ValueError):
        imp.coco(b'{"x": 1}', 'object_detection', imp.Report())
    px = imp.table(b'filename,class,xmin,ymin,xmax,ymax\na.jpg,cat,10,5,30,15\n', 'object_detection', imp.Report())
    assert px[0].px and px[0].geom == {'x': 10.0, 'y': 5.0, 'w': 20.0, 'h': 10.0}
    norm = imp.table(b'image\tlabel\tcx\tcy\tw\th\nb\tdog\t0.5\t0.5\t0.2\t0.2\n', 'object_detection', imp.Report())
    assert not norm[0].px and norm[0].geom == pytest.approx({'x': 0.4, 'y': 0.4, 'w': 0.2, 'h': 0.2})
    tags = imp.table(b'a.jpg cat\nb.jpg dog\n', 'classification', imp.Report())
    assert [(t.key, t.cls, t.gtype) for t in tags] == [('a.jpg', 'cat', 'tag'), ('b.jpg', 'dog', 'tag')]
    assert imp.guess_format({'labels/a.txt': b'0 0.5 0.5 0.1 0.1'}) == 'yolo'
    assert imp.guess_format({'list.txt': b'a.jpg cat 1 2 3 4'}) == 'text'


def test_match_keys():
    idx = imp.media_index([{'id': 'A', 'file_name': 'a.jpg', 'uri': 'train/a.jpg', 'width': 1, 'height': 1,
                            'meta': {'rel_path': 'train/a.jpg'}},
                           {'id': 'C', 'file_name': 'c.png', 'uri': 'val/c.png', 'width': 1, 'height': 1, 'meta': {}}])
    assert imp.find(idx, 'train/a.jpg')[0] == 'A'
    assert imp.find(idx, 'images/train/a.jpg')[0] == 'A'
    assert imp.find(idx, 'train/a')[0] == 'A'                    # a YOLO label key: stem with its folder
    assert imp.find(idx, 'C.PNG')[0] == 'C' and imp.find(idx, 'c')[0] == 'C' and imp.find(idx, 'val/c.jpg')[0] == 'C'
    assert imp.find(idx, 'nope.jpg') is None


# ---------------------------------------------------------------- routes + job
def test_yolo_zip_upload_preview_and_modes(client, images, tmp_path):
    p, job = session(client, images)
    pid, det = p['id'], p['tasks'][0]
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, 'w') as z:
        z.writestr('ds/classes.txt', 'cat\ndog\n')
        z.writestr('ds/labels/train/a.txt', '0 0.5 0.5 0.2 0.2\n1 0.25 0.25 0.1 0.1\n')
        z.writestr('ds/labels/val/c.txt', '1 0.5 0.5 0.5 0.5\n')
        z.writestr('ds/labels/train/zzz.txt', '0 0.5 0.5 0.2 0.2\n')     # no such image
    up = client.post('/api/label/annotations/upload', files=[('files', ('lbl.zip', zbuf.getvalue(), 'application/zip'))]).json()
    assert up['files'] == 1
    wait_job(client, job)
    src = {'kind': 'upload', 'upload_id': up['upload_id']}
    pv = client.post('/api/label/annotations/preview', json={'source': src, 'project_id': pid, 'task_id': det['id']}).json()
    assert pv['format'] == 'yolo' and pv['records'] == 4 and pv['images'] == 3 and pv['matched'] == 2
    assert pv['unmatched_sample'] == ['train/zzz']
    assert {c['name']: c['match'] for c in pv['classes']} == {'cat': 'cat', 'dog': None}
    res = run(client, pid, {'task_id': det['id'], 'format': 'yolo', 'source': src})
    assert res['annotations'] == 3 and res['images'] == 2 and res['classes_added'] == ['dog'] and res['unmatched'] == 1
    a = anns(client, pid, 'a.jpg', det['id'])
    assert len(a) == 2 and all(x['source'] == 'import' for x in a)
    assert a[0]['geometry'] == pytest.approx({'x': 0.4, 'y': 0.4, 'w': 0.2, 'h': 0.2})
    # the upload is removed after a successful import
    assert client.post('/api/label/annotations/preview', json={'source': src}).status_code == 400


def test_coco_file_by_path_modes_and_class_map(client, images, tmp_path):
    p, job = session(client, images)
    pid, det, seg = p['id'], p['tasks'][0], p['tasks'][1]
    doc = {'images': [{'id': 1, 'file_name': 'a.jpg', 'width': 100, 'height': 50},
                      {'id': 2, 'file_name': 'c.png', 'width': 200, 'height': 100}],
           'categories': [{'id': 1, 'name': 'Cat'}, {'id': 2, 'name': 'bird'}],
           'annotations': [{'id': 1, 'image_id': 1, 'category_id': 1, 'bbox': [10, 10, 20, 20],
                            'segmentation': [[10, 10, 30, 10, 30, 30]]},
                           {'id': 2, 'image_id': 2, 'category_id': 2, 'bbox': [0, 0, 100, 50]}]}
    f = tmp_path / 'coco.json'
    f.write_text(json.dumps(doc))
    wait_job(client, job)
    src = {'kind': 'local', 'path': str(f)}
    res = run(client, pid, {'task_id': det['id'], 'format': 'auto', 'source': src, 'class_map': {'bird': 'ignore'}})
    assert res['format'] == 'coco' and res['annotations'] == 1 and res['skipped'] == {'class_ignored': 1}
    assert res['classes_added'] == []                                    # Cat -> the task's cat
    res = run(client, pid, {'task_id': det['id'], 'format': 'coco', 'source': src})            # merge: + 2
    assert res['annotations'] == 2 and res['classes_added'] == ['bird']
    assert len(anns(client, pid, 'a.jpg', det['id'])) == 2
    res = run(client, pid, {'task_id': det['id'], 'format': 'coco', 'source': src, 'mode': 'replace'})
    assert len(anns(client, pid, 'a.jpg', det['id'])) == 1
    res = run(client, pid, {'task_id': det['id'], 'format': 'coco', 'source': src, 'mode': 'skip_labeled'})
    assert res['annotations'] == 0 and res['skipped']['image_already_labeled'] == 2
    res = run(client, pid, {'task_id': seg['id'], 'format': 'coco', 'source': src})
    assert res['annotations'] == 2                                       # a polygon, and a box-only one as a polygon
    assert anns(client, pid, 'a.jpg', seg['id'])[0]['geometry_type'] == 'polygon'


def test_csv_pixels_and_database(client, images, tmp_path):
    p, job = session(client, images)
    pid, det = p['id'], p['tasks'][0]
    wait_job(client, job)
    csvf = tmp_path / 'boxes.csv'
    csvf.write_text('image,class,x1,y1,x2,y2,score\ntrain/b.jpg,cat,10,10,60,40,0.9\nc.png,cat,0,0,200,100,\n')
    res = run(client, pid, {'task_id': det['id'], 'format': 'text', 'source': {'kind': 'local', 'path': str(csvf)}})
    assert res['annotations'] == 2
    b = anns(client, pid, 'b.jpg', det['id'])[0]
    assert b['geometry'] == pytest.approx({'x': 0.1, 'y': 0.2, 'w': 0.5, 'h': 0.6}) and b['score'] == pytest.approx(0.9)
    assert client.post(f'/api/label/projects/{pid}/import-annotations',
                       json={'task_id': det['id'], 'mode': 'wipe', 'source': {}}).status_code == 400
    assert client.post('/api/label/annotations/preview', json={'source': {'kind': 'local', 'path': str(tmp_path / 'none')}}).status_code == 400


def test_sql_guard():
    for bad in ('DELETE FROM x', 'SELECT 1; DROP TABLE x', 'select * from a where b = 1 union select 1 into c; update t set a=1',
                'WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x'):
        with pytest.raises(ValueError):
            imp._check_sql(bad)
    assert imp._check_sql("SELECT image, 'delete' AS class FROM t;") == "SELECT image, 'delete' AS class FROM t"


def test_database_modes_on_sqlite(monkeypatch, tmp_path):
    """Both database modes through SQLAlchemy (SQLite stands in for PostgreSQL / SQL Server; test_db.py runs the
    real servers): the tables db_export.py writes, and a user SELECT in pixels."""
    from sqlalchemy import create_engine
    from dataviz.labeling import db_export, schema
    url = f'sqlite:///{tmp_path / "db.sqlite"}'
    monkeypatch.setattr(db_export, 'engine_for', lambda conn: create_engine(url))
    conn = {'kind': 'postgres', 'table_prefix': 'lbl_'}
    md, prefix = db_export._metadata(conn)
    eng = create_engine(url)
    md.create_all(eng)
    t = schema.tables(md, prefix)
    with eng.begin() as c:
        c.execute(t['projects'].insert(), [{'id': 'P', 'name': 'p'}])
        c.execute(t['tasks'].insert(), [{'id': 'T', 'project_id': 'P', 'name': 'Detect', 'task_type': 'object_detection'},
                                         {'id': 'T2', 'project_id': 'P', 'name': 'Other', 'task_type': 'object_detection'}])
        c.execute(t['label_classes'].insert(), [{'id': 'C', 'task_id': 'T', 'name': 'cat', 'color': '#ff0000'}])
        c.execute(t['media'].insert(), [{'id': 'M', 'project_id': 'P', 'file_name': 'a.jpg', 'uri': 'train/a.jpg'}])
        c.execute(t['annotations'].insert(), [
            {'id': 'A1', 'project_id': 'P', 'task_id': 'T', 'media_id': 'M', 'class_id': 'C', 'geometry_type': 'bbox',
             'geometry': {'x': 0.1, 'y': 0.2, 'w': 0.3, 'h': 0.4}, 'is_deleted': False},
            {'id': 'A2', 'project_id': 'P', 'task_id': 'T', 'media_id': 'M', 'class_id': 'C', 'geometry_type': 'bbox',
             'geometry': {'x': 0, 'y': 0, 'w': 1, 'h': 1}, 'is_deleted': True},
            {'id': 'A3', 'project_id': 'P', 'task_id': 'T2', 'media_id': 'M', 'class_id': 'C', 'geometry_type': 'bbox',
             'geometry': {'x': 0, 'y': 0, 'w': 1, 'h': 1}, 'is_deleted': False}])   # every row: executemany takes row 1's keys
        c.exec_driver_sql('CREATE TABLE boxes (img TEXT, cls TEXT, xmin REAL, ymin REAL, xmax REAL, ymax REAL)')
        c.exec_driver_sql("INSERT INTO boxes VALUES ('a.jpg', 'dog', 10, 10, 50, 30), ('b.jpg', 'dog', 0, 0, 5, 5)")
    eng.dispose()
    rep = imp.Report()
    recs = imp.database(conn, 'object_detection', rep, 'dataviz', task_name='Detect')
    assert [(r.key, r.cls, r.geom) for r in recs] == [('train/a.jpg', 'cat', {'x': 0.1, 'y': 0.2, 'w': 0.3, 'h': 0.4})]
    recs = imp.database(conn, 'object_detection', rep, 'sql',
                        'SELECT img AS image, cls AS class, xmin, ymin, xmax, ymax FROM boxes')
    assert len(recs) == 2 and recs[0].px and recs[0].geom == {'x': 10.0, 'y': 10.0, 'w': 40.0, 'h': 20.0}
    assert len(imp.database(conn, 'object_detection', rep, 'sql', 'SELECT img AS image, cls AS class, xmin, ymin, xmax, ymax FROM boxes', limit=1)) == 1
    with pytest.raises(ValueError):
        imp.database(conn, 'object_detection', rep, 'sql', 'DROP TABLE boxes')
