"""
Forecast a measure over time, from a saved result. Like stats.py it reads only the spool file through DuckDB.

    forecast(path, date, value=None, agg='sum', grain='auto', horizon=None, group=None)
    summary(answer)                   what the assistant reads: the answer without the history

DuckDB groups the rows into one value per time step (hour, day, week, month, quarter, year) and fills the steps that
have no rows. Then a few classic models compete on the last part of the history, held back (a backtest), and the most
accurate one there is fitted on the whole series:

    naive              the last value again
    seasonal naive     the value one season ago (last Monday for a Monday): the baseline the answer is compared with
    holt               exponential smoothing with a damped trend
    holt-winters       the same plus a repeating pattern (weekly for days, yearly for months, ...)

The models are written here on numpy and scipy, already in the analyst install, rather than taken from statsmodels,
which would add pandas and patsy to it. For one series of at most MAX_FIT steps they take milliseconds.

The answer says how far to trust it: an 80% and a 95% range that widen with the horizon, the backtest accuracy against
the naive guess, warnings for short or gappy history or a recent change of level, and the steps that sat far from what
the model expected (anomalies), found with a robust z-score on its one-step errors.
"""

import math

import numpy as np

from . import spool, stats

GRAINS = ('hour', 'day', 'week', 'month', 'quarter', 'year')
SEASON = {'hour': 24, 'day': 7, 'week': 52, 'month': 12, 'quarter': 4, 'year': 1}
HORIZON = {'hour': 24, 'day': 14, 'week': 8, 'month': 6, 'quarter': 4, 'year': 3}
PATTERN = {'hour': 'daily', 'day': 'weekly', 'week': 'yearly', 'month': 'yearly', 'quarter': 'yearly', 'year': ''}
MIN_POINTS = 8
MAX_FIT = 1000            # the most recent steps a model learns from
MAX_HORIZON = 120
MAX_GROUPS = 12
Z80, Z95 = 1.2816, 1.96
ANOMALY_Z = 4.0
_q = spool._q


# ---------------------------------------------------------------- the series
def _auto_grain(c, path, date):
    """The step that gives a useful number of points for the span of the dates."""
    lo, hi = c.execute(f'SELECT min(TRY_CAST({_q(date)} AS TIMESTAMP)), max(TRY_CAST({_q(date)} AS TIMESTAMP)) '
                       'FROM read_parquet(?)', [str(path)]).fetchone()
    days = (hi - lo).total_seconds() / 86400 if lo is not None else 0
    return 'month' if days >= 3 * 365 else 'week' if days >= 2 * 365 else 'day' if days >= 4 else 'hour'


def _series(c, path, date, value, agg, grain, group=None, label=None):
    """[(step start, value or None)] from the first to the last step, every step present, the last MAX_FIT of them;
    whether older steps were left out; and the dates of a first or last step that holds too few distinct dates to be
    complete (data that stops on a Wednesday makes the last week look like a fall)."""
    d = f'TRY_CAST({_q(date)} AS TIMESTAMP)'
    where = f'{d} IS NOT NULL' + (f' AND CAST({_q(group)} AS VARCHAR) = ?' if group else '')
    v = ('COUNT(*)' if not value else f'COUNT({_q(value)})' if agg == 'count'
         else f'{stats._agg(agg)}(TRY_CAST({_q(value)} AS DOUBLE))')
    rows = c.execute(f"""
        WITH s AS (SELECT date_trunc('{grain}', {d}) t, {v} v, count(DISTINCT {d}) nd FROM read_parquet(?) WHERE {where} GROUP BY 1),
        r AS (SELECT unnest(generate_series((SELECT min(t) FROM s), (SELECT max(t) FROM s), INTERVAL 1 {grain})) t)
        SELECT r.t, s.v, s.nd FROM r LEFT JOIN s USING (t) ORDER BY r.t DESC LIMIT {MAX_FIT + 1}""",
                     [str(path)] + ([label] if group else [])).fetchall()
    rows.reverse()
    cut = len(rows) > MAX_FIT
    rows = rows[-MAX_FIT:]
    partial = []
    if (not value or agg in ('sum', 'count')) and len(rows) > MIN_POINTS:
        usual = float(np.median([r[2] for r in rows if r[2]]))
        if usual >= 3:
            if rows[-1][2] < 0.88 * usual:
                partial.append(rows.pop()[0])
            if rows[0][2] < 0.88 * usual:
                partial.insert(0, rows.pop(0)[0])
    return [(t, v) for t, v, _ in rows], cut, partial


def _fill(vals, zero):
    """Steps with no rows: 0 for a sum or a count, else the straight line between their neighbours."""
    y = np.array([np.nan if v is None else float(v) for v in vals])
    gaps = int(np.isnan(y).sum())
    if gaps:
        ok = ~np.isnan(y)
        y = np.where(ok, y, 0.0) if zero else np.interp(np.arange(len(y)), np.flatnonzero(ok), y[ok])
    return y, gaps


def _future(c, last, grain, h):
    return [r[0] for r in c.execute(f'SELECT unnest(generate_series(?::TIMESTAMP + INTERVAL 1 {grain}, '
                                    f'?::TIMESTAMP + INTERVAL {int(h)} {grain}, INTERVAL 1 {grain}))', [last, last]).fetchall()]


# ---------------------------------------------------------------- the models (on a series scaled to mean 0, sd 1)
def _ets(y, m, alpha, beta, gamma, phi, season, l0, b0, s0):
    """One pass of additive exponential smoothing with a damped trend: the one-step errors and the final state.
    Plain floats, not numpy: this loop runs a few hundred times per fit."""
    level, slope, seas = l0, b0, list(s0)
    err = [0.0] * len(y)
    for t, yt in enumerate(y):
        s = seas[t % m] if season else 0.0
        db = phi * slope
        e = yt - (level + db + s)
        err[t] = e
        level = level + db + alpha * e
        slope = db + alpha * beta * e
        if season:
            seas[t % m] = s + gamma * (1 - alpha) * e
    return err, level, slope, seas


def _fit_ets(y, m, season):
    """Weights by least squares of the one-step errors. The trend and pattern weights are fractions of what alpha
    leaves, so every point the optimiser tries is a valid model."""
    from scipy.optimize import minimize          # loaded on first use, like stats.py: the app starts without scipy
    mm = m if season else 1
    if season:
        first = float(y[:m].mean())
        b0 = float(y[m:2 * m].mean() - first) / m
        l0, s0 = first - b0 * (m - 1) / 2, list(y[:m] - first)
    else:
        k = min(len(y), 4)
        l0, b0, s0 = float(y[0]), float(y[k - 1] - y[0]) / max(k - 1, 1), [0.0]
    yl = y.tolist()

    def sse(p):
        e = _ets(yl, mm, p[0], p[1], p[2], p[3], season, l0, b0, s0)[0]
        return math.fsum(x * x for x in e)

    got = minimize(sse, [0.3, 0.1, 0.1 if season else 0.0, 0.95], method='L-BFGS-B',
                   bounds=[(0.01, 0.99), (0.0, 1.0), (0.0, 1.0 if season else 0.0), (0.8, 0.98)], options={'maxiter': 80})
    a, b, g, f = (float(x) for x in got.x)
    err, level, slope, seas = _ets(yl, mm, a, b, g, f, season, l0, b0, s0)
    return {'kind': 'ets', 'season': season, 'm': mm, 'a': a, 'b': b, 'g': g, 'f': f, 'level': level, 'slope': slope,
            'seas': seas, 'n': len(yl), 'err': np.array(err), 'warm': mm + 1 if season else 2}


def _predict_ets(st, h):
    """Point forecasts and their standard deviation: the variance of additive ETS grows by c_j^2 a step, with
    c_j = alpha(1 + beta phi_j) + gamma [j is a whole season] (Hyndman & Athanasopoulos, FPP3 ch. 8.7)."""
    a, b, g, f, m, n = st['a'], st['b'], st['g'], st['f'], st['m'], st['n']
    e = st['err'][st['warm']:] if len(st['err']) > st['warm'] + 4 else st['err']
    sigma2 = float(np.dot(e, e)) / max(len(e) - (4 if st['season'] else 3), 1)
    out, var, damp, acc = [], [], 0.0, 0.0
    for j in range(1, h + 1):
        damp += f ** j
        out.append(st['level'] + damp * st['slope'] + (st['seas'][(n + j - 1) % m] if st['season'] else 0.0))
        var.append(sigma2 * (1 + acc))
        c = a * (1 + b * damp) + (g * (1 - a) if st['season'] and j % m == 0 else 0.0)
        acc += c * c
    return np.array(out), np.sqrt(var)


def _fit_naive(y, m):
    return {'kind': 'naive', 'm': m, 'y': y, 'err': np.concatenate([np.zeros(m), y[m:] - y[:-m]]), 'warm': m + 1 if m > 1 else 1}


def _predict_naive(st, h):
    y, m = st['y'], st['m']
    e = st['err'][m:]
    sigma = math.sqrt(float(np.dot(e, e)) / max(len(e) - 1, 1))
    out = np.array([y[len(y) - m + j % m] for j in range(h)])
    return out, sigma * np.sqrt(np.array([j // m + 1 for j in range(h)], dtype=float))


MODELS = {   # key: (name, fit, needs two seasons of history)
    'naive': ('Last value again', lambda y, m: _fit_naive(y, 1), False),
    'snaive': ('Same as one season ago', lambda y, m: _fit_naive(y, m), True),
    'holt': ('Smoothing with a damped trend', lambda y, m: _fit_ets(y, m, False), False),
    'hw': ('Holt-Winters: trend and a repeating pattern', lambda y, m: _fit_ets(y, m, True), True),
}


def _predict(st, h):
    return _predict_ets(st, h) if st['kind'] == 'ets' else _predict_naive(st, h)


def _backtest(y, m, h, most=3):
    """Rolling-origin backtest: each model learns the history up to a cut and forecasts the h steps after it, at up to
    `most` cuts; the score is the mean absolute error over them. The seasonal models enter only when every training
    part holds two full seasons. scale: the in-sample error of the naive guess, for MASE."""
    n = len(y)
    seasonal = m > 1 and n - h >= 2 * m + 2
    need = 2 * m + 2 if seasonal else MIN_POINTS // 2
    folds = max((k for k in range(1, most + 1) if n - k * h >= need), default=1)
    scores = {}
    for k, (_, fit, needs) in MODELS.items():
        if needs and not seasonal:
            continue
        errs = []
        with np.errstate(all='raise'):
            try:
                for i in range(folds, 0, -1):
                    end = n - (i - 1) * h
                    train, test = y[:end - h], y[end - h:end]
                    errs.append(float(np.mean(np.abs(test - _predict(fit(train, m), h)[0]))))
            except (ValueError, FloatingPointError, ArithmeticError):
                continue
        scores[k] = float(np.mean(errs))
    lag = m if seasonal else 1
    scale = float(np.mean(np.abs(y[lag:] - y[:-lag])))
    return scores, scale, seasonal, folds


# ---------------------------------------------------------------- one series
def _one(c, path, date, value, agg, grain, horizon, group=None, label=None, folds=3):
    """The forecast of one series, in its own units; None when it is too short (only for a group). A group's series is
    backtested at one cut instead of three: a dozen of them still answer in about a second."""
    raw, cut, partial = _series(c, path, date, value, agg, grain, group, label)
    zero = not value or agg in ('sum', 'count')
    y, gaps = _fill([r[1] for r in raw], zero)
    n = len(y)
    if n < MIN_POINTS:
        if group:
            return None
        raise ValueError(f'a forecast needs at least {MIN_POINTS} {grain}s of history; this result has {n}. '
                         'Pick a shorter step or a longer date range')
    mu, sd = float(y.mean()), float(y.std()) or 1.0
    z = (y - mu) / sd                       # the models are additive, so scaling changes nothing but the optimiser's footing
    m = SEASON[grain]
    h_bt = max(1, min(horizon, n // 4))
    scores, scale, seasonal, folds = _backtest(z, m, h_bt, folds)
    if not scores:
        raise ValueError('no model could be fitted to this series')
    best = min(scores, key=scores.get)
    st = MODELS[best][1](z, m)
    yhat, spread = _predict(st, horizon)
    yhat, spread = yhat * sd + mu, spread * sd
    floor = float(y.min()) >= 0            # a measure that never went below zero is not forecast below zero
    band = {}
    for name, q in (('80', Z80), ('95', Z95)):
        lo = yhat - q * spread
        band['lo' + name], band['hi' + name] = (np.maximum(lo, 0.0) if floor else lo), yhat + q * spread
    return {'ts': [r[0] for r in raw], 'y': y, 'cut': cut, 'partial': partial, 'gaps': gaps, 'zero': zero, 'm': m, 'seasonal': seasonal,
            'best': best, 'state': st, 'sd': sd, 'mu': mu, 'err': st['err'] * sd, 'warm': st['warm'],
            'scores': {k: v * sd for k, v in scores.items()}, 'scale': scale * sd, 'h_bt': h_bt, 'folds': folds,
            'yhat': np.maximum(yhat, 0.0) if floor else yhat, **band, 'future': _future(c, raw[-1][0], grain, horizon)}


# ---------------------------------------------------------------- the entry point
def _check_columns(c, path, date, value, group):
    names = stats._names(c, path)
    stats._check(names, *[x for x in (date, value, group) if x])
    if not date:
        raise ValueError('pick the date column')
    if c.execute(f'SELECT count(TRY_CAST({_q(date)} AS TIMESTAMP)) FROM read_parquet(?)', [str(path)]).fetchone()[0] == 0:
        raise ValueError(f'{date} holds no dates')
    if value and c.execute(f'SELECT count(TRY_CAST({_q(value)} AS DOUBLE)) FROM read_parquet(?)', [str(path)]).fetchone()[0] == 0:
        raise ValueError(f'{value} holds no numbers: pick a number column, or count rows')


def forecast(path, date, value=None, agg='sum', grain='auto', horizon=None, group=None):
    agg = agg or 'sum'
    stats._agg(agg)
    with stats._conn() as c:
        _check_columns(c, path, date, value or None, group or None)
        grain = _auto_grain(c, path, date) if grain in (None, '', 'auto') else grain
        if grain not in GRAINS:
            raise ValueError(f'step must be one of auto, {", ".join(GRAINS)}')
        horizon = max(1, min(int(horizon or HORIZON[grain]), MAX_HORIZON))
        r = _one(c, path, date, value or None, agg, grain, horizon)
        groups = _groups(c, path, date, value or None, agg, grain, horizon, group) if group else None
    return _answer(r, date, value or None, agg, grain, horizon, group or None, groups)


def _groups(c, path, date, value, agg, grain, horizon, group):
    """The largest groups, each with its own forecast: which are expected to grow or fall most."""
    labels = [x[0] for x in c.execute(f'SELECT CAST({_q(group)} AS VARCHAR) g FROM read_parquet(?) WHERE {_q(group)} IS NOT NULL '
                                      f'GROUP BY g ORDER BY count(*) DESC, g LIMIT {MAX_GROUPS}', [str(path)]).fetchall()]
    out = []
    for g in labels:
        r = _one(c, path, date, value, agg, grain, horizon, group, g, folds=1)
        if r is None:
            out.append({'name': g, 'model': None, 'next': None, 'ahead': None, 'recent': None, 'change_pct': None,
                        'note': 'too little history'})
            continue
        k = min(horizon, len(r['y']))
        ahead, recent = float(r['yhat'].mean()), float(r['y'][-k:].mean())
        out.append({'name': g, 'model': MODELS[r['best']][0], 'next': float(r['yhat'][0]), 'ahead': ahead, 'recent': recent,
                    'change_pct': round((ahead - recent) / abs(recent) * 100, 1) if recent else None, 'note': ''})
    out.sort(key=lambda x: (x['change_pct'] is None, -(x['change_pct'] or 0)))
    return out


# ---------------------------------------------------------------- words
def _fmt(v):
    a = abs(v)
    return f'{v:,.0f}' if a >= 100 else f'{v:,.1f}' if a >= 10 else f'{v:,.2f}'


def _when(t, grain):
    return f'{t:%Y-%m-%d %H}:00' if grain == 'hour' else f'{t:%Y-%m-%d}' if grain in ('day', 'week') else \
        f'{t:%Y} Q{(t.month - 1) // 3 + 1}' if grain == 'quarter' else f'{t:%Y}' if grain == 'year' else f'{t:%B %Y}'


def _peak(ts, vals, grain):
    """Where the repeating pattern is highest, in calendar words. vals line up with ts (the last season)."""
    t = ts[int(np.argmax(vals))]
    return {'day': f'{t:%A}s', 'hour': f'around {t:%H}:00', 'month': f'in {t:%B}', 'quarter': f'in Q{(t.month - 1) // 3 + 1}'}.get(
        grain, f'in week {t.isocalendar()[1]}')


def _anomalies(ts, y, m, limit=10):
    """Steps far from their neighbours: the expected value is the median of the same phase in the 3 seasons before and
    after (the 3 steps either side when there is no pattern), so one spike does not make its neighbours look odd, as it
    does against a model that learns from it. Scored with a robust z-score (median and MAD of the differences)."""
    n = len(y)
    if n < 8:
        return []
    offs = [k * m for k in (-3, -2, -1, 1, 2, 3)]
    idx = np.arange(n)[:, None] + np.array(offs)[None, :]
    ok = (idx >= 0) & (idx < n)
    near = np.where(ok, y[np.clip(idx, 0, n - 1)], np.nan)
    enough = ok.sum(axis=1) >= 3
    if not enough.any():
        return []
    expected = np.nanmedian(np.where(enough[:, None], near, 0.0), axis=1)
    d = np.where(enough, y - expected, np.nan)
    med = float(np.nanmedian(d))
    mad = float(np.nanmedian(np.abs(d - med))) * 1.4826
    if not mad or mad <= 1e-9 * (float(np.abs(y).max()) or 1):
        return []
    z = np.nan_to_num((d - med) / mad)
    hit = np.flatnonzero(np.abs(z) > ANOMALY_Z)
    hit = np.sort(hit[np.argsort(-np.abs(z[hit]))][:limit])
    return [{'t': spool._json(ts[i]), 'value': float(y[i]), 'expected': float(expected[i]), 'z': round(float(z[i]), 1),
             'direction': 'high' if z[i] > 0 else 'low'} for i in hit]


def _warnings(r, grain, horizon):
    y, step, out = r['y'], grain, []
    if r['cut']:
        out.append(f'Only the last {MAX_FIT:,} {step}s are used.')
    for t in r['partial']:
        out.append(f'The {step} of {_when(t, grain)} looks incomplete (fewer dates than usual), so it is left out.')
    if r['gaps']:
        how = 'counted as 0' if r['zero'] else 'filled with the line between their neighbours'
        out.append(f'{r["gaps"]:,} of {len(y):,} {step}s have no rows ({how}).')
    if r['m'] > 1 and not r['seasonal']:
        out.append(f'Less than two full {PATTERN[grain]} cycles of history, so a repeating pattern cannot be learned.')
    if horizon > len(y) / 2:
        out.append('The forecast reaches far compared with the history: read the range, not the line.')
    err = r['err'][r['warm']:]
    k = max(3, len(y) // 10)
    if len(err) > k + 3:
        tail, sd = err[-k:], float(np.std(err[:-k]))
        if sd and (np.all(tail > 0) or np.all(tail < 0)) and abs(float(tail.mean())) > 1.5 * sd:
            out.append(f'The last {k} {step}s sit {"above" if tail.mean() > 0 else "below"} what the model expected: '
                       'the level may have changed, so the next steps are less certain.')
    return out


def _stretch(r, grain):
    h, k = r['h_bt'], r['folds']
    return f'the last {h} {grain}s' if k == 1 else f'the last {k * h} {grain}s ({k} rounds of {h})'


def _answer(r, date, value, agg, grain, horizon, group, groups):
    y, ts, best, st = r['y'], r['ts'], r['best'], r['state']
    what = f'({agg} of {value})' if value else 'rows'
    base = 'snaive' if 'snaive' in r['scores'] else 'naive'
    mae, base_mae = r['scores'][best], r['scores'].get(base)
    vs = 1 - mae / base_mae if base_mae else (0.0 if base_mae == 0 and mae == 0 else None)
    warnings = _warnings(r, grain, horizon)
    if vs is not None and vs <= 0 and best != base:
        warnings.append('On the held-back history this did not beat repeating the past: treat it as a rough guide.')

    text = [(f'Next {grain} ({_when(r["future"][0], grain)}): about {_fmt(r["yhat"][0])} {what}, '
             f'likely {_fmt(r["lo80"][0])} to {_fmt(r["hi80"][0])}.')]
    if horizon > 1:
        text.append(f'Over the next {horizon} {grain}s: about {_fmt(float(r["yhat"].sum()))} in all.' if r['zero'] else
                    f'In {horizon} {grain}s: about {_fmt(r["yhat"][-1])}, likely {_fmt(r["lo80"][-1])} to {_fmt(r["hi80"][-1])}.')
    if st['kind'] == 'ets':
        level, slope = st['level'] * r['sd'] + r['mu'], st['slope'] * r['sd']
        pct = slope / abs(level) * 100 if abs(level) > 1e-9 else 0
        if abs(pct) >= 0.5:
            text.append(f'Trend {"+" if pct > 0 else ""}{pct:.1f}% a {grain}, flattening further out.')
    season = None
    if best in ('hw', 'snaive'):
        m, n = r['m'], len(y)
        season = m
        vals = [st['seas'][(n - m + j) % m] for j in range(m)] if best == 'hw' else list(y[-m:])
        text.append(f'It repeats a {PATTERN[grain]} pattern: highest {_peak(ts[-m:], vals, grain)}.')
    if vs is not None and best != base:
        text.append(f'Tested on {_stretch(r, grain)} it was {abs(vs) * 100:.0f}% {"more" if vs > 0 else "less"} '
                    f'accurate than {"repeating the last season" if base == "snaive" else "repeating the last value"}.')
    elif best == base:
        text.append(f'Tested on {_stretch(r, grain)}, repeating the past did as well as any model.')

    bands = zip(r['future'], r['yhat'], r['lo80'], r['hi80'], r['lo95'], r['hi95'])
    return {'date': date, 'value': value, 'agg': agg, 'grain': grain, 'horizon': horizon, 'group': group,
            'history': [{'t': spool._json(t), 'v': float(v)} for t, v in zip(ts, y)],
            'forecast': [{'t': spool._json(t), 'yhat': float(a), 'lo80': float(b), 'hi80': float(c), 'lo95': float(d), 'hi95': float(e)}
                         for t, a, b, c, d, e in bands],
            'model': {'key': best, 'name': MODELS[best][0]}, 'season': season,
            'backtest': {'steps': r['h_bt'], 'folds': r['folds'], 'mae': mae, 'mase': mae / r['scale'] if r['scale'] else None,
                         'baseline': MODELS[base][0], 'baseline_mae': base_mae, 'vs_baseline': vs,
                         'models': sorted(({'name': MODELS[k][0], 'mae': v} for k, v in r['scores'].items()), key=lambda x: x['mae'])},
            'anomalies': _anomalies(ts, y, r['m'] if r['seasonal'] else 1), 'warnings': warnings, 'text': ' '.join(text), 'groups': groups}


def summary(f, steps=12):
    """What the assistant reads: the answer without the history, numbers rounded."""
    def rnd(v):
        return round(v, 4) if isinstance(v, float) else v
    return {**{k: f[k] for k in ('date', 'value', 'agg', 'grain', 'horizon', 'group', 'model', 'season', 'warnings', 'text')},
            'forecast': [{k: rnd(v) for k, v in p.items()} for p in f['forecast'][:steps]], 'forecast_steps': len(f['forecast']),
            'backtest': {k: rnd(f['backtest'][k]) for k in ('steps', 'mae', 'mase', 'baseline', 'vs_baseline')},
            'history_steps': len(f['history']), 'last_actual': f['history'][-1] if f['history'] else None,
            'anomalies': f['anomalies'][:5], 'groups': [{k: rnd(v) for k, v in g.items()} for g in f['groups']] if f['groups'] else None}
