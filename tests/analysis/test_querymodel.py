"""SQL and the Visual builder stay in step, and nothing the builder cannot hold is lost silently."""

import pytest
import sqlglot

from dataviz.analysis import querymodel as qm

JOIN = 'SELECT o.Id, c.Name FROM dbo.Orders AS o LEFT JOIN dbo.Customers AS c ON o.CustId = c.Id WHERE o.Total > 5 ORDER BY o.Id DESC'


def test_tables_joins_and_columns():
    m = qm.parse(JOIN, 'mssql')
    assert [(t['schema'], t['name'], t['alias'], t['sel']) for t in m['tables']] == [
        ('dbo', 'Orders', 'o', ['Id']), ('dbo', 'Customers', 'c', ['Name'])]
    assert m['joins'] == [{'a': ['o', 'CustId'], 'b': ['c', 'Id'], 'type': 'LEFT'}]
    assert m['parts']['where'] == 'o.Total > 5' and 'DESC' in m['parts']['order'] and not m['locked']


def test_round_trip_keeps_the_meaning():
    m = qm.parse(JOIN, 'mssql')
    m['parts']['parsed'] = True
    again = qm.parse(qm.generate(m, 'mssql'), 'mssql')
    assert [t['sel'] for t in again['tables']] == [['Id'], ['Name']]
    assert again['joins'] == m['joins'] and again['parts']['where'] == m['parts']['where']


def test_every_generated_text_parses():
    for sql, d in [(JOIN, 'mssql'), ('SELECT a.x FROM s.a AS a INNER JOIN s.b AS b ON a.i = b.i', 'postgres')]:
        out = qm.generate(qm.parse(sql, d), d)
        sqlglot.parse_one(out, dialect=qm._d(d))


def test_cast_case_window_and_subquery():
    m = qm.parse("""SELECT CAST(o.Total AS int) AS Total, TRY_CAST(o.Note AS varchar(10)) AS Note,
        CASE WHEN o.Total > 1 THEN 'big' ELSE 'small' END AS Size,
        ROW_NUMBER() OVER (PARTITION BY o.Seg ORDER BY o.Total DESC) AS Rn,
        (SELECT MAX(x.A) FROM dbo.X AS x) AS Most, o.Total * 2 AS Twice
        FROM dbo.Orders AS o""", 'mssql')
    t = m['tables'][0]
    assert t['cast'] == {'Total': {'type': 'INTEGER', 'safe': False}, 'Note': {'type': 'VARCHAR(10)', 'safe': True}} or \
        t['cast']['Total']['safe'] is False and t['cast']['Note']['safe'] is True
    assert [(c['alias'], c['kind']) for c in m['calc']] == [('Size', 'case'), ('Rn', 'window'), ('Most', 'sub'), ('Twice', 'expr')]
    assert not m['locked']
    out = qm.generate(m, 'mssql')
    assert 'TRY_CAST' in out and 'ROW_NUMBER()' in out and 'AS [Size]' in out


def test_every_window_function_survives():
    for fn in ('ROW_NUMBER()', 'RANK()', 'DENSE_RANK()', 'NTILE(4)', 'PERCENT_RANK()', 'CUME_DIST()', 'LAG(a, 1)', 'LEAD(a, 1)',
               'FIRST_VALUE(a)', 'LAST_VALUE(a)', 'SUM(a)', 'AVG(a)', 'COUNT(*)', 'MIN(a)', 'MAX(a)'):
        m = qm.parse(f'SELECT {fn} OVER (ORDER BY b) AS w FROM t', 'postgres')
        assert m['calc'][0]['kind'] == 'window' and not m['locked'], fn


def test_subquery_in_from_is_a_joinable_card():
    m = qm.parse('SELECT o.Id FROM dbo.Orders AS o JOIN (SELECT Id, COUNT(*) AS N FROM dbo.Items GROUP BY Id) AS li ON o.Id = li.Id', 'mssql')
    sub = m['tables'][1]
    assert sub['kind'] == 'sub' and sub['alias'] == 'li' and 'GROUP BY' in sub['sql'] and len(m['joins']) == 1
    assert 'GROUP BY' in qm.generate(m, 'mssql')


def test_subquery_in_where_stays_in_the_filter():
    m = qm.parse('SELECT a FROM t WHERE a IN (SELECT b FROM u)', 'postgres')
    assert 'SELECT' in m['parts']['where'] and not m['locked']


def test_openquery_is_a_remote_card():
    m = qm.parse("SELECT * FROM OPENQUERY(ERP, 'SELECT id FROM inv') AS e", 'mssql')
    assert m['tables'][0]['kind'] == 'remote' and m['tables'][0]['sql'] == 'SELECT id FROM inv'
    assert 'OPENQUERY' in qm.generate(m, 'mssql')


@pytest.mark.parametrize('sql, why, dialect', [
    ('SELECT a FROM t UNION ALL SELECT a FROM u', 'UNION', 'postgres'),
    ('SELECT * FROM t CROSS APPLY fn(t.a) x', 'APPLY', 'mssql'),
    ('SELECT * FROM a CROSS JOIN b', 'CROSS JOIN', 'postgres'),
    ('SELECT * FROM t AS a JOIN t AS b ON a.i = b.i', 'self join', 'postgres'),
    ('SELECT * FROM a JOIN b ON a.i > b.i', 'join condition', 'postgres'),
    ('SELECT * FROM a JOIN b ON a.i = b.i AND a.j = b.j', 'join condition', 'postgres'),
    ('WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r) SELECT * FROM r', 'recursive', 'postgres'),
    ('SELECT a, SUM(b) FROM t GROUP BY ROLLUP (a)', 'ROLLUP', 'postgres'),
])
def test_what_the_builder_cannot_hold_locks_it_and_names_why(sql, why, dialect):
    assert any(why in r for r in qm.parse(sql, dialect)['locked'])


def test_cte_becomes_a_card_and_stays_in_the_text():
    m = qm.parse('WITH x AS (SELECT 1 AS a) SELECT x.a FROM x', 'postgres')
    assert m['tables'][0]['kind'] == 'cte' and not m['locked']
    assert qm.generate(m, 'postgres').startswith('WITH "x" AS')


def test_a_write_is_not_drawn():
    assert qm.parse('DELETE FROM t', 'postgres')['statement'] == 'write'


def test_broken_sql_reports_an_error():
    assert 'error' in qm.parse('SELEC FROM', 'postgres')


def test_two_statements_are_two_unjoined_results():
    m = qm.parse('SELECT a FROM t; SELECT b FROM u;', 'postgres')
    assert len(m['tables']) == 2 and len(qm._groups(m)) == 2
    assert qm.generate(m, 'postgres').count('SELECT') == 2


def model(**k):
    return {'tables': [], 'joins': [], 'calc': [], 'parts': {}, **k}


def test_unjoined_tables_are_one_select_each_joined_share_one():
    t = [{'id': 'a', 'schema': 's', 'name': 'A', 'alias': 'a', 'sel': ['x']}, {'id': 'b', 'schema': 's', 'name': 'B', 'alias': 'b', 'sel': ['y']}]
    assert qm.generate(model(tables=t), 'postgres').count('SELECT') == 2
    joined = model(tables=t, joins=[{'a': ['a', 'x'], 'b': ['b', 'y'], 'type': 'FULL'}])
    out = qm.generate(joined, 'postgres')
    assert out.count('SELECT') == 1 and 'FULL OUTER JOIN' in out


def test_a_hidden_table_leaves_the_query():
    t = [{'id': 'a', 'name': 'A', 'alias': 'a', 'sel': ['x'], 'show': False}]
    assert qm.generate(model(tables=t), 'postgres') == ''


def test_star_is_kept_beside_columns():
    m = qm.parse('SELECT c.*, o.Total FROM o JOIN c ON o.i = c.i', 'postgres')
    out = qm.generate(m, 'postgres')
    assert '"c".*' in out and '"o"."Total"' in out


def test_names_are_quoted_for_the_dialect():
    t = [{'id': 'a', 'schema': 'dbo', 'name': 'we]ird name', 'alias': 'a', 'sel': ['x;y']}]
    assert '[we]]ird name]' in qm.generate(model(tables=t), 'mssql')
    t2 = [{'id': 'a', 'schema': 'public', 'name': 'we"ird', 'alias': 'a', 'sel': ['x']}]
    assert '"we""ird"' in qm.generate(model(tables=t2), 'postgres')


def test_top_and_limit_follow_the_dialect():
    t = [{'id': 'a', 'name': 'A', 'alias': 'a', 'sel': ['x']}]
    assert 'TOP (1000)' in qm.generate(model(tables=t), 'mssql')
    assert qm.generate(model(tables=t), 'postgres').endswith('LIMIT 1000;')


def test_mongo_pipeline():
    t = [{'id': 'e', 'name': 'events', 'sel': ['type']}, {'id': 'c', 'name': 'customers', 'sel': ['name']}]
    out = qm.generate(model(tables=t, joins=[{'a': ['e', 'cid'], 'b': ['c', '_id'], 'type': 'LEFT'}]), 'mongo')
    from dataviz.analysis import classify
    got = classify.classify_mongo(out)[0]
    assert got['kind'] == 'read' and got['command']['args'][0][0]['$lookup']['from'] == 'customers'
