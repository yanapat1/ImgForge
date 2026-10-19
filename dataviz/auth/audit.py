"""
The audit log (app.audit_log): who signed in, failed to, created / changed / deleted accounts, changed sharing
or server settings. log() never raises: an audit failure must not break the action it records.
"""

from sqlalchemy import text

from .. import db
from ..core import logging as vlog

_log = vlog.get_logger('audit')


def log(action, target=None, meta=None, user_id=None, ip=None):
    try:
        with db.begin() as c:
            c.execute(text("""INSERT INTO app.audit_log(user_id, action, target, meta, ip)
                              VALUES (:u, :a, :t, CAST(:m AS jsonb), :ip)"""),
                      {'u': user_id, 'a': str(action)[:60], 't': None if target is None else str(target)[:200],
                       'm': None if meta is None else _json(meta), 'ip': (ip or '')[:64] or None})
    except Exception as e:      # noqa: BLE001 - recording must never fail the request
        _log.warning(f'audit log write failed ({action}): {vlog.esc(str(e))}')


def _json(v):
    import json
    return json.dumps(v, default=str)


def list_events(limit=200):
    """The newest `limit` events: [{at, user: {id, username} | None, action, target, meta, ip}]."""
    limit = max(1, min(int(limit), 1000))
    with db.connect() as c:
        rows = c.execute(text("""
            SELECT a.at, a.user_id, u.username, a.action, a.target, a.meta, a.ip
            FROM app.audit_log a LEFT JOIN app.users u ON u.id = a.user_id
            ORDER BY a.at DESC, a.id DESC LIMIT :n"""), {'n': limit}).all()
    return [{'at': r.at.timestamp(), 'user': {'id': r.user_id, 'username': r.username} if r.user_id else None,
             'action': r.action, 'target': r.target, 'meta': r.meta, 'ip': r.ip} for r in rows]


list = list_events          # noqa: A001 - the name the plan uses (audit.list(limit))
