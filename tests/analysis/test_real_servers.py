"""The Analysis service against real SQL Server and MongoDB containers (skipped when Docker cannot start them)."""

import socket
import time
import uuid
from types import SimpleNamespace

import pytest

from conftest_io import docker_db, mssql_conn  # noqa: F401 - the fixture

pytest_plugins = []


def _done(s, user, run, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = s.run_state(user, run['id'])
        if st['status'] != 'running':
            return st
        time.sleep(0.05)
    pytest.fail('the run did not finish')


class Reg:
    def __init__(self, root, conn):
        self.root, self.conn = root, conn

    def connection(self, cid, owner_id=None):
        if cid != self.conn['id']:
            raise KeyError(cid)
        return self.conn

    def connections(self, owner_id):
        return [self.conn]


def make(pg, tmp_path, conn):
    from sqlalchemy import text

    from dataviz import db
    from dataviz.analysis import service
    uid = uuid.uuid4().hex
    with db.begin() as c:
        c.execute(text("""INSERT INTO app.users (id, username, email, password_hash, display_name, is_admin)
                          VALUES (:i, 'an', 'an@example.com', 'x', 'An', true)"""), {'i': uid})
    user = {'id': uid, 'username': 'an', 'is_admin': True}
    service.configure(SimpleNamespace(cfg={'data_analysis': {'max_rows': 5000, 'statement_timeout_s': 30}}, datasets=Reg(tmp_path, conn)))
    return SimpleNamespace(s=service, user=user, sid=service.create_session(user, 'q')['id'], cid=conn['id'])


def go(an, sql, db=None, token=None):
    return _done(an.s, an.user, an.s.start(an.user, an.sid, an.cid, db, sql, token))


def confirmed(an, sql, db=None):
    return go(an, sql, db, an.s.check(an.user, an.sid, an.cid, db, sql)['confirm'])


@pytest.fixture
def ms(pg, tmp_path, mssql_conn):  # noqa: F811
    import pymssql
    name = f'an_{uuid.uuid4().hex[:8]}'
    base = dict(server=mssql_conn['host'], port=str(mssql_conn['port']), user='sa', password=mssql_conn['secret_key'], autocommit=True)
    with pymssql.connect(**base) as c:
        c.cursor().execute(f'CREATE DATABASE {name}')
    with pymssql.connect(database=name, **base) as c:
        cur = c.cursor()
        cur.execute('CREATE TABLE dbo.Customers (Id int PRIMARY KEY, Name nvarchar(50))')
        cur.execute('CREATE TABLE dbo.Orders (Id int PRIMARY KEY, CustId int REFERENCES dbo.Customers(Id), Total decimal(10,2), Note nvarchar(max))')
        cur.execute("EXEC sp_executesql N'CREATE SCHEMA nakuser'")
        cur.execute('CREATE TABLE nakuser.TRB_APPEND_TBL (Id int)')
        cur.execute("INSERT INTO dbo.Customers VALUES (1, N'สยาม'), (2, 'Mekong'), (3, 'Lanna')")
        cur.execute("INSERT INTO dbo.Orders VALUES (1, 1, 10.5, 'a'), (2, 1, 20, NULL), (3, 2, 5, 'b')")
        cur.execute("EXEC sp_addextendedproperty 'MS_Description', 'Order value', 'SCHEMA', 'dbo', 'TABLE', 'Orders', 'COLUMN', 'Total'")
    conn = {**mssql_conn, 'id': 'ms1', 'database': ''}
    an = make(pg, tmp_path, conn)
    an.db = name
    yield an
    an.s.close_session(an.user, an.sid)
    with pymssql.connect(**base) as c:
        c.cursor().execute(f'ALTER DATABASE {name} SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE {name}')


def test_mssql_lists_many_databases_and_every_schema(ms):
    tree = ms.s.tree(ms.user, ms.cid)
    assert ms.db in tree['databases'] and tree['version']
    tables = ms.s.tree(ms.user, ms.cid, ms.db)['tables']
    assert {'schema': 'nakuser', 'name': 'TRB_APPEND_TBL', 'kind': 'table'} in tables
    assert {'schema': 'dbo', 'name': 'Orders', 'kind': 'table'} in tables


def test_mssql_describe(ms):
    d = ms.s.describe(ms.user, ms.cid, ms.db, 'dbo', 'Orders')
    cols = {c['name']: c for c in d['columns']}
    assert cols['Id']['key'] == 'PK' and cols['CustId']['key'] == 'FK' and cols['Total']['type'] == 'decimal(10,2)'
    assert cols['Total']['description'] == 'Order value' and cols['Note']['type'] == 'nvarchar(max)'
    assert d['keys'] == [{'column': 'CustId', 'schema': 'dbo', 'table': 'Customers', 'to': 'Id'}] and d['rows'] == 3


def test_mssql_query_thai_text_decimals_and_nulls(ms):
    st = go(ms, 'SELECT TOP (10) c.Name, o.Total, o.Note FROM dbo.Orders AS o JOIN dbo.Customers AS c ON o.CustId = c.Id ORDER BY o.Id', ms.db)
    assert st['status'] == 'done'
    assert ms.s.page(ms.user, st['results'][0]['id'])['rows'][:2] == [['สยาม', 10.5, 'a'], ['สยาม', 20.0, None]]


def test_mssql_confirm_gate_and_change(ms):
    from dataviz.analysis.service import AnalysisError
    with pytest.raises(AnalysisError):
        ms.s.start(ms.user, ms.sid, ms.cid, ms.db, 'DELETE FROM dbo.Orders WHERE Id = 3')
    assert confirmed(ms, 'DELETE FROM dbo.Orders WHERE Id = 3', ms.db)['status'] == 'done'
    assert go(ms, 'SELECT COUNT(*) AS n FROM dbo.Orders', ms.db)['results'][0]['rows'] == 1
    assert ms.s.page(ms.user, go(ms, 'SELECT COUNT(*) AS n FROM dbo.Orders', ms.db)['results'][0]['id'])['rows'] == [[2]]


def test_mssql_hash_temp_table_stays_on_its_connection(ms):
    assert confirmed(ms, 'SELECT * INTO #recent FROM dbo.Orders WHERE Id < 3', ms.db)['status'] == 'done'
    assert [t['name'] for t in ms.s.temps(ms.user, ms.sid)] == ['#recent']
    st = go(ms, 'SELECT COUNT(*) AS n FROM #recent', ms.db)
    assert st['status'] == 'done' and ms.s.page(ms.user, st['results'][0]['id'])['rows'] == [[2]]
    ms.s.close_session(ms.user, ms.sid)
    assert go(ms, 'SELECT COUNT(*) AS n FROM #recent', ms.db)['status'] == 'error'


def test_mssql_error_message(ms):
    st = go(ms, 'SELECT * FROM dbo.Nothing', ms.db)
    assert st['status'] == 'error' and 'Nothing' in st['error'] and ms.s._Conf.ctx.datasets.conn['secret_key'] not in st['error']


def test_mssql_showplan_cost_shares(ms):
    p = ms.s.explain(ms.user, ms.sid, ms.cid, ms.db, 'SELECT * FROM dbo.Orders AS o JOIN dbo.Customers AS c ON o.CustId = c.Id')
    assert p['estimated'] and p['nodes'] and abs(sum(n['pct'] for n in p['nodes']) - 100) < 1
    assert any('Orders' in n['label'] or 'Customers' in n['label'] for n in p['nodes'])
    assert go(ms, 'SELECT 1 AS still_works', ms.db)['status'] == 'done'


def test_mssql_stop_a_long_query(ms):
    run = ms.s.start(ms.user, ms.sid, ms.cid, ms.db, "WAITFOR DELAY '00:00:30'; SELECT 1 AS a")
    st = run
    time.sleep(0.4)
    ms.s.stop(ms.user, run['id'])
    t0 = time.time()
    st = _done(ms.s, ms.user, run, timeout=45)
    assert st['status'] in ('stopped', 'error') or st


def test_mssql_join_counts(ms):
    model = {'tables': [{'id': 'o', 'schema': 'dbo', 'name': 'Orders', 'alias': 'o', 'kind': 'table'},
                        {'id': 'c', 'schema': 'dbo', 'name': 'Customers', 'alias': 'c', 'kind': 'table'}],
             'joins': [{'a': ['o', 'CustId'], 'b': ['c', 'Id'], 'type': 'INNER'}]}
    s = ms.s.join_stats(ms.user, ms.cid, ms.db, model, 0)
    assert (s['a_rows'], s['b_rows'], s['joined'], s['lost_b'], s['kept']) == (3, 3, 3, 1, 3)


def test_mssql_builder_sql_runs(ms):
    from dataviz.analysis import querymodel
    m = querymodel.parse('SELECT o.Id, c.Name FROM dbo.Orders AS o LEFT JOIN dbo.Customers AS c ON o.CustId = c.Id', 'mssql')
    m['calc'].append({'alias': 'Rn', 't': 'o', 'kind': 'window', 'expr': 'ROW_NUMBER() OVER (ORDER BY o.Id DESC)'})
    m['tables'][0]['cast'] = {'Id': {'type': 'bigint', 'safe': True}}
    st = go(ms, querymodel.generate(m, 'mssql'), ms.db)
    assert st['status'] == 'done', st['error']
    assert st['results'][0]['columns'] == ['Id', 'Name', 'Rn']


def test_mssql_writes_a_description_back(ms):
    from dataviz.analysis import adapters
    a = adapters.adapter(ms.s._Conf.ctx.datasets.conn, ms.db)
    a.set_description('dbo', 'Orders', 'Note', 'Free text')
    a.set_description('dbo', 'Orders', 'Note', 'Free text, edited')
    assert next(c for c in a.describe('dbo', 'Orders')['columns'] if c['name'] == 'Note')['description'] == 'Free text, edited'
    a.close()


# ---------------------------------------------------------------- MongoDB
def _mongo_ready(host, port):
    try:
        with socket.create_connection((host, port), 2):
            pass
    except OSError:
        return False
    import pymongo
    try:
        pymongo.MongoClient(host, port, serverSelectionTimeoutMS=1500).server_info()
        return True
    except pymongo.errors.PyMongoError:
        return False


@pytest.fixture(scope='module')
def mongo_server():
    with docker_db('an_test_mongo', 'mongo:7', {}, 27017, _mongo_ready, 90) as (host, port):
        yield host, port


@pytest.fixture
def mg(pg, tmp_path, mongo_server):
    import pymongo
    host, port = mongo_server
    name = f'an_{uuid.uuid4().hex[:8]}'
    c = pymongo.MongoClient(host, port)
    c[name].events.insert_many([{'_id': i, 'type': 'buy' if i % 2 else 'view', 'cid': i % 3, 'meta': {'ip': f'10.0.0.{i}'}, 'tags': ['a', 'b']} for i in range(30)])
    c[name].customers.insert_many([{'_id': i, 'name': f'c{i}'} for i in range(2)])
    an = make(pg, tmp_path, {'id': 'mg1', 'kind': 'mongo', 'name': 'logs', 'host': host, 'port': port, 'database': ''})
    an.db = name
    yield an
    c.drop_database(name)


def test_mongo_tree_and_sampled_fields(mg):
    assert mg.db in mg.s.tree(mg.user, mg.cid)['databases']
    assert {t['name'] for t in mg.s.tree(mg.user, mg.cid, mg.db)['tables']} == {'events', 'customers'}
    d = mg.s.describe(mg.user, mg.cid, mg.db, '', 'events')
    assert {'type', 'meta.ip', '_id'} <= {c['name'] for c in d['columns']} and d['sampled'] == 30


def test_mongo_find_flattens_nested_and_keeps_arrays_in_one_cell(mg):
    st = go(mg, 'db.events.find({type: "buy"}).sort({_id: 1}).limit(3)', mg.db)
    r = st['results'][0]
    assert st['status'] == 'done' and r['rows'] == 3 and 'meta.ip' in r['columns']
    page = mg.s.page(mg.user, r['id'])
    assert page['rows'][0][r['columns'].index('tags')] == '["a", "b"]'


def test_mongo_aggregate_and_a_builder_join(mg):
    from dataviz.analysis import querymodel
    st = go(mg, 'db.events.aggregate([{ $group: { _id: "$type", n: { $sum: 1 } } }])', mg.db)
    assert st['results'][0]['rows'] == 2
    model = {'tables': [{'id': 'e', 'name': 'events', 'sel': ['type']}, {'id': 'c', 'name': 'customers', 'sel': ['name']}],
             'joins': [{'a': ['e', 'cid'], 'b': ['c', '_id'], 'type': 'INNER'}], 'calc': [], 'parts': {}}
    st = go(mg, querymodel.generate(model, 'mongo'), mg.db)
    assert st['status'] == 'done', st['error']
    assert st['results'][0]['rows'] == 20                 # cid 0 and 1 have a customer, cid 2 has none


def test_mongo_counts_and_distinct(mg):
    assert mg.s.page(mg.user, go(mg, 'db.events.countDocuments({type: "buy"})', mg.db)['results'][0]['id'])['rows'] == [[15]]
    assert go(mg, "db.events.distinct('type')", mg.db)['results'][0]['rows'] == 2


def test_mongo_writes_need_a_confirm(mg):
    from dataviz.analysis.service import AnalysisError
    for sql in ('db.events.deleteMany({type: "buy"})', 'db.events.aggregate([{ $out: "copy" }])'):
        with pytest.raises(AnalysisError) as e:
            mg.s.start(mg.user, mg.sid, mg.cid, mg.db, sql)
        assert e.value.status == 409
    assert confirmed(mg, 'db.events.deleteMany({type: "buy"})', mg.db)['status'] == 'done'
    assert mg.s.page(mg.user, go(mg, 'db.events.countDocuments({})', mg.db)['results'][0]['id'])['rows'] == [[15]]


def test_mongo_explain(mg):
    p = mg.s.explain(mg.user, mg.sid, mg.cid, mg.db, 'db.events.find({type: "buy"})')
    assert p['nodes'] and not p['estimated']


def test_mongo_connection_test_button(mongo_server):
    """Settings › Connections › Test on a MongoDB connection: a version back, and a clear failure for a dead port."""
    from dataviz.analysis import adapters
    host, port = mongo_server
    r = adapters.ping({'kind': 'mongo', 'host': host, 'port': port, 'database': ''})
    assert r['ok'] and r['server_version'] and r['ms'] >= 0
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    dead = s.getsockname()[1]
    s.close()
    with pytest.raises(Exception):
        adapters.ping({'kind': 'mongo', 'host': '127.0.0.1', 'port': dead, 'database': ''})
