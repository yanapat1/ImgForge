"""
The sample database: on the first start of an install with the Data Analysis zone, the app's own PostgreSQL server gets
a small example database, so a new user has something to query, join and draw before connecting their own.

    database  sample_shop        owned by the role sample_shop_user, so a user may change or drop any of it
    tables    customers (200) · customer_profiles (150, 1 : 0..1) · products (40) · orders (1 500, N : 1)
              · order_items (~4 000, the N : M bridge of orders and products) · view monthly_sales
    setting   app.app_settings['analysis_sample'] = {status: ready | removed | unavailable, host, port, password (encrypted)}

    ensure_sample(url, log)   create it once (a no-op when the setting says it was made, removed or cannot be made)
    ensure_connection(user)   give a user the "Sample shop (example)" connection once (deleting it never brings it back)
    is_sample(conn)           whether a saved connection is the sample one
    remove_sample(log) / reset_sample(log)   for `python -m dataviz analysis-sample remove|reset` and an admin's Restore

The role owns only its database: it gets CONNECT there and nothing on the app's own tables, so the sample's password
cannot read the app's data. The server's user needs CREATEROLE + CREATEDB (a superuser in the shipped docker compose);
without them the sample is skipped and the setting says why.
"""

import random
import secrets
import threading
from datetime import UTC, date, datetime, timedelta
from urllib.parse import unquote, urlsplit

from sqlalchemy import text

from .. import db

DB, ROLE, KEY = 'sample_shop', 'sample_shop_user', 'analysis_sample'
NAME = 'Sample shop (example)'
PREF = 'analysis_sample_added'
TIMEOUT_MS = 60_000
_lock = threading.Lock()

# ---------------------------------------------------------------- the data (fixed seed: every install gets the same rows)
FIRST = ['Ava', 'Ben', 'Chloe', 'Daniel', 'Emma', 'Felix', 'Grace', 'Hugo', 'Isla', 'Jack', 'Kai', 'Lena', 'Mason', 'Nina',
         'Oscar', 'Pim', 'Quinn', 'Ravi', 'Sara', 'Tom', 'Uma', 'Victor', 'Wen', 'Yuki', 'Zoe']
LAST = ['Anders', 'Brooks', 'Chen', 'Diaz', 'Evans', 'Fischer', 'Garcia', 'Huang', 'Ito', 'Jensen', 'Khan', 'Lopez',
        'Moreau', 'Nakamura', 'Okafor', 'Patel', 'Rossi', 'Sato', 'Tanaka', 'Wong']
CITIES = [('Bangkok', 'Thailand'), ('Chiang Mai', 'Thailand'), ('Singapore', 'Singapore'), ('Tokyo', 'Japan'),
          ('Osaka', 'Japan'), ('Berlin', 'Germany'), ('Paris', 'France'), ('London', 'United Kingdom'),
          ('New York', 'United States'), ('Austin', 'United States'), ('Sydney', 'Australia'), ('Toronto', 'Canada')]
PRODUCTS = {'Coffee': ['House blend 250 g', 'Single origin 250 g', 'Espresso beans 1 kg', 'Decaf 250 g', 'Cold brew pack'],
            'Tea': ['Green tea 100 g', 'Jasmine tea 100 g', 'Earl grey 100 g', 'Chai mix 200 g', 'Herbal sampler'],
            'Equipment': ['Pour-over dripper', 'Paper filters (100)', 'Gooseneck kettle', 'Hand grinder', 'French press',
                          'Milk frother', 'Digital scale', 'Moka pot', 'Espresso tamper', 'Cleaning tablets'],
            'Mugs': ['Ceramic mug', 'Travel mug', 'Glass cup set', 'Espresso cups (2)', 'Double-wall glass'],
            'Snacks': ['Almond biscotti', 'Dark chocolate bar', 'Granola bites', 'Butter cookies', 'Dried mango'],
            'Gifts': ['Gift card 500', 'Gift card 1000', 'Tasting box', 'Barista course voucher', 'Subscription 3 months',
                      'Subscription 6 months', 'Tote bag', 'Apron', 'Recipe book', 'Sticker pack']}
STATUS = ['delivered'] * 14 + ['shipped'] * 3 + ['paid'] * 2 + ['cancelled', 'refunded']
TIERS = ['bronze'] * 5 + ['silver'] * 3 + ['gold'] * 2 + ['platinum']


def rows(seed=2026):
    """-> {table: list of tuples}, the same every time."""
    r = random.Random(seed)
    customers = []
    for i in range(1, 201):
        f, la = r.choice(FIRST), r.choice(LAST)
        city, country = r.choice(CITIES)
        customers.append((i, f'{f} {la}', f'{f.lower()}.{la.lower()}{i}@example.com', city, country,
                          date(2024, 1, 1) + timedelta(days=r.randrange(0, 640))))
    profiles = [(c, r.randrange(1955, 2006), r.choice(TIERS), r.random() < 0.6) for c in sorted(r.sample(range(1, 201), 150))]
    products, pid = [], 0
    for cat, names in PRODUCTS.items():
        for n in names:
            pid += 1
            base = {'Coffee': 380, 'Tea': 220, 'Equipment': 900, 'Mugs': 320, 'Snacks': 120, 'Gifts': 700}[cat]
            products.append((pid, f'SKU-{cat[:3].upper()}-{pid:03d}', n, cat, round(base * r.uniform(0.5, 1.9), 2)))
    price = {p[0]: p[4] for p in products}
    weights = [r.paretovariate(1.4) for _ in customers]           # a few customers buy a lot, as in a real shop
    start, span = datetime(2025, 1, 1, tzinfo=UTC), (datetime(2026, 10, 31, tzinfo=UTC) - datetime(2025, 1, 1, tzinfo=UTC)).total_seconds()
    orders, items = [], []
    for oid in range(1, 1501):
        cid = r.choices(range(1, 201), weights)[0]
        when = start + timedelta(seconds=int(r.random() ** 0.8 * span))     # more orders lately
        chosen = r.sample(range(1, len(products) + 1), r.choice([1, 1, 2, 2, 3, 3, 4, 5]))
        total = 0.0
        for p in chosen:
            qty = r.choice([1, 1, 1, 2, 2, 3])
            unit = round(price[p] * r.choice([1, 1, 1, 0.9, 0.85]), 2)
            items.append((oid, p, qty, unit))
            total += qty * unit
        orders.append((oid, cid, when, r.choice(STATUS), round(total, 2)))
    return {'customers': customers, 'customer_profiles': profiles, 'products': products, 'orders': orders, 'order_items': items}


DDL = """
CREATE TABLE customers (
  id integer PRIMARY KEY, name text NOT NULL, email text NOT NULL UNIQUE, city text, country text, signed_up date NOT NULL);
CREATE TABLE customer_profiles (
  customer_id integer PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
  birth_year integer, loyalty_tier text NOT NULL, marketing_opt_in boolean NOT NULL DEFAULT false);
CREATE TABLE products (
  id integer PRIMARY KEY, sku text NOT NULL UNIQUE, name text NOT NULL, category text NOT NULL, price numeric(10,2) NOT NULL);
CREATE TABLE orders (
  id integer PRIMARY KEY, customer_id integer NOT NULL REFERENCES customers(id), ordered_at timestamptz NOT NULL,
  status text NOT NULL, total numeric(12,2) NOT NULL);
CREATE TABLE order_items (
  order_id integer NOT NULL REFERENCES orders(id) ON DELETE CASCADE, product_id integer NOT NULL REFERENCES products(id),
  qty integer NOT NULL CHECK (qty > 0), unit_price numeric(10,2) NOT NULL, PRIMARY KEY (order_id, product_id));
CREATE INDEX orders_customer ON orders(customer_id);
CREATE INDEX orders_when ON orders(ordered_at);
CREATE INDEX order_items_product ON order_items(product_id);
CREATE VIEW monthly_sales AS
  SELECT date_trunc('month', o.ordered_at)::date AS month, count(DISTINCT o.id) AS orders, sum(i.qty * i.unit_price) AS revenue
  FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.status NOT IN ('cancelled', 'refunded') GROUP BY 1;
"""

COMMENTS = {
    'customers': ('People who bought from the shop. One row per customer.', {
        'id': 'Customer number (primary key)', 'name': 'Full name', 'email': 'Email address, unique per customer',
        'city': 'City of the delivery address', 'country': 'Country of the delivery address', 'signed_up': 'Day the account was made'}),
    'customer_profiles': ('Optional extra details: at most one per customer (1 : 0..1 with customers).', {
        'customer_id': 'The customer (primary key and foreign key: one profile per customer)', 'birth_year': 'Year of birth',
        'loyalty_tier': 'bronze, silver, gold or platinum', 'marketing_opt_in': 'Agreed to marketing email'}),
    'products': ('What the shop sells.', {
        'id': 'Product number (primary key)', 'sku': 'Stock keeping unit, unique', 'name': 'Product name',
        'category': 'Coffee, Tea, Equipment, Mugs, Snacks or Gifts', 'price': 'List price'}),
    'orders': ('One row per order. Many orders belong to one customer (N : 1).', {
        'id': 'Order number (primary key)', 'customer_id': 'Who ordered (foreign key to customers)',
        'ordered_at': 'When the order was placed', 'status': 'paid, shipped, delivered, cancelled or refunded',
        'total': 'Order value: sum of qty × unit_price of its items'}),
    'order_items': ('The products in each order: the bridge that makes orders and products many-to-many (N : M).', {
        'order_id': 'The order (foreign key)', 'product_id': 'The product (foreign key)', 'qty': 'How many',
        'unit_price': 'Price paid for one, after any discount'}),
    'monthly_sales': ('Orders and revenue per month, without cancelled and refunded orders.', {}),
}

COLUMNS = {'customers': ['id', 'name', 'email', 'city', 'country', 'signed_up'],
           'customer_profiles': ['customer_id', 'birth_year', 'loyalty_tier', 'marketing_opt_in'],
           'products': ['id', 'sku', 'name', 'category', 'price'],
           'orders': ['id', 'customer_id', 'ordered_at', 'status', 'total'],
           'order_items': ['order_id', 'product_id', 'qty', 'unit_price']}


# ---------------------------------------------------------------- the setting
def state():
    with db.connect() as c:
        return c.execute(text('SELECT value FROM app.app_settings WHERE key = :k'), {'k': KEY}).scalar() or {}


def _store(value):
    import json
    with db.begin() as c:
        c.execute(text("""INSERT INTO app.app_settings(key, value) VALUES (:k, CAST(:v AS jsonb))
                          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"""), {'k': KEY, 'v': json.dumps(value)})


def _parts(url):
    u = urlsplit(db.normalize_url(url).replace('postgresql+psycopg://', 'postgresql://'))
    from urllib.parse import parse_qs
    q = parse_qs(u.query)
    return {'host': u.hostname or 'localhost', 'port': u.port or 5432, 'user': unquote(u.username or ''),
            'password': unquote(u.password or ''), 'sslmode': (q.get('sslmode') or ['prefer'])[0]}


def _connect(p, dbname, user=None, password=None):
    import psycopg
    return psycopg.connect(host=p['host'], port=p['port'], dbname=dbname, user=user or p['user'] or None,
                           password=(password if user else p['password']) or None, sslmode=p['sslmode'], autocommit=True,
                           connect_timeout=10, application_name='dataviz-sample', options=f'-c statement_timeout={TIMEOUT_MS}')


def _q(name):
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------- create / remove
def _create(url, log, reuse=True):
    from ..db import crypto
    p = _parts(url)
    with _connect(p, 'postgres') as c:
        can = c.execute('SELECT rolsuper OR (rolcreaterole AND rolcreatedb) FROM pg_roles WHERE rolname = current_user').fetchone()
        if not (can and can[0]):
            why = 'the database user cannot create roles and databases (needs CREATEROLE and CREATEDB)'
            _store({'status': 'unavailable', 'reason': why})
            log(f'sample database skipped: {why}')
            return False
        old = state().get('password')             # a restore keeps the password: users' saved connections keep working
        password = (crypto.decrypt_json(old.encode()) or {}).get('p') if old else None
        if not password or not all(ch.isalnum() or ch in '-_' for ch in password):
            password = secrets.token_urlsafe(24)
        role_there = c.execute('SELECT 1 FROM pg_roles WHERE rolname = %s', [ROLE]).fetchone()
        # a password is a literal in CREATE / ALTER ROLE: token_urlsafe only has [A-Za-z0-9_-], so quoting is safe
        c.execute(f"{'ALTER' if role_there else 'CREATE'} ROLE {_q(ROLE)} WITH LOGIN NOCREATEDB NOCREATEROLE PASSWORD '{password}'")
        db_there = c.execute('SELECT 1 FROM pg_database WHERE datname = %s', [DB]).fetchone()
        if db_there and not reuse:
            c.execute(f'DROP DATABASE {_q(DB)} WITH (FORCE)')
            db_there = None
        if not db_there:
            c.execute(f'CREATE DATABASE {_q(DB)} OWNER {_q(ROLE)}')
        c.execute(f'REVOKE ALL ON DATABASE {_q(DB)} FROM PUBLIC')
        c.execute(f'GRANT CONNECT, TEMP ON DATABASE {_q(DB)} TO {_q(ROLE)}')
    with _connect(p, DB, ROLE, password) as c:             # as the role: it owns every table, so it may drop them
        has = c.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'orders'").fetchone()
        if not has:
            with c.transaction():
                c.execute(DDL)
                data = rows()
                for table, cols in COLUMNS.items():
                    with c.cursor().copy(f'COPY {table} ({", ".join(cols)}) FROM STDIN') as cp:
                        for row in data[table]:
                            cp.write_row(row)
                for table, (about, cols) in COMMENTS.items():
                    kind = 'VIEW' if table == 'monthly_sales' else 'TABLE'
                    c.execute(f'COMMENT ON {kind} {table} IS %s'.replace('%s', _lit(about)))
                    for col, note in cols.items():
                        c.execute(f'COMMENT ON COLUMN {table}.{col} IS {_lit(note)}')
                c.execute('ANALYZE')
    _store({'status': 'ready', 'host': p['host'], 'port': p['port'], 'sslmode': p['sslmode'],
            'password': crypto.encrypt_json({'p': password}).decode(), 'created_at': datetime.now(UTC).isoformat()})
    log(f'sample database {DB} is ready (role {ROLE})')
    return True


def _lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def ensure_sample(url, log=print):
    """Make the sample once. Safe to call at every start and from several processes (an advisory lock)."""
    with _lock:
        if state().get('status') in ('ready', 'removed', 'unavailable'):
            return False
        with db.connect() as c:
            c.execute(text("SELECT pg_advisory_lock(hashtext('dataviz.sample'))"))
            try:
                if state().get('status') in ('ready', 'removed', 'unavailable'):
                    return False
                return _create(url, log)
            finally:
                c.execute(text("SELECT pg_advisory_unlock(hashtext('dataviz.sample'))"))


def ensure_soon(url, log):
    """At start-up: in a thread, so a slow server never holds up the start."""
    def run():
        try:
            ensure_sample(url, log)
        except Exception as e:  # noqa: BLE001 - the app works without it; the next start tries again
            log(f'sample database not made ({type(e).__name__}: {str(e).splitlines()[0] if str(e) else ""}); the next start tries again')
    threading.Thread(target=run, name='dataviz-sample', daemon=True).start()


def remove_sample(url, log=print):
    p = _parts(url)
    with _connect(p, 'postgres') as c:
        c.execute(f'DROP DATABASE IF EXISTS {_q(DB)} WITH (FORCE)')
        c.execute(f'DROP ROLE IF EXISTS {_q(ROLE)}')
    _store({'status': 'removed'})
    log(f'sample database {DB} removed')


def reset_sample(url, log=print):
    """Make it again from scratch (an admin's Restore, `analysis-sample reset`): the rows as on the first day."""
    with _lock:
        return _create(url, log, reuse=False)


# ---------------------------------------------------------------- users' connections
def is_sample(conn):
    return conn.get('kind') == 'postgres' and conn.get('database') == DB and conn.get('username') == ROLE


def ensure_connection(user, registry, prefs, put_prefs):
    """Add the sample connection to this user's connections once. -> True when it was added now."""
    if prefs.get(PREF):
        return False
    st = state()
    if st.get('status') != 'ready':
        return False
    from ..db import crypto
    try:
        password = crypto.decrypt_json(st['password'].encode())['p']
        registry.upsert_connection({'kind': 'postgres', 'name': NAME, 'host': st['host'], 'port': st['port'], 'database': DB,
                                    'username': ROLE, 'secret_key': password, 'schema': 'public',
                                    'encrypt': st.get('sslmode') in ('require', 'verify-ca', 'verify-full')}, user['id'])
    except ValueError:
        pass                                   # a connection of that name exists: theirs stays
    put_prefs(user['id'], {PREF: True})
    return True


def cli(argv):
    """`python -m dataviz analysis-sample status|reset|remove [--config path]`: with the app stopped or running."""
    import argparse

    from ..core import config as vconfig
    ap = argparse.ArgumentParser(prog='python -m dataviz analysis-sample', description='The Data Analysis example database.')
    ap.add_argument('action', choices=['status', 'reset', 'remove'], help='status: what it is now; reset: make it again; remove: drop it')
    ap.add_argument('--config', default=None)
    a = ap.parse_args(argv)
    cfg = vconfig.load_config(a.config)
    url = str((cfg.get('database') or {}).get('url') or '')
    if not url or url.startswith('${'):
        print('No database configured: set DATABASE_URL')
        return 2
    db.init(url, pool_size=1)
    if a.action == 'status':
        st = state()
        print(f'{DB}: {st.get("status") or "not made yet"}' + (f' ({st["reason"]})' if st.get('reason') else ''))
        return 0
    from ..db import crypto
    crypto.init(key_file=vconfig.resolve(cfg['dataset']['workspace_root']) / 'secret.key')
    if a.action == 'remove':
        remove_sample(url)
    else:
        return 0 if reset_sample(url) else 1
    return 0
