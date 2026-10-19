"""
The one PostgreSQL engine of the process (SQLAlchemy Core over psycopg 3, pooled).

    db.init(url)                 at start-up (cli.py) or per test (tests/conftest.py)
    with db.begin() as c: ...    one transaction (commit on exit, rollback on error)
    with db.connect() as c: ...  autocommit-free read connection (rolled back on exit)

Every repository / store reads the engine through engine(), so a test can point the whole app at a fresh
database by calling init() again.
"""

import threading
from contextlib import contextmanager

import orjson
from sqlalchemy import create_engine, event

_lock = threading.Lock()
_engine = None


def _json_dumps(v):
    return orjson.dumps(v, option=orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_NON_STR_KEYS).decode()


def normalize_url(url):
    """postgres:// and postgresql:// mean the psycopg 3 driver (SQLAlchemy would pick psycopg2)."""
    url = str(url or '').strip()
    for old in ('postgres://', 'postgresql://'):
        if url.startswith(old):
            return 'postgresql+psycopg://' + url[len(old):]
    return url


def init(url, pool_size=10, echo=False):
    """(Re)create the engine for `url`; the previous one is disposed."""
    global _engine
    url = normalize_url(url)
    if not url.startswith('postgresql+psycopg://'):
        raise ValueError('database.url must be a PostgreSQL URL (postgresql://user:password@host:5432/db)')
    eng = create_engine(url, pool_size=int(pool_size), max_overflow=int(pool_size), pool_pre_ping=True,
                        pool_recycle=1800, json_serializer=_json_dumps, json_deserializer=orjson.loads,
                        connect_args={'connect_timeout': 10, 'application_name': 'dataviz'}, echo=echo)

    @event.listens_for(eng, 'connect')
    def _session_defaults(dbapi_conn, _rec):          # every pooled connection: UTC, and a guard on runaway queries
        with dbapi_conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute("SET statement_timeout = '600s'")
        dbapi_conn.commit()

    with _lock:
        old, _engine = _engine, eng
    if old is not None:
        old.dispose()
    return eng


def engine():
    if _engine is None:
        raise RuntimeError('the database is not initialised (db.init(url) was not called)')
    return _engine


def dispose():
    global _engine
    with _lock:
        old, _engine = _engine, None
    if old is not None:
        old.dispose()


@contextmanager
def begin():
    """One transaction: commit when the block ends, rollback when it raises."""
    with engine().begin() as c:
        yield c


@contextmanager
def connect():
    """A connection for reads; anything left open is rolled back when the block ends."""
    with engine().connect() as c:
        yield c
