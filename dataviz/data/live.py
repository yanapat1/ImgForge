"""
LiveFeed: a labeling task's edits reach an open visualize session without a re-index. One feed per State (a
labeling session, or a combined dataset with labeling members), one pump per feed however many tabs poll.

    capture(state)          at index time (State._finish_index): per labeling part, the change cursor and the
                            annotation id behind every box (LabelingSource.snapshot_info), checked against the
                            fingerprint the index was built from (a stale index cannot be patched: reindex)
    feed_for(state)         the State's LiveFeed (state.live_feed), made on first use
    feed.pull()             sync: the media changed since the cursor (ProjectStore.changed_media), their live
                            rows diffed against the State -> one patch applied (data/patching.py), logged
    await feed.poll(since, wait)   the long poll of GET /api/live (server/routes/live.py)

Patch (one log entry, what the page applies):
    {gen, cls: {sid: class}, deleted: [sid], restored: [sid], moved: [{id, bbox:[x1,y1,x2,y2]}],
     new: [{ann, image, cls, xywhn}], diff: {cmp_key: {status, summary}} (compare engine, when it answered)}
poll() answers {gen, live, patches: [...], pending_new, reindex?, resync?}:
    reindex   more than LIMIT media changed at once, the class list changed, or the index was already stale:
              the page offers a re-index (sticky)
    resync    `since` is older than the log keeps (or from another server): reload the points

The pump (an asyncio task started by the first poll) waits for a NOTIFY of the project (db/listen.py) up to 25 s
(or sleeps 2 s while the listener is down), then pulls in a thread; it ends 60 s after the last poll.
New boxes (ann ids the index has none for) are reported as provisional (`new`, counted in pending_new); placing
them in the plot is on_new()'s job: a coalesced background job crops + embeds them with the feature of the projection
the page shows (poll's `proj`) and places each at the similarity-weighted mean of its 10 nearest projected boxes,
logged as a `placed` patch [{ann, proj, x, y, sim, cls, image}] (a dot marked provisional until the next index).
"""

import asyncio
import threading
import time
from collections import deque

from ..core import logging as vlog
from ..labeling import formats
from . import patching

LIMIT = 2000             # more changed media than this in one pull: re-index instead of patching
LOG_SIZE = 512
WAIT_MAX = 25.0
IDLE_S = 60.0
POLL_S = 2.0
EPS = 1e-6
_lg = vlog.get_logger('live')
_attach_lock = threading.Lock()


def _stem(fn):
    i = fn.rfind('.')
    return fn[:i] if i > 0 else fn


class _Part:
    """One labeling source of the State: the plain session (alias None) or one member of a combined dataset."""

    def __init__(self, src, alias=None, origin=None, remap=None):
        self.src, self.alias, self.origin, self.remap = src, alias, origin, remap
        self.pid, self.tid = src.pid, src.task_id
        self.cursor = 0
        self.stale = False
        self.classes = []
        self.ann2sid = {}        # annotation id -> sample id
        self.label_sids = {}     # member label -> [sample ids] at index time
        self.mid2label = {}      # media id -> member label
        self.geo = {}            # ann id -> normalized (cx, cy, w, h) last seen
        self.new = {}            # media id -> {ann id: new box} (provisional, not in the index)

    def full(self, name):
        return f'{self.alias}/{name}' if self.alias else name

    def ucls(self, k):
        if self.remap is None:
            return int(k)
        return int(self.remap[k]) if 0 <= k < len(self.remap) else None


def _parts(state):
    src = state.source
    if getattr(src, 'kind', None) == 'labeling':
        return [(_Part(src), state.fingerprint, 0)]
    if getattr(src, 'kind', None) == 'union':
        blocks = {b['alias']: b for b in (getattr(state, 'members', None) or [])}
        out = []
        for i, m in enumerate(src.members):
            if getattr(m.src, 'kind', None) != 'labeling' or m.alias not in blocks:
                continue
            b = blocks[m.alias]
            out.append((_Part(m.src, m.alias, i, m.remap_in), b.get('fp'), i))
    else:
        return []
    return out


def capture(state):
    """[_Part] of a freshly indexed State (stored as state.live_base); [] when it has no labeling source."""
    parts = []
    for part, fp, origin in _parts(state):
        src = part.src
        try:
            info = src.snapshot_info()
            snap_fp = src._snap[0] if getattr(src, '_snap', None) else None
            part.classes = list(src.read_classes())
        except Exception as e:  # noqa: BLE001 - the session / task went away: nothing to follow
            _lg.info(f'live feed: {src.uri} not followed ({e})')
            part.stale = True
            parts.append(part)
            continue
        part.cursor = int(info['seq'])
        # the index (a plain session's, or the member block's) was built from the snapshot of this fingerprint
        part.stale = fp is not None and snap_fp != fp
        refs = info['refs']
        part.mid2label = {mid: name for name, mid in info['mids'].items()}
        pre = f'{part.alias}/' if part.alias else ''
        for s in state.samples:
            if part.alias is not None and s.get('origin', 0) != origin:
                continue
            label = s.get('label') or ''
            if pre:
                if not label.startswith(pre):
                    continue
                label = label[len(pre):]
            r, k = refs.get(label), s.get('box_idx')
            if r is None or k is None or not 0 <= k < len(r):
                continue
            part.ann2sid[r[k][0]] = s['id']
            part.label_sids.setdefault(label, []).append(s['id'])
        parts.append(part)
    return parts


def _lines(text, refs):
    out = []
    for j, (ln, ref) in enumerate(zip(text.splitlines(), refs)):
        p = ln.split()
        out.append((j, ref[0], int(p[0]), *(float(v) for v in p[1:5])))
    return out


class LiveFeed:
    def __init__(self, state):
        self.state = state
        self.parts = getattr(state, 'live_base', None)
        if self.parts is None:
            self.parts = state.live_base = capture(state)
        self.gen = 0
        self.log = deque(maxlen=LOG_SIZE)
        self.reindex = {'reason': 'stale index'} if any(p.stale for p in self.parts) else None
        self._pull_lock = threading.Lock()
        self._loop = None
        self._pump = None
        self._changed = None
        self._pollers = 0
        self._last_poll = time.time()
        self._closed = False
        self.n_pulls = 0
        self._gen_lock = threading.Lock()
        self.proj = None                 # projection key the page shows (poll's proj): where new boxes are placed
        self.placed = {}                 # ann -> {x, y, sim, proj} of provisional dots
        self._to_place = {}              # ann -> new-box record waiting for the placement job
        self._place_lock = threading.Lock()

    @property
    def live(self):
        return bool(self.parts)

    @property
    def cursors(self):
        return {(p.pid, p.tid): p.cursor for p in self.parts}

    @property
    def pending_new(self):
        return sum(len(v) for p in self.parts for v in p.new.values())

    def on_new(self, boxes):
        """New boxes: queue them for provisional placement (one coalesced job places every queued box)."""
        if not self.proj:
            return
        with self._place_lock:
            for b in boxes:
                self._to_place[b['ann']] = b
        st = self.state
        st.submit(f'live-place:{st.tag}', 'Live · place new boxes', self._place_job)

    def _place_job(self, job):
        """Place every queued new box, round after round: boxes queued while a round runs are taken by the next one
        (submit() returns this running job for them, so nobody else would)."""
        n = 0
        while True:
            with self._place_lock:
                todo, self._to_place = self._to_place, {}
            if not todo:
                return n
            n += self._place(todo, job)

    def _place(self, todo, job):
        """Crop + embed these new boxes, place them by their nearest projected neighbours (cosine)."""
        import numpy as np
        from PIL import Image
        from ..ml import features as feats
        from .indexer import _crop_boxes
        st, key = self.state, self.proj
        if not todo or not key:
            return 0
        feature = key.split('|', 1)[0]
        proj = st.get_projection(key)
        if proj is None or feature not in st.registry or not (feature in st.features or st.feature_cached(feature)):
            return 0
        X = feats.l2_normalize(np.asarray(st.get_features(feature), np.float32))
        ids = np.asarray(proj['id'])
        R, rxy = X[ids], np.asarray(proj['xy'], np.float32)
        out_dir = st.cache_dir / 'live'
        out_dir.mkdir(exist_ok=True)
        pad = float(st.cfg['index']['crop_pad'])
        thumb = int(st.cfg['index']['thumb_size'])
        recs, paths, samples = [], [], []
        for ann, b in todo.items():
            cx, cy, w, h = b['xywhn']
            try:
                with st.source.open_image(b['image']) as im:
                    img = im.convert('RGB') if im.mode != 'RGB' else im.copy()
            except (OSError, FileNotFoundError, ValueError) as e:
                _lg.info(f'live feed: cannot open {b["image"]} to place a new box ({e})')
                continue
            if not isinstance(img, Image.Image):
                continue
            ss, crops = _crop_boxes(img, [(int(b['cls']), cx, cy, w, h, 0)], b['image'], '', thumb, pad)
            if not ss:
                continue
            path = out_dir / f'{ann}.jpg'
            path.write_bytes(crops[0])
            recs.append(b)
            paths.append(path)
            samples.append(ss[0])
        if not recs:
            return 0
        q = feats.l2_normalize(np.asarray(st.registry[feature].fn(paths, samples, job.progress), np.float32))
        S = q @ R.T
        k = min(10, len(ids))
        top = np.argpartition(-S, k - 1, axis=1)[:, :k]
        sim = np.take_along_axis(S, top, 1)
        wgt = np.exp((sim - sim.max(1, keepdims=True)) / 0.02)     # relative to the best match: sharp at any dim
        xy = (rxy[top] * wgt[..., None]).sum(1) / wgt.sum(1, keepdims=True)
        placed = []
        for b, (x, y), s in zip(recs, xy, sim.max(1)):
            e = {'ann': b['ann'], 'proj': key, 'x': round(float(x), 4), 'y': round(float(y), 4),
                 'sim': round(float(s), 3), 'cls': int(b['cls']), 'image': b['image']}
            self.placed[b['ann']] = e
            placed.append(e)
        self._publish({'cls': {}, 'deleted': [], 'restored': [], 'moved': [], 'new': [], 'placed': placed})
        return len(placed)

    def _publish(self, out):
        """Log a patch made outside the pump (the placement job) and wake the long polls."""
        with self._gen_lock:
            self.gen += 1
            out['gen'] = self.gen
            out['pending_new'] = self.pending_new
            self.log.append((self.gen, out))
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._wake)

    def _wake(self):
        if self._changed is not None:
            self._changed.set()
            self._changed = asyncio.Event()

    # ------------------------------------------------------------ the pull
    def _diff_part(self, part, patch, labels, stage):
        """Diff one labeling part into `patch`. What the feed remembers (box geometry, new boxes, the cursor) goes to
        `stage`, committed by pull() only once the patch is applied: a failed / abandoned pull is redone in full."""
        st = self.state
        store = part.src._store()
        ids, cursor = store.changed_media(part.tid, part.cursor, limit=LIMIT)
        if not ids:
            return
        if len(ids) > LIMIT:
            self.reindex = {'reason': 'bulk change', 'n': len(ids)}
            return
        task = part.src.check()
        cmap, names = formats.class_map(task['classes'])
        if list(names) != list(part.classes):
            self.reindex = {'reason': 'classes changed'}
            return
        mask = patching.deleted_mask(st)
        for mid in ids:
            label = part.mid2label.get(mid)
            media = None
            if label is None or part.src.only_done:
                media = store.media_item(mid)
                label = label or f'{_stem(media["file_name"])}.txt'
            _, _, view = part.src._live(mid)
            if part.src.only_done and (media['status'] or {}).get(part.tid) != 'done':
                lines = []
            else:
                text, refs = formats_lines(task['task_type'], view, cmap)
                lines = _lines(text, refs)
            present, new = set(), {}
            for j, ann, k, cx, cy, w, h in lines:
                u = part.ucls(k)
                sid = part.ann2sid.get(ann)
                if u is None:
                    self.reindex = {'reason': 'classes changed'}
                    return
                if sid is None:
                    image = part.src.image_for_label(label) if media is None else media['file_name']
                    new[ann] = {'ann': ann, 'image': part.full(image or label), 'cls': u,
                                'xywhn': [cx, cy, w, h]}
                    continue
                present.add(sid)
                if mask[sid]:
                    patch['restored'].append(sid)
                patch['box_idx'][sid] = j
                if int(st.labels[sid]) != u:
                    patch['cls'][sid] = u
                smp = st.samples[sid]
                g = (cx, cy, w, h)
                was = part.geo.get(ann)
                from .indexer import yolo_to_pixel
                bb = list(yolo_to_pixel(cx, cy, w, h, smp['img_w'], smp['img_h']))
                if was is None:          # first sight: the index box is rounded (and maybe draft-scaled)
                    moved = any(abs(a - b) > 1 for a, b in zip(bb, smp['bbox']))
                else:
                    moved = any(abs(a - b) > EPS for a, b in zip(g, was))
                stage.append((part.geo.__setitem__, ann, g))
                if moved:
                    patch['moved'].append({'id': sid, 'bbox': bb})
            for sid in part.label_sids.get(label, ()):
                if sid not in present and not mask[sid]:
                    patch['deleted'].append(sid)
            fresh = [b for a, b in new.items() if a not in part.new.get(mid, {})]
            patch['new'].extend(fresh)
            if new:
                stage.append((part.new.__setitem__, mid, new))
            else:
                stage.append((lambda k, _v, d=part.new: d.pop(k, None), mid, None))
            labels.add(part.full(label))
        stage.append((lambda _k, v, p=part: setattr(p, 'cursor', v), None, cursor))

    def pull(self):
        """One pull of every labeling part (sync: the pump runs it in a thread). Returns the logged patch, or None
        when nothing changed."""
        if self._closed or not self.parts:
            return None
        with self._pull_lock:
            self.n_pulls += 1
            if self.reindex is not None:
                return None
            patch = {'cls': {}, 'deleted': [], 'restored': [], 'moved': [], 'new': [], 'box_idx': {}}
            labels = set()
            st = self.state
            cur = getattr(st, '_curation', None)
            fix_lock = cur.fix_lock if cur is not None else threading.Lock()
            with fix_lock:                       # never between a curation write and its own State patch
                n_new = self.pending_new
                stage = []
                for part in self.parts:
                    try:
                        self._diff_part(part, patch, labels, stage)
                    except KeyError as e:        # the session / task was removed
                        self.reindex = {'reason': str(e.args[0] if e.args else e)}
                    if self.reindex is not None:
                        return None
                lock = cur.lock if cur is not None else st.index_lock
                with lock:
                    out = patching.apply_patch(st, patch)
                for fn, k, v in stage:          # applied: now the feed may remember what it saw
                    fn(k, v)
                if cur is not None and labels:
                    try:
                        cur.forget_files(sorted(labels))
                    except Exception as e:  # noqa: BLE001 - the next scan fixes it
                        _lg.info(f'live feed: curation re-scan failed ({e})')
            out['new'] = patch['new']
            if patch['new']:
                self.on_new(patch['new'])
            if not (out['cls'] or out['deleted'] or out['restored'] or out['moved'] or out['new']
                    or self.pending_new != n_new):
                return None
            out.pop('images', None)
            for b in out['new']:                  # gone again before it was placed / placed earlier
                self.placed.pop(b['ann'], None)
            with self._gen_lock:
                self.gen += 1
                out['gen'] = self.gen
                out['pending_new'] = self.pending_new
                self.log.append((self.gen, out))
            return out

    # ------------------------------------------------------------ the long poll
    def _answer(self, since):
        out = {'gen': self.gen, 'live': True, 'pending_new': self.pending_new,
               'patches': [p for g, p in self.log if g > since]}
        if self.reindex is not None:
            out['reindex'] = True
            out.update({k: v for k, v in self.reindex.items() if k != 'reindex'})
        return out

    def _ensure_pump(self):
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._pump is None or self._pump.done():
            self._loop = loop
            self._changed = asyncio.Event()
            self._pump = loop.create_task(self._run())

    async def _wait_change(self, timeout):
        from ..db import listen
        lst = listen.listener()
        if lst is None or not lst.available:
            await asyncio.sleep(min(POLL_S, timeout))
            return
        loop = asyncio.get_running_loop()
        evs = [(p.pid, lst.waiter(p.pid, loop)) for p in {p.pid: p for p in self.parts}.values()]
        try:
            await asyncio.wait([asyncio.ensure_future(ev.wait()) for _, ev in evs], timeout=timeout,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pid, ev in evs:
                lst.discard(pid, ev)

    async def _run(self):
        try:
            while not self._closed:
                gen = self.gen
                try:
                    await asyncio.to_thread(self.pull)
                except Exception as e:  # noqa: BLE001 - the database was briefly away: try again
                    _lg.warning(f'live feed pull failed: {type(e).__name__}: {e}')
                    await asyncio.sleep(POLL_S)
                if self.gen != gen or self.reindex is not None:
                    self._changed.set()
                    self._changed = asyncio.Event()
                if self._pollers == 0 and time.time() - self._last_poll > IDLE_S:
                    break
                if self.reindex is not None:
                    await asyncio.sleep(POLL_S)
                    continue
                await self._wait_change(WAIT_MAX)
        finally:
            if self._changed is not None:
                self._changed.set()

    async def poll(self, since=None, wait=WAIT_MAX, proj=None):
        """The answer of GET /api/live. since None: where the page starts ({gen, live})."""
        if not self.parts:
            return {'live': False, 'gen': 0}
        self._last_poll = time.time()
        if proj and proj != self.proj:
            self.proj = proj
            if self.pending_new:                  # boxes drawn before the page said what it shows: place them now
                self.on_new([b for p in self.parts for v in p.new.values() for b in v.values()])
        self._ensure_pump()
        if since is None:
            return {'gen': self.gen, 'live': True, 'pending_new': self.pending_new,
                    **({'reindex': True} if self.reindex is not None else {})}
        since = int(since)
        oldest = self.log[0][0] if self.log else self.gen + 1
        if since > self.gen or (self.log and since < oldest - 1):
            return {'resync': True, 'gen': self.gen, 'live': True, 'pending_new': self.pending_new}
        out = self._answer(since)
        if out['patches'] or out.get('reindex'):
            return out
        wait = max(0.0, min(float(wait or 0), WAIT_MAX))
        if wait <= 0:
            return out
        self._pollers += 1
        try:
            ev = self._changed
            try:
                await asyncio.wait_for(ev.wait(), wait)
            except asyncio.TimeoutError:
                pass
        finally:
            self._pollers -= 1
            self._last_poll = time.time()
        return self._answer(since)

    def stop(self):
        self._closed = True
        pump, loop = self._pump, self._loop
        if pump is not None and not pump.done() and loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(pump.cancel)
            except RuntimeError:
                pass


def formats_lines(task_type, view, cmap):
    from .labeling_source import label_lines
    return label_lines(task_type, view, cmap)


def feed_for(state):
    """The State's LiveFeed (state.live_feed), made on first use."""
    feed = getattr(state, 'live_feed', None)
    if feed is None:
        with _attach_lock:
            feed = getattr(state, 'live_feed', None)
            if feed is None:
                feed = state.live_feed = LiveFeed(state)
    return feed
