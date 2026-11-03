"""The Objects | Full images switch: a labelled session can also be shown one point per image (/s/<sid>@images/),
its own State (Ctx key '<sid>@images') whose class is each image's most frequent box class, '(no boxes)' without
any. The same access as the session; a combined dataset shows every member as images."""

from authutil import account, client, login
from conftest import make_jpeg
from dataviz.data import sources, views
from dataviz.data.indexer import build_index
from vizutil import make_viz_app, make_yolo, union_session, viz_session


def _data(tmp_path):
    root = make_yolo(tmp_path / 'd', classes=('cat', 'dog'),
                     boxes=(('a', '0 0.5 0.5 0.4 0.4\n1 0.3 0.3 0.2 0.2\n1 0.6 0.6 0.1 0.1\n'),   # dog x2: dog
                            ('b', '0 0.5 0.5 0.2 0.2\n'),                                       # cat
                            ('c', '3 0.5 0.5 0.2 0.2\n')))                                      # past the list
    make_jpeg(root / 'images' / 'e.jpg', 64, 48)                                                 # no label file
    return root


def test_image_view_majority_class(tmp_path):
    src = sources.LocalSource(_data(tmp_path))
    plain = views.view_for(src, 'images')
    view = views.view_for(src, 'images', class_from_labels=True)
    assert view.class_from_labels and not plain.class_from_labels
    assert view.fingerprint() != plain.fingerprint()                   # its own workspace; the old one stays valid
    idx = build_index(view, tmp_path / 'ws', thumb_size=32, workers=1)
    assert idx['classes'] == ['cat', 'dog', 'class_2', 'class_3', '(no boxes)']
    by = {s['image']: idx['classes'][s['cls']] for s in idx['samples']}
    assert by == {'a.jpg': 'dog', 'b.jpg': 'cat', 'c.jpg': 'class_3', 'e.jpg': '(no boxes)'}
    # an unlabelled folder asked for labels classes: plain images (nothing to take the class from)
    flat = tmp_path / 'flat'
    make_jpeg(flat / 'x.jpg', 32, 32)
    assert not views.view_for(sources.LocalSource(flat), 'images', class_from_labels=True).class_from_labels


def test_layer_route_state_and_access(pg, tmp_path):
    alice, bob = account('alice'), account('bob')
    sid = viz_session(alice['id'], 'D', _data(tmp_path))
    ctx, app = make_viz_app(tmp_path / 'ws')
    c = client(app)
    assert login(c, 'alice').status_code == 200
    boxes = c.get(f'/s/{sid}/api/points').json()
    imgs = c.get(f'/s/{sid}@images/api/points').json()
    assert len(boxes['id']) == 5 and len(imgs['id']) == 4
    assert c.get(f'/s/{sid}@images/api/meta').json()['classes'][-1] == '(no boxes)'
    caps = c.get(f'/s/{sid}/api/status').json()['caps']
    assert caps['unit'] == 'box' and caps['points'] == {'layer': None, 'switch': True}
    caps = c.get(f'/s/{sid}@images/api/status').json()['caps']
    assert caps['unit'] == 'image' and caps['points'] == {'layer': 'images', 'switch': True}
    assert set(ctx.states) == {sid, f'{sid}@images'} and ctx.loaded(sid)
    assert c.get(f'/s/{sid}@nope/api/points').status_code == 404
    ctx.evict(sid)                                                      # unloading a session drops both layers
    assert not ctx.states
    b = client(app)
    assert login(b, 'bob').status_code == 200
    assert b.get(f'/s/{sid}@images/api/points').status_code == 404      # no access: 404 like the session itself


def test_union_layer(pg, tmp_path):
    alice = account('alice')
    a = make_yolo(tmp_path / 'A', classes=('cat', 'dog'), boxes=(('a1', '1 0.5 0.5 0.4 0.4\n'),))
    b = make_yolo(tmp_path / 'B', classes=('dog',), boxes=(('b1', '0 0.5 0.5 0.3 0.3\n'), ('b2', '')))
    A, B = viz_session(alice['id'], 'A', a), viz_session(alice['id'], 'B', b)
    U = union_session(alice['id'], 'A vs B', [(A, 'A'), (B, 'B')])
    ctx, app = make_viz_app(tmp_path / 'ws')
    c = client(app)
    assert login(c, 'alice').status_code == 200
    pts = c.get(f'/s/{U}@images/api/points').json()
    meta = c.get(f'/s/{U}@images/api/meta').json()
    names = [meta['classes'][k] for k in pts['cls']]
    assert pts['image'] == ['A/a1.jpg', 'B/b1.jpg', 'B/b2.jpg'] and pts['origin'] == [0, 1, 1]
    assert names == ['dog', 'dog', '(no boxes)']
    assert c.get(f'/s/{U}@images/api/status').json()['caps']['unit'] == 'image'
