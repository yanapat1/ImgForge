"""The Analysis service against a real PostgreSQL: the same server the app uses plays the user's database."""

import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import psycopg
import pytest


def done(service, user, run, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = service.run_state(user, run['id'])
        if st['status'] != 'running':
            return st
        time.sleep(0.02)
    pytest.fail('the run did not finish')


@pytest.fixture
def an(pg, tmp_path):
    from dataviz.analysis import service
    from dataviz.data import connections
    from sqlalchemy import text
    from dataviz import db
    import uuid
    uid = uuid.uuid4().hex
    with db.begin() as c:
        c.execute(text("""INSERT INTO app.users (id, username, email, password_hash, display_name, is_admin)
                          VALUES (:i, 'an', 'an@example.com', 'x', 'An', true)"""), {'i': uid})
    user = {'id': uid, 'username': 'an', 'is_admin': True}
    reg = connections.Registry(tmp_path)
    url = urlsplit(pg.replace('+psycopg', ''))
    cid = reg.upsert_connection({'kind': 'postgres', 'name': 'target', 'host': url.hostname, 'port': url.port,
                                 'database': url.path.lstrip('/'), 'username': url.username, 'secret_key': url.password,
                                 'schema': 'public', 'table_prefix': 'lbl_'}, uid)['id']
    service.configure(SimpleNamespace(cfg={'data_analysis': {'max_rows': 1000, 'statement_timeout_s': 30}}, datasets=reg))
    with psycopg.connect(pg.replace('+psycopg', ''), autocommit=True) as c:
        c.execute('CREATE TABLE public.orders (id int PRIMARY KEY, total numeric(10,2), note text)')
        c.execute("COMMENT ON COLUMN public.orders.total IS 'Order value'")
        c.execute("INSERT INTO public.orders SELECT i, i * 1.5, CASE WHEN i % 2 = 0 THEN 'even' END FROM generate_series(1, 50) i")
    sid = service.create_session(user, 'q3')['id']
    return SimpleNamespace(s=service, user=user, cid=cid, sid=sid, db=url.path.lstrip('/'), url=pg.replace('+psycopg', ''), reg=reg)


def go(an, sql, token=None):
    return done(an.s, an.user, an.s.start(an.user, an.sid, an.cid, an.db, sql, token))


def test_read_runs_and_pages(an):
    st = go(an, 'SELECT id, total, note FROM public.orders ORDER BY id')
    assert st['status'] == 'done'
    res = st['results'][0]
    assert res['rows'] == 50 and res['columns'] == ['id', 'total', 'note']
    assert an.s.page(an.user, res['id'], offset=48)['rows'] == [[49, 73.5, None], [50, 75.0, 'even']]


def test_stats_are_computed_once(an):
    res = go(an, 'SELECT id, note FROM public.orders')['results'][0]
    st = an.s.stats(an.user, res['id'])
    assert st['columns']['note']['nulls'] == 25 and st['duplicates'] == 0
    assert an.s.stats(an.user, res['id']) is st


def test_row_limit_marks_the_result(an):
    res = go(an, 'SELECT * FROM generate_series(1, 5000)')['results'][0]
    assert res['rows'] == 1000 and res['truncated']


def test_a_write_without_a_token_is_refused(an):
    from dataviz.analysis.service import AnalysisError
    with pytest.raises(AnalysisError) as e:
        an.s.start(an.user, an.sid, an.cid, an.db, 'DELETE FROM public.orders')
    assert e.value.status == 409
    with psycopg.connect(an.url) as c:
        assert c.execute('SELECT count(*) FROM public.orders').fetchone()[0] == 50


def test_confirmed_write_runs_once_and_is_audited(an):
    from sqlalchemy import text
    from dataviz import db
    from dataviz.analysis.service import AnalysisError
    sql = 'DELETE FROM public.orders WHERE id > 40'
    chk = an.s.check(an.user, an.sid, an.cid, an.db, sql)
    assert chk['confirm'] and chk['statements'][0]['kind'] == 'write'
    assert go(an, sql, chk['confirm'])['status'] == 'done'
    with psycopg.connect(an.url) as c:
        assert c.execute('SELECT count(*) FROM public.orders').fetchone()[0] == 40
    with pytest.raises(AnalysisError):                          # single use
        an.s.start(an.user, an.sid, an.cid, an.db, sql, chk['confirm'])
    with db.connect() as c:
        row = c.execute(text("SELECT meta FROM app.audit_log WHERE action = 'analysis.change'")).first()
    assert row and 'DELETE' in str(row[0])


def test_a_token_is_bound_to_the_exact_text(an):
    from dataviz.analysis.service import AnalysisError
    token = an.s.check(an.user, an.sid, an.cid, an.db, 'DELETE FROM public.orders WHERE id = 1')['confirm']
    with pytest.raises(AnalysisError):
        an.s.start(an.user, an.sid, an.cid, an.db, 'DELETE FROM public.orders', token)


def test_the_database_itself_refuses_a_write_that_looks_like_a_read(an):
    with psycopg.connect(an.url, autocommit=True) as c:
        c.execute("""CREATE FUNCTION public.sneaky() RETURNS int AS $$ BEGIN DELETE FROM public.orders; RETURN 1; END $$
                     LANGUAGE plpgsql""")
    st = go(an, 'SELECT public.sneaky()')
    assert st['status'] == 'error' and 'read-only' in st['error']
    with psycopg.connect(an.url) as c:
        assert c.execute('SELECT count(*) FROM public.orders').fetchone()[0] == 50


def test_an_error_keeps_the_password_out(an):
    conn = an.reg.connection(an.cid)
    st = go(an, f"SELECT 1/0, '{conn['secret_key']}'") if conn['secret_key'] else go(an, 'SELECT 1/0')
    assert st['status'] == 'error'
    if conn['secret_key']:
        assert conn['secret_key'] not in st['error']


def test_temp_table_lives_on_the_pinned_connection_and_leaves_with_the_session(an):
    sql = 'CREATE TEMP TABLE recent AS SELECT * FROM public.orders WHERE id < 5'
    chk = an.s.check(an.user, an.sid, an.cid, an.db, sql)
    assert chk['statements'][0]['kind'] == 'temp'
    assert go(an, sql, chk['confirm'])['status'] == 'done'
    assert [t['name'] for t in an.s.temps(an.user, an.sid)] == ['recent']
    assert go(an, 'SELECT count(*) FROM recent')['results'][0]['rows'] == 1
    an.s.close_session(an.user, an.sid)
    assert an.s.temps(an.user, an.sid) == []
    assert go(an, 'SELECT count(*) FROM recent')['status'] == 'error'


def test_several_statements_give_several_results(an):
    st = go(an, 'SELECT 1 AS a; SELECT 2 AS b;')
    assert [r['columns'] for r in st['results']] == [['a'], ['b']]


def test_stop_keeps_what_arrived(an):
    run = an.s.start(an.user, an.sid, an.cid, an.db, 'SELECT pg_sleep(30)')
    time.sleep(0.3)
    an.s.stop(an.user, run['id'])
    t0 = time.time()
    st = done(an.s, an.user, run)
    assert st['status'] == 'stopped' and time.time() - t0 < 5


def test_tree_and_describe(an):
    assert an.db in an.s.tree(an.user, an.cid)['databases']
    tables = an.s.tree(an.user, an.cid, an.db)['tables']
    assert {'schema': 'public', 'name': 'orders', 'kind': 'table'} in tables
    d = an.s.describe(an.user, an.cid, an.db, 'public', 'orders')
    total = next(c for c in d['columns'] if c['name'] == 'total')
    assert total['type'] == 'numeric(10,2)' and total['description'] == 'Order value' and total['from'] == 'database'
    assert next(c for c in d['columns'] if c['name'] == 'id')['key'] == 'PK'


def test_own_description_wins_and_can_be_cleared(an):
    an.s.set_description(an.user, an.cid, an.db, 'public', 'orders', 'note', 'Sales note')
    cols = {c['name']: c for c in an.s.describe(an.user, an.cid, an.db, 'public', 'orders')['columns']}
    assert cols['note']['description'] == 'Sales note' and cols['note']['from'] == 'user'
    an.s.set_description(an.user, an.cid, an.db, 'public', 'orders', 'note', ' ')
    cols = {c['name']: c for c in an.s.describe(an.user, an.cid, an.db, 'public', 'orders')['columns']}
    assert cols['note']['description'] == ''


def test_explain_gives_shares_of_the_cost(an):
    p = an.s.explain(an.user, an.sid, an.cid, an.db, 'SELECT * FROM public.orders o JOIN public.orders b ON o.id = b.id')
    assert p['estimated'] and abs(sum(n['pct'] for n in p['nodes']) - 100) < 1
    assert any('orders' in n['label'] for n in p['nodes'])


def test_explain_refuses_a_write(an):
    from dataviz.analysis.service import AnalysisError
    with pytest.raises(AnalysisError, match='only a read'):
        an.s.explain(an.user, an.sid, an.cid, an.db, 'DELETE FROM public.orders')


def test_another_user_cannot_touch_any_of_it(an):
    from dataviz.analysis.service import AnalysisError
    other = {'id': 'someone-else', 'username': 'x', 'is_admin': True}
    res = go(an, 'SELECT 1 AS a')
    for call in (lambda: an.s.page(other, res['results'][0]['id']),
                 lambda: an.s.run_state(other, res['id']),
                 lambda: an.s.history(other, an.sid),
                 lambda: an.s.tree(other, an.cid),
                 lambda: an.s.start(other, an.sid, an.cid, an.db, 'SELECT 1'),
                 lambda: an.s.delete_session(other, an.sid)):
        with pytest.raises(AnalysisError) as e:
            call()
        assert e.value.status == 404


def test_history_and_scripts(an):
    go(an, 'SELECT 7 AS x')
    h = an.s.history(an.user, an.sid)
    assert h[0]['sql'] == 'SELECT 7 AS x' and h[0]['rows'] == 1 and h[0]['kind'] == 'read'
    pid = an.s.save_script(an.user, an.sid, {'name': 'command1', 'body': 'SELECT 1'})['id']
    an.s.save_script(an.user, an.sid, {'id': pid, 'name': 'renamed', 'body': 'SELECT 2'})
    assert [(s['name'], s['body']) for s in an.s.scripts(an.user, an.sid)] == [('renamed', 'SELECT 2')]
    an.s.delete_script(an.user, an.sid, pid)
    assert an.s.scripts(an.user, an.sid) == []


def test_deleting_a_session_removes_its_files(an):
    from pathlib import Path
    res = go(an, 'SELECT 1 AS a')['results'][0]
    path = Path(an.s.result_path(an.user, res['id']))
    assert path.exists()
    an.s.delete_session(an.user, an.sid)
    assert not path.exists()


def join_model():
    return {'tables': [{'id': 'o', 'schema': 'public', 'name': 'orders', 'alias': 'o', 'kind': 'table'},
                       {'id': 'c', 'schema': 'public', 'name': 'buyers', 'alias': 'c', 'kind': 'table'}],
            'joins': [{'a': ['o', 'cid'], 'b': ['c', 'id'], 'type': 'INNER'}]}


def test_join_counts_match_a_hand_computed_case(an):
    with psycopg.connect(an.url, autocommit=True) as c:
        c.execute('CREATE TABLE public.buyers (id int)')
        c.execute('INSERT INTO public.buyers VALUES (1), (2), (2), (9)')     # 2 twice: fan-out; 9 has no order
        c.execute('ALTER TABLE public.orders ADD COLUMN cid int')
        c.execute('UPDATE public.orders SET cid = CASE WHEN id <= 2 THEN 1 WHEN id = 3 THEN 2 WHEN id = 4 THEN 7 END')
    # orders: cid 1 x2, 2 x1, 7 x1, NULL x46. buyers: 1 x1, 2 x2, 9 x1
    s = an.s.join_stats(an.user, an.cid, an.db, join_model(), 0)
    assert s['a_rows'] == 50 and s['b_rows'] == 4
    assert s['matched_a'] == 3 and s['matched_b'] == 3 and s['lost_a'] == 47 and s['lost_b'] == 1
    assert s['joined'] == 4 and s['kept'] == 4 and s['fan_out']          # 1 x 2 orders + 2 x 2 buyers
    assert not s['a_unique'] and not s['b_unique']                       # cid 1 twice, id 2 twice: N:M in this data
    with psycopg.connect(an.url) as c:
        assert c.execute('SELECT count(*) FROM public.orders o JOIN public.buyers b ON o.cid = b.id').fetchone()[0] == 4
        assert c.execute('SELECT count(*) FROM public.orders o LEFT JOIN public.buyers b ON o.cid = b.id').fetchone()[0] == 51
    m = join_model()
    m['joins'][0]['type'] = 'LEFT'
    assert an.s.join_stats(an.user, an.cid, an.db, m, 0)['kept'] == 51


def test_describe_says_which_columns_are_unique_on_their_own(an):
    with psycopg.connect(an.url, autocommit=True) as c:
        c.execute('CREATE TABLE public.cust (id int PRIMARY KEY, email text UNIQUE, a int, b int, UNIQUE (a, b))')
        c.execute('CREATE TABLE public.prof (cust_id int UNIQUE REFERENCES public.cust)')
        c.execute('CREATE TABLE public.buys (cust_id int REFERENCES public.cust, code text)')
        c.execute('CREATE UNIQUE INDEX buys_code ON public.buys (code) WHERE code IS NOT NULL')      # partial: not unique
    cust = an.s.describe(an.user, an.cid, an.db, 'public', 'cust')
    assert sorted(cust['unique']) == ['email', 'id']                   # (a, b) together is not a 1 for a or b
    assert an.s.describe(an.user, an.cid, an.db, 'public', 'prof')['unique'] == ['cust_id']   # 1 : 0..1
    assert an.s.describe(an.user, an.cid, an.db, 'public', 'buys')['unique'] == []           # many : 1
    ai = an.s.describe_for_ai(an.user, an.cid, an.db, 'public', 'cust')
    assert sorted(ai['unique_columns']) == ['email', 'id']


def rid(an, sql):
    return an.s.run_for_ai(an.user, an.sid, an.cid, an.db, sql, 'q', 20)


def allow_rows(an, on=True):
    an.s.update_session(an.user, an.sid, state={'ai_rows': on})


def size(x):
    import orjson
    return len(orjson.dumps(x, default=str))


def test_a_query_result_gives_no_rows_to_the_model(an):
    got = rid(an, "SELECT id, note, 'secret-value' AS s FROM public.orders")
    assert got['rows'] == 50 and 'result_id' in got and not isinstance(got['rows'], list)
    assert 'even' not in str({k: v for k, v in got.items() if k != 'columns'})


def test_a_change_is_refused_on_the_read_tool(an):
    from dataviz.analysis.service import AnalysisError
    with pytest.raises(AnalysisError, match='run_change'):
        rid(an, 'DELETE FROM public.orders')


def test_query_result_needs_an_aggregate_without_permission(an):
    from dataviz.analysis.service import AnalysisError
    r = rid(an, 'SELECT id, total, note FROM public.orders')['result_id']
    got = an.s.query_result(an.user, an.sid, r, 'SELECT note, COUNT(*) AS n, SUM(total) AS s FROM result GROUP BY note ORDER BY n')
    assert got['rows_returned'] == 2
    with pytest.raises(AnalysisError, match='row access is off'):
        an.s.query_result(an.user, an.sid, r, 'SELECT * FROM result')
    allow_rows(an)
    assert an.s.query_result(an.user, an.sid, r, 'SELECT * FROM result')['rows_returned'] == 20


@pytest.mark.parametrize('sql', ["SELECT * FROM read_csv('/etc/passwd')", 'SELECT * FROM other', "COPY result TO '/tmp/x.csv'",
                                 "SELECT COUNT(*) FROM read_text('/etc/passwd')", "SELECT COUNT(*) FROM '/etc/passwd'", 'DROP VIEW result'])
def test_query_result_reaches_nothing_but_the_result(an, sql):
    from dataviz.analysis.service import AnalysisError
    r = rid(an, 'SELECT id FROM public.orders')['result_id']
    with pytest.raises(AnalysisError):
        an.s.query_result(an.user, an.sid, r, sql)


def test_samples_need_permission_and_stay_small(an):
    from dataviz.analysis.service import AnalysisError
    r = rid(an, "SELECT i, repeat('x', 5000) AS big FROM generate_series(1, 900) i")['result_id']
    with pytest.raises(AnalysisError, match='not allowed row access'):
        an.s.sample_for_ai(an.user, an.sid, r)
    allow_rows(an)
    got = an.s.sample_for_ai(an.user, an.sid, r)
    assert got['rows_returned'] <= 20 and got['rows_total'] == 900 and size(got) <= 4000
    assert all(len(row[1]) <= 81 for row in got['rows'])


def test_a_huge_result_costs_the_same_as_a_small_one(an):
    big = rid(an, 'SELECT i, i * 2 AS b FROM generate_series(1, 900) i')
    assert size(big) <= 4000


def test_two_thousand_columns_stay_under_the_cap(an):
    with psycopg.connect(an.url, autocommit=True) as c:
        c.execute('CREATE TABLE public.wide (' + ', '.join(f'column_number_{i} int' for i in range(1500)) + ')')
    got = an.s.describe_for_ai(an.user, an.cid, an.db, 'public', 'wide')
    assert size(got) <= 4000 and got['total_columns'] == 1500 and got['truncated']
    assert len(got['columns']) < 200
    wide = rid(an, 'SELECT * FROM public.wide')
    assert size(wide) <= 4000


def test_search_catalog_finds_by_description_and_never_dumps_all(an):
    an.s.set_description(an.user, an.cid, an.db, 'public', 'orders', 'note', 'free text from the sales team')
    got = an.s.search_catalog(an.user, an.cid, an.db, 'sales team')
    assert got['tables'][0]['name'] == 'orders' and len(got['tables']) <= 30


def test_explain_for_the_model_is_short(an):
    got = an.s.explain_for_ai(an.user, an.sid, an.cid, an.db, 'SELECT * FROM public.orders a JOIN public.orders b ON a.id = b.id')
    assert got['top'] and size(got) <= 4000


def test_a_forecast_reads_the_saved_result_and_the_model_gets_no_history(an):
    from dataviz.analysis.service import AnalysisError
    sql = ("SELECT d::date AS day, 100 + extract(dow FROM d) * 10 + extract(doy FROM d) AS v "
           "FROM generate_series(DATE '2025-01-06', DATE '2025-06-30', INTERVAL '1 day') d")
    res = go(an, sql)['results'][0]
    f = an.s.forecast(an.user, res['id'], 'day', 'v', 'sum', 'auto', 7, None)
    assert f['grain'] == 'day' and len(f['forecast']) == 7 and f['season'] == 7
    ai = an.s.forecast_for_ai(an.user, rid(an, sql)['result_id'], 'day', 'v')
    assert 'history' not in ai and ai['forecast'] and size(ai) <= 4000
    with pytest.raises(AnalysisError):
        an.s.forecast({'id': 'someone-else'}, res['id'], 'day', 'v')


def test_export_writes_a_file_per_format_and_only_for_its_owner(an):
    from pathlib import Path

    from dataviz.analysis.service import AnalysisError
    res = go(an, 'SELECT id, total, note FROM public.orders ORDER BY id')['results'][0]
    path, name, media, temp = an.s.export_result(an.user, res['id'], 'parquet')
    assert not temp and name.endswith('.parquet') and path == Path(an.s.result_path(an.user, res['id']))
    path, name, media, temp = an.s.export_result(an.user, res['id'], 'CSV')
    assert temp and name == f'result-{res["id"][:8]}.csv' and media.startswith('text/csv')
    assert path.read_text().splitlines()[:2] == ['id,total,note', '1,1.50,']
    path.unlink()
    with pytest.raises(AnalysisError, match='unknown export format'):
        an.s.export_result(an.user, res['id'], 'pdf')
    with pytest.raises(AnalysisError) as e:
        an.s.export_result({'id': 'someone-else'}, res['id'], 'csv')
    assert e.value.status == 404


def test_join_overview_counts_the_whole_query_in_one_go(an):
    """Three tables in a chain: the rows the query keeps and how many keys of each table reach it (hand-computed)."""
    with psycopg.connect(an.url, autocommit=True) as c:
        c.execute('CREATE TABLE public.ja (id int)')
        c.execute('INSERT INTO public.ja VALUES (1), (2), (3), (4), (5)')
        c.execute('CREATE TABLE public.jb (a_id int, c_id int)')
        c.execute('INSERT INTO public.jb VALUES (1, 10), (1, 11), (2, 10), (3, 12), (9, 10)')
        c.execute('CREATE TABLE public.jc (id int)')
        c.execute('INSERT INTO public.jc VALUES (10), (11), (13)')

    def model(kind='INNER', where=None):
        return {'tables': [{'id': t, 'schema': 'public', 'name': f'j{t}', 'alias': t, 'kind': 'table'} for t in 'abc'],
                'joins': [{'a': ['a', 'id'], 'b': ['b', 'a_id'], 'type': kind}, {'a': ['b', 'c_id'], 'b': ['c', 'id'], 'type': kind}],
                'parts': {'where': where} if where else {}}
    got = an.s.join_overview(an.user, an.cid, an.db, model())
    assert got == {'rows': 3, 'used': {'a': 2, 'b': 2, 'c': 2}, 'key': {'a': 'id', 'b': 'a_id', 'c': 'id'}}
    # a, b: (1,10) (1,11) (2,10) (3,12) (4,-) (5,-); c keeps 10 and 11
    assert an.s.join_overview(an.user, an.cid, an.db, model('LEFT'))['used'] == {'a': 5, 'b': 3, 'c': 2}
    assert an.s.join_overview(an.user, an.cid, an.db, model(where='"a"."id" <> 1')) == \
        {'rows': 1, 'used': {'a': 1, 'b': 1, 'c': 1}, 'key': {'a': 'id', 'b': 'a_id', 'c': 'id'}}
    from dataviz.analysis.service import AnalysisError
    with pytest.raises(AnalysisError, match='add a join'):
        an.s.join_overview(an.user, an.cid, an.db, {'tables': model()['tables'], 'joins': []})
    with pytest.raises(AnalysisError, match='not a plain read'):
        an.s.join_overview(an.user, an.cid, an.db, model(where='1=1; DELETE FROM public.ja'))
