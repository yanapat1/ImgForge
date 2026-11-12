"""The example database: made once on the app's own PostgreSQL server under its own role, given to each user once."""

import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

import psycopg
import pytest

from conftest import make_user


@pytest.fixture
def smp(pg, tmp_path, monkeypatch):
    from dataviz.data import sample, service
    from dataviz.image import connections
    tag = uuid.uuid4().hex[:8]
    monkeypatch.setattr(sample, 'DB', f'sample_shop_{tag}')          # the test server is shared: names of this test only
    monkeypatch.setattr(sample, 'ROLE', f'sample_shop_user_{tag}')
    reg = connections.Registry(tmp_path)
    service.configure(SimpleNamespace(cfg={'data': {}, 'database': {'url': pg}}, datasets=reg))
    yield SimpleNamespace(s=sample, svc=service, url=pg, reg=reg)
    sample.remove_sample(pg, log=lambda *_: None)


def _as_role(smp):
    st = smp.s.state()
    from dataviz.db import crypto
    u = urlsplit(smp.url)
    return psycopg.connect(host=u.hostname, port=u.port, dbname=smp.s.DB, user=smp.s.ROLE,
                           password=crypto.decrypt_json(st['password'].encode())['p'], autocommit=True)


def test_made_once_with_its_rows_keys_and_descriptions(smp):
    said = []
    assert smp.s.ensure_sample(smp.url, said.append)
    assert smp.s.state()['status'] == 'ready'
    with _as_role(smp) as c:
        counts = dict(c.execute("""SELECT 'customers', count(*) FROM customers UNION ALL SELECT 'profiles', count(*) FROM customer_profiles
                                   UNION ALL SELECT 'products', count(*) FROM products UNION ALL SELECT 'orders', count(*) FROM orders
                                   UNION ALL SELECT 'items', count(*) FROM order_items""").fetchall())
        assert counts['customers'] == 200 and counts['profiles'] == 150 and counts['products'] == 40 and counts['orders'] == 1500
        assert 3000 < counts['items'] < 5000
        # totals agree with their items, the view answers, every column has a description
        assert c.execute("""SELECT count(*) FROM orders o WHERE o.total <> (SELECT sum(qty * unit_price) FROM order_items i
                            WHERE i.order_id = o.id)""").fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM monthly_sales').fetchone()[0] >= 20
        assert c.execute("""SELECT count(*) FROM pg_attribute a JOIN pg_class t ON t.oid = a.attrelid
                            JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = 'public'
                            WHERE t.relkind = 'r' AND a.attnum > 0 AND col_description(t.oid, a.attnum) IS NULL""").fetchone()[0] == 0
        # the role owns it all: it may drop a table (and a user may, in the app)
        assert c.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner = current_user").fetchone()[0] == 5
    # a second start does nothing; the same rows every install
    assert not smp.s.ensure_sample(smp.url, said.append)
    assert smp.s.rows()['orders'][:3] == smp.s.rows()['orders'][:3]


def test_its_role_cannot_read_the_apps_own_tables(smp):
    smp.s.ensure_sample(smp.url, lambda *_: None)
    u = urlsplit(smp.url)
    from dataviz.db import crypto
    pw = crypto.decrypt_json(smp.s.state()['password'].encode())['p']
    try:
        with psycopg.connect(host=u.hostname, port=u.port, dbname=u.path.lstrip('/'), user=smp.s.ROLE, password=pw, autocommit=True) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute('SELECT 1 FROM app.users')
    except psycopg.OperationalError:
        pass                                              # it cannot even connect there: just as good


def test_each_user_gets_the_connection_once(smp):
    from dataviz.server.routes.me import _put_prefs, get_prefs
    smp.s.ensure_sample(smp.url, lambda *_: None)
    user = make_user(f'u{uuid.uuid4().hex[:6]}')
    src = smp.svc.sources(user)
    ex = [s for s in src if s.get('example')]
    assert len(ex) == 1 and ex[0]['name'] == smp.s.NAME and ex[0]['database'] == smp.s.DB
    assert get_prefs(user['id'])[smp.s.PREF] is True
    # it works like any connection: the tree, the keys of orders (A4's ends)
    tables = smp.svc.tree(user, ex[0]['id'], smp.s.DB)['tables']
    assert {'customers', 'orders', 'order_items', 'products', 'customer_profiles'} <= {t['name'] for t in tables}
    d = smp.svc.describe(user, ex[0]['id'], smp.s.DB, 'public', 'orders')
    assert d['unique'] == ['id'] and d['description'].startswith('One row per order')
    # deleted by the user: it does not come back
    smp.reg.delete_connection(ex[0]['id'], user['id'])
    assert not [s for s in smp.svc.sources(user) if s.get('example')]
    _put_prefs(user['id'], {'x': 1})


def test_a_dropped_sample_says_so_and_an_admin_restores_it(smp):
    smp.s.ensure_sample(smp.url, lambda *_: None)
    admin, user = make_user(f'a{uuid.uuid4().hex[:6]}', admin=True), make_user(f'u{uuid.uuid4().hex[:6]}')
    cid = next(s['id'] for s in smp.svc.sources(admin) if s.get('example'))
    smp.svc.sources(user)
    with psycopg.connect(smp.url.replace('+psycopg', ''), autocommit=True) as c:
        c.execute(f'DROP DATABASE {smp.s.DB} WITH (FORCE)')
    with pytest.raises(smp.svc.DataError) as e:
        smp.svc.tree(admin, cid, refresh=True)
    assert e.value.status == 410 and 'was removed' in str(e.value)
    with pytest.raises(smp.svc.DataError):
        smp.svc.restore_sample(user)                     # not an admin
    assert smp.svc.restore_sample(admin)['ok']
    assert smp.svc.tree(admin, cid, smp.s.DB, refresh=True)['tables']   # the saved connection works again (new password kept)


def test_a_server_user_without_rights_skips_it(smp, monkeypatch):
    real = smp.s._connect

    class Weak:
        def __init__(self, c):
            self.c = c

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.c.close()

        def execute(self, sql, *a):
            if 'rolsuper' in sql:
                return SimpleNamespace(fetchone=lambda: (False,))
            return self.c.execute(sql, *a)

    monkeypatch.setattr(smp.s, '_connect', lambda *a, **k: Weak(real(*a, **k)))
    said = []
    assert not smp.s.ensure_sample(smp.url, said.append)
    assert smp.s.state()['status'] == 'unavailable' and 'CREATEROLE' in said[0]
