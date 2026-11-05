"""DVC1 columnar transport (server/binary.py): layout, casting, and the ETag / 304 helpers."""

import numpy as np
import orjson
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from dataviz.server import binary
from dataviz.server.binary import DTYPES, MAGIC, etag_for, pack_columns, unpack_columns
from dataviz.server.static import JsonGZip


def _header(buf):
    assert buf[:4] == MAGIC
    n = int.from_bytes(buf[4:8], 'little')
    return orjson.loads(buf[8:8 + n]), n


@pytest.mark.parametrize('code', list(DTYPES))
def test_round_trip_every_dtype(code):
    t = DTYPES[code]
    a = (np.arange(7) - (3 if np.dtype(t).kind != 'u' else 0)).astype(t)
    meta, cols = unpack_columns(pack_columns({'x': a}))
    assert meta is None and cols['x'].dtype.kind == np.dtype(t).kind
    assert cols['x'].dtype.itemsize == np.dtype(t).itemsize
    np.testing.assert_array_equal(cols['x'], a)
    assert _header(pack_columns({'x': a}))[0]['cols'][0]['dtype'] == code


def test_alignment_and_header():
    cols = {'a': np.arange(3, dtype=np.uint8), 'b': np.arange(5, dtype=np.float32), 'c': np.arange(1, dtype=np.int16),
            'd': np.zeros(0, np.float32), 'e': np.arange(9, dtype=np.uint32)}
    buf = pack_columns(cols, {'tag': 'x'})
    head, n = _header(buf)
    assert head['meta'] == {'tag': 'x'}
    assert [c['name'] for c in head['cols']] == list(cols)
    first = head['cols'][0]['offset']
    assert first == 8 + n + (-(8 + n) % 8)
    assert buf[8 + n:first] == b'\0' * (first - 8 - n)
    end = 0
    for c, a in zip(head['cols'], cols.values()):
        assert c['offset'] % 8 == 0 and c['offset'] >= end and c['length'] == a.size
        end = c['offset'] + a.nbytes
        assert buf[c['offset']:end] == a.astype(a.dtype.newbyteorder('<')).tobytes()
    assert len(buf) % 8 == 0
    _, got = unpack_columns(buf)
    for k, a in cols.items():
        np.testing.assert_array_equal(got[k], a)


def test_many_columns_offsets_stable():
    cols = {f'c{i}': np.arange(i * 37, dtype=np.float32) for i in range(40)}
    _, got = unpack_columns(pack_columns(cols))
    for k, a in cols.items():
        np.testing.assert_array_equal(got[k], a)


def test_casting():
    _, c = unpack_columns(pack_columns({
        'b': np.array([True, False, True]), 'i': np.array([-1, 2, 2**31 - 1], np.int64),
        'u': np.array([0, 2**32 - 1], np.int64), 'f': np.array([0.5, 1e10, np.nan])}))
    assert c['b'].dtype == np.uint8 and c['b'].tolist() == [1, 0, 1]
    assert c['i'].dtype == np.int32 and c['i'].tolist() == [-1, 2, 2**31 - 1]
    assert c['u'].dtype == np.uint32 and c['u'].tolist() == [0, 2**32 - 1]
    assert c['f'].dtype == np.float32 and c['f'][0] == 0.5 and np.isnan(c['f'][2])
    with pytest.raises(ValueError):
        pack_columns({'x': np.array([2**40], np.int64)})
    with pytest.raises(ValueError):
        pack_columns({'x': np.array([-1, 2**32], np.int64)})
    with pytest.raises(ValueError):
        pack_columns({'x': np.zeros((2, 2), np.float32)})
    with pytest.raises(ValueError):
        pack_columns({'x': np.array(['a'])})


def test_empty():
    buf = pack_columns({})
    head, _ = _header(buf)
    assert head == {'meta': None, 'cols': []} and len(buf) % 8 == 0
    meta, c = unpack_columns(pack_columns({'z': np.array([], np.int64)}, {'n': 0}))
    assert meta == {'n': 0} and c['z'].size == 0 and c['z'].dtype == np.int32


def test_meta_round_trip():
    meta = {'names': ['a', 'b'], 'n': 3, 'nested': {'x': [1.5, None]}, 'unicode': 'ไทย'}
    assert unpack_columns(pack_columns({'x': np.ones(2, np.uint8)}, meta))[0] == meta


def test_bad_magic():
    with pytest.raises(ValueError):
        unpack_columns(b'NOPE' + bytes(8))


def test_etag_for():
    a = etag_for('tag', 1, 2)
    assert a.startswith('"') and a.endswith('"') and len(a) == 18
    assert a == etag_for('tag', 1, 2) and a != etag_for('tag', 1, 3)


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(JsonGZip)
    state = {'v': 1}

    @app.get('/cols')
    def cols(request: Request):
        return binary.columns_response(request, {'x': np.arange(1000, dtype=np.float32)}, {'v': state['v']},
                                       etag=etag_for('cols', state['v']))

    @app.get('/plain')
    def plain(request: Request):
        return binary.columns_response(request, {'x': np.arange(3, dtype=np.uint8)})

    @app.get('/json')
    def js(request: Request):
        return binary.json_etag_response(request, orjson.dumps({'v': state['v']}), etag_for('json', state['v']))

    with TestClient(app) as c:
        c.state = state
        yield c


def test_columns_etag_304(client):
    r = client.get('/cols')
    assert r.status_code == 200 and r.headers['content-type'] == 'application/octet-stream'
    assert r.headers['cache-control'] == 'private, no-cache'
    tag = r.headers['etag']
    meta, c = unpack_columns(r.content)
    assert meta == {'v': 1} and c['x'].size == 1000
    r2 = client.get('/cols', headers={'If-None-Match': tag})
    assert r2.status_code == 304 and r2.headers['etag'] == tag and r2.content == b''
    client.state['v'] = 2
    r3 = client.get('/cols', headers={'If-None-Match': tag})
    assert r3.status_code == 200 and r3.headers['etag'] != tag


@pytest.mark.parametrize('form', ['W/{t}', '"zzz", {t}', '"zzz",W/{t} , "q"', '*'])
def test_if_none_match_forms(client, form):
    tag = client.get('/cols').headers['etag']
    assert client.get('/cols', headers={'If-None-Match': form.format(t=tag)}).status_code == 304


def test_if_none_match_miss(client):
    assert client.get('/cols', headers={'If-None-Match': '"other", W/"x"'}).status_code == 200


def test_no_etag(client):
    r = client.get('/plain', headers={'If-None-Match': '*'})
    assert r.status_code == 200 and 'etag' not in r.headers
    assert unpack_columns(r.content)[1]['x'].tolist() == [0, 1, 2]


def test_json_etag(client):
    r = client.get('/json')
    assert r.status_code == 200 and r.json() == {'v': 1} and r.headers['content-type'] == 'application/json'
    r2 = client.get('/json', headers={'If-None-Match': r.headers['etag']})
    assert r2.status_code == 304 and r2.headers['etag'] == r.headers['etag']


def test_octet_stream_is_gzipped(client):
    r = client.get('/cols', headers={'Accept-Encoding': 'gzip'})
    assert r.headers.get('content-encoding') == 'gzip'
    assert unpack_columns(r.content)[1]['x'].size == 1000     # httpx decodes it
