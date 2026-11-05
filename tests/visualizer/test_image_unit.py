"""
Images with no labels in the visualizer (data/views.py): the S3 browser counts images, a flat S3 prefix / local folder
is a source of its own (layout 'flat'), and ImageView indexes one point per image through the normal indexer.
"""

import json

from PIL import Image

from conftest_io import jpeg_bytes
from dataviz.data import sources, views
from dataviz.data.indexer import build_index

pytest_plugins = ['conftest_io']             # fixtures: s3_server, s3_bucket


def _put_flat(client, bucket):
    for key, data in {'flat/a.jpg': jpeg_bytes(64, 48), 'flat/b.PNG': _png(), 'flat/.hidden.jpg': jpeg_bytes(),
                      'flat/notes.txt': b'x', 'flat/sub/c.jpg': jpeg_bytes(32, 32),
                      'yolo/images/x.jpg': jpeg_bytes(), 'yolo/labels/x.txt': b'0 0.5 0.5 0.2 0.2\n'}.items():
        client.put_object(Bucket=bucket, Key=key, Body=data)


def _png():
    import io
    buf = io.BytesIO()
    Image.new('RGB', (20, 10), (0, 90, 200)).save(buf, 'PNG')
    return buf.getvalue()


def test_s3_browse_counts_images(s3_server, s3_bucket):
    client, bucket = s3_bucket
    _put_flat(client, bucket)
    top = sources.browse(s3_server, bucket, '')
    assert {f['name']: (f['yolo'], f['images']) for f in top['folders']} == {'flat': (False, 2), 'yolo': (True, 0)}
    here = sources.browse(s3_server, bucket, '/flat/')
    assert here['images'] == 2 and here['path'] == 'flat' and here['yolo'] is False       # hidden + .txt not counted
    assert [(f['name'], f['images']) for f in here['folders']] == [('sub', 1)]


def test_s3_flat_prefix_is_a_source(s3_server, s3_bucket, tmp_path):
    client, bucket = s3_bucket
    _put_flat(client, bucket)
    src = sources.S3Source(s3_server, bucket, 'flat')
    st = src.check()
    assert st['layout'] == 'flat' and st['images'] == 3 and st['labels'] == 0
    assert src.list_images() == ['a.jpg', 'b.PNG', 'sub/c.jpg'] and src.list_labels() == []
    assert src.open_image('sub/c.jpg').size == (32, 32)
    assert src.image_ref('sub/c.jpg') == f's3://{bucket}/flat/sub/c.jpg'
    yolo = sources.S3Source(s3_server, bucket, 'yolo')
    assert yolo.check()['layout'] == 'yolo' and yolo.list_images() == ['x.jpg']
    assert views.view_for(yolo) is yolo                               # has labels: boxes, as before

    view = views.view_for(src)
    assert isinstance(view, views.ImageView) and view.uri.endswith('#images')
    idx = build_index(view, tmp_path / 'ws', thumb_size=64, workers=1)
    assert [(s['image'], s['img_w'], s['img_h'], s['bbox']) for s in idx['samples']] == [
        ('a.jpg', 64, 48, [0, 0, 64, 48]), ('b.PNG', 20, 10, [0, 0, 20, 10]), ('sub/c.jpg', 32, 32, [0, 0, 32, 32])]
    assert idx['classes'] == ['image'] and len(list((tmp_path / 'ws' / 'crops').glob('*.jpg'))) == 3
    assert view.fingerprint() != src.fingerprint()                    # its own workspace
    assert build_index(view, tmp_path / 'ws', thumb_size=64)['samples'] == idx['samples']   # cache hit, same ids


def test_s3_cache_mode_mirrors_flat_prefix(s3_server, s3_bucket, tmp_path):
    client, bucket = s3_bucket
    _put_flat(client, bucket)
    src = sources.S3Source(s3_server, bucket, 'flat', mode='cache', mirror_root=tmp_path / 'mirror')
    src.check()
    src.sync(refresh=False)
    assert src.local.layout == 'flat' and src.list_images() == ['a.jpg', 'b.PNG', 'sub/c.jpg']


def test_local_flat_folder_with_folder_classes(tmp_path):
    root = tmp_path / 'shots'
    (root / 'cam1').mkdir(parents=True)
    (root / 'cam2').mkdir()
    (root / '.thumbs').mkdir()
    big = Image.new('RGB', (2000, 1000), (10, 120, 40))
    big.save(root / 'cam1' / 'big.jpg', quality=80)
    (root / 'cam2' / 'b.jpg').write_bytes(jpeg_bytes(30, 60))
    (root / '.thumbs' / 'skip.jpg').write_bytes(jpeg_bytes())
    src = sources.LocalSource(root)
    src.check()
    assert src.layout == 'flat' and src.list_images() == ['cam1/big.jpg', 'cam2/b.jpg'] and not src.label_writable
    fp = src.fingerprint()
    view = views.view_for(src, class_from_folder=True)
    idx = build_index(view, tmp_path / 'ws', thumb_size=64, workers=1)
    assert idx['classes'] == ['cam1', 'cam2'] and [s['cls'] for s in idx['samples']] == [0, 1]
    s0 = idx['samples'][0]                                            # draft-decoded, reported in original pixels
    assert (s0['img_w'], s0['img_h'], s0['bbox']) == (2000, 1000, [0, 0, 2000, 1000])
    assert json.loads((tmp_path / 'ws' / 'samples.json').read_text())['fingerprint'] == view.fingerprint()
    assert sources.LocalSource(root).fingerprint() == fp


def test_yolo_fingerprint_unchanged(tmp_path):
    """A YOLO folder keeps its fingerprint (else every existing workspace would look like a new dataset)."""
    import hashlib
    root = tmp_path / 'd'
    (root / 'images').mkdir(parents=True)
    (root / 'labels').mkdir()
    (root / 'images' / 'a.jpg').write_bytes(jpeg_bytes())
    (root / 'labels' / 'a.txt').write_text('0 0.5 0.5 0.1 0.1\n')
    h = hashlib.sha1(b'1')
    for sub in ('labels', 'images'):
        for p in sorted((root / sub).iterdir()):
            st = p.stat()
            h.update(f'{sub}/{p.name}:{st.st_size}:{int(st.st_mtime)}'.encode())
    src = sources.LocalSource(root)
    assert src.layout == 'yolo' and src.fingerprint() == h.hexdigest()
    assert views.view_for(src) is src and isinstance(views.view_for(src, 'images'), views.ImageView)
