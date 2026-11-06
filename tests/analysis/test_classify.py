"""The safety gate: only a proven read runs without a confirmation."""

import pytest

from dataviz.analysis import classify as c


def kinds(sql, dialect='tsql', temps=()):
    return [s['kind'] for s in c.classify(sql, dialect, temps)]


@pytest.mark.parametrize('sql, dialect', [
    ('SELECT 1', 'tsql'),
    ('SELECT TOP (10) * FROM dbo.Orders', 'tsql'),
    ("SELECT a, CASE WHEN b > 1 THEN 'x' ELSE 'y' END FROM t", 'tsql'),
    ('SELECT ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) FROM t', 'tsql'),
    ('WITH x AS (SELECT 1 AS a) SELECT * FROM x', 'postgres'),
    ('SELECT 1 UNION ALL SELECT 2', 'postgres'),
    ("SELECT * FROM OPENQUERY(ERP, 'SELECT id FROM invoices') AS e", 'tsql'),
    ("SELECT 'DELETE FROM x' AS word", 'tsql'),
    ('SELECT [delete] FROM [update]', 'tsql'),
    ('-- DROP TABLE x\nSELECT 1', 'tsql'),
    ('/* UPDATE y */ SELECT 1', 'tsql'),
])
def test_reads(sql, dialect):
    assert kinds(sql, dialect) == ['read']


@pytest.mark.parametrize('sql, dialect', [
    ("DELETE FROM dbo.Orders WHERE Note LIKE 'TEST%'", 'tsql'),
    ('UPDATE t SET a = 1', 'tsql'),
    ('INSERT INTO t VALUES (1)', 'tsql'),
    ('TRUNCATE TABLE t', 'tsql'),
    ('DROP TABLE t', 'tsql'),
    ('ALTER TABLE t ADD c INT', 'tsql'),
    ('CREATE TABLE t (a INT)', 'tsql'),
    ('MERGE a USING b ON a.i = b.i WHEN MATCHED THEN UPDATE SET a.x = 1;', 'tsql'),
    ('EXEC sp_who', 'tsql'),
    ('EXECUTE dbo.DoThings 1', 'tsql'),
    ("SELECT * INTO dbo.Copy FROM dbo.Orders", 'tsql'),
    ('WITH d AS (DELETE FROM x RETURNING *) SELECT * FROM d', 'postgres'),
    ('SELECT * FROM t FOR UPDATE', 'postgres'),
    ("SELECT * FROM OPENQUERY(ERP, 'DELETE FROM invoices')", 'tsql'),
    ("SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'SELECT 1')", 'tsql'),
    ('GRANT SELECT ON t TO me', 'tsql'),
    ('SELEC broken FROM', 'tsql'),
    ('this is not sql', 'tsql'),
    ('SET NOCOUNT ON', 'tsql'),
])
def test_anything_else_is_a_write(sql, dialect):
    assert kinds(sql, dialect) == ['write']


def test_a_write_hidden_after_a_read_is_found_and_only_it_confirms():
    st = c.classify('SELECT 1; DELETE FROM t;', 'tsql')
    assert [s['kind'] for s in st] == ['read', 'write']
    assert [s['op'] for s in c.needs_confirm(st)] == ['DELETE']


def test_go_separates_batches_but_not_inside_text():
    assert kinds('SELECT 1\nGO\nSELECT 2') == ['read', 'read']
    assert len(c.split("SELECT 'a\nGO\nb'")) == 1


def test_split_keeps_semicolons_in_quotes_and_comments():
    assert len(c.split("SELECT ';' ; -- a;b\nSELECT [x;y] ; /* ; */ SELECT 1")) == 3
    assert c.split('  ; ;  -- only a comment\n') == []


def test_postgres_dollar_quoting():
    assert len(c.split('SELECT $$a;b$$; SELECT 2')) == 2


@pytest.mark.parametrize('sql, dialect', [
    ('SELECT * INTO #recent FROM dbo.Orders', 'tsql'),
    ('CREATE TABLE #t (a INT)', 'tsql'),
    ('CREATE TEMP TABLE r AS SELECT 1 AS a', 'postgres'),
    ('INSERT INTO #t VALUES (1)', 'tsql'),
    ('DROP TABLE #t', 'tsql'),
])
def test_temp_statements(sql, dialect):
    assert kinds(sql, dialect) == ['temp']


def test_a_session_temp_table_by_name():
    assert kinds('DELETE FROM recent', 'postgres') == ['write']
    assert kinds('DELETE FROM recent', 'postgres', temps=['recent']) == ['temp']


def test_a_write_into_a_real_table_from_a_temp_is_a_write():
    assert kinds('INSERT INTO dbo.Orders SELECT * FROM #t') == ['write']


def test_tables_are_reported():
    assert c.classify('SELECT * FROM dbo.a JOIN b ON 1 = 1')[0]['tables'] == ['dbo.a', 'b']


# ---------------------------------------------------------------- MongoDB
@pytest.mark.parametrize('text', [
    'db.events.find({type: "buy"})',
    "db.events.find({type: 'buy'}, {_id: 0}).limit(5).sort({ts: -1})",
    'db.events.aggregate([{ $group: { _id: "$type", n: { $sum: 1 } } },])',
    'db.getCollection("my.events").countDocuments({})',
    "db.events.distinct('type')",
])
def test_mongo_reads(text):
    assert c.classify_mongo(text)[0]['kind'] == 'read'


@pytest.mark.parametrize('text, why', [
    ('db.a.aggregate([{ $out: "b" }])', '$out'),
    ('db.a.aggregate([{ $merge: { into: "b" } }])', '$merge'),
    ('db.a.find({ $where: "this.a > 1" })', '$where'),
    ('db.a.aggregate([{ $addFields: { x: { $function: { body: "f", args: [], lang: "js" } } } }])', '$function'),
    ('db.a.deleteMany({})', 'deleteMany'),
    ('db.a.insertOne({a: 1})', 'insertOne'),
    ('db.a.drop()', 'drop'),
    ('db.dropDatabase()', 'parsed'),
    ('show dbs', 'parsed'),
    ('db.a.find({a: 1', 'parsed'),
])
def test_mongo_writes(text, why):
    got = c.classify_mongo(text)[0]
    assert got['kind'] == 'write' and why in got['reason']


def test_mongo_command_shape():
    cmd = c.parse_mongo("db.events.find({a: 1}, {b: 1}).limit(5)")
    assert cmd == {'collection': 'events', 'method': 'find', 'args': [{'a': 1}, {'b': 1}], 'modifiers': {'limit': 5}}


def test_quoted_values_keep_their_text():
    assert c.relaxed_json("{a: 'it\\'s', b: \"x:y\", c: [1, 2,],}") == {'a': "it's", 'b': 'x:y', 'c': [1, 2]}
