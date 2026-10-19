"""
Server-wide switches admins change from the page (app.app_settings, one JSONB value per key).

    signup_enabled            True     anyone who reaches the server may create an account
    who_can_add_connections   everyone 'admins' keeps outbound S3 / FTP / DB / model / LLM servers to admins

Reads are cached for a few seconds in-process (can_add_connections runs on every connection request);
update() clears the cache.
"""

import threading
import time

from sqlalchemy import text

from .. import db

DEFAULTS = {'signup_enabled': True, 'who_can_add_connections': 'everyone'}
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
