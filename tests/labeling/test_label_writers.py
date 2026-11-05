"""The label format registry (labeling/writers): each image writer's files for a box / polygon / keypoints / class,
COCO streamed per split, OBB corners, the text / audio writers, task filtering, and the embeddings folder converter
(npy + meta.csv <-> CSV <-> Parquet). No database: rows are built the way ProjectStore.iter_task_export yields them."""

import csv
import json
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from dataviz.labeling import writers
from dataviz.labeling.sinks import LocalDirSink
from dataviz.labeling.writers import vector


def box(cid, x, y, w, h, **kw):
    return {'id': kw.pop('id', f'b{cid}{x}'), 'class_id': cid, 'geometry_type': 'bbox',
            'geometry': {'x': x, 'y': y, 'w': w, 'h': h, **kw}}


def media(i, name, w=200, h=100, rel=None, **kw):
    return {'id': f'm{i}', 'file_name': name, 'width': w, 'height': h, 'status': 'done',
            'meta': {'rel_path': rel or name}, **kw}


TASK = {'id': 't1', 'name': 'det', 'task_type': 'object_detection', 'config': {},
        'classes': [{'id': 'c0', 'name': 'cat'}, {'id': 'c1', 'name': 'dog'}]}
INDEX = {'c0': 0, 'c1': 1}
NAMES = ['cat', 'dog']


def run(keys, items, tmp_path, task=TASK, mode='bucket'):
    sink = LocalDirSink(tmp_path)
    if mode == 'bucket':
        ctx = writers.bucket_ctx(tmp_path, INDEX, lambda rel: 'labels/' + rel.rsplit('.', 1)[0] + '.txt', keys=keys)
    else:
        ctx = writers.export_ctx('det', INDEX, keys=keys)
    ws = []
    for k in keys:
        w = writers.get(k)()
        w.begin(sink, task, NAMES, ctx)
        ws.append(w)
    for m, rows in items:
        for w in ws:
            w.add(m, rows)
    return {w.key: w.finish() for w in ws}


ITEMS = [(media(1, 'a.jpg', rel='train/a.jpg'), [box('c1', .25, .5, .5, .25)]),
         (media(2, 'b.jpg', rel='val/b.jpg'), [box('c0', 0, 0, 1, 1), box('c1', .1, .1, .2, .2)]),
         (media(3, 'neg.jpg', rel='val/neg.jpg'), [])]


def test_registry_filters_by_task_and_media():
    keys = {f['key'] for f in writers.for_task('object_detection', 'image')}
    assert {'yolo', 'yolo_obb', 'coco', 'voc', 'labelme', 'createml', 'csv'} <= keys
    assert 'imagefolder' not in keys and 'conll' not in keys
    assert {f['key'] for f in writers.for_task('classification', 'image')} >= {'yolo', 'imagefolder', 'csv'}
    assert 'conll' in {f['key'] for f in writers.for_task('text_span', 'text')}
    assert writers.check(['COCO', 'yolo']) == ['yolo', 'coco']           # registry order, case-insensitive
    with pytest.raises(ValueError, match='unknown label format'):
        writers.check(['nope'])
    assert all(f['layout'] for f in writers.for_task(None, None))         # every writer shows a folder tree


def test_coco_streams_one_file_per_split(tmp_path):
    out = run(['coco'], ITEMS, tmp_path)
    assert out['coco']['splits'] == ['train', 'val'] and out['coco']['n_annotations'] == 3
    tr = json.loads((tmp_path / 'annotations/instances_train.json').read_text())
    va = json.loads((tmp_path / 'annotations/instances_val.json').read_text())
    assert [c['name'] for c in tr['categories']] == ['cat', 'dog'] and tr['categories'][0]['id'] == 1
    assert tr['images'] == [{'id': 1, 'file_name': 'train/a.jpg', 'width': 200, 'height': 100}]
    assert tr['annotations'][0]['bbox'] == [50.0, 50.0, 100.0, 25.0] and tr['annotations'][0]['category_id'] == 2
    assert len(va['images']) == 2 and len(va['annotations']) == 2          # the labeled negative is listed
    ids = [a['id'] for a in tr['annotations'] + va['annotations']]
    assert len(set(ids)) == len(ids)


def test_voc_labelme_createml_csv(tmp_path):
    run(['voc', 'labelme', 'createml', 'csv'], ITEMS, tmp_path)
    x = ET.parse(tmp_path / 'Annotations/train/a.xml').getroot()
    assert x.find('object/name').text == 'dog'
    bb = x.find('object/bndbox')
    assert [bb.find(k).text for k in ('xmin', 'ymin', 'xmax', 'ymax')] == ['50', '50', '150', '75']
    lm = json.loads((tmp_path / 'labelme/val/b.json').read_text())
    assert lm['imageWidth'] == 200 and len(lm['shapes']) == 2 and lm['shapes'][0]['shape_type'] == 'rectangle'
    assert lm['imagePath'].endswith('b.jpg')
    cm = json.loads((tmp_path / 'createml.json').read_text())
    a = next(e for e in cm if e['image'].endswith('a.jpg'))
    assert a['annotations'][0]['label'] == 'dog'
    assert a['annotations'][0]['coordinates'] == {'x': 100.0, 'y': 62.5, 'width': 100.0, 'height': 25.0}
    with open(tmp_path / 'labels.csv', newline='') as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 4                                                   # 3 shapes + the negative's empty row
    assert {r['label'] for r in rows} == {'cat', 'dog', ''}


def test_yolo_and_obb_keep_their_folders_apart(tmp_path):
    rot = [(media(1, 'a.jpg', rel='images/a.jpg'), [box('c0', .4, .4, .2, .2, angle=90)])]
    run(['yolo', 'yolo_obb'], rot, tmp_path)
    assert (tmp_path / 'labels/images/a.txt').read_text().startswith('0 0.5')
    obb = (tmp_path / 'labels-obb/images/a.txt').read_text().split()
    assert obb[0] == '0' and len(obb) == 9
    pts = np.array(obb[1:], float).reshape(4, 2)
    assert np.allclose(pts.mean(0), [.5, .5]) and np.all((pts >= 0) & (pts <= 1))
    assert (tmp_path / 'classes.txt').read_text().split() == NAMES
    assert 'names' in (tmp_path / 'data.yaml').read_text()


def test_polygon_and_keypoints_in_coco(tmp_path):
    seg = {**TASK, 'task_type': 'segmentation'}
    poly = {'id': 'p', 'class_id': 'c0', 'geometry_type': 'polygon', 'geometry': {'points': [[0, 0], [.5, 0], [.5, .5]]}}
    run(['coco'], [(media(1, 'a.jpg'), [poly])], tmp_path / 's', task=seg)
    a = json.loads((tmp_path / 's/annotations/instances_default.json').read_text())['annotations'][0]
    assert a['segmentation'] == [[0.0, 0.0, 100.0, 0.0, 100.0, 50.0]] and a['area'] == 2500.0

    kp = {**TASK, 'task_type': 'keypoints', 'config': {'keypoints': ['nose', 'tail'], 'skeleton': [[0, 1]]}}
    parent = box('c0', 0, 0, .5, .5, id='par')
    pts = {'id': 'k', 'class_id': 'c0', 'parent_id': 'par', 'geometry_type': 'keypoints',
           'geometry': {'points': [[.1, .2, 2], [0, 0, 0]]}}
    run(['coco'], [(media(1, 'a.jpg'), [parent, pts])], tmp_path / 'k', task=kp)
    d = json.loads((tmp_path / 'k/annotations/instances_default.json').read_text())
    assert d['categories'][0]['keypoints'] == ['nose', 'tail'] and d['categories'][0]['skeleton'] == [[1, 2]]
    k = next(x for x in d['annotations'] if 'keypoints' in x)
    assert k['keypoints'] == [20.0, 20.0, 2, 0, 0, 0] and k['num_keypoints'] == 1
    assert k['bbox'] == [0.0, 0.0, 100.0, 50.0]                             # the parent box


def test_export_layout_goes_under_the_task_folder(tmp_path):
    run(['coco', 'voc'], ITEMS[:1], tmp_path, mode='export')
    assert (tmp_path / 'det/annotations/instances_default.json').is_file()
    assert (tmp_path / 'det/Annotations/a.xml').is_file()


def test_text_and_audio_writers(tmp_path):
    (tmp_path / 'doc.txt').write_text('Alice met Bob')
    tt = {**TASK, 'task_type': 'text_span'}
    span = {'id': 's', 'class_id': 'c0', 'geometry_type': 'text_span', 'char_start': 0, 'char_end': 5, 'geometry': {}}
    run(['text_jsonl', 'conll'], [(media(1, 'doc.txt'), [span])], tmp_path, task=tt)
    j = json.loads((tmp_path / 'text.jsonl').read_text().splitlines()[0])
    assert j['text'] == 'Alice met Bob' and j['spans'] == [[0, 5, 'cat']]
    conll = (tmp_path / 'conll.txt').read_text()
    assert 'Alice\tB-cat' in conll and 'Bob\tO' in conll

    at = {**TASK, 'task_type': 'audio_segment'}
    seg = {'id': 'a', 'class_id': 'c1', 'geometry_type': 'time_segment', 't_start_ms': 500, 't_end_ms': 1500,
           'value_text': 'woof', 'geometry': {}}
    run(['audio_jsonl', 'segments_csv'], [(media(1, 'x.wav', duration_ms=2000), [seg])], tmp_path / 'au', task=at)
    a = json.loads((tmp_path / 'au/metadata.jsonl').read_text().splitlines()[0])
    assert a['text'] == 'woof' and a['segments'][0]['label'] == 'dog'
    with open(tmp_path / 'au/segments.csv', newline='') as f:
        r = next(csv.DictReader(f))
    assert float(r['start']) == 0.5 and float(r['end']) == 1.5


def test_coco_memory_stays_flat(tmp_path):
    import tracemalloc
    items = ((media(i, f'{i}.jpg'), [box('c0', .1, .1, .2, .2, id=f'{i}')]) for i in range(50_000))
    tracemalloc.start()
    run(['coco'], items, tmp_path)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 40 * 2 ** 20, f'peak {peak / 2 ** 20:.0f} MB'
    d = json.loads((tmp_path / 'annotations/instances_default.json').read_text())
    assert len(d['images']) == 50_000 and len(d['annotations']) == 50_000


# ---------------------------------------------------------------- embeddings folders
def _emb(folder, n=10):
    folder.mkdir()
    with open(folder / 'meta.csv', 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['id', 'label'])
        w.writerows([[f'r{i}', 'ab'[i % 2]] for i in range(n)])
    a = np.random.default_rng(0).random((n, 4), dtype=np.float32)
    np.save(folder / 'clip.npy', a)
    return a


def test_vector_npy_to_csv_and_back(tmp_path):
    a = _emb(tmp_path / 'e')
    r = vector.convert(tmp_path / 'e', tmp_path / 'c', 'csv')
    assert r == {'from': 'npy', 'to': 'csv', 'rows': 10, 'spaces': {'clip': 4}, 'files': ['embeddings.csv']}
    head = (tmp_path / 'c/embeddings.csv').read_text().splitlines()[0]
    assert head == 'id,label,clip_0,clip_1,clip_2,clip_3'
    vector.convert(tmp_path / 'c', tmp_path / 'n', 'npy')
    assert np.allclose(np.load(tmp_path / 'n/clip.npy'), a)
    assert (tmp_path / 'n/meta.csv').read_text().splitlines()[1] == 'r0,a'
    with pytest.raises(ValueError, match='already'):
        vector.convert(tmp_path / 'n', tmp_path / 'x', 'npy')
    with pytest.raises(ValueError, match='not an embeddings folder'):
        vector.convert(tmp_path, tmp_path / 'y', 'csv')


def test_vector_parquet_roundtrip(tmp_path):
    pytest.importorskip('pyarrow')
    a = _emb(tmp_path / 'e')
    vector.convert(tmp_path / 'e', tmp_path / 'p', 'parquet')
    vector.convert(tmp_path / 'p', tmp_path / 'n', 'npy')
    assert np.allclose(np.load(tmp_path / 'n/clip.npy'), a)
