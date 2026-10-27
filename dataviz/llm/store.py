"""
The saved LLM providers, the feature switches (all off by default) and the token budget, per user, in
PostgreSQL (the format db/legacy.py imports llm.json into):

    app.llm_providers    one row per provider, private to its owner: config = the provider minus id / name /
                         kind / api_key (base_url, model, vision, ..., verified_at, verify, last_error),
                         secrets = Fernet-encrypted {api_key}
    app.llm_user_config  per user: active_provider_id, features {key: bool}, budget {max_input_tokens}

Every method acts for one user (a user dict; None = the signed-in user, context.user()). API keys are
write-only: the page only ever sees `has_key`. `api_key: env:NAME` (read the server's environment variable at
call time) is admin-only, both when saved and when used: a non-admin's provider never resolves an env: key, so
nobody can make the server send its environment secrets to a URL they control.

A provider is "connected" when it is the active one and its last Test connection passed (`verified_at`); editing its
kind / URL / model / key clears that, so a changed provider has to be tested again before any LLM UI shows.
"""

import os
import time
import uuid

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from .. import db

KINDS = ('openai', 'openai_compat', 'anthropic', 'gemini')
BOX_FORMATS = ('norm1000_yxyx', 'norm1000_xyxy', 'pixel_xyxy')
# key -> (where it shows: 'viz' | 'label' | 'dc' (the Data Center page) | 'any', capability it needs: None | 'vision' | 'audio')
FEATURES = {
    'assistant': ('viz', None), 'dataset_qa': ('viz', None), 'explain_eval': ('viz', None),
    'ask_selection': ('viz', 'vision'), 'label_check': ('viz', 'vision'), 'report': ('viz', None),
    'nl_filter': ('viz', None), 'auto_tag': ('viz', 'vision'),
    'prelabel': ('label', 'vision'), 'label_assist': ('label', 'vision'), 'voice': ('any', 'audio'),
    'data_center': ('dc', None),        # the Data Center agent (llm/agent.py): its own panel, not the ◆ one
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


ENV_ADMIN_ONLY = 'only admins can use an env: API key (a server environment variable)'
_J = {k: bindparam(k, type_=JSONB) for k in ('cfg', 'f', 'b')}


def _user(user):
    if user is None:
        from ..auth import context
        user = context.user()
    return user


def _env(p):
    return (p.get('api_key') or '').strip().startswith('env:')


def _prov(r):
    from ..db import crypto
    p = dict(r.config or {})
    if r.secrets:
        p.update({k: v for k, v in (crypto.decrypt_json(r.secrets) or {}).items() if k == 'api_key'})
    p.update(id=r.id, name=r.name, kind=r.kind)
    return p


class LLMStore:
    def __init__(self, root=None):
        self.root = root                     # unused (kept for old callers): everything is in the database

    @staticmethod
    def _budget(v):
        try:
            return max(4000, min(MAX_BUDGET, int(v)))
        except (TypeError, ValueError):
            return MAX_BUDGET

    # ------------------------------------------------------------ storage
    def load(self, user=None):
        """{providers, active, features, budget} of one user (no user: nothing saved)."""
        user = _user(user)
        data = {'providers': [], 'active': None, 'features': {}, 'budget': {}}
        if user and user.get('id'):
            with db.connect() as c:
                rows = c.execute(text('SELECT * FROM app.llm_providers WHERE owner_id = :o ORDER BY created_at, name'),
                                 {'o': user['id']}).all()
                cfg = c.execute(text('SELECT * FROM app.llm_user_config WHERE user_id = :u'), {'u': user['id']}).first()
            data['providers'] = [_prov(r) for r in rows]
            if not user.get('is_admin'):             # never resolved for a non-admin (demoted since it was saved)
                for p in data['providers']:
                    if _env(p):
                        p['api_key'] = ''
            if cfg is not None:
                data.update(active=cfg.active_provider_id, features=dict(cfg.features or {}), budget=dict(cfg.budget or {}))
        feats = data['features'] if isinstance(data['features'], dict) else {}
        data['features'] = {k: bool(feats.get(k, False)) for k in FEATURES}
        b = data['budget'] if isinstance(data['budget'], dict) else {}
        data['budget'] = {'max_input_tokens': self._budget(b.get('max_input_tokens'))}
        if data['active'] and not any(p['id'] == data['active'] for p in data['providers']):
            data['active'] = None
        return data

    def _save_config(self, c, uid, data):
        c.execute(text("""INSERT INTO app.llm_user_config(user_id, active_provider_id, features, budget)
                          VALUES (:u, :a, :f, :b) ON CONFLICT (user_id) DO UPDATE
                          SET active_provider_id = EXCLUDED.active_provider_id, features = EXCLUDED.features,
                              budget = EXCLUDED.budget""").bindparams(_J['f'], _J['b']),
                  {'u': uid, 'a': data['active'], 'f': data['features'], 'b': data['budget']})

    def _save_provider(self, c, uid, p, new):
        from ..db import crypto
        config = {k: v for k, v in p.items() if k not in ('id', 'name', 'kind', 'api_key')}
        args = {'id': p['id'], 'o': uid, 'n': str(p['name'])[:200], 'k': p['kind'], 'cfg': config,
                's': crypto.encrypt_json({'api_key': p['api_key']} if p.get('api_key') else None)}
        if new:
            c.execute(text("""INSERT INTO app.llm_providers(id, owner_id, name, kind, config, secrets)
                              VALUES (:id, :o, :n, :k, :cfg, :s)""").bindparams(_J['cfg']), args)
        else:
            c.execute(text("""UPDATE app.llm_providers SET name = :n, kind = :k, config = :cfg, secrets = :s,
                              updated_at = now() WHERE id = :id AND owner_id = :o""").bindparams(_J['cfg']), args)

    # ------------------------------------------------------------ providers
    def provider(self, pid=None, user=None, data=None):
        """One of the user's providers with its secret (server side only); pid None = the active one. None when
        missing (or someone else's)."""
        data = data or self.load(user)
        pid = pid or data['active']
        return next((p for p in data['providers'] if p['id'] == pid), None)

    def active(self, user=None):
        """The user's active provider if it is connected (tested OK), else None: what every LLM route runs on."""
        p = self.provider(user=user)
        return p if p and p.get('verified_at') else None

    @staticmethod
    def public(p):
        out = {k: p.get(k) for k in ('id', *_FIELDS, 'verified_at', 'verify', 'last_error')}
        out['has_key'] = bool(p.get('api_key'))
        out['key_env'] = _env(p)
        out['api_key_masked'] = p.get('api_key') if out['key_env'] else _mask(p.get('api_key'))
        return out

    @staticmethod
    def merged(body, old=None, admin=False):
        """A provider from a form body over the saved one: blank key = keep, normalised fields, defaults.
        Raises ValueError with a human message (an env: key unless admin)."""
        old = dict(old or {})
        out = dict(old)
        for k in (*_FIELDS, 'api_key'):
            if k not in body:
                continue
            v = body[k]
            if k == 'api_key' and not (v or '').strip():
                continue
            out[k] = v.strip() if isinstance(v, str) else v
        if _env(out) and not admin:
            raise ValueError(ENV_ADMIN_ONLY)
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

    def _need(self, user):
        user = _user(user)
        if not user or not user.get('id'):
            raise ValueError('sign in first')
        return user

    def upsert(self, body, user=None):
        """Save one of the user's providers (KeyError for an id that is not theirs); the first one becomes active."""
        user = self._need(user)
        data = self.load(user)
        old = None
        if body.get('id'):
            old = self.provider(body['id'], data=data)
            if old is None:
                raise KeyError(f'no provider {body["id"]!r}')
        p = self.merged(body, old, admin=bool(user.get('is_admin')))
        with db.begin() as c:
            self._save_provider(c, user['id'], p, new=old is None)
            if not data['active']:
                data['active'] = p['id']
            self._save_config(c, user['id'], data)
        return p

    def delete(self, pid, user=None):
        user = self._need(user)
        data = self.load(user)
        with db.begin() as c:
            n = c.execute(text('DELETE FROM app.llm_providers WHERE id = :id AND owner_id = :o'),
                          {'id': pid, 'o': user['id']}).rowcount
            if not n:
                raise KeyError(f'no provider {pid!r}')
            rest = [p for p in data['providers'] if p['id'] != pid]
            if data['active'] in (pid, None):
                data['active'] = rest[0]['id'] if rest else None
            self._save_config(c, user['id'], data)

    def record_test(self, pid, result, user=None):
        """Store a Test connection result on one of the user's providers: ok -> verified_at, else last_error."""
        user = self._need(user)
        p = self.provider(pid, user)
        if p is None:
            return
        if result.get('ok'):
            p['verified_at'] = time.time()
            p['verify'] = {k: result.get(k) for k in ('ms', 'text_ok', 'vision_ok', 'audio_ok')}
            p.pop('last_error', None)
        else:
            p.pop('verified_at', None)
            p['last_error'] = str(result.get('error') or 'test failed')[:300]
        with db.begin() as c:
            self._save_provider(c, user['id'], p, new=False)

    def set_config(self, body, user=None):
        """features / budget / active from the Settings page (unknown keys ignored)."""
        user = self._need(user)
        data = self.load(user)
        if isinstance(body.get('features'), dict):
            for k, v in body['features'].items():
                if k in FEATURES:
                    data['features'][k] = bool(v)
        if isinstance(body.get('budget'), dict) and 'max_input_tokens' in body['budget']:
            data['budget']['max_input_tokens'] = self._budget(body['budget']['max_input_tokens'])
        if body.get('active') and self.provider(body['active'], data=data):
            data['active'] = body['active']
        with db.begin() as c:
            self._save_config(c, user['id'], data)
        return data

    # ------------------------------------------------------------ what the page may show
    def state(self, user=None):
        """The page's view: connected?, the active provider (no secrets), effective feature switches
        (switch on AND connected AND the model has the capability), and the budget."""
        data = self.load(user)
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

    def on(self, feature, user=None):
        return self.state(user)['features'].get(feature, False)

    def config(self, user=None):
        data = self.load(user)
        return {'providers': [self.public(p) for p in data['providers']], 'active': data['active'],
                'features': data['features'], 'budget': data['budget'],
                'catalog': {k: {'where': w, 'needs': n} for k, (w, n) in FEATURES.items()}}
