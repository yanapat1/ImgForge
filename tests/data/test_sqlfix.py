"""check_sql's engine: syntax errors with their place, text written for another database moved to the connection's
syntax, and valid text left exactly as the user wrote it."""

import pytest

from dataviz.data import sqlfix


def test_postgres_text_on_sql_server_is_rewritten():
    got = sqlfix.check("select \"a b\", x from t where x ilike '%a%' limit 10", 'mssql')
    assert got['fixed'] == "SELECT TOP 10 [a b], x FROM t WHERE LOWER(x) LIKE LOWER('%a%')"
    assert not got['ok'] and not got['errors'] and any('TOP 10' in c for c in got['changes'])


def test_sql_server_text_on_postgres_is_rewritten():
    got = sqlfix.check('SELECT TOP 10 [a b], ISNULL(c, 0) FROM dbo.t', 'postgres')
    assert got['fixed'] == 'SELECT "a b", COALESCE(c, 0) FROM dbo.t LIMIT 10'
    assert got['source'] == 'SQL Server (T-SQL)'


@pytest.mark.parametrize('sql, kind', [
    ('select a b, count(*) from t where a != 1 group by a', 'postgres'),       # AS, != and case are not changes
    ('SELECT TOP (5) [x] FROM dbo.t', 'mssql'),
    ('SELECT a::int FROM t', 'postgres'),
])
def test_valid_text_is_left_alone(sql, kind):
    got = sqlfix.check(sql, kind)
    assert got['ok'] and got['fixed'] is None and got['changes'] == []


def test_a_syntax_error_names_its_line_and_column():
    got = sqlfix.check('SELECT 1;\nSELECT a\nFROM t\nWHERE (b = 1', 'postgres')
    assert got['statements'] == 2 and got['fixed'] is None
    assert got['errors'] == [{'stmt': 2, 'line': 4, 'col': 12, 'message': 'Expecting )'}]


def test_only_the_statement_that_needs_it_changes_and_go_stays():
    text = '-- report\nselect   a from t\nGO\nSELECT a FROM t LIMIT 3\nGO\n'
    got = sqlfix.check(text, 'mssql')
    assert got['fixed'] == '-- report\nselect   a from t\nGO\nSELECT TOP 3 a FROM t\nGO\n'
    assert all(c.startswith('statement 2:') for c in got['changes'])


def test_unknown_functions_are_a_warning_not_a_rewrite():
    got = sqlfix.check('SELECT my_udf(a) FROM t', 'postgres')
    assert got['ok'] and got['fixed'] is None and 'MY_UDF' in got['warnings'][0]


def test_a_named_source_syntax_is_used():
    got = sqlfix.check('SELECT a FROM t LIMIT 5, 10', 'mssql', 'mysql')
    assert got['fixed'] and 'OFFSET 5' in got['fixed'] and 'FETCH' in got['fixed']
    with pytest.raises(ValueError):
        sqlfix.check('SELECT 1', 'mssql', 'nosuch')


def test_mongo_is_checked_not_rewritten():
    assert sqlfix.check('db.c.find({a: 1}).limit(3)', 'mongo')['ok']
    bad = sqlfix.check('db.c.find({a: 1}).foo(3)', 'mongo')
    assert not bad['ok'] and 'foo' in bad['errors'][0]['message']


def test_empty_and_too_long_are_refused():
    with pytest.raises(ValueError):
        sqlfix.check('  ', 'postgres')
    with pytest.raises(ValueError):
        sqlfix.check('x' * (sqlfix.MAX_TEXT + 1), 'postgres')
