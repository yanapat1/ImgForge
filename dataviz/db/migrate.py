"""
Bring the database to the newest schema at start-up: `alembic upgrade head`, run in-process (no alembic.ini)
under a PostgreSQL advisory lock, so two servers starting together never migrate at the same time.

New migration: add a file to db/migrations/versions (copy 0001's header, set down_revision to the previous
one), change db/schema.py to match, and run the suite (tests/test_db_schema.py compares the two).
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

from .engine import engine as _engine

MIGRATIONS = Path(__file__).with_name('migrations')
_LOCK_KEY = 0x64617461766a7a      # any constant: the advisory lock id of "dataviz migrations"


def _config(connection=None):
    cfg = Config()
    cfg.set_main_option('script_location', str(MIGRATIONS))
    if connection is not None:
        cfg.attributes['connection'] = connection
    return cfg


def head():
    return ScriptDirectory.from_config(_config()).get_current_head()


def current(conn):
    return MigrationContext.configure(conn, opts={'version_table_schema': 'public'}).get_current_revision()


def upgrade(log=None):
    """Migrate to head; returns (before, after) revisions."""
    with _engine().connect() as conn:
        conn.execute(text('SELECT pg_advisory_lock(:k)'), {'k': _LOCK_KEY})
        try:
            before = current(conn)
            conn.commit()
            if before != head():
                if log:
                    log(f'database schema {before or "empty"} → {head()}')
                command.upgrade(_config(conn), 'head')
                conn.commit()
            return before, current(conn)
        finally:
            conn.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': _LOCK_KEY})
            conn.commit()
