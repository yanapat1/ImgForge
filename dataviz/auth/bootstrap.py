"""
Start-up: make sure the server has an active admin and its settings rows.

ensure_first_admin() creates the admin from DATAVIZ_ADMIN_USERNAME / _EMAIL / _PASSWORD when no active admin
exists (or promotes an existing account with that username). With no password given it generates one and
returns it, so the CLI banner can print it once; it is not stored anywhere in clear.
"""

import os
import secrets

from sqlalchemy import text

from .. import db
from . import audit, settings, users


def ensure_first_admin(env=None, log=print):
    """-> (user dict, generated password or None); (None, None) when an active admin already exists."""
    env = os.environ if env is None else env
    with db.begin() as c:
        c.execute(text('LOCK TABLE app.users IN SHARE ROW EXCLUSIVE MODE'))   # two workers starting together
        if users.any_admin(c):
            return None, None
        username = (env.get('DATAVIZ_ADMIN_USERNAME') or 'admin').strip().lower()
        email = (env.get('DATAVIZ_ADMIN_EMAIL') or 'admin@localhost').strip()
        password = env.get('DATAVIZ_ADMIN_PASSWORD') or ''
        existing = c.execute(text('SELECT id FROM app.users WHERE lower(username) = :u'), {'u': username}).first()
        if existing is not None:
            u = users.update(existing.id, is_admin=True, is_active=True, conn=c)
            log(f'promoted the existing user {username!r} to admin (no active admin was left)')
            action, generated = 'admin_promote', None
        else:
            generated = None
            if not password:
                password = generated = secrets.token_urlsafe(12)
            u = users.create(username, email, 'Administrator', password, is_admin=True, conn=c)
            log(f'created the first admin account {username!r}')
            action = 'admin_bootstrap'
    audit.log(action, target=u['id'], meta={'username': u['username']})
    return u, generated


def ensure_defaults():
    settings.ensure_defaults()
