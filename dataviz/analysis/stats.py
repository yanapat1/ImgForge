"""
Statistics on a saved result. Everything reads the spool file through DuckDB, so nothing reaches the user's database.

    profile(path)                     one summary per column, with a plain note about what stands out
    test(path, kind, ...)             a statistic test with its effect size and a sentence a non-statistician can read
    pivot(path, row, col, value, agg) a pivot table
    chart(path, x, y, agg, limit)     the numbers behind a chart
    quality(path)                     checks that usually matter before analysis

A test states its effect size next to its p-value: with many rows even a tiny difference is "significant".
"""

import math

import duckdb

from . import spool

AGGS = {'sum': 'SUM', 'avg': 'AVG', 'count': 'COUNT', 'min': 'MIN', 'max': 'MAX'}
MAX_GROUPS = 50
MAX_SAMPLE = 200000          # the tests read at most this many rows
_q = spool._q


def _conn():
    return duckdb.connect(':memory:', config={'threads': 2})


def _names(c, path):
    return spool._names(c, path)


def _check(names, *cols):
    for x in cols:
        if x not in names:
            raise ValueError(f'no column {x!r} in this result')


def _agg(agg):
    if agg not in AGGS:
        raise ValueError(f'agg must be one of {", ".join(AGGS)}')
    return AGGS[agg]


def profile(path):
    st = spool.stats(path)
    with _conn() as c:
        total = c.execute('SELECT count(*) FROM read_parquet(?)', [str(path)]).fetchone()[0]
    out = []
    for name, s in st.items():
        notes = []
        if s['null_pct'] >= 50:
            notes.append(f'{s["null_pct"]:.0f}% empty, weak for analysis')
        elif s['nulls']:
            notes.append(f'{s["nulls"]:,} empty')
        if total and s['distinct'] >= total * 0.95 and not s['nulls']:
            notes.append('almost unique: looks like an ID')
        elif s['distinct'] == 1:
            notes.append('one value only')
        out.append({'name': name, 'type': s['type'], 'numeric': s['numeric'], 'nulls': s['nulls'], 'null_pct': s['null_pct'],
                    'distinct': s['distinct'], 'min': spool._json(s['min']), 'max': spool._json(s['max']), 'mean': s['mean'],
                    'note': '; '.join(notes), 'hist': spool.histogram(path, name, 16)})
    return {'rows': total, 'columns': out}


def _groups(c, path, value, group):
    """{group label: numbers}, largest groups first."""
    rows = c.execute(f'SELECT CAST({_q(group)} AS VARCHAR), {_q(value)}::DOUBLE FROM read_parquet(?) '
                     f'WHERE {_q(group)} IS NOT NULL AND {_q(value)} IS NOT NULL LIMIT {MAX_SAMPLE}', [str(path)]).fetchall()
    out = {}
    for g, v in rows:
        out.setdefault(g, []).append(v)
    return dict(sorted(out.items(), key=lambda kv: -len(kv[1])))


def _p(p):
    return 'p < 0.001' if p < 0.001 else f'p = {p:.3f}'


def _size(d, small, medium, large):
    d = abs(d)
    return 'very small' if d < small else 'small' if d < medium else 'medium' if d < large else 'large'


def _two(path, value, group, a, b, kind):
    import numpy as np
    from scipy import stats as sp
    with _conn() as c:
        _check(_names(c, path), value, group)
        groups = _groups(c, path, value, group)
    if a is None or b is None:
        if len(groups) < 2:
            raise ValueError(f'{group} needs at least two groups')
        a, b = list(groups)[:2]
    x, y = np.array(groups.get(str(a), [])), np.array(groups.get(str(b), []))
    if len(x) < 2 or len(y) < 2:
        raise ValueError('each group needs at least two values')
    if kind == 'mannwhitney':
        r = sp.mannwhitneyu(x, y, alternative='two-sided')
        effect = 2 * r.statistic / (len(x) * len(y)) - 1
        stat = {'name': 'U', 'value': float(r.statistic)}
        size = _size(effect, 0.1, 0.3, 0.5)
        size_row = {'name': 'rank correlation', 'value': round(float(effect), 3), 'label': size}
    else:
        r = sp.ttest_ind(x, y, equal_var=False)
        pooled = math.sqrt(((len(x) - 1) * x.var(ddof=1) + (len(y) - 1) * y.var(ddof=1)) / (len(x) + len(y) - 2)) or 1.0
        effect = (x.mean() - y.mean()) / pooled
        stat = {'name': 't', 'value': float(r.statistic)}
        size = _size(effect, 0.2, 0.5, 0.8)
        size_row = {'name': "Cohen's d", 'value': round(float(effect), 3), 'label': size}
    p = float(r.pvalue)
    higher, lower = (a, b) if x.mean() >= y.mean() else (b, a)
    diff = abs(float(x.mean() - y.mean()))
    if p >= 0.05:
        text = f'The average {value} of {a} and {b} is not clearly different: a gap like this could easily be chance ({_p(p)}).'
    else:
        text = f'{higher} has a higher average {value} than {lower}, by about {diff:,.2f}. This is unlikely to be chance ({_p(p)}), and the difference is {size}.'
    return {'test': 'Mann-Whitney U' if kind == 'mannwhitney' else "Welch's t-test", 'stat': stat, 'p': p, 'effect': size_row,
            'groups': [{'name': str(g), 'n': int(len(v)), 'mean': float(np.mean(v))} for g, v in ((a, x), (b, y))], 'text': text}


def _anova(path, value, group):
    import numpy as np
    from scipy import stats as sp
    with _conn() as c:
        _check(_names(c, path), value, group)
        groups = _groups(c, path, value, group)
    use = {k: np.array(v) for k, v in list(groups.items())[:MAX_GROUPS] if len(v) >= 2}
    if len(use) < 2:
        raise ValueError(f'{group} needs at least two groups with two values each')
    r = sp.f_oneway(*use.values())
    every = np.concatenate(list(use.values()))
    between = sum(len(v) * (v.mean() - every.mean()) ** 2 for v in use.values())
    eta = float(between / (((every - every.mean()) ** 2).sum() or 1))
    p, size = float(r.pvalue), _size(eta, 0.01, 0.06, 0.14)
    top = max(use, key=lambda k: use[k].mean())
    text = (f'The average {value} differs across {group} ({_p(p)}), and the effect is {size}. {top} is highest.' if p < 0.05
            else f'The average {value} does not clearly differ across {group} ({_p(p)}).')
    return {'test': 'One-way ANOVA', 'stat': {'name': 'F', 'value': float(r.statistic)}, 'p': p,
            'effect': {'name': 'eta squared', 'value': round(eta, 4), 'label': size},
            'groups': [{'name': k, 'n': int(len(v)), 'mean': float(v.mean())} for k, v in use.items()], 'text': text}


def _chi2(path, a, b):
    import numpy as np
    from scipy import stats as sp
    with _conn() as c:
        _check(_names(c, path), a, b)
        rows = c.execute(f'SELECT CAST({_q(a)} AS VARCHAR), CAST({_q(b)} AS VARCHAR), count(*) FROM read_parquet(?) '
                         f'WHERE {_q(a)} IS NOT NULL AND {_q(b)} IS NOT NULL GROUP BY 1, 2', [str(path)]).fetchall()
    rs, cs = sorted({r[0] for r in rows}), sorted({r[1] for r in rows})
    if len(rs) < 2 or len(cs) < 2:
        raise ValueError('both columns need at least two values')
    if len(rs) > MAX_GROUPS or len(cs) > MAX_GROUPS:
        raise ValueError(f'too many different values for this test (over {MAX_GROUPS})')
    m = np.zeros((len(rs), len(cs)))
    for x, y, n in rows:
        m[rs.index(x), cs.index(y)] = n
    r = sp.chi2_contingency(m)
    v = math.sqrt(r.statistic / (m.sum() * (min(m.shape) - 1)))
    p, size = float(r.pvalue), _size(v, 0.1, 0.3, 0.5)
    low = bool((r.expected_freq < 5).mean() > 0.2)
    text = (f'{a} and {b} are related ({_p(p)}), and the relationship is {size}.' if p < 0.05
            else f'{a} and {b} do not look related ({_p(p)}).')
    if p < 0.05 and size == 'very small':
        text += ' With this many rows, even tiny differences reach significance, so look at the effect size.'
    if low:
        text += ' Some groups are too small, so treat this result with care.'
    return {'test': 'Chi-square', 'stat': {'name': 'χ²', 'value': float(r.statistic), 'df': int(r.dof)}, 'p': p,
            'effect': {'name': "Cramér's V", 'value': round(v, 4), 'label': size}, 'text': text}


def _corr(path, x, y, method):
    from scipy import stats as sp
    with _conn() as c:
        _check(_names(c, path), x, y)
        rows = c.execute(f'SELECT {_q(x)}::DOUBLE, {_q(y)}::DOUBLE FROM read_parquet(?) WHERE {_q(x)} IS NOT NULL AND {_q(y)} IS NOT NULL '
                         f'LIMIT {MAX_SAMPLE}', [str(path)]).fetchall()
    if len(rows) < 3:
        raise ValueError('a correlation needs at least three rows')
    a, b = [r[0] for r in rows], [r[1] for r in rows]
    if len(set(a)) < 2 or len(set(b)) < 2:
        raise ValueError('a column with one value has no correlation')
    r = sp.spearmanr(a, b) if method == 'spearman' else sp.pearsonr(a, b)
    v, p = float(r.statistic), float(r.pvalue)
    size = _size(v, 0.1, 0.3, 0.5)
    way = 'rises' if v > 0 else 'falls'
    text = f'As {x} rises, {y} {way}. The link is {size} ({_p(p)}). A correlation does not show that one causes the other.' if p < 0.05 \
        else f'{x} and {y} do not show a clear link ({_p(p)}).'
    return {'test': 'Spearman correlation' if method == 'spearman' else 'Pearson correlation', 'stat': {'name': 'r', 'value': v}, 'p': p,
            'effect': {'name': 'strength', 'value': round(v, 4), 'label': size}, 'n': len(rows), 'text': text}


def _normal(path, value):
    import numpy as np
    from scipy import stats as sp
    with _conn() as c:
        _check(_names(c, path), value)
        rows = c.execute(f'SELECT {_q(value)}::DOUBLE FROM read_parquet(?) WHERE {_q(value)} IS NOT NULL USING SAMPLE 5000', [str(path)]).fetchall()
    x = np.array([r[0] for r in rows])
    if len(x) < 8:
        raise ValueError('this test needs at least eight values')
    r = sp.normaltest(x)
    p, skew = float(r.pvalue), float(sp.skew(x))
    side = 'right' if skew > 0 else 'left'
    text = (f'{value} is not bell-shaped ({_p(p)}) and leans {side}. Prefer Mann-Whitney over a t-test for small groups.' if p < 0.05
            else f'{value} looks roughly bell-shaped ({_p(p)}).')
    return {'test': "D'Agostino normality", 'stat': {'name': 'k²', 'value': float(r.statistic)}, 'p': p,
            'effect': {'name': 'skew', 'value': round(skew, 3), 'label': ''}, 'text': text}


TESTS = ('ttest', 'mannwhitney', 'anova', 'chi2', 'pearson', 'spearman', 'normal')


def test(path, kind, a=None, b=None, group_a=None, group_b=None):
    """kind: ttest / mannwhitney (a value, b group), anova (a value, b group), chi2 (two categories), pearson / spearman, normal (a)."""
    if kind in ('ttest', 'mannwhitney'):
        return _two(path, a, b, group_a, group_b, kind)
    if kind == 'anova':
        return _anova(path, a, b)
    if kind == 'chi2':
        return _chi2(path, a, b)
    if kind in ('pearson', 'spearman'):
        return _corr(path, a, b, kind)
    if kind == 'normal':
        return _normal(path, a)
    raise ValueError(f'kind must be one of {", ".join(TESTS)}')


def suggest(path):
    """Which tests fit the columns of this result."""
    st = spool.stats(path)
    nums = [k for k, v in st.items() if v['numeric'] and v['distinct'] > 10]
    cats = [k for k, v in st.items() if 2 <= v['distinct'] <= 20]
    out = []
    if nums and cats:
        two = next((c for c in cats if st[c]['distinct'] == 2), None)
        if two:
            out.append({'kind': 'ttest', 'a': nums[0], 'b': two, 'label': f'Does {nums[0]} differ between the two {two} groups?'})
        out.append({'kind': 'anova', 'a': nums[0], 'b': cats[0], 'label': f'Does {nums[0]} differ across {cats[0]}?'})
    if len(cats) >= 2:
        out.append({'kind': 'chi2', 'a': cats[0], 'b': cats[1], 'label': f'Are {cats[0]} and {cats[1]} related?'})
    if len(nums) >= 2:
        out.append({'kind': 'pearson', 'a': nums[0], 'b': nums[1], 'label': f'Do {nums[0]} and {nums[1]} move together?'})
    if nums:
        out.append({'kind': 'normal', 'a': nums[0], 'b': None, 'label': f'Is {nums[0]} bell-shaped?'})
    return out


def pivot(path, row, col, value, agg='sum', limit=40):
    fn = _agg(agg)
    with _conn() as c:
        _check(_names(c, path), row, col)
        v = f'{fn}({_q(value)})' if value else 'COUNT(*)'
        if value:
            _check(_names(c, path), value)
        cols = [r[0] for r in c.execute(f'SELECT CAST({_q(col)} AS VARCHAR) v FROM read_parquet(?) WHERE {_q(col)} IS NOT NULL '
                                        f'GROUP BY v ORDER BY count(*) DESC, v LIMIT {int(limit)}', [str(path)]).fetchall()]
        rows = c.execute(f'SELECT CAST({_q(row)} AS VARCHAR), CAST({_q(col)} AS VARCHAR), {v} FROM read_parquet(?) GROUP BY 1, 2',
                         [str(path)]).fetchall()
    table, order = {}, []
    for r, k, n in rows:
        if r not in table:
            table[r] = {}
            order.append(r)
        table[r][k] = spool._json(n)
    return {'columns': cols, 'rows': [{'name': r, 'cells': [table[r].get(k) for k in cols]} for r in sorted(order, key=str)[:200]]}


def chart(path, x, y=None, agg='sum', limit=30):
    """{labels, values}: y summed (or counted) per x, in x's natural order when x is a date or number."""
    fn = _agg(agg)
    with _conn() as c:
        names = _names(c, path)
        _check(names, x)
        if y:
            _check(names, y)
        v = f'{fn}({_q(y)})' if y else 'COUNT(*)'
        rows = c.execute(f'SELECT {_q(x)} AS k, {v} AS v FROM read_parquet(?) WHERE {_q(x)} IS NOT NULL GROUP BY k ORDER BY k LIMIT {max(1, min(int(limit), 500))}',
                         [str(path)]).fetchall()
    return {'labels': [str(spool._json(r[0])) for r in rows], 'values': [float(r[1] or 0) for r in rows]}


def quality(path):
    st = spool.stats(path)
    with _conn() as c:
        total = c.execute('SELECT count(*) FROM read_parquet(?)', [str(path)]).fetchone()[0]
    dup = spool.duplicate_rows(path)
    out = [{'rule': 'No duplicate rows', 'ok': dup == 0, 'rows': dup}]
    for name, s in st.items():
        if s['nulls']:
            out.append({'rule': f'{name} has no empty values', 'ok': False, 'rows': s['nulls']})
    return {'rows': total, 'checks': out}
