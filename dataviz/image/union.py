"""
UnionSource: one visualize session over several others (a "combined dataset"), read in place - nothing is copied.
What the Compare tab diffs (data/diff.py, data/compare.py) and what "Promote to labeling" materializes
(data/promote.py).

    names       every image / label of member `alias` is '<alias>/<its own name>' (aliases are unique per union)
    classes     the union of the members' class names, in member order, matched by name (case-folded); a member's
                class ids are remapped on read (label text, samples) and back on write
    writes      routed to the member that owns the label (curation, the image editor), after a labeler check on
                that member: a combined dataset never lets anyone write where they could not write directly
    fingerprint sha1 of the members' (alias, unit, fingerprint): any member change = a new union version

Fast assemble (build_index): each member is indexed into ITS OWN workspace - the very folder its own session uses
(a cache hit when it is current) - and the union's samples are the members' samples concatenated (ids offset,
names prefixed, classes remapped, `origin` = member index). Crops are hard links (a copy across file systems).
Feature matrices are concatenated from the members' caches (assemble_features), computing only what a member
lacks - in the member's workspace, where its own session finds it too. So combining two indexed datasets decodes
no image and runs no model.
"""

import hashlib
import json
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..core import logging as vlog
from . import imagehash
from .indexer import build_index, index_lock
from .sources import Conflict, sha1_bytes

UNION_VERSION = 1
MAX_MEMBERS = 8
ALIAS_RE = re.compile(r'^[A-Za-z0-9_-]{1,24}$')
_lg = vlog.get_logger('union')


def default_alias(i):
    return chr(ord('A') + i) if i < 26 else f'M{i + 1}'


@dataclass
class Member:
    alias: str
    sid: str                 # its visualize session id
    name: str
    src: object              # the member's source as its own session indexes it (views.view_for applied)
    pos: int = 0
    remap_in: np.ndarray = field(default=None)    # member class id -> union class id
    remap_out: dict = field(default_factory=dict)  # union class id -> member class id


def _fold(name):
    return str(name).strip().casefold()


def remap_text(text, table):
    """Label text with the class (first token) of every valid YOLO line mapped through `table` (dict or array);
    other lines are left exactly as they are (curation still reports them). KeyError: a class with no mapping."""
    out = []
    for line in text.splitlines(keepends=True):
        p = line.split()
        if len(p) >= 5:
            try:
                k = int(p[0])
                [float(v) for v in p[1:5]]
            except ValueError:
                out.append(line)
                continue
            if isinstance(table, dict):
                to = table[k]
            else:
                if not 0 <= k < len(table) or table[k] < 0:
                    raise KeyError(k)
                to = int(table[k])
            lead = line[:len(line) - len(line.lstrip())]
            rest = line.lstrip()[len(p[0]):]
            out.append(f'{lead}{to}{rest}')
        else:
            out.append(line)
    return ''.join(out)


def _decode(b):
    return b.decode('utf-8', 'replace') if isinstance(b, (bytes, bytearray)) else (b or '')


class UnionSource:
    kind = 'union'
    root = None                    # no single folder: models evaluate on "all", no folder splits
    layout = 'yolo'

    def __init__(self, ds, members, workspace_root, check_write=None):
        """members: [Member] in position order (2..MAX_MEMBERS). check_write(member_sid): raise PermissionError
        unless the current user may write that member's labels (the route layer passes access.check)."""
        self.ds = ds
        self.id = ds['id']
        self.name = re.sub(r'[/\\]', '-', ds.get('name') or 'combined').strip() or 'combined'
        self.uri = f'union://{self.id}'
        self.members = list(members)
        self.workspace_root = Path(workspace_root)
        self.check_write = check_write
        self.by_alias = {m.alias: m for m in self.members}
        units = {getattr(m.src, 'unit', None) or 'box' for m in self.members}
        self.unit = 'image' if units == {'image'} else None
        self._mixed = len(units) > 1
        self._classes = None
        self._img_sets = {}

    # ------------------------------------------------------------ classes
    def _unify(self):
        """Union classes: the members' class names in member order, a name shared ACROSS members (case-folded) is one
        class. Within a member the mapping is one to one (two of its classes that fold alike - 'Car', 'car' - stay
        two union classes), so remapping a label out and back never changes a box nobody edited."""
        if self._classes is not None:
            return self._classes
        self._classes, self._where = [], {}
        for m in self.members:
            m.remap_in, m.remap_out = np.zeros(0, np.int32), {}
            for k, n in enumerate(m.src.read_classes()):
                self._add(m, k, str(n))
        return self._classes

    def _add(self, m, k, name):
        """Give member class k (named `name`) a union id: the first class of that folded name this member does not
        map to yet, else a new one."""
        ids = self._where.setdefault(_fold(name), [])
        u = next((i for i in ids if i not in m.remap_out), None)
        if u is None:
            u = len(self._classes)
            self._classes.append(name)
            ids.append(u)
        if k >= len(m.remap_in):
            m.remap_in = np.concatenate([m.remap_in, np.full(k + 1 - len(m.remap_in), -1, np.int32)])
        m.remap_in[k] = u
        m.remap_out[u] = k
        return u

    def _grow(self, m, k):
        """A member class id beyond its class list (an auto 'class_<k>'): every id up to k gets a union class of its
        own (never one shared by the gap)."""
        self._unify()
        for j in range(len(m.remap_in), k + 1):
            self._add(m, j, f'class_{j}')
        return int(m.remap_in[k])

    def read_classes(self):
        return list(self._unify())

    # ------------------------------------------------------------ identity
    def check(self):
        if self._mixed:
            raise ValueError('the members mix one point per box and one point per image: '
                             'set every member to the same unit')
        for m in self.members:
            m.src.check()

    def fingerprint(self, fps=None):
        fps = fps or [m.src.fingerprint() for m in self.members]
        parts = [(m.alias, getattr(m.src, 'unit', None) or 'box', f) for m, f in zip(self.members, fps)]
        return hashlib.sha1(f'union|{UNION_VERSION}|{json.dumps(parts)}'.encode()).hexdigest()

    def member_fingerprints(self):
        return {m.alias: m.src.fingerprint() for m in self.members}

    # ------------------------------------------------------------ names
    def split(self, name):
        """(Member, inner name) of a union name; FileNotFoundError when no member has that alias."""
        alias, sep, inner = str(name).partition('/')
        m = self.by_alias.get(alias)
        if m is None or not sep:
            raise FileNotFoundError(f'no image {name!r} in {self.uri}')
        return m, inner

    def list_images(self):
        return [f'{m.alias}/{n}' for m in self.members for n in m.src.list_images()]

    def list_labels(self):
        return [f'{m.alias}/{n}' for m in self.members for n in m.src.list_labels()]

    def image_for_label(self, label):
        from .indexer import find_image
        m, inner = self.split(label)
        f = getattr(m.src, 'image_for_label', None)
        if f:
            img = f(inner)
        else:
            names = self._img_sets.get(m.alias)
            if names is None:
                names = self._img_sets[m.alias] = set(m.src.list_images())
            img = find_image(names, Path(inner).stem)
        return f'{m.alias}/{img}' if img else None

    # ------------------------------------------------------------ reading
    def _in(self, m, text):
        self._unify()
        try:
            return remap_text(text, m.remap_in)
        except KeyError as e:                      # a class id past the member's list: grow and retry
            self._grow(m, int(e.args[0]))
            return self._in(m, text)

    def _member_text(self, m, inner):
        if hasattr(m.src, 'current_label'):            # labeling: the store's text now, as its write checks it
            try:
                return m.src.current_label(inner)[0]
            except FileNotFoundError:
                return ''
        if hasattr(m.src, 'read_label_bytes'):
            return _decode(m.src.read_label_bytes(inner))
        try:
            return m.src.read_label(inner)
        except FileNotFoundError:
            return ''

    def read_label(self, name):
        m, inner = self.split(name)
        return self._in(m, m.src.read_label(inner))

    def read_labels(self, names):
        return [self._in(*self._mt(n)) for n in names]

    def _mt(self, name):
        m, inner = self.split(name)
        return m, self._member_text(m, inner)

    def read_label_bytes(self, name):
        m, inner = self.split(name)
        return self._in(m, self._member_text(m, inner)).encode('utf-8')

    def open_image(self, name):
        m, inner = self.split(name)
        return m.src.open_image(inner)

    def image_ref(self, name):
        m, inner = self.split(name)
        return m.src.image_ref(inner)

    def label_ref(self, name):
        m, inner = self.split(name)
        return m.src.label_ref(inner)

    def image_sig(self, name):
        m, inner = self.split(name)
        f = getattr(m.src, 'image_sig', None)
        return f(inner) if f else None

    def read_image_bytes(self, name):
        m, inner = self.split(name)
        return m.src.read_image_bytes(inner)

    def read_image_part(self, name):
        m, inner = self.split(name)
        return m.src.read_image_part(inner)

    # ------------------------------------------------------------ writing (curation / image editor)
    @property
    def label_writable(self):
        return any(getattr(m.src, 'label_writable', False) for m in self.members)

    @property
    def live(self):
        """A labeling member: its edits reach the union through the live change feed (data/live.py)."""
        return any(getattr(m.src, 'kind', None) == 'labeling' for m in self.members)

    def _out(self, m, text):
        self._unify()
        try:
            return remap_text(text, m.remap_out)
        except KeyError as e:
            names = self._classes
            k = int(e.args[0])
            cname = names[k] if 0 <= k < len(names) else str(k)
            raise ValueError(f'class “{cname}” is not one of member {m.alias}’s classes ({m.name}): '
                             f'add it there first') from None

    def _write(self, name, text, expect_sha1, rows):
        m, inner = self.split(name)
        if not getattr(m.src, 'label_writable', False):
            raise ValueError(f'member {m.alias} ({m.name}) has no label files to write')
        if self.check_write is not None:
            self.check_write(m.sid)
        cur = self._member_text(m, inner)
        if expect_sha1 is not None and sha1_bytes(self._in(m, cur).encode('utf-8')) != expect_sha1:
            raise Conflict(name, sha1_bytes(self._in(m, cur).encode('utf-8')))
        new = self._out(m, _decode(text))
        own_sha = sha1_bytes(cur.encode('utf-8'))
        payload = new.encode('utf-8') if m.src.kind in ('local', 's3') else new
        before_rows = None
        if rows and hasattr(m.src, 'write_label_rows'):
            _, before_rows = m.src.write_label_rows(inner, payload, own_sha)
        else:
            m.src.write_label(inner, payload, own_sha)
        after = sha1_bytes(self._in(m, self._member_text(m, inner)).encode('utf-8'))
        return after, before_rows

    def write_label(self, name, text, expect_sha1=None):
        return self._write(name, text, expect_sha1, rows=False)[0]

    def write_label_rows(self, name, text, expect_sha1=None):
        """(sha1 of the label as the union reads it afterwards, the member's rows before or None)."""
        return self._write(name, text, expect_sha1, rows=True)

    def restore_rows(self, name, rows, expect_sha1=None):
        m, inner = self.split(name)
        if self.check_write is not None:
            self.check_write(m.sid)
        cur = self._member_text(m, inner)
        if expect_sha1 is not None and sha1_bytes(self._in(m, cur).encode('utf-8')) != expect_sha1:
            raise Conflict(name, sha1_bytes(self._in(m, cur).encode('utf-8')))
        m.src.restore_rows(inner, rows, sha1_bytes(cur.encode('utf-8')))
        return sha1_bytes(self._in(m, self._member_text(m, inner)).encode('utf-8'))

    # ------------------------------------------------------------ fast assemble
    def member_workspace(self, m, fp=None):
        from ..workspace import resolve_workspace
        return resolve_workspace(self.workspace_root, m.src, fp)

    def build_index(self, cache_dir, thumb_size=224, pad=0.1, rebuild=False, progress=None, workers='auto'):
        """samples.json of the union (same shape as indexer.build_index + origin / src_id per sample and a
        `members` block), assembled from the members' own indexes. Cached by the union fingerprint."""
        progress = progress or (lambda *a, **k: None)
        if self._mixed:
            self.check()
        cache_dir = Path(cache_dir)
        index_path = cache_dir / 'samples.json'
        with index_lock(cache_dir):
            progress(stage='checking the members')
            fps = [m.src.fingerprint() for m in self.members]
            fp = self.fingerprint(fps)
            if not rebuild and index_path.exists():
                try:
                    cached = _loads(index_path.read_bytes())
                except ValueError:
                    cached = None
                if cached and cached.get('fingerprint') == fp:
                    _lg.info(f'cache hit: {len(cached["samples"]):,} boxes from {len(self.members)} members')
                    return cached
            crops = cache_dir / 'crops'
            crops.mkdir(parents=True, exist_ok=True)
            index_path.unlink(missing_ok=True)
            for old in crops.glob('*.jpg'):
                old.unlink()
            classes = self._unify()
            samples, blocks, images, links = [], [], {}, []
            stats = {'label_files': 0, 'missing_images': 0, 'bad_lines': 0, 'bad_images': 0, 'images': 0}
            for i, (m, mfp) in enumerate(zip(self.members, fps)):
                stage = f'member {m.alias} ({i + 1}/{len(self.members)}): {m.name}'
                progress(stage=stage)
                ws = self.member_workspace(m, mfp)
                with index_lock(ws):
                    idx = build_index(m.src, ws, thumb_size=thumb_size, pad=pad, rebuild=False,
                                      progress=_prefixed(progress, stage), workers=workers)
                    progress(stage=f'{stage} · hashing images')
                    hashes = imagehash.ensure(m.src, ws, idx['fingerprint'], progress=_prefixed(progress, stage))
                    off = len(samples)
                    own = idx['classes']
                    for k in range(len(own)):
                        if k >= len(m.remap_in):
                            self._grow(m, k)
                    for s in idx['samples']:
                        k = int(s['cls'])
                        u = int(m.remap_in[k]) if k < len(m.remap_in) else self._grow(m, k)
                        r = dict(s)
                        r.update(id=off + s['id'], image=f'{m.alias}/{s["image"]}', label=f'{m.alias}/{s["label"]}',
                                 session=f'{m.alias}/{s.get("session", "")}', cls=u,
                                 cls_name=classes[u], origin=i, src_id=s['id'])
                        samples.append(r)
                        links.append((ws / 'crops' / f'{s["id"]:06d}.jpg', crops / f'{off + s["id"]:06d}.jpg'))
                    for name, e in hashes.items():
                        images[f'{m.alias}/{name}'] = {**e, 'o': i}
                    for k, v in (idx.get('stats') or {}).items():
                        if isinstance(v, (int, float)) and k in stats:
                            stats[k] += v
                    blocks.append({'alias': m.alias, 'sid': m.sid, 'name': m.name, 'fp': idx['fingerprint'],
                                   'offset': off, 'n': len(idx['samples']), 'ws': ws.name,
                                   'n_images': len(hashes), 'unit': getattr(m.src, 'unit', None) or 'box'})
                    progress(0, len(idx['samples']), f'{stage} · linking crops')
                    _link_all(links)
                    links.clear()
            _hash_groups(images)
            imagehash.save(cache_dir, fp, images, complete=True)
            result = {'fingerprint': fp, 'classes': list(classes), 'samples': samples, 'stats': stats,
                      'members': blocks, 'unit': self.unit or 'box'}
            tmp = index_path.with_name(f'{index_path.name}.{os.getpid()}.tmp')
            tmp.write_bytes(_dumps(result))
            os.replace(tmp, index_path)
            _lg.info(f'assembled {len(samples):,} boxes from {len(self.members)} members')
            return result

    # ------------------------------------------------------------ features
    def assemble_features(self, key, index, registry, cfg, progress=None, features_dir=None):
        """The union's feature matrix for `key`: the members' cached matrices (computed in a member's own
        workspace when missing) copied block by block into one .npy memmap. None for an unknown key."""
        from ..ml import features as feats
        progress = progress or (lambda *a, **k: None)
        blocks = index.get('members') or []
        n = sum(b['n'] for b in blocks)
        parts, dim, dtype = [], None, np.float32
        for b in blocks:
            m = self.by_alias[b['alias']]
            ws = self.workspace_root / b['ws']
            fdir = ws / 'features'
            emb = feats.load_cached(fdir, key, b['fp'], b['n']) if b['n'] else np.zeros((0, dim or 1), np.float32)
            if emb is None:
                emb = feats.vectors_restore(fdir, key, b['fp'], b['n'], cfg)
            if emb is None:
                progress(stage=f'{key} · member {m.alias}: {m.name}')
                idx = _loads((ws / 'samples.json').read_bytes())
                if idx.get('fingerprint') != b['fp'] or len(idx['samples']) != b['n']:
                    raise ValueError(f'member {m.alias} was re-indexed since this combined dataset was built: '
                                     'update the combined dataset first')
                msamples = idx['samples']
                emb = registry[key].fn([ws / 'crops' / f'{s["id"]:06d}.jpg' for s in msamples], msamples, progress)
                if len(emb) != b['n']:
                    raise ValueError(f'{key}: {len(emb)} rows for the {b["n"]} boxes of member {m.alias}')
                feats.save_cached(fdir, key, b['fp'], emb)
                feats.vectors_store(b['fp'], key, emb, cfg)
            if b['n']:
                if dim is None:
                    dim, dtype = emb.shape[1], emb.dtype
                elif emb.shape[1] != dim:
                    raise ValueError(f'{key}: member {m.alias} has {emb.shape[1]}-d features, '
                                     f'another member {dim}-d')
            parts.append(emb)
        if dim is None:
            return None
        fdir = Path(features_dir)
        fdir.mkdir(parents=True, exist_ok=True)
        final = fdir / f'{feats.cache_name(key)}.npy'
        tmp = fdir / f'{feats.cache_name(key)}.{os.getpid()}.tmp.npy'
        out = np.lib.format.open_memmap(tmp, mode='w+', dtype=dtype, shape=(n, dim))
        pos = 0
        for emb in parts:
            for a in range(0, len(emb), 65536):                 # flat memory however big
                chunk = np.asarray(emb[a:a + 65536])
                out[pos + a:pos + a + len(chunk)] = chunk
            pos += len(emb)
        out.flush()
        del out
        os.replace(tmp, final)
        (fdir / f'{feats.cache_name(key)}.json').write_text(
            json.dumps({'fingerprint': index['fingerprint'], 'n': n, 'dim': int(dim), 'assembled': True}))
        return np.load(final, mmap_mode='r')


def _prefixed(progress, stage):
    """progress() of a member's step, its stage shown under the member's name."""
    def f(*a, **k):
        if len(a) >= 3 and a[2]:
            a = (*a[:2], f'{stage} · {a[2]}', *a[3:])
        if k.get('stage'):
            k['stage'] = f'{stage} · {k["stage"]}'
        progress(*a, **k)
    return f


def _link_all(links):
    def one(pair):
        a, b = pair
        try:
            os.link(a, b)
        except FileExistsError:
            pass
        except OSError:                     # another file system / no hard links: copy
            shutil.copyfile(a, b)
    with ThreadPoolExecutor(16, thread_name_prefix='union-link') as ex:
        list(ex.map(one, links, chunksize=256))


def _hash_groups(images):
    """`g` per image: an id shared by images with the same content (2+ copies anywhere in the union), else -1."""
    groups = {}
    for name, e in images.items():
        k = imagehash.match_key(e)
        if k:
            groups.setdefault(k, []).append(name)
    g = 0
    for e in images.values():
        e['g'] = -1
    for names in groups.values():
        if len(names) > 1:
            for n in names:
                images[n]['g'] = g
            g += 1


try:
    import orjson

    def _dumps(o):
        return orjson.dumps(o)

    def _loads(b):
        return orjson.loads(b)
except ImportError:                          # pragma: no cover
    def _dumps(o):
        return json.dumps(o).encode()

    def _loads(b):
        return json.loads(b)
