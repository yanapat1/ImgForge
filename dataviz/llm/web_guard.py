"""
The web search privacy guard: every query and URL the server sends out (tools/web.py) passes `check` first, so the
model never decides on its own how much internal information leaves.

    ok, reasons, redacted = check(query_or_url, ctx)

`reasons` has one category per internal term found ('bucket name', 'special word', 'secret' ...); `redacted` is the
text with each of them replaced by `[category]` (what the trace may show). Never echo the terms themselves.

Levels (ctx.web_level, capped by the caller):
    1 public      blocks every internal term and every special word
    2 technical   also lets through class names and tags (formats / model names are generic anyway); still blocks
                  bucket, folder and file names, paths, hosts, people, connections and every special word
    3 full        blocks only secrets and the special words marked 'never'

Internal terms are collected per request: the names the tools returned this turn (ctx.seen), the user's buckets
and their tags, the visualizer session (class names, dataset name, its folder), the user's connections (names,
hosts, buckets, secrets), the user's own names and email, plus what the text itself shows: internal IPs and host
names, email addresses, absolute paths and API-key-like secrets. Special words: the admin's organisation list
(settings llm_org_words) + the user's personal list (ctx.web_cfg['words']); a `/regex/` entry is a regex.

Matching is case-insensitive with separators normalised (night_cam-01 == night cam 01), on word boundaries for
Latin text and as a substring for Thai and the other scripts written without spaces. Generic words (images,
labels, train, yolo, COCO classes, common English words, anything under 3 characters) never count as internal.
"""

import ipaddress
import re
from urllib.parse import unquote, urlsplit

MAX_TEXT = 4000                 # characters of a query / URL that are checked (longer is refused by the tool)
NEVER = 99                      # allowed from this level = never allowed

# allowed from level n: a term blocks while level < n
_ALLOWED_FROM = {
    'class name': 2, 'tag': 2,
    'bucket name': 3, 'folder or file name': 3, 'dataset name': 3, 'path': 3, 'host': 3, 'internal address': 3,
    'person': 3, 'email': 3, 'connection': 3,
    'secret': NEVER,
}

GENERIC = set("""
image images img imgs picture pictures photo photos label labels labeled labelled annotation annotations annotate
train training val valid validation test testing data dataset datasets sample samples split splits subset
yolo yolov5 yolov8 yolov9 yolov10 yolo11 yolo12 ultralytics coco voc pascal imagenet openimages cvat labelme
labelstudio roboflow kaggle huggingface detect detection detector segment segmentation classify classification
classifier pose keypoint keypoints obb bbox bboxes box boxes mask masks polygon polygons class classes category
categories model models weights checkpoint epoch epochs batch folder folders file files bucket buckets path
jpg jpeg png bmp tif tiff webp gif txt json yaml yml xml csv zip tar parquet
format formats export import upload download local remote cloud server client default new old copy backup temp tmp
the and for with how what why when where which who you your our are can not but all any this that from into
about best top more most less many much some use using used get set make made does did done have has had will
one two three first last next other same each every both only also just than then them they their there here
error errors issue issues fix bug help guide tutorial example examples docs documentation version latest
python pytorch torch tensorflow numpy opencv cuda gpu cpu linux windows docker github api http https www com org
person people man woman child bicycle bike car cars motorcycle airplane bus train truck boat traffic light lights
fire hydrant stop sign parking meter bench bird cat dog horse sheep cow elephant bear zebra giraffe backpack
umbrella handbag tie suitcase frisbee skis snowboard sports ball kite baseball bat glove skateboard surfboard
tennis racket bottle wine glass cup fork knife spoon bowl banana apple sandwich orange broccoli carrot hot pizza
donut cake chair couch potted plant bed dining table toilet tv laptop mouse keyboard cell phone microwave oven
toaster sink refrigerator book clock vase scissors teddy hair drier toothbrush vehicle vehicles pedestrian
pedestrians road street night day camera cameras video frame frames object objects small large accuracy precision
recall map iou loss metric metrics augmentation augment occlusion blur dark noise noisy duplicate duplicates
""".split())

# scripts written without spaces between words: substring matching
_NOSPACE = re.compile(r'[฀-๿຀-໿က-႟ក-៿぀-ヿ㐀-鿿豈-﫿]')
_SEP = re.compile(r'[\s_\-./\\:,;|+]+')

_SECRETS = [
    re.compile(r'\bsk-[A-Za-z0-9_\-]{16,}'),                        # OpenAI / Anthropic-like
    re.compile(r'\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}'),
    re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'),                   # AWS access key id
    re.compile(r'\bAIza[0-9A-Za-z_\-]{30,}'),                       # Google API key
    re.compile(r'\bxox[abprs]-[A-Za-z0-9\-]{10,}'),                 # Slack
    re.compile(r'\bhf_[A-Za-z0-9]{20,}'),                           # Hugging Face
    re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}'),     # JWT
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    re.compile(r'(?i)\b(?:password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|secret[_\-]?key|auth)'
               r'\s*[=:]\s*\S+'),
    re.compile(r'(?m)^\s*[A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASS)\s*=\s*\S+'),    # .env lines
    re.compile(r'\b[0-9a-fA-F]{32,}\b'),                            # long hex tokens
    re.compile(r'(?<![A-Za-z0-9+/_\-])(?=[A-Za-z0-9+/_\-]*[0-9])(?=[A-Za-z0-9+/_\-]*[a-z])(?=[A-Za-z0-9+/_\-]*[A-Z])'
               r'[A-Za-z0-9+/_\-]{32,}={0,2}'),                     # long base64-like tokens (mixed case + digits)
]
_IPV4 = re.compile(r'(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])')
_HOST = re.compile(r'(?i)\b(?:localhost|[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*\.(?:local|lan|internal|intranet|corp|'
                   r'home\.arpa|localdomain|svc|cluster\.local))\b')
_EMAIL = re.compile(r'(?i)\b[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}\b')
_PATH = re.compile(r'(?:(?<![\w/])/(?:home|root|workspace|mnt|data|srv|opt|var|tmp|users|media|volumes)/\S+'
                   r'|\b[A-Za-z]:\\\S+|~/\S+)', re.I)
_SECRET_KEYS = ('api_key', 'secret_key', 'access_key', 'secret', 'password', 'token', 'session_token', 'key')


# ---------------------------------------------------------------- normalising
def _norm(s):
    """(normalised text, index map normalised -> original): lower-case, every run of separators one space."""
    out, idx, sep = [], [], True
    for i, ch in enumerate(str(s)):
        if _SEP.fullmatch(ch):
            if not sep:
                out.append(' ')
                idx.append(i)
            sep = True
            continue
        for c in ch.lower():
            out.append(c)
            idx.append(i)
        sep = False
    if out and out[-1] == ' ':
        out.pop()
        idx.pop()
    return ''.join(out), idx


def norm(s):
    return _norm(s)[0]


def _generic(n):
    """True when a normalised term must never count as internal."""
    if len(n) < 3 or n.isdigit():
        return True
    words = n.split(' ')
    return all(w in GENERIC or len(w) < 3 or w.isdigit() for w in words)


def _finder(n):
    """A compiled pattern for one normalised term (word boundaries, or a substring for no-space scripts)."""
    if _NOSPACE.search(n):
        return re.compile(re.escape(n))
    return re.compile(r'(?<!\w)' + re.escape(n) + r'(?!\w)')


# ---------------------------------------------------------------- what is internal
class _Terms:
    """normalised term -> (category, allowed from level); the strictest source wins, except that a name the tools
    returned (ctx.seen) and that is also a class name / tag counts as the technical one."""

    def __init__(self):
        self.t = {}
        self.seen = set()

    def add(self, s, cat, special=False, allowed=None):
        if not isinstance(s, str) or not s.strip():
            return
        n = norm(s)[:300]
        if not n or (not special and _generic(n)):
            return
        a = allowed if allowed is not None else _ALLOWED_FROM[cat]
        cur = self.t.get(n)
        if cur is None or a > cur[1] or (n in self.seen and cat != 'folder or file name'):
            self.t[n] = (cat, a)
            if cat == 'folder or file name':
                self.seen.add(n)
            elif n in self.seen:
                self.seen.discard(n)


def _add_path(T, s, cat='folder or file name'):
    """A name or path: the whole string and every folder / file name in it (and a file name without extension)."""
    T.add(s, cat)
    if '/' in s or '\\' in s:
        for part in re.split(r'[\\/]+', s):
            T.add(part, cat)
            if '.' in part:
                T.add(part.rsplit('.', 1)[0], cat)


def _host_of(v):
    return (urlsplit(v).hostname or v) if '://' in v else v


def _special(ctx):
    """[(text, strength)]: the organisation list (admin) + the user's personal list."""
    out = []
    try:
        from ..auth import settings
        out += [(w.get('text'), w.get('strength') or 'never') for w in settings.get('llm_org_words') or []
                if isinstance(w, dict)]
    except Exception:  # noqa: BLE001 - no database (tests, CLI)
        pass
    cfg = getattr(ctx, 'web_cfg', None) or {}
    out += [(w.get('text'), w.get('strength') or 'private') for w in cfg.get('words') or [] if isinstance(w, dict)]
    return [(t, s) for t, s in out if isinstance(t, str) and t.strip()]


def collect(ctx):
    """The internal terms of this request -> _Terms (best effort: a source that cannot be read is skipped)."""
    T = _Terms()
    for s in sorted(getattr(ctx, 'seen', None) or (), key=str):
        if isinstance(s, str):
            _add_path(T, s)
    user = getattr(ctx, 'user', None) or {}
    for k in ('username', 'display_name', 'name'):
        T.add(user.get(k), 'person')
    if user.get('email'):
        T.add(user['email'], 'email')
        T.add(user['email'].split('@', 1)[0], 'person')
    try:
        from .tools import _svc
        for b in _svc().list_buckets(user) or []:
            T.add(b.get('name'), 'bucket name')
            for t in b.get('tags') or []:
                tag = t.get('tag') if isinstance(t, dict) else t
                if isinstance(tag, str):
                    T.add(tag.split(':', 1)[-1] if tag.startswith('format:') else tag, 'tag')
            for k in ('root', 'path', 'prefix'):
                if isinstance(b.get(k), str):
                    _add_path(T, b[k], 'path')
            s3 = b.get('s3') if isinstance(b.get('s3'), dict) else {}
            for k in ('endpoint', 's3_bucket', 'bucket'):
                v = b.get(k) or s3.get(k)
                if isinstance(v, str) and v:
                    T.add(*((_host_of(v), 'host') if k == 'endpoint' else (v, 'bucket name')))
    except Exception:  # noqa: BLE001 - no database / service in this context
        pass
    st = getattr(ctx, 'state', None)
    if st is not None:
        for c in getattr(st, 'classes', None) or []:
            T.add(str(c), 'class name')
        for k in ('dataset_name', 'name', 'session_name', 'title'):
            v = getattr(st, k, None)
            if isinstance(v, str):
                T.add(v, 'dataset name')
        src = getattr(st, 'source', None)
        for k in ('root', 'path', 'prefix', 'bucket'):
            v = getattr(src, k, None)
            if v is not None and str(v):
                _add_path(T, str(v), 'path')
    scope = getattr(ctx, 'scope', None) or {}
    for k in ('session', 'session_name', 'dataset', 'dataset_name', 'name'):
        if isinstance(scope.get(k), str):
            T.add(scope[k], 'dataset name')
    if isinstance(scope.get('path'), str):
        _add_path(T, scope['path'])
    app = getattr(ctx, 'app', None)
    reg = getattr(app, 'datasets', None)
    if reg is not None and user.get('id'):
        try:
            for c in reg.connections(user['id']) or []:
                T.add(c.get('name'), 'connection')
                for k in ('endpoint', 'url', 'host'):
                    v = c.get(k)
                    if isinstance(v, str) and v:
                        T.add(_host_of(v), 'host')
                if isinstance(c.get('bucket'), str):
                    T.add(c['bucket'], 'bucket name')
                for k in _SECRET_KEYS:
                    v = c.get(k)
                    if isinstance(v, str) and len(v) >= 8:
                        T.add(v, 'secret', special=True)
        except Exception:  # noqa: BLE001 - no database
            pass
    for text, strength in _special(ctx):
        a = NEVER if strength == 'never' else 3
        if len(text) > 2 and text.startswith('/') and text.endswith('/'):
            T.t.setdefault(('re', text), ('special word', a))
        else:
            T.add(text, 'special word', special=True, allowed=a)
    return T


# ---------------------------------------------------------------- the check
def _bad_ip(s):
    try:
        a = ipaddress.ip_address(s)
    except ValueError:
        return False
    return a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_unspecified


def _scan(text, terms, level):
    """[(start, end, category)] over the original text, for the terms that block at this level."""
    hits = []
    for rx in _SECRETS:
        hits += [(m.start(), m.end(), 'secret') for m in rx.finditer(text)]
    if level < 3:
        hits += [(m.start(1), m.end(1), 'internal address') for m in _IPV4.finditer(text) if _bad_ip(m.group(1))]
        hits += [(m.start(), m.end(), 'host') for m in _HOST.finditer(text)]
        hits += [(m.start(), m.end(), 'email') for m in _EMAIL.finditer(text)]
        hits += [(m.start(), m.end(), 'path') for m in _PATH.finditer(text)]
    n, idx = _norm(text)
    for key, (cat, allowed) in terms.t.items():
        if level >= allowed:
            continue
        if isinstance(key, tuple):                     # a /regex/ special word
            try:
                rx = re.compile(key[1][1:-1], re.I)
            except re.error:
                continue
            hits += [(m.start(), m.end(), cat) for m in rx.finditer(text) if m.end() > m.start()]
            continue
        for m in _finder(key).finditer(n):
            hits.append((idx[m.start()], idx[m.end() - 1] + 1, cat))
    return hits


def _merge(hits):
    """Overlapping hits -> one each (the first category wins), in text order."""
    out = []
    for s, e, cat in sorted(hits, key=lambda h: (h[0], -h[1])):
        if out and s < out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e), out[-1][2])
        else:
            out.append((s, e, cat))
    return out


def _redact(text, hits):
    out, last = [], 0
    for s, e, cat in hits:
        out += [text[last:s], f'[{cat}]']
        last = e
    return ''.join(out + [text[last:]])


def _url_text(url):
    """What of a URL is checked against the terms: host, path and query (decoded), plus the internal host test."""
    p = urlsplit(url)
    return ' '.join(x for x in (p.hostname or '', unquote(p.path or ''), unquote(p.query or '').replace('&', ' '),
                                unquote(p.fragment or '')) if x)


def check(text, ctx, level=None):
    """(ok, reasons, redacted). `level` defaults to ctx.web_level (at least 1: the guard never lets more through
    than Public when no level is set)."""
    level = max(1, min(3, int(level if level is not None else getattr(ctx, 'web_level', 1) or 1)))
    text = str(text or '')[:MAX_TEXT]
    terms = collect(ctx)
    is_url = bool(re.match(r'(?i)^[a-z][a-z0-9+.\-]*://', text.strip()))
    if is_url:
        u = text.strip()
        p = urlsplit(u)
        host = (p.hostname or '').strip('[]')
        reasons = []
        if level < 3 and host and (_bad_ip(host) or _HOST.fullmatch(host) or '.' not in host):
            reasons.append('host')
        if p.username or p.password:
            reasons.append('secret')
        reasons += [cat for _, _, cat in _merge(_scan(_url_text(u), terms, level)) if cat != 'path']
        if not any(c == 'secret' for c in reasons):
            reasons += ['secret' for _ in _merge(_scan(u, _Terms(), 3))]
        return (not reasons), reasons, f'{p.scheme}://[{reasons[0]}]' if reasons else u
    hits = _merge(_scan(text, terms, level))
    reasons = [c for _, _, c in hits]
    return (not hits), reasons, _redact(text, hits)


def message(reasons):
    """What the model gets for a blocked query (never the terms themselves)."""
    cats = ', '.join(dict.fromkeys(reasons))
    n = len(reasons)
    return (f'query contained {n} internal term{"s" if n != 1 else ""} ({cats}): rephrase it with general words '
            'only')
