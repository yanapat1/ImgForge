"""
Server-wide switches admins change from the page (app.app_settings, one JSONB value per key).

    signup_enabled            True     anyone who reaches the server may create an account
    who_can_add_connections   everyone 'admins' keeps outbound S3 / FTP / DB / model / LLM servers to admins
    llm_web_max_level         3        the highest web search level anyone may use (0 off .. 3 full)
    llm_org_words             []       organisation special words [{text, strength never | private}]: never searched
    llm_chat_retention_days   730      deleted ◆ chats are purged after this many days (env DATAVIZ_LLM_CHAT_RETENTION_DAYS)
    llm_max_sessions          20       saved ◆ chats per user and assistant (the one not opened longest goes first)
    llm_steps_max             50       most steps (model calls) of one ◆ answer; effort high uses all of them
    llm_steps_medium          25       ... with a 15-40B model or a context window of 40k or less
    llm_steps_small           20       ... with a model of 14B or less
    llm_think_output          16384    output tokens an answer may get while it thinks (also the cut-off retry's
                                       ceiling); never more than the model's context leaves
    llm_agents_max            8        background agents running at once on the whole server

Reads are cached for a few seconds in-process (can_add_connections runs on every connection request);
update() clears the cache.
"""

import os
import threading
import time

from sqlalchemy import text

from .. import db

DEFAULTS = {'signup_enabled': True, 'who_can_add_connections': 'everyone', 'llm_web_max_level': 3, 'llm_org_words': [],
            'llm_chat_retention_days': int(os.environ.get('DATAVIZ_LLM_CHAT_RETENTION_DAYS') or 730),
            'llm_max_sessions': 20, 'llm_steps_max': 50, 'llm_steps_medium': 25, 'llm_steps_small': 20,
            'llm_think_output': 16384, 'llm_agents_max': 8}
# whole-number settings: (lowest, highest)
RANGES = {'llm_web_max_level': (0, 3), 'llm_chat_retention_days': (30, 3650), 'llm_max_sessions': (1, 200),
          'llm_steps_max': (5, 200), 'llm_steps_medium': (5, 200), 'llm_steps_small': (5, 200),
          'llm_think_output': (1024, 131072), 'llm_agents_max': (1, 64)}
CACHE_TTL = 5.0

_lock = threading.Lock()
_cache = {'at': 0.0, 'data': None}


def _validate(key, value):
    if key == 'signup_enabled':
        if not isinstance(value, bool):
            raise ValueError('signup_enabled must be true or false')
    elif key == 'who_can_add_connections':
        if value not in ('everyone', 'admins'):
            raise ValueError('who_can_add_connections must be everyone or admins')
    elif key in RANGES:
        lo, hi = RANGES[key]
        if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
            raise ValueError(f'{key} must be a whole number from {lo} to {hi}')
    elif key == 'llm_org_words':
        if not isinstance(value, list):
            raise ValueError('llm_org_words must be a list')
        out = []
        for w in value[:5000]:
            t = str((w or {}).get('text') if isinstance(w, dict) else w or '').strip()[:200]
            st = (w or {}).get('strength') if isinstance(w, dict) else None
            if t and not any(x['text'].lower() == t.lower() for x in out):
                out.append({'text': t, 'strength': st if st in ('never', 'private') else 'never'})
        value = out
    else:
        raise ValueError(f'unknown setting {key!r}')
    return value


def all_settings():
    """Every setting (defaults filled in)."""
    with _lock:
        if _cache['data'] is not None and time.monotonic() - _cache['at'] < CACHE_TTL:
            return dict(_cache['data'])
    with db.connect() as c:
        rows = c.execute(text('SELECT key, value FROM app.app_settings WHERE key = ANY(:k)'),
                         {'k': list(DEFAULTS)}).all()
    data = {**DEFAULTS, **{r.key: r.value for r in rows}}
    with _lock:
        _cache.update(at=time.monotonic(), data=data)
    return dict(data)


def get(key):
    return all_settings()[key]


def clear_cache():
    with _lock:
        _cache.update(at=0.0, data=None)


def update(partial):
    """Set the given keys (ValueError for unknown keys / bad values); returns every setting."""
    if not isinstance(partial, dict):
        raise ValueError('send the settings as an object')
    clean = {k: _validate(k, v) for k, v in partial.items()}
    with db.begin() as c:
        for k, v in clean.items():
            c.execute(text("""INSERT INTO app.app_settings(key, value) VALUES (:k, CAST(:v AS jsonb))
                              ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"""),
                      {'k': k, 'v': _json(v)})
    clear_cache()
    return all_settings()


def _json(v):
    import json
    return json.dumps(v)


def ensure_defaults():
    """Write the settings that have no row yet (start-up), so the database shows what is in force."""
    with db.begin() as c:
        for k, v in DEFAULTS.items():
            c.execute(text("""INSERT INTO app.app_settings(key, value) VALUES (:k, CAST(:v AS jsonb))
                              ON CONFLICT (key) DO NOTHING"""), {'k': k, 'v': _json(v)})
    clear_cache()
