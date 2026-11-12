"""Statistics on a saved result, checked against numbers worked out independently."""

import math
import random

import pytest

from dataviz.analysis import spool, stats


@pytest.fixture
def data(tmp_path):
    r = random.Random(7)
    rows = []
    for i in range(600):
        seg = 'Enterprise' if i % 3 == 0 else 'Consumer'
        status = 'open' if r.random() < (0.8 if seg == 'Enterprise' else 0.2) else 'closed'
        rows.append([i, seg, status, r.gauss(100 if seg == 'Enterprise' else 50, 10), i * 2 + r.gauss(0, 1), r.random()])
    p = tmp_path / 'd.parquet'
    spool.Spool(p).write(['id', 'seg', 'status', 'total', 'twice', 'noise'], [rows], 10000)
    return p


def test_t_test_matches_the_formula(tmp_path):
    p = tmp_path / 't.parquet'
    spool.Spool(p).write(['v', 'g'], [[[x, 'a'] for x in (1.0, 2.0, 3.0, 4.0)] + [[x, 'b'] for x in (2.0, 4.0, 6.0, 8.0)]], 100)
    got = stats.test(p, 'ttest', 'v', 'g')
    va, vb = 5 / 3, 20 / 3                       # sample variances
    t = (2.5 - 5.0) / math.sqrt(va / 4 + vb / 4)
    assert got['stat']['value'] == pytest.approx(t)
    assert got['effect']['value'] == pytest.approx(-2.5 / math.sqrt((va + vb) / 2), abs=0.001)


def test_two_groups_differ_and_the_sentence_says_so(data):
    got = stats.test(data, 'ttest', 'total', 'seg')
    assert got['p'] < 0.001 and got['effect']['label'] == 'large'
    assert 'Enterprise has a higher average total' in got['text']


def test_no_difference_is_said_plainly(tmp_path):
    p = tmp_path / 'n.parquet'
    r = random.Random(1)
    spool.Spool(p).write(['v', 'g'], [[[r.gauss(0, 1), 'ab'[i % 2]] for i in range(200)]], 1000)
    got = stats.test(p, 'ttest', 'v', 'g')
    assert got['p'] > 0.05 and 'not clearly different' in got['text']


def test_mann_whitney(data):
    assert stats.test(data, 'mannwhitney', 'total', 'seg')['p'] < 0.001


def test_anova(data):
    got = stats.test(data, 'anova', 'total', 'seg')
    assert got['p'] < 0.001 and got['effect']['label'] == 'large'


def test_chi_square_matches_the_formula(tmp_path):
    p = tmp_path / 'c.parquet'
    rows = [['a', 'x']] * 10 + [['a', 'y']] * 20 + [['b', 'x']] * 30 + [['b', 'y']] * 40
    spool.Spool(p).write(['r', 'c'], [rows], 1000)
    got = stats.test(p, 'chi2', 'r', 'c')
    assert got['stat']['df'] == 1 and got['stat']['value'] == pytest.approx(0.44642857, abs=1e-4)      # Yates-corrected 2x2


def test_a_tiny_effect_is_called_tiny(tmp_path):
    p = tmp_path / 'b.parquet'
    rows = [['a', 'x']] * 26000 + [['a', 'y']] * 24000 + [['b', 'x']] * 25000 + [['b', 'y']] * 25000
    spool.Spool(p).write(['r', 'c'], [rows], 200000)
    got = stats.test(p, 'chi2', 'r', 'c')
    assert got['p'] < 0.001 and got['effect']['label'] == 'very small' and 'look at the effect size' in got['text']


def test_correlation(data):
    got = stats.test(data, 'pearson', 'id', 'twice')
    assert got['stat']['value'] > 0.99 and 'does not show that one causes' in got['text']
    assert stats.test(data, 'spearman', 'id', 'twice')['stat']['value'] > 0.99


def test_normality(data):
    assert 'bell-shaped' in stats.test(data, 'normal', 'noise')['text']


@pytest.mark.parametrize('call, why', [
    (lambda d: stats.test(d, 'ttest', 'nope', 'seg'), 'no column'),
    (lambda d: stats.test(d, 'nope', 'a', 'b'), 'kind must be'),
    (lambda d: stats.test(d, 'ttest', 'total', 'seg', 'x', 'y'), 'at least two values'),
    (lambda d: stats.chart(d, 'seg', 'total', 'drop'), 'agg must be'),
])
def test_bad_input_is_explained(data, call, why):
    with pytest.raises(ValueError, match=why):
        call(data)


def test_a_constant_column_has_no_correlation(tmp_path):
    p = tmp_path / 'k.parquet'
    spool.Spool(p).write(['a', 'b'], [[[1, i] for i in range(5)]], 10)
    with pytest.raises(ValueError, match='one value'):
        stats.test(p, 'pearson', 'a', 'b')


def test_suggestions_fit_the_columns(data):
    kinds = {s['kind'] for s in stats.suggest(data)}
    assert {'ttest', 'chi2', 'pearson', 'normal'} <= kinds


def test_profile_notes(data):
    got = {c['name']: c for c in stats.profile(data)['columns']}
    assert 'looks like an ID' in got['id']['note'] and got['seg']['distinct'] == 2


def test_chart_and_pivot(data):
    c = stats.chart(data, 'seg', 'id', 'count')
    assert dict(zip(c['labels'], c['values'])) == {'Consumer': 400, 'Enterprise': 200}
    assert stats.chart(data, 'seg')['values'] == [400, 200]
    # top groups by value: the biggest first (desc), the smallest first (asc), with the limit applied after the sort
    assert stats.chart(data, 'seg', order='asc')['labels'] == ['Enterprise', 'Consumer']
    assert stats.chart(data, 'seg', order='desc', limit=1) == {'labels': ['Consumer'], 'values': [400]}
    with pytest.raises(ValueError, match='order must be'):
        stats.chart(data, 'seg', order='drop; --')
    # a KPI over the whole result, whatever the number of groups
    assert stats.total(data) == {'value': 600}
    assert stats.total(data, 'id', 'count') == {'value': 600}
    assert stats.total(data, 'id', 'min')['value'] <= stats.total(data, 'id', 'max')['value']
    p = stats.pivot(data, 'seg', 'status', None, 'count')
    assert sorted(p['columns']) == ['closed', 'open'] and len(p['rows']) == 2


def test_quality(data):
    q = stats.quality(data)
    assert q['checks'][0] == {'rule': 'No duplicate rows', 'ok': True, 'rows': 0}


def test_profile_distinct_is_exact(tmp_path):
    # SUMMARIZE's approx_unique said 507 distinct for 500 rows and 495 for a unique id
    p = tmp_path / 'd.parquet'
    spool.Spool(p).write(['id', 'total', 'note'], [[[i, i * 1.5, 'even' if i % 2 == 0 else None] for i in range(1, 501)]], 1000)
    got = {c['name']: c for c in stats.profile(p)['columns']}
    assert (got['id']['distinct'], got['total']['distinct'], got['note']['distinct']) == (500, 500, 1)
    assert 'looks like an ID' in got['id']['note'] and 'one value only' in got['note']['note']
