"""
Video files of the Data Center: probe, a poster frame, and cutting a video into image frames (service.video_info,
service.extract_frames). Pure: no database and no service; paths are absolute and already checked by the caller.

Decoding is PyAV (FFmpeg with frame + slice threads); OpenCV's VideoCapture is the fallback for a file PyAV cannot
open. What makes it fast:
  - a frame is only converted to pixels when it is kept (frame.reformat does colour conversion + resize in one
    swscale pass); skipped frames are decoded and dropped,
  - sparse picks (2 frames every 10 s, 200 over an hour) seek from keyframe to keyframe instead of decoding the gaps
    (a packet-only keyframe index, no decoding, says where a seek lands),
  - "keyframes" decodes only the I-frames (skip_frame NONKEY); the smart pass skips non-reference frames,
  - JPEG / PNG / WebP encoding runs on a thread pool behind a bounded queue (cv2.imencode drops the GIL), so the
    decoder never waits on the disk and memory stays flat on 4K video.

Which frames: one pattern covers the rate presets, "every E seconds take T frames from a window of W seconds,
consecutive or spread", plus all / every n-th / count / keyframes / scene / smart. Times are seconds from the start
of the video stream, matched to real frames by their timestamps, so 29.97 fps, 60 fps and variable frame rate work.
"""

import bisect
import csv
import functools
import math
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .store import Cancelled, DCError

MODES = ('all', 'every', 'pattern', 'count', 'keyframes', 'scene', 'smart')
FORMATS = {'jpg': '.jpg', 'png': '.png', 'webp': '.webp'}
LAYOUTS = ('yolo', 'yolo-split', 'per-video', 'flat')
SPLITS = ('train', 'val', 'test')
MAX_FRAMES = 200_000                # one job writes at most this many frames
SEEK_GAP = 2.0                      # seek when the next wanted frame is further ahead than this (seconds)
POSTER_SCAN = 4.0                   # a poster decodes at most this far past a keyframe (seconds)
SMART_TIME = 0.1                    # smart: weight of the time distance next to the look distance
SMART_RATE = 8.0                    # smart mode looks at most this many candidate frames per second
EPS = 1e-6
DEFAULTS = {
    'mode': 'pattern', 'n': 1, 'every_s': 1.0, 'window_s': 1.0, 'take': 1, 'pick': 'spread', 'threshold': 0.1,
    'dedupe': 0.0, 'start': 0.0, 'end': None, 'max_side': None, 'format': 'jpg', 'quality': 90, 'layout': 'yolo',
    'split': (0.8, 0.1, 0.1), 'block_s': None, 'max_frames': MAX_FRAMES,
}
BYTES_PER_PX = {'jpg': 0.22, 'png': 1.6, 'webp': 0.12}     # rough output size for the estimate


# ---------------------------------------------------------------------------------------------- options
def _num(v, name, lo=None, hi=None, integer=False, open_lo=False):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise DCError(f'{name} must be a number') from None
    if not math.isfinite(x):
        raise DCError(f'{name} must be a number')
    if integer:
        if x != int(x):
            raise DCError(f'{name} must be a whole number')
        x = int(x)
    if lo is not None and (x <= lo if open_lo else x < lo):
        raise DCError(f'{name} must be {"above" if open_lo else "at least"} {lo}')
    if hi is not None and x > hi:
        raise DCError(f'{name} must be at most {hi}')
    return x


def options(d=None):
    """The extraction options, checked and filled with defaults (DCError 400 on a wrong or unknown key).
    `fps` is a shorthand: fps=15 -> 15 frames spread over every second; fps=0.5 -> one frame every 2 s."""
    d = dict(d or {})
    unknown = sorted(set(d) - set(DEFAULTS) - {'fps'})
    if unknown:
        raise DCError(f'unknown option{"s" if len(unknown) > 1 else ""} {", ".join(unknown)} '
                      f'(known: fps, {", ".join(DEFAULTS)})')
    o = dict(DEFAULTS)
    if d.get('fps') is not None:
        fps = _num(d.pop('fps'), 'fps', lo=0, hi=1000, open_lo=True)
        if fps >= 1:
            o.update(mode='pattern', every_s=1.0, window_s=1.0, take=round(fps), pick='spread')
        else:
            o.update(mode='pattern', every_s=1 / fps, window_s=1 / fps, take=1, pick='spread')
    d.pop('fps', None)
    o.update({k: v for k, v in d.items() if v is not None or k in ('end', 'max_side', 'block_s')})
    if o['mode'] not in MODES:
        raise DCError(f'mode must be one of {", ".join(MODES)}')
    o['n'] = _num(o['n'], 'n', lo=1, hi=MAX_FRAMES, integer=True)
    o['every_s'] = _num(o['every_s'], 'every_s', lo=0, hi=86400, open_lo=True)
    o['window_s'] = _num(o['window_s'], 'window_s', lo=0, hi=86400, open_lo=True)
    if o['window_s'] > o['every_s'] + EPS:
        raise DCError('window_s cannot be longer than every_s (the windows would overlap)')
    o['take'] = _num(o['take'], 'take', lo=1, hi=1000, integer=True)
    if o['pick'] not in ('spread', 'consecutive'):
        raise DCError('pick must be spread or consecutive')
    o['threshold'] = _num(o['threshold'], 'threshold', lo=0, hi=1, open_lo=True)
    o['dedupe'] = _num(o['dedupe'], 'dedupe', lo=0, hi=0.5)
    o['start'] = _num(o['start'], 'start', lo=0)
    if o['end'] is not None:
        o['end'] = _num(o['end'], 'end', lo=0)
        if o['end'] <= o['start']:
            raise DCError('end must be after start')
    if o['max_side'] is not None:
        o['max_side'] = _num(o['max_side'], 'max_side', lo=16, hi=16384, integer=True)
    if o['format'] not in FORMATS:
        raise DCError(f'format must be one of {", ".join(FORMATS)}')
    o['quality'] = _num(o['quality'], 'quality', lo=1, hi=100, integer=True)
    if o['layout'] not in LAYOUTS:
        raise DCError(f'layout must be one of {", ".join(LAYOUTS)}')
    sp = o['split']
    if not isinstance(sp, (list, tuple)) or len(sp) != 3:
        raise DCError('split must be three ratios: train, val, test')
    sp = tuple(_num(x, 'split', lo=0, hi=1) for x in sp)
    if abs(sum(sp) - 1) > 1e-3 or sp[0] <= 0:
        raise DCError('the split ratios must add up to 1, with some train')
    o['split'] = sp
    if o['block_s'] is not None:
        o['block_s'] = _num(o['block_s'], 'block_s', lo=0.1, hi=86400)
    o['max_frames'] = _num(o['max_frames'], 'max_frames', lo=1, hi=MAX_FRAMES, integer=True)
    return o


def describe(o):
    """A short human line for options `o` (the job label, the assistant's preview)."""
    m = o['mode']
    if m == 'all':
        s = 'every frame'
    elif m == 'every':
        s = f'every {o["n"]}th frame'
    elif m == 'count':
        s = f'{o["n"]} frames spread over the video'
    elif m == 'keyframes':
        s = 'keyframes only'
    elif m == 'scene':
        s = f'scene changes ≥ {o["threshold"]:g}'
    elif m == 'smart':
        s = f'the {o["n"]} most varied frames'
    else:
        e, w, t = o['every_s'], o['window_s'], o['take']
        how = 'first' if o['pick'] == 'consecutive' else ''
        if abs(e - 1) < EPS and abs(w - 1) < EPS:
            s = f'{how + " " if how else ""}{t} per second'
        elif abs(e - w) < EPS and t == 1:
            s = f'1 frame every {e:g} s'
        else:
            s = f'{how + " " if how else ""}{t} from {w:g} s every {e:g} s'
    if o['start'] or o['end'] is not None:
        s += f' · {o["start"]:g}–{"end" if o["end"] is None else f"{o["end"]:g}"} s'
    if o['dedupe']:
        s += ' · no near-duplicates'
    return s


# ---------------------------------------------------------------------------------------------- frame pickers
class _Every:
    """mode all (n=1) / every: every n-th decoded frame from `start`."""

    seeks = False

    def __init__(self, n):
        self.n, self.i = n, 0

    def keep(self, t):
        k = self.i % self.n == 0
        self.i += 1
        return k

    def next_t(self, t):
        return t


class _Windows:
    """pattern, consecutive: the first `take` frames of every window [start + k·every, + window)."""

    seeks = True

    def __init__(self, o):
        self.s, self.e, self.w, self.take = o['start'], o['every_s'], o['window_s'], o['take']
        self.k, self.got = -1, 0

    def _win(self, t):
        k = math.floor((t - self.s) / self.e + EPS)
        return k, t - self.s - k * self.e < self.w - EPS

    def keep(self, t):
        k, inside = self._win(t)
        if k != self.k:
            self.k, self.got = k, 0
        if inside and self.got < self.take:
            self.got += 1
            return True
        return False

    def next_t(self, t):
        k, inside = self._win(t)
        if inside and (k != self.k or self.got < self.take):
            return t
        return self.s + (k + 1) * self.e


class _Targets:
    """pattern spread / count / smart: a sorted stream of wanted times, each matched to the first frame at or after
    it (within half a frame), every frame counted once. Done when the stream runs out."""

    seeks = True

    def __init__(self, times, dt):
        self.it, self.half = iter(times), dt / 2
        self.cur = next(self.it, None)

    def keep(self, t):
        hit = False
        while self.cur is not None and self.cur <= t + self.half + EPS:
            hit = True
            self.cur = next(self.it, None)
        return hit

    def next_t(self, t):
        return math.inf if self.cur is None else self.cur - self.half


def _spread_times(o, end):
    """The wanted times of pattern spread: take times spread over each window (endless when the end is unknown)."""
    k = 0
    while True:
        s = o['start'] + k * o['every_s']
        if end is not None and s >= end - EPS:
            return
        for i in range(o['take']):
            t = s + i * o['window_s'] / o['take']
            if end is not None and t >= end - EPS:
                return
            yield t
        k += 1


def _count_times(o, end):
    span = end - o['start']
    return [o['start'] + (i + 0.5) * span / o['n'] for i in range(o['n'])]


def _picker(o, info, times=None):
    """The frame picker of options `o` for a video with `info` (smart passes its picked `times`)."""
    m, dt = o['mode'], 1 / (info['fps'] or 30)
    end = _end(o, info)
    if times is not None:
        return _Targets(times, dt)
    if m in ('all', 'keyframes', 'scene'):
        return _Every(1)
    if m == 'every':
        return _Every(o['n'])
    if m == 'count':
        if end is None:
            raise DCError('this video does not say how long it is: pick a rate instead of a total count')
        return _Targets(_count_times(o, end), dt)
    if o['pick'] == 'consecutive':
        return _Windows(o)
    return _Targets(_spread_times(o, end), dt)


def _end(o, info):
    d = info.get('duration') or None
    return min(o['end'], d) if o['end'] is not None and d else (o['end'] or d)


def diff(a, b):
    """How different two small gray frames are: mean absolute difference, 0 (same) .. 1."""
    return float(np.abs(a.astype(np.int16) - b).mean()) / 255.0


# ---------------------------------------------------------------------------------------------- probing
def _av():
    import av
    return av


@functools.lru_cache(maxsize=512)
def _probe(path, mtime_ns, size):
    try:
        return _probe_av(path, size)
    except DCError:
        raise
    except Exception:  # noqa: BLE001 - PyAV cannot read it: try OpenCV before giving up
        return _probe_cv2(path, size)


def probe(path):
    """{w, h, fps, frames, duration, codec, rotation, size, decoder}: w / h are as shown (rotation applied).
    Cached per (path, mtime, size). DCError 415 when the file holds no readable video."""
    st = os.stat(path)
    return dict(_probe(path, st.st_mtime_ns, st.st_size))


def _probe_av(path, size):
    av = _av()
    with av.open(path) as c:
        if not c.streams.video:
            raise DCError(f'{os.path.basename(path)} has no video stream', 415)
        s = c.streams.video[0]
        tb = float(s.time_base or 0)
        fps = float(s.average_rate or s.guessed_rate or s.base_rate or 0)
        dur = float(s.duration * s.time_base) if s.duration and s.time_base else (c.duration or 0) / 1e6
        rot = 0
        w, h = s.codec_context.width, s.codec_context.height
        s.thread_type = 'AUTO'
        for f in c.decode(s):                          # the first frame: the real size and the rotation
            rot, w, h = int(f.rotation or 0), f.width, f.height
            break
        frames = int(s.frames or 0) or (round(dur * fps) if dur and fps else 0)
        rot = (round(rot / 90) * 90) % 360
        if rot in (90, 270):
            w, h = h, w
        return {'w': w, 'h': h, 'fps': round(fps, 3), 'frames': frames, 'duration': round(dur, 3),
                'codec': s.codec_context.name, 'rotation': rot, 'size': size, 'decoder': 'pyav',
                'time_base': tb}


def _probe_cv2(path, size):
    import cv2
    cap = cv2.VideoCapture(path)
    try:
        if not cap.isOpened():
            raise DCError(f'{os.path.basename(path)} is not a video this server can read', 415)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cc = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
        codec = ''.join(chr((cc >> (8 * i)) & 0xFF) for i in range(4)).strip('\0 ') or 'unknown'
        if not w or not h:
            raise DCError(f'{os.path.basename(path)} is not a video this server can read', 415)
        return {'w': w, 'h': h, 'fps': round(fps, 3), 'frames': frames,
                'duration': round(frames / fps, 3) if fps else 0.0, 'codec': codec, 'rotation': 0, 'size': size,
                'decoder': 'opencv', 'time_base': 0.0}
    finally:
        cap.release()


@functools.lru_cache(maxsize=64)
def _keyframes(path, mtime_ns, size):
    """The keyframe times of the video stream (packets only: no decoding), for seek decisions."""
    av = _av()
    with av.open(path) as c:
        s = c.streams.video[0]
        t0 = float(s.start_time * s.time_base) if s.start_time is not None else 0.0
        return tuple(sorted(float(p.pts * p.time_base) - t0 for p in c.demux(s)
                            if p.is_keyframe and p.pts is not None))


def keyframes(path):
    st = os.stat(path)
    return _keyframes(path, st.st_mtime_ns, st.st_size)


# ---------------------------------------------------------------------------------------------- readers
def _size(w, h, max_side):
    """The output size: (w, h) shrunk so the long side is at most max_side (even numbers for swscale)."""
    if not max_side or max(w, h) <= max_side:
        return w, h
    k = max_side / max(w, h)
    return max(2, round(w * k / 2) * 2), max(2, round(h * k / 2) * 2)


class _AvFrame:
    __slots__ = ('f', 'rot', 't')

    def __init__(self, f, t, rot):
        self.f, self.t, self.rot = f, t, rot

    def bgr(self, max_side=None):
        f = self.f
        w, h = _size(f.width, f.height, max_side)
        if (w, h) == (f.width, f.height):
            a = f.to_ndarray(format='bgr24')
        else:
            a = f.reformat(width=w, height=h, format='bgr24', interpolation='AREA').to_ndarray()
        return np.ascontiguousarray(np.rot90(a, self.rot // 90)) if self.rot else a

    def gray(self, w=64, h=36):
        return self.f.reformat(width=w, height=h, format='gray', interpolation='AREA').to_ndarray()


class _AvReader:
    """Frames of the first video stream with times from its start; seek(t) jumps to the keyframe at or before t."""

    def __init__(self, path, skip=None):
        av = _av()
        self.av = av
        self.c = av.open(path)
        if not self.c.streams.video:
            self.c.close()
            raise DCError(f'{os.path.basename(path)} has no video stream', 415)
        self.s = self.c.streams.video[0]
        self.s.thread_type = 'AUTO'
        self.s.codec_context.thread_count = 0
        if skip:
            self.s.codec_context.skip_frame = skip
        self.tb = float(self.s.time_base)
        self.t0 = float(self.s.start_time * self.s.time_base) if self.s.start_time is not None else None
        fps = float(self.s.average_rate or self.s.guessed_rate or 0) or 30.0
        self.dt = 1 / fps
        self._seek = None
        self.errors = 0

    def seek(self, t):
        self._seek = t

    def frames(self):
        last = -self.dt
        while True:
            if self._seek is not None:
                ts = int(((self.t0 or 0.0) + max(0.0, self._seek)) / self.tb)
                self._seek = None
                self.c.seek(ts, stream=self.s, backward=True, any_frame=False)
            gen = self.c.decode(self.s)
            try:
                for f in gen:
                    if f.time is None:
                        t = last + self.dt
                    else:
                        if self.t0 is None:
                            self.t0 = float(f.time)
                        t = float(f.time) - self.t0
                    last = t
                    yield _AvFrame(f, t, (round((f.rotation or 0) / 90) * 90) % 360)
                    if self._seek is not None:
                        break
                else:
                    return
            except self.av.error.InvalidDataError:
                self.errors += 1                # a damaged packet: go on with the next one
                if self.errors > 1000:
                    return
            except self.av.error.EOFError:
                return

    def close(self):
        self.c.close()


class _CvFrame:
    __slots__ = ('_a', 'cap', 'rot', 't')

    def __init__(self, cap, t):
        self.cap, self.t, self.rot, self._a = cap, t, 0, None

    def _img(self):
        if self._a is None:
            ok, a = self.cap.retrieve()
            if not ok:
                raise DCError('could not decode a frame', 415)
            self._a = a
        return self._a

    def bgr(self, max_side=None):
        import cv2
        a = self._img()
        w, h = _size(a.shape[1], a.shape[0], max_side)
        return a if (w, h) == (a.shape[1], a.shape[0]) else cv2.resize(a, (w, h), interpolation=cv2.INTER_AREA)

    def gray(self, w=64, h=36):
        import cv2
        return cv2.resize(cv2.cvtColor(self._img(), cv2.COLOR_BGR2GRAY), (w, h), interpolation=cv2.INTER_AREA)


class _CvReader:
    """The OpenCV fallback: sequential grab() (decode without conversion), retrieve() only for kept frames."""

    def __init__(self, path):
        import cv2
        self.cv2 = cv2
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise DCError(f'{os.path.basename(path)} is not a video this server can read', 415)
        fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 0) or 30.0
        self.dt = 1 / fps
        self._seek = None
        self.errors = 0

    def seek(self, t):
        self._seek = t

    def frames(self):
        i = 0
        while True:
            if self._seek is not None:
                self.cap.set(self.cv2.CAP_PROP_POS_MSEC, self._seek * 1000)
                i = round(self._seek / self.dt)
                self._seek = None
            if not self.cap.grab():
                return
            ms = self.cap.get(self.cv2.CAP_PROP_POS_MSEC)
            t = ms / 1000 if ms and ms > 0 else i * self.dt
            i += 1
            yield _CvFrame(self.cap, t)

    def close(self):
        self.cap.release()


def _reader(path, info, skip=None):
    if info.get('decoder') == 'pyav':
        return _AvReader(path, skip)
    if skip == 'NONKEY':
        raise DCError('keyframes-only needs a video PyAV can read: pick another mode for this file', 415)
    return _CvReader(path)


# ---------------------------------------------------------------------------------------------- poster frames
def frame_at(path, t=None, w=320, quality=82):
    """A JPEG of the frame at `t` seconds (default: 10 % in, at most 5 s), at most `w` px on its long side: a seek to
    the keyframe before it, then at most POSTER_SCAN seconds decoded forward (a very long GOP gives the nearest
    frame within that reach, which is plenty for a poster or a film strip)."""
    import cv2
    info = probe(path)
    d = info['duration'] or 0
    if t is None:
        t = min(5.0, d * 0.1)
    t = max(0.0, min(float(t), max(0.0, d - 0.05)) if d else float(t))
    r = _reader(path, info)
    try:
        if t > 0:
            r.seek(t)
        fr, first = None, None
        for f in r.frames():
            fr = f
            first = f.t if first is None else first
            if f.t >= t - r.dt / 2 - EPS or f.t - first >= POSTER_SCAN:
                break
        if fr is None:
            raise DCError('no frame there', 415)
        ok, buf = cv2.imencode('.jpg', fr.bgr(max(16, min(int(w), 2048))), [cv2.IMWRITE_JPEG_QUALITY, quality])
    finally:
        r.close()
    if not ok:
        raise DCError('cannot make a poster frame', 415)
    return buf.tobytes()


# ---------------------------------------------------------------------------------------------- estimate
def estimate(info, o, kf=None):
    """{frames, approx, times, bytes}: how many frames options `o` take from a video with `info` and up to 8 of their
    times (the dialog's film strip). Rate and count modes walk the nominal frame grid with the same pickers the
    extraction uses (jumping over gaps, so the cost follows the frames kept); keyframes are exact with the packet
    index `kf` (keyframes()); scene and smart are guesses."""
    fps = info['fps'] or 30.0
    end = _end(o, info)
    start = o['start']
    m = o['mode']
    span = (end - start) if end else 0.0
    if end is not None and span <= 0:
        return {'frames': 0, 'approx': False, 'times': [], 'bytes': 0}
    n_range = max(0, math.ceil(end * fps - EPS) - math.ceil(start * fps - EPS)) if end else 0
    approx = False
    times = None
    if end is None and m != 'smart':
        approx, n = True, None
    elif m in ('all', 'every'):
        n = math.ceil(n_range / (o['n'] if m == 'every' else 1))
    elif m == 'keyframes':
        if kf is not None:                              # the packet index: exact
            times = [t for t in kf if start - EPS <= t < end - EPS]
            n = len(times)
        else:
            approx, n = True, max(1, round(span / 2))
    elif m == 'scene':
        approx, n = True, None
    elif m == 'smart':
        approx, n = True, min(o['n'], max(1, int(span * SMART_RATE))) if span else o['n']
    else:
        p = _picker(o, info)
        n, got, j = 0, [], math.ceil(start * fps - EPS)
        cap = o['max_frames'] + 1
        while n < cap:
            t = j / fps
            if t >= end - EPS:
                break
            if p.keep(t):
                n += 1
                if len(got) < 4096:
                    got.append(t)
            nt = p.next_t(t)
            if nt == math.inf:
                break
            j = max(j + 1, math.ceil(nt * fps - EPS))
        times = got
    if n is not None:
        n = min(n, o['max_frames'])
    if times is None:
        k = min(8, n or 8)
        times = [start + (i + 0.5) * span / k for i in range(k)] if span else []
    elif len(times) > 8:
        times = [times[round(i * (len(times) - 1) / 7)] for i in range(8)]
    w, h = _size(info['w'], info['h'], o['max_side'])
    return {'frames': n, 'approx': approx, 'times': [round(t, 3) for t in times],
            'bytes': int((n or 0) * w * h * BYTES_PER_PX[o['format']] * (o['quality'] / 90 if o['format'] != 'png' else 1))}


# ---------------------------------------------------------------------------------------------- extraction
class _Writer:
    """Encode + write frames on a thread pool, at most 2×workers frames (and INFLIGHT bytes) in flight: the
    decoder waits when the encoders fall behind, so a 4K video never piles frames up in memory."""

    INFLIGHT = 256 << 20

    def __init__(self, o, workers=None):
        import cv2
        self.cv2 = cv2
        self.ext = FORMATS[o['format']]
        q = o['quality']
        self.params = {'jpg': [cv2.IMWRITE_JPEG_QUALITY, q], 'png': [cv2.IMWRITE_PNG_COMPRESSION, 1],
                       'webp': [cv2.IMWRITE_WEBP_QUALITY, q]}[o['format']]
        self.n = workers or max(1, min(8, os.cpu_count() or 1))
        self.pool = ThreadPoolExecutor(self.n, thread_name_prefix='dc-frames')
        self.pending = deque()
        self.bytes = 0

    def _write(self, path, img):
        ok, buf = self.cv2.imencode(self.ext, img, self.params)
        if not ok:
            raise DCError(f'could not encode {os.path.basename(path)}')
        with open(path, 'wb') as f:
            f.write(buf)
        return len(buf)

    def put(self, path, img):
        cap = max(2, min(2 * self.n, self.INFLIGHT // max(1, img.nbytes)))
        while len(self.pending) >= cap:
            self.bytes += self.pending.popleft().result()       # re-raises a worker's error here
        self.pending.append(self.pool.submit(self._write, path, img))

    def close(self, wait=True):
        try:
            while self.pending:
                fut = self.pending.popleft()
                if wait:
                    self.bytes += fut.result()
                else:
                    fut.cancel()
        finally:
            self.pool.shutdown(wait=True)


def _smart_times(path, info, o, progress, cancelled, label):
    """Smart pass 1: look at up to SMART_RATE candidate frames a second (non-reference frames are not even
    decoded) as 32×18 gray, then pick n by farthest-point sampling: each next pick is the candidate furthest from
    every frame picked so far, the distance being how different it looks plus a little of how far apart in time.
    Distinct scenes are caught and a long static stretch still gets covered. -> the picked times, in order."""
    end = _end(o, info)
    r = _reader(path, info, 'NONREF' if info.get('decoder') == 'pyav' else None)
    ts, gs, last = [], [], -math.inf
    try:
        if o['start'] > SEEK_GAP:
            r.seek(o['start'])
        total = info['frames'] or None
        for i, fr in enumerate(r.frames()):
            if cancelled():
                raise Cancelled()
            t = fr.t
            if t < o['start'] - EPS:
                continue
            if end is not None and t >= end - EPS:
                break
            if t - last >= 1 / SMART_RATE - EPS:
                ts.append(t)
                gs.append(fr.gray(32, 18))
                last = t
            if i % 100 == 0:
                progress(i, total, f'{label}: finding the most varied frames')
    finally:
        r.close()
    if len(ts) <= o['n']:
        return ts
    g = np.stack(gs).reshape(len(gs), -1).astype(np.float32) / 255.0
    tt = np.asarray(ts, np.float64)
    span = max(tt[-1] - tt[0], EPS)
    near = np.full(len(ts), np.inf)
    picked, p = [], 0
    for _ in range(o['n']):                              # n steps of one vector pass each: O(n × candidates)
        picked.append(p)
        d = np.abs(g - g[p]).mean(axis=1) + SMART_TIME * np.abs(tt - tt[p]) / span
        np.minimum(near, d, out=near)
        p = int(np.argmax(near))
        if near[p] <= EPS:
            break
    return [ts[j] for j in sorted(picked)]


def extract(path, folder, stem, o, info=None, progress=None, cancelled=None, label=None, start_count=0):
    """Write the frames options `o` pick from video `path` into `folder` as <stem>_<frame:06d>.<ext>.
    -> {rows: [(file, frame, time_s)], frames, bytes, skipped_dupes, errors, capped, decoder}.
    progress(done, total, stage, extra) and cancelled() come from the job; store.Cancelled stops it."""
    info = info or probe(path)
    progress = progress or (lambda *a, **k: None)
    cancelled = cancelled or (lambda: False)
    label = label or os.path.basename(path)
    fps = info['fps'] or 30.0
    end = _end(o, info)
    os.makedirs(folder, exist_ok=True)
    times = _smart_times(path, info, o, progress, cancelled, label) if o['mode'] == 'smart' else None
    picker = _picker(o, info, times)
    min_change = max(o['threshold'] if o['mode'] == 'scene' else 0.0, o['dedupe'])
    budget = o['max_frames'] - start_count
    est = estimate(info, o)['frames']
    total = min(est, budget) if est is not None else None
    r = _reader(path, info, 'NONKEY' if o['mode'] == 'keyframes' else None)
    can_seek = picker.seeks and info.get('decoder') == 'pyav'
    kfs = None
    w = _Writer(o)
    rows, used, last_g = [], set(), None
    dupes, capped, ok = 0, False, False
    tick = time.monotonic()
    try:
        if o['start'] > SEEK_GAP:
            r.seek(o['start'])
        for fr in r.frames():
            if cancelled():
                raise Cancelled()
            t = fr.t
            if t < o['start'] - EPS:
                continue
            if end is not None and t >= end - EPS:
                break
            if picker.keep(t):
                g = fr.gray() if min_change else None
                if g is not None and last_g is not None and diff(g, last_g) < min_change:
                    dupes += 1
                else:
                    if len(rows) >= budget:
                        capped = True
                        break
                    idx = round(t * fps)
                    name = f'{stem}_{idx:06d}'
                    if name in used:
                        k = 1
                        while f'{name}_{k}' in used:
                            k += 1
                        name = f'{name}_{k}'
                    used.add(name)
                    fn = name + w.ext
                    w.put(os.path.join(folder, fn), fr.bgr(o['max_side']))
                    rows.append((fn, idx, round(t, 4)))
                    if g is not None:
                        last_g = g
            nt = picker.next_t(t)
            if nt == math.inf:
                break
            if can_seek and nt - t > SEEK_GAP:
                if kfs is None:
                    kfs = keyframes(path)
                i = bisect.bisect_right(kfs, nt + EPS) - 1
                if i >= 0 and kfs[i] > t + 1.5 / fps:  # only when the seek lands ahead of where we are
                    r.seek(kfs[i])
            now = time.monotonic()
            if now - tick > 0.25:
                tick = now
                progress(len(rows), total, label, f'{t:.0f}/{info["duration"] or 0:.0f} s')
        ok = True
    finally:
        try:
            w.close(wait=ok)
        finally:
            r.close()
    progress(len(rows), total, label)
    return {'rows': rows, 'frames': len(rows), 'bytes': w.bytes, 'skipped_dupes': dupes, 'errors': r.errors,
            'capped': capped, 'decoder': info.get('decoder')}


# ---------------------------------------------------------------------------------------------- layouts
def image_folder(base, layout, stem):
    """Where the frames of video `stem` are written inside the job's folder `base`."""
    if layout in ('yolo', 'yolo-split'):
        return os.path.join(base, 'images')
    if layout == 'per-video':
        return os.path.join(base, stem)
    return base


def assign_splits(blocks, ratios):
    """Spread the ordered `blocks` over train / val / test so the counts follow `ratios` (interleaved: the split
    furthest below its share takes the next block), and every split with a share gets a block when there are
    enough. -> [split per block]."""
    want = [s for s, r in zip(SPLITS, ratios) if r > 0]
    share = dict(zip(SPLITS, ratios))
    count = dict.fromkeys(SPLITS, 0)
    out = []
    for i, _ in enumerate(blocks):
        empty = [s for s in want if count[s] == 0]
        left = len(blocks) - i
        if empty and left <= len(empty):
            s = empty[0]
        else:
            s = max(want, key=lambda x: (share[x] * (i + 1) - count[x], share[x]))
        count[s] += 1
        out.append(s)
    return out


def finish(base, layout, videos, o):
    """Lay out the job folder `base` once every video is written. videos = [(video rel, stem, rows, duration)].
    yolo: empty labels/, data.yaml (no classes yet); yolo-split: the frames move into images/{train,val,test}/ by
    time block (by whole video when there are 3 or more), so near-identical neighbours never sit in two splits.
    Every layout gets frames.csv (file, video, frame, time_s, split). -> {splits?: {split: n}}."""
    out = {}
    split_of = {}
    if layout == 'yolo-split':
        if len(videos) >= 3:
            keys = [(stem, 0) for _, stem, _, _ in videos]
            blk = {(stem, fn): (stem, 0) for _, stem, rows, _ in videos for fn, _, _ in rows}
        else:
            keys, blk = [], {}
            for _, stem, rows, dur in videos:
                bs = o['block_s'] or max(1.0, min(10.0, (dur or 0) / 20 or 10.0))
                for fn, _, t in rows:
                    k = (stem, int(t // bs))
                    blk[(stem, fn)] = k
                    if not keys or keys[-1] != k:
                        keys.append(k)
        keys = list(dict.fromkeys(keys))
        where = dict(zip(keys, assign_splits(keys, o['split'])))
        img = os.path.join(base, 'images')
        for s in SPLITS:
            os.makedirs(os.path.join(img, s), exist_ok=True)
        counts = dict.fromkeys(SPLITS, 0)
        for _, stem, rows, _ in videos:
            for fn, _, _ in rows:
                s = where[blk[(stem, fn)]]
                os.replace(os.path.join(img, fn), os.path.join(img, s, fn))
                split_of[(stem, fn)] = s
                counts[s] += 1
        out['splits'] = counts
    if layout in ('yolo', 'yolo-split'):
        if layout == 'yolo-split':
            for s in SPLITS:
                os.makedirs(os.path.join(base, 'labels', s), exist_ok=True)
            used = [s for s in SPLITS if out['splits'][s]]
            tr = 'images/train'
            va = 'images/val' if 'val' in used else tr
            lines = ['path: .', f'train: {tr}', f'val: {va}'] + (['test: images/test'] if 'test' in used else [])
        else:
            os.makedirs(os.path.join(base, 'labels'), exist_ok=True)
            lines = ['path: .', 'train: images', 'val: images']
        with open(os.path.join(base, 'data.yaml'), 'w', encoding='utf-8') as f:
            f.write('# frames cut from video by the Data Center: add classes, then label\n' +
                    '\n'.join(lines + ['nc: 0', 'names: []']) + '\n')
    with open(os.path.join(base, 'frames.csv'), 'w', newline='', encoding='utf-8') as f:
        wr = csv.writer(f)
        wr.writerow(['file', 'video', 'frame', 'time_s', 'split'])
        for vrel, stem, rows, _ in videos:
            sub = os.path.relpath(image_folder(base, layout, stem), base).replace(os.sep, '/')
            for fn, idx, t in rows:
                s = split_of.get((stem, fn), '')
                p = '/'.join(x for x in (sub if sub != '.' else '', s, fn) if x)
                wr.writerow([p, vrel, idx, t, s])
    return out


def stems(rels):
    """A unique, file-name-safe stem per video rel path (clip, clip-2, ...)."""
    out, seen = [], set()
    for rel in rels:
        base = os.path.splitext(rel.rsplit('/', 1)[-1])[0]
        s = ''.join(ch if ch.isalnum() or ch in '-_.' else '_' for ch in base).strip('._') or 'video'
        s = s[:80]
        cand, k = s, 2
        while cand.lower() in seen:
            cand, k = f'{s}-{k}', k + 1
        seen.add(cand.lower())
        out.append(cand)
    return out
