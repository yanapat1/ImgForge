"""The result spool: rows on disk, paged and summarised without the user's database."""

import datetime as dt
import decimal

import pytest

from dataviz.data import spool


def make(tmp_path, cols, rows, limit=1000, size=2):
    s = spool.Spool(tmp_path / 'r.parquet')
    batches = [rows[i:i + size] for i in range(0, len(rows), size)]
    return s.path, s.write(cols, batches, limit)


def test_write_and_page(tmp_path):
    p, info = make(tmp_path, ['id', 'name'], [[i, f'n{i}'] for i in range(5)])
    assert info['rows'] == 5 and not info['truncated']
    got = spool.page(p, offset=2, limit=2)
    assert got['rows'] == [[2, 'n2'], [3, 'n3']] and got['total'] == 5


def test_row_limit_is_reported(tmp_path):
    _, info = make(tmp_path, ['a'], [[i] for i in range(10)], limit=4)
    assert info['rows'] == 4 and info['truncated']


def test_exact_limit_with_nothing_more_in_the_batch(tmp_path):
    _, info = make(tmp_path, ['a'], [[1], [2]], limit=2, size=2)
    assert info['rows'] == 2


def test_duplicate_and_empty_column_names(tmp_path):
    _, info = make(tmp_path, ['id', 'ID', '', None], [[1, 2, 3, 4]])
    assert info['columns'] == ['id', 'ID_2', 'column3', 'column4']


def test_empty_result_keeps_its_columns(tmp_path):
    p, info = make(tmp_path, ['a', 'b'], [])
    assert info['rows'] == 0 and spool.page(p)['columns'] == ['a', 'b']


def test_all_null_first_then_values(tmp_path):
    p, info = make(tmp_path, ['a'], [[None], [None], [1], [2]], size=2)
    assert info['rows'] == 4


def test_mixed_types_and_driver_values(tmp_path):
    class Odd:
        def __str__(self):
            return 'odd'
    p, _ = make(tmp_path, ['a', 'b', 'c', 'd'], [
        [1, decimal.Decimal('1.50'), dt.datetime(2026, 1, 2, 3, 4), Odd()],
        ['x', decimal.Decimal('2.25'), dt.datetime(2026, 1, 3), Odd()]], size=5)
    rows = spool.page(p)['rows']
    assert rows[0] == ['1', 1.5, '2026-01-02 03:04:00', 'odd'] and rows[1][0] == 'x'


def test_later_batch_of_another_type_does_not_fail(tmp_path):
    p, info = make(tmp_path, ['a'], [[1], ['text']], size=1)
    assert info['rows'] == 2


def test_sort_and_filter_without_new_query(tmp_path):
    p, _ = make(tmp_path, ['id', 'seg'], [[1, 'SME'], [2, None], [3, 'Enterprise'], [4, 'SME']])
    assert [r[0] for r in spool.page(p, sort='id', desc=True)['rows']] == [4, 3, 2, 1]
    assert spool.page(p, sort='seg')['rows'][-1][1] is None
    got = spool.page(p, q='sme')
    assert got['total'] == 2


def test_sort_on_an_unknown_column_is_refused(tmp_path):
    p, _ = make(tmp_path, ['a'], [[1]])
    with pytest.raises(ValueError, match='no column'):
        spool.page(p, sort='a; DROP TABLE x')


def test_quotes_in_a_column_name(tmp_path):
    p, _ = make(tmp_path, ['we"ird'], [[1]])
    assert spool.page(p, sort='we"ird')['rows'] == [[1]]


def test_page_size_is_capped(tmp_path):
    p, _ = make(tmp_path, ['a'], [[i] for i in range(1200)], limit=2000, size=500)
    assert len(spool.page(p, limit=5000)['rows']) == spool.PAGE_MAX


def test_stats(tmp_path):
    p, _ = make(tmp_path, ['n', 's'], [[1, 'a'], [3, 'a'], [None, 'b'], [5, None]])
    st = spool.stats(p)
    assert st['n']['numeric'] and st['n']['nulls'] == 1 and st['n']['mean'] == 3
    assert not st['s']['numeric'] and st['s']['mean'] is None


def test_histogram(tmp_path):
    p, _ = make(tmp_path, ['n', 's'], [[i, 'x' if i % 2 else 'y'] for i in range(10)], size=4)
    h = spool.histogram(p, 'n', bins=5)
    assert h['kind'] == 'num' and sum(h['counts']) == 10
    assert spool.histogram(p, 's')['labels'] == ['x', 'y']


def test_histogram_of_one_value(tmp_path):
    p, _ = make(tmp_path, ['n'], [[7], [7]])
    assert spool.histogram(p, 'n')['counts'] == [2]


def test_duplicates(tmp_path):
    p, _ = make(tmp_path, ['a'], [[1], [1], [2]])
    assert spool.duplicate_rows(p) == 1


def test_a_million_rows_page_quickly(tmp_path):
    import time
    s = spool.Spool(tmp_path / 'big.parquet')
    s.write(['a', 'b'], ([[i, i * 2] for i in range(k, k + 10000)] for k in range(0, 1000000, 10000)), 2000000)
    t = time.time()
    got = spool.page(s.path, offset=900000, limit=100, sort='b', desc=True)
    assert got['total'] == 1000000 and time.time() - t < 3


# ---------------------------------------------------------------- export formats
ROWS = [[1, 1.5, 'plain', dt.date(2026, 1, 2), dt.datetime(2026, 1, 2, 3, 4, 5), decimal.Decimal('2.50'), True],
        [2, None, 'a, "quoted"\nnew line | pipe <b>', None, None, None, False],
        [3, -0.25, 'ünïcødé 中文 =SUM(A1)', dt.date(2026, 12, 31), None, decimal.Decimal('-1'), None]]
COLS = ['id', 'x', 'note', 'day', 'at', 'money', 'ok']


def test_every_export_format_reads_back(tmp_path):
    import csv
    import json
    import zipfile

    import yaml
    p, _ = make(tmp_path, COLS, ROWS)
    out = {f: spool.export(p, f, tmp_path / f'o.{f}') for f in spool.FORMATS if f != 'parquet'}
    rows = list(csv.reader(open(out['csv'], encoding='utf-8', newline='')))
    assert rows[0] == COLS and rows[2][2] == 'a, "quoted"\nnew line | pipe <b>' and rows[2][1] == ''
    assert list(csv.reader(open(out['tsv'], encoding='utf-8', newline=''), delimiter='\t'))[3][2] == 'ünïcødé 中文 =SUM(A1)'
    js = json.load(open(out['json'], encoding='utf-8'))
    assert len(js) == 3 and js[0]['id'] == 1 and js[1]['x'] is None and js[2]['note'].startswith('ünïcødé')
    assert [json.loads(x)['id'] for x in open(out['jsonl'], encoding='utf-8')] == [1, 2, 3]
    y = yaml.safe_load(open(out['yaml'], encoding='utf-8'))
    assert y[0]['day'] == '2026-01-02' and y[0]['money'] == 2.5 and y[1]['note'] == ROWS[1][2] and y[2]['ok'] is None
    md = open(out['md'], encoding='utf-8').read().splitlines()
    assert md[0] == '| ' + ' | '.join(COLS) + ' |' and len(md) == 5 and 'line \\| pipe' in md[3] and '<br>' in md[3]
    page = open(out['html'], encoding='utf-8').read()
    assert page.count('<tr>') == 4 and '&lt;b&gt;' in page and '<b>' not in page
    with zipfile.ZipFile(out['xlsx']) as z:
        sheet = z.read('xl/worksheets/sheet1.xml').decode()
    assert sheet.count('<row ') == 4 and '<f>' not in sheet, 'text that starts with = stays text, never a formula'


def test_export_limits(tmp_path):
    p, _ = make(tmp_path, ['a'], [[i] for i in range(30)], size=10)
    with pytest.raises(ValueError, match='unknown format'):
        spool.export(p, 'pdf', tmp_path / 'o.pdf')
    old = spool.FORMATS
    try:
        spool.FORMATS = {**old, 'xlsx': (*old['xlsx'][:2], 20), 'md': (*old['md'][:2], 10)}
        with pytest.raises(ValueError, match='Excel holds 20 rows'):
            spool.export(p, 'xlsx', tmp_path / 'o.xlsx')
        md = open(spool.export(p, 'md', tmp_path / 'o.md'), encoding='utf-8').read()
        assert md.count('\n| ') == 10 and 'The first 10 of 30 rows' in md
    finally:
        spool.FORMATS = old
    (tmp_path / 'e').mkdir()
    empty, _ = make(tmp_path / 'e', ['a'], [])
    assert open(spool.export(empty, 'yaml', tmp_path / 'e.yaml'), encoding='utf-8').read() == '[]\n'
