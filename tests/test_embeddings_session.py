"""An "embeddings" visualize session end to end over HTTP: saving it, the folder check, points with metadata, a
projection of its own vectors, media previews (and their path checks), and the image-only routes answering 409."""

import csv
import time
import wave

import numpy as np
import pytest
from authutil import account, client, login
from PIL import Image
from vizutil import make_viz_app

pytestmark = pytest.mark.usefixtures('tmp_roots')


def make_folder(root, n=24):
    root.mkdir(parents=True)
    (root / 'media').mkdir()
    rng = np.random.default_rng(0)
    centres = rng.normal(size=(3, 6))
    np.save(root / 'emb.npy', (centres[np.arange(n) % 3] + 0.1 * rng.normal(size=(n, 6))).astype(np.float32))
    with wave.open(str(root / 'media' / 'a.wav'), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b'\0\0' * 800)
    Image.new('RGB', (20, 20), (200, 10, 10)).save(root / 'media' / 'p.png')
    with open(root / 'meta.csv', 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['id', 'label', 'audio', 'image', 'text', 'snr', 'speaker'])
        for i in range(n):
            w.writerow([f'u{i}', 'xyz'[i % 3], 'media/a.wav' if i % 2 == 0 else '', 'media/p.png' if i == 0 else '',
                        f'utterance {i}', f'{i * 0.5}', f'spk{i % 4}'])
    return root


@pytest.fixture
def emb(pg, tmp_path):
    folder = make_folder(tmp_path / 'speech')
    alice = account('alice')
    ctx, app = make_viz_app(tmp_path / 'ws')
    c = client(app)
    assert login(c, 'alice').status_code == 200
    return {'c': c, 'folder': folder, 'user': alice, 'ctx': ctx, 'tmp': tmp_path}


def save(c, path, **kw):
    return c.post('/api/datasets', json={'kind': 'embeddings', 'path': str(path), **kw})


def test_check_and_save(emb):
    c, folder = emb['c'], emb['folder']
    r = c.get('/api/fs/embeddings', params={'path': str(folder)})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s['n'] == 24 and s['features'] == [{'key': 'byo-emb', 'name': 'emb', 'dim': 6}]
    assert s['previews'] == ['image', 'audio', 'text'] or sorted(s['previews']) == ['audio', 'image', 'text']
    assert {x['name']: x['type'] for x in s['columns']} == {'snr': 'num', 'speaker': 'cat'}
    assert c.get('/api/fs/embeddings', params={'path': str(emb['tmp'])}).status_code == 400   # no meta.csv
    assert c.get('/api/fs/embeddings', params={'path': '/etc'}).status_code == 400            # outside the roots
    browse = c.get('/api/fs/browse', params={'path': str(emb['tmp'])}).json()
    assert {f['name']: f['embeddings'] for f in browse['folders']}['speech'] is True

    r = save(c, folder)
    assert r.status_code == 200, r.text
    assert r.json()['kind'] == 'embeddings' and r.json()['name'] == 'speech'
    np.save(folder / 'broken.npy', np.zeros((3, 2)))
    assert save(c, folder, name='bad').status_code == 400      # validated before anything is written
    assert [d['name'] for d in c.get('/api/datasets').json()['datasets']] == ['speech']


def test_session_routes(emb):
    c = emb['c']
    sid = save(c, emb['folder']).json()['id']
    base = f'/s/{sid}/api'
    st = c.get(f'{base}/status').json()
    assert st['dataset']['kind'] == 'embeddings'
    assert st['caps'] == {'boxes': False, 'images': False, 'curation': False, 'models': False, 'meta': True}
    assert [f['key'] for f in st['features']] == ['byo-emb'] and st['default_feature'] == 'byo-emb'

    p = c.get(f'{base}/points').json()
    assert len(p['id']) == 24 and p['row_id'][3] == 'u3' and p['cls'][:3] == [0, 1, 2]
    assert p['meta_types'] == {'snr': 'num', 'speaker': 'cat'}
    assert p['meta']['snr'][4] == 2.0 and p['meta']['speaker'][5] == 'spk1'
    assert p['preview'][0] == ['audio', 'image', 'text'] and p['preview'][1] == ['text']
    assert p['text'][1] == 'utterance 1' and 'area' not in p
    assert st['dataset']['modality'] == ['audio', 'image', 'text']

    s = c.get(f'{base}/sample/2').json()
    assert s['meta'] == {'snr': 1.0, 'speaker': 'spk2'} and s['same_image'] == []

    r = c.get(f'{base}/preview/0/audio')
    assert r.status_code == 200 and r.headers['content-type'] == 'audio/wav' and r.content[:4] == b'RIFF'
    assert c.get(f'{base}/preview/0/audio', headers={'Range': 'bytes=0-3'}).status_code == 206
    assert c.get(f'{base}/preview/1/audio').status_code == 404              # this row has no audio
    assert c.get(f'{base}/preview/0/secret').status_code == 404
    assert c.get(f'{base}/crop/0').status_code == 200                       # image preview -> thumbnail
    assert c.get(f'{base}/crop/1').status_code == 404

    assert c.get(f'{base}/image/0').status_code == 409
    assert c.post(f'{base}/curation/scan', json={}).status_code == 409
    assert c.get(f'{base}/eval/overview').status_code == 409

    req = {'feature': 'byo-emb', 'method': 'pca', 'params': {}, 'classes': None}
    for _ in range(200):
        r = c.post(f'{base}/projection', json=req).json()
        if r['ready']:
            break
        time.sleep(0.05)
    assert r['ready'] and len(r['x']) == 24 and set(r['info']['quality']) >= {'trustworthiness', 'continuity'}

    # emb.npy is not unit length (euclidean): near-duplicates cut by rank, not by a cosine
    dup = c.get(f'{base}/duplicates', params={'feature': 'byo-emb', 'threshold': 0.97}).json()
    assert dup['cutoff']['by'] == 'rank' and dup['cutoff']['distance'] > 0

    out = c.post(f'{base}/export', json={'ids': [0, 1], 'feature': 'byo-emb'})
    lines = out.text.strip().splitlines()
    assert lines[0].startswith('id,row_id,class_id,class_name,') and 'snr' in lines[0] and lines[1].startswith('0,u0,0,x,')


def test_preview_path_rechecked(emb):
    """A row edited to point outside the folder after indexing is still refused at serve time."""
    c = emb['c']
    sid = save(c, emb['folder']).json()['id']
    base = f'/s/{sid}/api'
    assert c.get(f'{base}/points').status_code == 200
    st = emb['ctx'].states[sid]
    st.samples[0]['preview']['audio'] = '../../../etc/passwd'
    assert c.get(f'{base}/preview/0/audio').status_code == 404


def test_url_previews_off_by_default(emb):
    """A preview cell holding a URL is not handed to the page (nor redirected to) unless the config allows it."""
    folder, c = emb['folder'], emb['c']
    meta = (folder / 'meta.csv').read_text().splitlines()
    meta[2] = meta[2].replace('u1,y,,', 'u1,y,https://example.test/a.wav,', 1)      # row 1 had no audio
    (folder / 'meta.csv').write_text('\n'.join(meta) + '\n')
    summary = c.get('/api/fs/embeddings', params={'path': str(folder)}).json()
    assert summary['url_previews'] == 1
    sid = save(c, folder).json()['id']
    base = f'/s/{sid}/api'
    assert c.get(f'{base}/points').json()['preview'][1] == ['text']
    r = c.get(f'{base}/preview/1/audio', follow_redirects=False)
    assert r.status_code == 404 and 'allow_url_previews' in r.text
    emb['ctx'].states[sid].cfg['embeddings']['allow_url_previews'] = True
    r = c.get(f'{base}/preview/1/audio', follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers['location'] == 'https://example.test/a.wav'
