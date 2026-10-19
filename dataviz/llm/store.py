"""
<workspace_root>/llm.json: the saved LLM providers (one active), the feature switches (all off by default) and the
token budget. Owner-only like connections.json; API keys are write-only: the page only ever sees `has_key`.

A provider is "connected" when it is the active one and its last Test connection passed (`verified_at`); editing its
kind / URL / model / key clears that, so a changed provider has to be tested again before any LLM UI shows.
"""

import json
import os
import threading
import time
import uuid
from pathlib import Path

KINDS = ('openai', 'openai_compat', 'anthropic', 'gemini')
BOX_FORMATS = ('norm1000_yxyx', 'norm1000_xyxy', 'pixel_xyxy')
# key -> (where it shows, capability it needs: None | 'vision' | 'audio')
FEATURES = {
    'assistant': ('viz', None), 'dataset_qa': ('viz', None), 'explain_eval': ('viz', None),
    'ask_selection': ('viz', 'vision'), 'label_check': ('viz', 'vision'), 'report': ('viz', None),
    'nl_filter': ('viz', None), 'auto_tag': ('viz', 'vision'),
    'prelabel': ('label', 'vision'), 'label_assist': ('label', 'vision'), 'voice': ('any', 'audio'),
}
# answered in the ◆ panel (their buttons elsewhere open it), so they also need `assistant`
PANEL = ('dataset_qa', 'explain_eval', 'ask_selection', 'label_check', 'report', 'auto_tag')
MAX_BUDGET = 100_000            # hard cap on estimated input tokens per request (asked for: never more than 100k)
_FIELDS = ('name', 'kind', 'base_url', 'model', 'vision', 'audio', 'box_format', 'timeout_s', 'max_output_tokens',
           'temperature')
_RETEST = ('kind', 'base_url', 'model', 'api_key')      # changing one of these means "not verified" again
DEFAULT_BOX = {'gemini': 'norm1000_yxyx'}               # everyone else: norm1000_xyxy


def _mask(key):
    key = key or ''
    return (key[:2] + '•' * max(0, len(key) - 6) + key[-4:]) if len(key) > 6 else '•' * len(key)


def resolve_key(p):
    """The provider's API key; `env:NAME` reads the environment variable NAME at call time."""
    k = (p.get('api_key') or '').strip()
    return os.environ.get(k[4:].strip(), '') if k.startswith('env:') else k


class LLMStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'llm.json'
        self._lock = threading.Lock()

    # ------------------------------------------------------------ storage
    def load(self):
        try:
            data = json.loads(self.path.read_text()) if self.path.exists() else {}
        except ValueError:
            data = {}
        data.setdefault('providers', [])
        data.setdefault('active', None)
        feats = data.get('features') if isinstance(data.get('features'), dict) else {}
        data['features'] = {k: bool(feats.get(k, False)) for k in FEATURES}
        b = data.get('budget') if isinstance(data.get('budget'), dict) else {}
        data['budget'] = {'max_input_tokens': self._budget(b.get('max_input_tokens'))}
        return data

    def save(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # never world-readable, even briefly
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=1)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    @staticmethod
    def _budget(v):
        try:
            return max(4000, min(MAX_BUDGET, int(v)))
        except (TypeError, ValueError):
            return MAX_BUDGET

    # ------------------------------------------------------------ providers
    def provider(self, pid=None, data=None):
        """A saved provider with its secret (server side only); pid None = the active one. None when missing."""
        data = data or self.load()
        pid = pid or data['active']
        return next((p for p in data['providers'] if p['id'] == pid), None)

    def active(self):
        """The active provider if it is connected (tested OK), else None: what every LLM route runs on."""
        p = self.provider()
        return p if p and p.get('verified_at') else None

    @staticmethod
    def public(p):
        out = {k: p.get(k) for k in ('id', *_FIELDS, 'verified_at', 'verify', 'last_error')}
        out['has_key'] = bool(p.get('api_key'))
        out['key_env'] = (p.get('api_key') or '').startswith('env:')
        out['api_key_masked'] = p.get('api_key') if out['key_env'] else _mask(p.get('api_key'))
        return out

    @staticmethod
    def merged(body, old=None):
        """A provider from a form body over the saved one: blank key = keep, normalised fields, defaults.
        Raises ValueError with a human message."""
        old = dict(old or {})
        out = dict(old)
        for k in (*_FIELDS, 'api_key'):
            if k not in body:
                continue
            v = body[k]
            if k == 'api_key' and not (v or '').strip():
                continue
            out[k] = v.strip() if isinstance(v, str) else v
        kind = out.get('kind') or 'openai_compat'
        if kind not in KINDS:
            raise ValueError(f'unknown provider kind {kind!r} ({", ".join(KINDS)})')
        out['kind'] = kind
        url = str(out.get('base_url') or '').strip().rstrip('/')
        if not url.startswith(('http://', 'https://')):
            raise ValueError('the base URL must start with http:// or https://')
        out['base_url'] = url
        if not str(out.get('model') or '').strip():
            raise ValueError('pick a model')
        out['name'] = str(out.get('name') or '').strip() or out['model']
        out['vision'] = bool(out.get('vision'))
        out['audio'] = bool(out.get('audio')) and kind != 'anthropic'       # Claude has no audio input
        if out.get('box_format') not in BOX_FORMATS:
            out['box_format'] = DEFAULT_BOX.get(kind, 'norm1000_xyxy')
        num = lambda k, d, lo, hi, cast=float: max(lo, min(hi, cast(out.get(k) if out.get(k) not in (None, '') else d)))
        try:
            out['timeout_s'] = num('timeout_s', 120, 5, 600)
            out['max_output_tokens'] = num('max_output_tokens', 2048, 64, 32768, int)
            out['temperature'] = num('temperature', 0.2, 0, 2)
        except (TypeError, ValueError):
            raise ValueError('timeout, max output tokens and temperature must be numbers') from None
        if any(out.get(k) != old.get(k) for k in _RETEST):
            out.pop('verified_at', None)
            out.pop('verify', None)
        out.setdefault('id', body.get('id') or uuid.uuid4().hex[:10])
        return out

    def upsert(self, body):
        with self._lock:
            data = self.load()
            old = self.provider(body.get('id'), data) if body.get('id') else None
            p = self.merged(body, old)
            data['providers'] = [x for x in data['providers'] if x['id'] != p['id']] + [p]
            if not data['active'] or not self.provider(data['active'], data):
                data['active'] = p['id']
            self.save(data)
            return p

    def delete(self, pid):
        with self._lock:
            data = self.load()
            data['providers'] = [p for p in data['providers'] if p['id'] != pid]
            if data['active'] == pid:
                data['active'] = data['providers'][0]['id'] if data['providers'] else None
            self.save(data)

    def record_test(self, pid, result):
        """Store a Test connection result on a saved provider: ok -> verified_at, else last_error."""
        with self._lock:
            data = self.load()
            p = self.provider(pid, data)
            if p is None:
                return
            if result.get('ok'):
                p['verified_at'] = time.time()
                p['verify'] = {k: result.get(k) for k in ('ms', 'text_ok', 'vision_ok', 'audio_ok')}
                p.pop('last_error', None)
            else:
                p.pop('verified_at', None)
                p['last_error'] = str(result.get('error') or 'test failed')[:300]
            self.save(data)

    def set_config(self, body):
        """features / budget / active from the Settings page (unknown keys ignored)."""
        with self._lock:
            data = self.load()
            if isinstance(body.get('features'), dict):
                for k, v in body['features'].items():
                    if k in FEATURES:
                        data['features'][k] = bool(v)
            if isinstance(body.get('budget'), dict) and 'max_input_tokens' in body['budget']:
                data['budget']['max_input_tokens'] = self._budget(body['budget']['max_input_tokens'])
            if body.get('active') and self.provider(body['active'], data):
                data['active'] = body['active']
            self.save(data)
            return data

    # ------------------------------------------------------------ what the page may show
    def state(self):
        """The page's view: connected?, the active provider (no secrets), effective feature switches
        (switch on AND connected AND the model has the capability), and the budget."""
        data = self.load()
        p = self.provider(data=data)
        connected = bool(p and p.get('verified_at'))
        caps = {None: True, 'vision': bool(p and p.get('vision')), 'audio': bool(p and p.get('audio'))}
        on = {k: connected and data['features'][k] and caps[need] for k, (_, need) in FEATURES.items()}
        for k in PANEL:             # these live in (or open) the ◆ panel: useless while the panel itself is off
            on[k] = on[k] and on['assistant']
        return {
            'connected': connected,
            'provider': {k: p.get(k) for k in ('id', 'name', 'kind', 'model', 'vision', 'audio')} if p else None,
            'features': on, 'switches': data['features'], 'budget': data['budget'],
        }

    def on(self, feature):
        return self.state()['features'].get(feature, False)

    def config(self):
        data = self.load()
        return {'providers': [self.public(p) for p in data['providers']], 'active': data['active'],
                'features': data['features'], 'budget': data['budget'],
                'catalog': {k: {'where': w, 'needs': n} for k, (w, n) in FEATURES.items()}}
