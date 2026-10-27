"""
Columnar binary transport ("DVC1"): many equal-or-unequal length 1-D numeric columns in one response, decoded by
the page (static/ts/core/compare-core.ts, decodeColumns) as typed-array views straight onto the ArrayBuffer, with no
copy and no JSON number parsing.

Layout (all integers little-endian):

    0      4 bytes   magic b'DVC1'
    4      u32       H = byte length of the header JSON
    8      H bytes   header, UTF-8 JSON: {"meta": <any JSON or null>,
                                          "cols": [{"name": str, "dtype": str, "offset": int, "length": int}, ...]}
    8+H    zero padding up to the next multiple of 8
    ...    column buffers in header order, each starting at its `offset`, each followed by zero padding to a
           multiple of 8

    offset   absolute byte offset from the start of the payload; always % 8 == 0, which is what lets TS build
             `new Float32Array(buf, offset, length)` etc. (a typed-array view must be aligned to its element size)
    length   element count (not bytes); bytes = length * itemsize
    dtype    'u1' 'i1' 'u2' 'i2' 'u4' 'i4' 'f2' 'f4' -> Uint8 Int8 Uint16 Int16 Uint32 Int32 Float16* Float32 Array
             (*f2: Float16Array where available, else decode the Uint16 bits)

Arrays are cast to the nearest supported dtype: bool -> u1, int64/uint64 -> i4 / u4 when the values fit (else
ValueError), float64 -> f4. Only 1-D columns.

Caching: columns_response / json_etag_response answer 304 when If-None-Match matches the given ETag, so an unchanged
payload costs one round trip. Responses are gzipped by server/static.py JsonGZip like any other API response.
"""

import hashlib

import numpy as np
import orjson
from fastapi.responses import Response

MAGIC = b'DVC1'
DTYPES = {'u1': np.uint8, 'i1': np.int8, 'u2': np.uint16, 'i2': np.int16, 'i4': np.int32, 'u4': np.uint32,
          'f2': np.float16, 'f4': np.float32}
_CODE = {(np.dtype(t).kind, np.dtype(t).itemsize): k for k, t in DTYPES.items()}
CACHE = 'private, no-cache'


def _pad(n):
    return -n % 8


def _cast(name, a):
    """a as a little-endian array of a supported dtype (bool -> u1, wide ints -> 32 bit if they fit, f8 -> f4)."""
    a = np.asarray(a)
    if a.ndim != 1:
        raise ValueError(f'column {name!r}: 1-D only, got shape {a.shape}')
    k = a.dtype.kind
    if k == 'b':
        a = a.astype(np.uint8)
    elif k in 'iu' and a.dtype.itemsize > 4:
        t = np.uint32 if k == 'u' or (a.size and a.min() >= 0 and a.max() > np.iinfo(np.int32).max) else np.int32
        if a.size and (a.min() < np.iinfo(t).min or a.max() > np.iinfo(t).max):
            raise ValueError(f'column {name!r}: values out of 32-bit range')
        a = a.astype(t)
    elif k == 'f' and a.dtype.itemsize > 4:
        a = a.astype(np.float32)
    elif k not in 'iuf':
        raise ValueError(f'column {name!r}: unsupported dtype {a.dtype}')
    a = a.astype(a.dtype.newbyteorder('<'), copy=False)
    return _CODE[a.dtype.kind, a.dtype.itemsize], np.ascontiguousarray(a)


def pack_columns(cols: dict[str, np.ndarray], meta: dict | None = None) -> bytes:
    """The DVC1 payload for `cols` (insertion order kept) plus a free-form JSON `meta`."""
    cast = [(name, *_cast(name, a)) for name, a in cols.items()]

    def header(offsets):
        return orjson.dumps({'meta': meta, 'cols': [{'name': n, 'dtype': c, 'offset': o, 'length': int(a.size)}
                                                    for (n, c, a), o in zip(cast, offsets)]})

    # offsets depend on the header length, which depends on the offsets' digits: iterate until stable
    offsets = [0] * len(cast)
    while True:
        h = header(offsets)
        pos = 8 + len(h) + _pad(8 + len(h))
        new = []
        for _, _, a in cast:
            new.append(pos)
            pos += a.nbytes + _pad(a.nbytes)
        if new == offsets:
            break
        offsets = new
    out = bytearray(pos)
    out[:8] = MAGIC + len(h).to_bytes(4, 'little')
    out[8:8 + len(h)] = h
    for (_, _, a), o in zip(cast, offsets):
        out[o:o + a.nbytes] = a.tobytes()
    return bytes(out)


def unpack_columns(buf: bytes) -> tuple[dict, dict[str, np.ndarray]]:
    """(meta, {name: array}) from a DVC1 payload; arrays are read-only views onto `buf`."""
    if buf[:4] != MAGIC:
        raise ValueError('not a DVC1 payload')
    n = int.from_bytes(buf[4:8], 'little')
    head = orjson.loads(buf[8:8 + n])
    cols = {c['name']: np.frombuffer(buf, dtype=np.dtype(DTYPES[c['dtype']]).newbyteorder('<'),
                                     count=c['length'], offset=c['offset']) for c in head['cols']}
    return head['meta'], cols


def etag_for(*parts) -> str:
    """A strong ETag ('"<16 hex>"') from the parts' str()s."""
    h = hashlib.sha1('\x1f'.join(map(str, parts)).encode()).hexdigest()[:16]
    return f'"{h}"'


def not_modified(request, etag) -> bool:
    """If-None-Match lists `etag` (weak comparison: W/ ignored) or is '*'."""
    if not etag:
        return False
    inm = request.headers.get('if-none-match')
    if not inm:
        return False
    want = etag.removeprefix('W/')
    for t in inm.split(','):
        t = t.strip()
        if t == '*' or t.removeprefix('W/') == want:
            return True
    return False


def _cached(request, etag, make, media_type):
    if not_modified(request, etag):
        return Response(status_code=304, headers={'ETag': etag, 'Cache-Control': CACHE})
    headers = {'ETag': etag, 'Cache-Control': CACHE} if etag else {}
    return Response(make(), media_type=media_type, headers=headers)


def columns_response(request, cols, meta=None, etag=None) -> Response:
    """DVC1 columns as application/octet-stream; 304 when the client already has `etag`."""
    return _cached(request, etag, lambda: pack_columns(cols, meta), 'application/octet-stream')


def json_etag_response(request, body: bytes, etag: str) -> Response:
    """Already-serialized JSON bytes with an ETag; 304 when the client already has it."""
    return _cached(request, etag, lambda: body, 'application/json')
