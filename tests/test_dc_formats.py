"""Data Center format detection (dataviz/datacenter/formats.py): pure, on temp folders, no database."""

import json

import numpy as np

from conftest import make_jpeg
from dataviz.datacenter import formats, store


def detect(path):
    return formats.detect(formats.LocalTree(path))


def yolo(root, line='0 0.5 0.5 0.2 0.2', splits=('train', 'val'), n=3, yaml_text='names: [cat, dog]\n',
         split_first=False):
    for s in splits:
        for i in range(n):
            img = root / s / 'images' / f'{s}{i}.jpg' if split_first else root / 'images' / s / f'{s}{i}.jpg'
            lab = root / s / 'labels' / f'{s}{i}.txt' if split_first else root / 'labels' / s / f'{s}{i}.txt'
            make_jpeg(img, 16, 16)
            lab.parent.mkdir(parents=True, exist_ok=True)
            lab.write_text(line + '\n' + line + '\n')
    if yaml_text:
        (root / 'data.yaml').write_text(yaml_text)
    return root


def test_yolo_detect(tmp_path):
    f = detect(yolo(tmp_path / 'd'))
    assert f['format'] == 'yolo-detect' and f['label'] == 'YOLO detect'
    assert f['confidence'] >= 0.9 and f['classes'] == 2
    ck = {c['name']: c for c in f['checks']}
    assert ck['images/']['count'] == 6 and ck['images/']['detail'] == 'train · val'
    assert ck['mirror']['ok'] and 'data.yaml resolves' in f['reason']


def test_yolo_split_first_and_classes_txt(tmp_path):
    root = yolo(tmp_path / 'd', split_first=True, yaml_text=None)
    (root / 'classes.txt').write_text('a\nb\nc\n')
    f = detect(root)
    assert f['format'] == 'yolo-detect' and f['classes'] == 3 and f['splits'] == {'train': 3, 'val': 3}


def test_yolo_seg_pose_obb(tmp_path):
    seg = detect(yolo(tmp_path / 's', line='0 0.1 0.1 0.5 0.1 0.5 0.5\n1 0.1 0.1 0.5 0.1 0.5 0.5 0.3 0.6 0.2 0.2'))
    assert seg['format'] == 'yolo-seg'
    pose = detect(yolo(tmp_path / 'p', line='0 0.5 0.5 0.2 0.2 0.4 0.4 2 0.6 0.6 2',
                       yaml_text='names: [person]\nkpt_shape: [2, 3]\n'))
    assert pose['format'] == 'yolo-pose' and pose['classes'] == 1
    obb = detect(yolo(tmp_path / 'o', line='0 0.1 0.1 0.5 0.1 0.5 0.5 0.1 0.5'))
    assert obb['format'] == 'yolo-obb'


def test_coco(tmp_path):
    root = tmp_path / 'c'
    make_jpeg(root / 'images' / 'a.jpg')
    (root / 'annotations').mkdir()
    (root / 'annotations' / 'instances_train.json').write_text(json.dumps({
        'images': [{'id': 1, 'file_name': 'a.jpg'}], 'annotations': [{'id': 1, 'image_id': 1, 'bbox': [0, 0, 1, 1]}],
        'categories': [{'id': 1, 'name': 'cat'}, {'id': 2, 'name': 'dog'}]}))
    f = detect(root)
    assert f['format'] == 'coco' and f['classes'] == 2 and f['confidence'] >= 0.9


def test_voc(tmp_path):
    root = tmp_path / 'v'
    make_jpeg(root / 'JPEGImages' / 'a.jpg')
    (root / 'Annotations').mkdir()
    for i, cls in enumerate(('cat', 'dog')):
        (root / 'Annotations' / f'{i}.xml').write_text(
            f'<annotation><filename>{i}.jpg</filename><object><name>{cls}</name><bndbox><xmin>1</xmin></bndbox>'
            '</object></annotation>')
    f = detect(root)
    assert f['format'] == 'voc' and f['classes'] == 2


def test_classification(tmp_path):
    root = tmp_path / 'cls'
    for c in ('cat', 'dog', 'bird'):
        for i in range(2):
            make_jpeg(root / c / f'{i}.jpg')
    f = detect(root)
    assert f['format'] == 'classification' and f['classes'] == 3
    split = tmp_path / 'cls2'
    for s in ('train', 'val'):
        for c in ('cat', 'dog'):
            make_jpeg(split / s / c / '0.jpg')
    f = detect(split)
    assert f['format'] == 'classification' and f['classes'] == 2 and set(f['splits']) == {'train', 'val'}


def test_images_embeddings_other_empty(tmp_path):
    imgs = tmp_path / 'i'
    make_jpeg(imgs / 'a.jpg')
    make_jpeg(imgs / 'sub' / 'b.png')
    assert detect(imgs)['format'] == 'images'

    emb = tmp_path / 'e'
    emb.mkdir()
    np.save(emb / 'clip.npy', np.zeros((2, 4), np.float32))
    (emb / 'meta.csv').write_text('id,label\n0,a\n1,b\n')
    assert detect(emb)['format'] == 'embeddings'

    other = tmp_path / 'x'
    other.mkdir()
    (other / 'notes.docx').write_bytes(b'xx')
    make_jpeg(other / 'a.jpg')
    f = detect(other)
    assert f['format'] == 'other' and f['reason']

    no_labels = tmp_path / 'nl'
    make_jpeg(no_labels / 'images' / 'a.jpg')
    (no_labels / 'data.yaml').write_text('names: [a]\n')
    f = detect(no_labels)
    assert f['format'] == 'other' and 'labels/ missing' in f['reason']

    (tmp_path / 'empty').mkdir()
    (tmp_path / 'empty' / '.hidden').write_text('x')
    assert detect(tmp_path / 'empty')['format'] == 'empty'


def test_key_tree_matches_local(tmp_path):
    """The S3 path (KeyTree over an object listing) decides like the local one."""
    root = yolo(tmp_path / 'd')
    keys = {str(p.relative_to(root)).replace('\\', '/'): p.stat().st_size for p in root.rglob('*') if p.is_file()}
    tree = formats.KeyTree(keys, lambda rel, n: (root / rel).read_bytes()[:n] if n else (root / rel).read_bytes())
    assert formats.detect(tree)['format'] == 'yolo-detect'
    assert formats.tree_stats(tree) == formats.tree_stats(formats.LocalTree(root))


def test_fingerprint_and_tree_stats(tmp_path):
    root = yolo(tmp_path / 'd')
    fp = formats.fingerprint(root)
    assert formats.fingerprint(root) == fp
    (root / 'labels').rename(root / 'lbl')
    assert formats.fingerprint(root) != fp
    st = formats.tree_stats(formats.LocalTree(root))
    assert st['images'] == 6 and st['labels'] == 0 and st['size'] > 0


def test_clean_rel_rejects_escapes():
    import pytest
    for bad in ('../x', 'a/../../b', '/etc', 'C:/x', '..'):
        with pytest.raises(store.DCError):
            store.clean_rel(bad)
    assert store.clean_rel('./a//b/') == 'a/b' and store.clean_rel('') == ''
