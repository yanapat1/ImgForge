"""Forecasts on a saved result, checked on series whose trend, pattern and spikes are known."""

import datetime as dt
import random

import orjson
import pytest

from dataviz.analysis import forecast, spool

START = dt.date(2025, 1, 6)          # a Monday


def daily(i, r):
    """A rising daily total with a weekly pattern: Mondays high, weekends low."""
    d = START + dt.timedelta(days=i)
    return 100 + 0.3 * i + (40 if d.weekday() == 0 else -25 if d.weekday() >= 5 else 0) + r.gauss(0, 3)


def write(tmp_path, columns, rows, name='r.parquet'):
    p = tmp_path / name
    spool.Spool(p).write(columns, [rows], 10_000_000)
    return p


@pytest.fixture
def days(tmp_path):
    """400 days, split over two rows a day (the forecast sums them back), and the 14 days after, kept aside."""
    r = random.Random(5)
    vals = [daily(i, r) for i in range(414)]
    rows = [[START + dt.timedelta(days=i), v / 2, 'ab'[k]] for i, v in enumerate(vals[:400]) for k in (0, 1)]
    return write(tmp_path, ['day', 'sales', 'shop'], rows), vals[400:]


def test_a_weekly_pattern_is_found_and_the_range_holds_the_truth(days):
    p, truth = days
    f = forecast.forecast(p, 'day', 'sales', 'sum', horizon=14)
    assert f['grain'] == 'day' and f['horizon'] == 14 and len(f['forecast']) == 14
    assert f['season'] == 7 and f['model']['key'] in ('hw', 'snaive')
    assert 'highest Mondays' in f['text']
    assert f['forecast'][0]['t'].startswith(str(START + dt.timedelta(days=400)))
    inside = sum(p_['lo95'] <= v <= p_['hi95'] for p_, v in zip(f['forecast'], truth))
    assert inside >= 12
    err = sum(abs(p_['yhat'] - v) for p_, v in zip(f['forecast'], truth)) / 14
    assert err < 15                                         # the weekly swing alone is 65
    for p_ in f['forecast']:
        assert p_['lo95'] <= p_['lo80'] <= p_['yhat'] <= p_['hi80'] <= p_['hi95']
    assert f['forecast'][-1]['hi95'] - f['forecast'][-1]['lo95'] >= f['forecast'][0]['hi95'] - f['forecast'][0]['lo95']


def test_the_models_beat_repeating_the_last_value_on_a_pattern(days):
    p, _ = days
    f = forecast.forecast(p, 'day', 'sales', 'sum')
    mae = {m['name']: m['mae'] for m in f['backtest']['models']}
    assert mae[forecast.MODELS['hw'][0]] < mae[forecast.MODELS['naive'][0]] / 2
    assert f['backtest']['folds'] == 3 and f['backtest']['mase'] is not None
    assert f['backtest']['baseline'] == forecast.MODELS['snaive'][0]


def test_a_trend_without_a_pattern(tmp_path):
    r = random.Random(2)
    rows = [[dt.date(2020, 1, 1) + dt.timedelta(days=31 * i), 1000 + 25 * i + r.gauss(0, 5)] for i in range(30)]
    f = forecast.forecast(write(tmp_path, ['month', 'v'], rows), 'month', 'v', 'avg', grain='month', horizon=6)
    assert f['model']['key'] == 'holt'
    assert f['forecast'][0]['yhat'] > rows[-1][1]
    assert 'Trend +' in f['text']
    assert any('two full yearly cycles' in w for w in f['warnings'])


def test_a_monthly_yearly_pattern(tmp_path):
    r = random.Random(3)
    rows = [[dt.date(2021 + i // 12, i % 12 + 1, 15), 200 + (120 if i % 12 == 11 else 0) + r.gauss(0, 4)] for i in range(48)]
    f = forecast.forecast(write(tmp_path, ['m', 'v'], rows), 'm', 'v')
    assert f['grain'] == 'month' and f['season'] == 12
    assert 'in December' in f['text']


def test_a_spike_is_unusual_and_its_neighbours_are_not(tmp_path):
    r = random.Random(4)
    rows = [[START + dt.timedelta(days=i), daily(i, r) + (500 if i == 200 else 0)] for i in range(300)]
    f = forecast.forecast(write(tmp_path, ['day', 'v'], rows), 'day', 'v')
    odd = {a['t'][:10]: a for a in f['anomalies']}
    spike = str(START + dt.timedelta(days=200))
    assert spike in odd and odd[spike]['direction'] == 'high'
    assert str(START + dt.timedelta(days=207)) not in odd and str(START + dt.timedelta(days=193)) not in odd


def test_an_unfinished_last_week_is_left_out(tmp_path):
    rows = [[START + dt.timedelta(days=i), 10.0] for i in range(7 * 30 + 2)]        # ends on a Tuesday
    f = forecast.forecast(write(tmp_path, ['day', 'v'], rows), 'day', 'v', grain='week')
    assert f['history'][-1]['v'] == 70
    assert any('looks incomplete' in w for w in f['warnings'])
    assert f['forecast'][0]['yhat'] == pytest.approx(70, abs=1)


def test_steps_without_rows(tmp_path):
    rows = [[START + dt.timedelta(days=i), float(i)] for i in range(60) if i % 10 != 5]
    p = write(tmp_path, ['day', 'v'], rows)
    s = forecast.forecast(p, 'day', 'v', 'sum')
    assert any('6 of 60 days have no rows (counted as 0)' in w for w in s['warnings'])
    assert s['history'][5]['v'] == 0
    a = forecast.forecast(p, 'day', 'v', 'avg')
    assert a['history'][5]['v'] == pytest.approx(5)                                  # the line between 4 and 6
    assert any('line between their neighbours' in w for w in a['warnings'])


def test_counting_rows_and_never_below_zero(tmp_path):
    r = random.Random(1)
    rows = [[dt.datetime(2026, 1, 1) + dt.timedelta(hours=i), 'x'] for i in range(24 * 20) if r.random() < (0.9 if i % 24 > 8 else 0.05)]
    f = forecast.forecast(write(tmp_path, ['at', 'what'], rows), 'at', None, 'count', grain='hour', horizon=48)
    assert f['value'] is None and f['grain'] == 'hour' and len(f['forecast']) == 48
    assert all(p['lo95'] >= 0 and p['yhat'] >= 0 for p in f['forecast'])
    assert 'rows' in f['text']


def test_one_forecast_per_group_ranked_by_change(tmp_path):
    rows = []
    for i in range(120):
        d = START + dt.timedelta(days=i)
        rows += [[d, 100 + 2 * i, 'up'], [d, 400 - 2 * i, 'down'], [d, 50, 'flat']]
    rows.append([START, 1.0, 'tiny'])
    f = forecast.forecast(write(tmp_path, ['day', 'v', 'g'], rows), 'day', 'v', group='g', horizon=7)
    names = [g['name'] for g in f['groups']]
    assert names[0] == 'up' and names[-1] == 'tiny' and names[-2] == 'down'
    up, down, tiny = (next(g for g in f['groups'] if g['name'] == k) for k in ('up', 'down', 'tiny'))
    assert up['change_pct'] > 0 > down['change_pct']
    assert tiny['note'] == 'too little history' and tiny['change_pct'] is None


def test_long_history_is_cut_and_the_horizon_is_capped(tmp_path):
    rows = [[dt.datetime(2026, 1, 1) + dt.timedelta(hours=i), float(i % 24)] for i in range(1500)]
    f = forecast.forecast(write(tmp_path, ['at', 'v'], rows), 'at', 'v', grain='hour', horizon=10_000)
    assert len(f['history']) == forecast.MAX_FIT and f['horizon'] == forecast.MAX_HORIZON
    assert any('Only the last 1,000 hours' in w for w in f['warnings'])
    assert f['season'] == 24


@pytest.mark.parametrize('args, msg', [
    (('nope', 'v'), "no column 'nope'"),
    (('label', 'v'), 'label holds no dates'),
    (('day', 'label'), 'label holds no numbers'),
    (('day', 'v', 'median'), 'agg must be one of'),
    (('day', 'v', 'sum', 'fortnight'), 'step must be one of'),
])
def test_wrong_columns_are_said_plainly(tmp_path, args, msg):
    rows = [[START + dt.timedelta(days=i), float(i), 'text'] for i in range(30)]
    with pytest.raises(ValueError, match=msg):
        forecast.forecast(write(tmp_path, ['day', 'v', 'label'], rows), *args)


def test_too_little_history(tmp_path):
    rows = [[START + dt.timedelta(days=i), 1.0] for i in range(5)]
    with pytest.raises(ValueError, match='at least 8 days'):
        forecast.forecast(write(tmp_path, ['day', 'v'], rows), 'day', 'v', grain='day')


def test_dates_kept_as_text_work(tmp_path):
    rows = [[(START + dt.timedelta(days=i)).isoformat(), 5.0 + i % 7] for i in range(60)]
    assert forecast.forecast(write(tmp_path, ['day', 'v'], rows), 'day', 'v')['grain'] == 'day'


def test_the_summary_for_the_assistant_has_no_history(days):
    p, _ = days
    s = forecast.summary(forecast.forecast(p, 'day', 'sales', 'sum', horizon=60, group='shop'))
    assert 'history' not in s and s['history_steps'] == 400 and len(s['forecast']) == 12 and s['forecast_steps'] == 60
    assert len(s['groups']) == 2 and s['text']
    assert len(orjson.dumps(s)) < 6000
