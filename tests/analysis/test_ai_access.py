"""Per-table AI access (analysis/aiaccess.py): what the ◆ assistant may change in each table, checked on the parsed
statements. Read only (default) allows reads, CAST and temp tables; Edit rows adds INSERT / UPDATE / DELETE / MERGE;
Change structure adds ALTER / TRUNCATE / DROP TABLE. Permanent new tables, unreadable SQL and other objects are
refused at every level."""

import pytest

from dataviz.analysis import aiaccess, classify
from dataviz.analysis.aiaccess import AccessError


def _gate(sql, levels, kind='postgres', db='shop', temps=()):
    st = classify.classify_mongo(sql) if kind == 'mongo' else classify.classify(sql, kind, temps)
    aiaccess.gate(levels, 'c1', db, st, kind)


def lv(**tables):
    return {aiaccess.key('c1', 'shop', *t.split('.')): level for t, level in tables.items()}


ROWS = lv(**{'public.orders': 'rows'})
SCHEMA = lv(**{'public.orders': 'schema'})


@pytest.mark.parametrize('sql', ['SELECT CAST(total AS numeric(10,2)) FROM orders',
                                 'SELECT o.id::text FROM orders o JOIN items i ON i.order_id = o.id',
                                 'CREATE TEMP TABLE t AS SELECT * FROM orders'])
def test_reads_casts_and_temp_tables_need_nothing(sql):
    _gate(sql, {})


@pytest.mark.parametrize('sql', ["UPDATE orders SET status = 'x' WHERE id = 1", 'DELETE FROM orders WHERE id = 1',
                                 'INSERT INTO orders (id) VALUES (1)',
                                 'WITH x AS (SELECT 1 AS id) INSERT INTO orders (id) SELECT id FROM x',
                                 'UPDATE "Orders" SET status = 1', 'UPDATE public.orders SET status = 1'])
def test_row_changes_need_edit_rows(sql):
    with pytest.raises(AccessError, match='Edit rows'):
        _gate(sql, {})
    _gate(sql, ROWS)
    _gate(sql, SCHEMA)


@pytest.mark.parametrize('sql', ['ALTER TABLE orders ALTER COLUMN total TYPE numeric(12,2)',
                                 'ALTER TABLE orders ADD COLUMN note text', 'ALTER TABLE orders DROP COLUMN note',
                                 'TRUNCATE TABLE orders', 'DROP TABLE orders'])
def test_structure_changes_need_change_structure(sql):
    for levels in ({}, ROWS):
        with pytest.raises(AccessError, match='Change structure'):
            _gate(sql, levels)
    _gate(sql, SCHEMA)


def test_other_tables_and_schemas_stay_read_only():
    with pytest.raises(AccessError, match='sales.orders'):
        _gate('UPDATE sales.orders SET x = 1', ROWS)
    with pytest.raises(AccessError):
        _gate('DELETE FROM items', ROWS)
    with pytest.raises(AccessError):                    # a script: the strictest statement decides
        _gate("UPDATE orders SET x = 1; ALTER TABLE orders ADD COLUMN y int", ROWS)
    with pytest.raises(AccessError):                    # another database of the same connection
        _gate('UPDATE orders SET x = 1', ROWS, db='other')


@pytest.mark.parametrize('sql', ['CREATE TABLE copy AS SELECT * FROM orders', 'CREATE INDEX i ON orders (id)',
                                 'DROP VIEW v', 'GRANT SELECT ON orders TO bob', 'SELECT * INTO copy FROM orders'])
def test_never_allowed(sql):
    with pytest.raises(AccessError):
        _gate(sql, SCHEMA)


def test_mssql_default_schema_and_brackets():
    levels = lv(**{'dbo.Orders': 'rows'})
    _gate('UPDATE [Orders] SET [x] = 1', levels, kind='mssql')
    _gate('UPDATE dbo.orders SET x = 1', levels, kind='mssql')
    with pytest.raises(AccessError, match='cannot read'):         # what the parser cannot read is refused
        _gate('UPDATE TOP (10) dbo.orders SET x = 1', levels, kind='mssql')
    _gate('MERGE INTO dbo.Orders AS t USING src AS s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.x = s.x',
          levels, kind='mssql')
    with pytest.raises(AccessError):
        _gate('ALTER TABLE dbo.Orders ADD y int', levels, kind='mssql')
    _gate('UPDATE #tmp SET x = 1', {}, kind='mssql')


def test_mongo():
    levels = {aiaccess.key('c1', 'shop', '', 'orders'): 'rows'}
    _gate('db.orders.find({})', {}, kind='mongo')
    _gate('db.orders.updateMany({a: 1}, {$set: {b: 2}})', levels, kind='mongo')
    with pytest.raises(AccessError):
        _gate('db.orders.drop()', levels, kind='mongo')
    with pytest.raises(AccessError):
        _gate('db.items.deleteMany({})', levels, kind='mongo')


def test_put_items_and_index_line():
    prefs = {aiaccess.KEY: aiaccess.put({}, 'c1', 'shop', 'public', 'orders', 'rows')}
    prefs = {aiaccess.KEY: aiaccess.put(prefs, 'c1', 'shop', 'public', 'items', 'schema')}
    assert aiaccess.load(prefs) == {('c1', 'shop', 'public', 'orders'): 'rows', ('c1', 'shop', 'public', 'items'): 'schema'}
    line = aiaccess.index_line(aiaccess.items(prefs), 'c1')
    assert 'Change structure: shop.public.items' in line and 'Edit rows: shop.public.orders' in line
    prefs = {aiaccess.KEY: aiaccess.put(prefs, 'c1', 'SHOP', 'Public', 'Orders', 'read')}     # back to the default
    assert list(aiaccess.load(prefs).values()) == ['schema']
    assert 'every table is Read only' in aiaccess.index_line([], 'c1')
    with pytest.raises(AccessError):
        aiaccess.put({}, 'c1', 'shop', 'public', 'orders', 'admin')
    # junk in the blob is ignored
    assert aiaccess.items({aiaccess.KEY: {'items': [{'c': 'c1', 't': 'x', 'level': 'root'}, 'nope', {'t': 'y', 'level': 'rows'}]}}) == []
