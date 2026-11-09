"""
The Analysis assistant's tools (domain 'analysis'). They act as the signed-in user, through analysis/service.py, on the
user's own connections.

Token budget: a result never travels to the model. run_query returns its size, columns and summary statistics; the
model answers with query_result, an aggregate the server computes on the saved file. Real rows reach the model only
when the user allowed it for this session, and then only a capped sample that says how much it leaves out. Everything
below is cut to `data_analysis.ai_result_chars`, so a million rows or two thousand columns cost the same as ten.

Changes go through the same gate as the page: run_change is 'danger', so the user confirms each one in every mode.
"""

from . import E, I, S, Tool, ToolError, _obj, register

_R = dict(domain='analysis', ctx=True)
_U = dict(domain='analysis', ctx=True, risk='ui')
RUN_WAIT_S = 120


def _svc():
    from ...analysis import service
    return service


def _call(fn, *a, **k):
    from ...analysis.service import AnalysisError
    try:
        return fn(*a, **k)
    except (AnalysisError, ValueError) as e:
        raise ToolError(str(e)) from None


def _sid(ctx):
    sid = (ctx.scope or {}).get('session')
    if not sid:
        raise ToolError('no analysis session is open')
    return str(sid)


def _target(ctx, a):
    s = ctx.scope or {}
    cid = a.get('connection') or s.get('connection')
    if not cid:
        raise ToolError('which connection? list_sources shows them')
    return str(cid), a.get('database') or s.get('database') or None


def _list_sources(ctx, a):
    out = _call(_svc().sources, ctx.user)
    return {'sources': out}, f'→ {len(out)} sources'


def _search(ctx, a):
    cid, db = _target(ctx, a)
    if not db:
        raise ToolError('which database? pass database')
    got = _call(_svc().search_catalog, ctx.user, cid, db, a['query'])
    return got, f'→ {len(got["tables"])} tables'


def _describe(ctx, a):
    cid, db = _target(ctx, a)
    got = _call(_svc().describe_for_ai, ctx.user, cid, db or '', a.get('schema') or '', a['table'], a.get('offset') or 0)
    return got, f'→ {len(got["columns"])} columns of {got["total_columns"]}'


def _run_query(ctx, a):
    cid, db = _target(ctx, a)
    got = _call(_svc().run_for_ai, ctx.user, _sid(ctx), cid, db, a['sql'], a.get('name') or 'AI query', RUN_WAIT_S)
    rows = got.get('rows')
    return got, f'→ {rows:,} rows' if rows is not None else f'→ {got.get("status")}'


def _query_result(ctx, a):
    got = _call(_svc().query_result, ctx.user, _sid(ctx), a['result_id'], a['sql'])
    return got, f'→ {got["rows_returned"]} rows'


def _sample(ctx, a):
    got = _call(_svc().sample_for_ai, ctx.user, _sid(ctx), a['result_id'])
    return got, f'→ {got["rows_returned"]} sample rows of {got["rows_total"]:,}'


def _profile(ctx, a):
    got = _call(_svc().profile_for_ai, ctx.user, a['result_id'], a.get('column'))
    return got, '→ profile'


def _stat(ctx, a):
    got = _call(_svc().run_test, ctx.user, a['result_id'], a['kind'], a.get('a'), a.get('b'), a.get('group_a'), a.get('group_b'))
    return got, f'→ {got["test"]}'


def _forecast(ctx, a):
    got = _call(_svc().forecast_for_ai, ctx.user, a['result_id'], a['date'], a.get('value'), a.get('agg') or 'sum',
                a.get('grain') or 'auto', a.get('horizon'), a.get('group'))
    return got, f'→ {got.get("forecast_steps", 0)} {got.get("grain", "")}s ahead' if 'forecast' in got else '→ forecast'


def _explain(ctx, a):
    cid, db = _target(ctx, a)
    got = _call(_svc().explain_for_ai, ctx.user, _sid(ctx), cid, db, a['sql'])
    return got, f'→ {len(got["top"])} costly steps'


def _describe_set(ctx, a):
    cid, db = _target(ctx, a)
    _call(_svc().set_description, ctx.user, cid, db or '', a.get('schema') or '', a['table'], a.get('column') or '', a['description'], 'ai')
    return {'ok': True}, f'→ described {a.get("column") or a["table"]}'


def _change(ctx, a):
    cid, db = _target(ctx, a)
    got = _call(_svc().change_for_ai, ctx.user, _sid(ctx), cid, db, a['sql'], RUN_WAIT_S)
    return got, f'→ {got.get("status")}'


def _ui_ok(ctx, a):
    return dict(a), None


def _ui_run(ctx, a):
    return {'ok': True}, 'shown on the page'


CONN = {'connection': S('connection id, default the page\'s'), 'database': S('database, default the page\'s')}
register(Tool('list_sources', 'The databases this user connected: id, name, kind.', _obj({}), False, _list_sources, **_R))
register(Tool('search_catalog', 'Find tables by words in their name, columns or descriptions. Start here: never ask for a '
                                'whole database.', _obj({'query': S('words to look for'), **CONN}, ['query']), False, _search, **_R))
register(Tool('describe_table', 'One table: columns, types, keys, descriptions. Wide tables come in pages (offset).',
              _obj({'table': S('table name'), 'schema': S('schema'), 'offset': I('first column'), **CONN}, ['table']),
              False, _describe, **_R))
register(Tool('run_query', 'Run one read-only query. Returns its size, columns and statistics plus result_id, never the rows. '
                           'Then use query_result. Changes are refused here.',
              _obj({'sql': S('the query, in this database\'s syntax'), 'name': S('a short name'), **CONN}, ['sql']),
              False, _run_query, **_R))
register(Tool('query_result', 'Answer from a saved result without touching the database: SQL over the table named `result` '
                              '(DuckDB syntax). Without row permission it must aggregate (GROUP BY, COUNT, SUM ...).',
              _obj({'result_id': S('from run_query'), 'sql': S('SELECT ... FROM result ...')}, ['result_id', 'sql']),
              False, _query_result, **_R))
register(Tool('sample_rows', 'A small sample of real rows. Only when the user allowed it for this session; otherwise tell them '
                             'where to allow it.', _obj({'result_id': S('from run_query')}, ['result_id']), False, _sample, **_R))
register(Tool('profile_result', 'Summary of a result, or of one column: empty values, distinct, range.',
              _obj({'result_id': S('from run_query'), 'column': S('one column')}, ['result_id']), False, _profile, **_R))
register(Tool('stat_test', 'A statistic test on a result, with its effect size and a plain sentence.',
              _obj({'result_id': S('from run_query'), 'kind': E('the test', ['ttest', 'mannwhitney', 'anova', 'chi2', 'pearson', 'spearman', 'normal']),
                    'a': S('value, or first column'), 'b': S('group, or second column'), 'group_a': S('ttest: one group'), 'group_b': S('ttest: the other')},
                   ['result_id', 'kind', 'a']), False, _stat, **_R))
register(Tool('forecast', 'Forecast a measure of a result over time, from the saved file: a value per step (day, week, '
                         '...) with an 80% and 95% range, the model that tested best on held-back history, its accuracy '
                         'against repeating the past, warnings, unusual steps, and with group one forecast per group. '
                         'Never the rows. Say the range and the warnings, not only the line.',
              _obj({'result_id': S('from run_query'), 'date': S('the date or time column'),
                    'value': S('the number column; empty = count rows'), 'agg': E('per step', ['sum', 'avg', 'count', 'min', 'max']),
                    'grain': E('the step; auto picks one from the span of the dates', ['auto', 'hour', 'day', 'week', 'month', 'quarter', 'year']),
                    'horizon': I('steps ahead; default 14 days, 8 weeks, 6 months ...'), 'group': S('a category column: one forecast per group (the 12 largest)')},
                   ['result_id', 'date']), False, _forecast, **_R))
register(Tool('explain_query', 'Where a read spends its effort: the costliest steps. Does not run the query.',
              _obj({'sql': S('the query'), **CONN}, ['sql']), False, _explain, **_R))
register(Tool('set_column_description', 'Save what a column means, after the user told you. Stays in this app.',
              _obj({'table': S('table'), 'column': S('column; empty = the table itself'), 'schema': S('schema'),
                    'description': S('what it means'), **CONN}, ['table', 'description']), True, _describe_set,
              lambda b, a: f'Describe {a.get("column") or a.get("table")}', domain='analysis', ctx=True))
register(Tool('run_change', 'Change data or structure, or make a temp table. The user confirms it first, in every mode.',
              _obj({'sql': S('the statements'), **CONN}, ['sql']), True, _change, lambda b, a: a.get('sql', '')[:200],
              domain='analysis', ctx=True, risk='danger'))
register(Tool('set_editor_query', 'Page action: put a query in the SQL editor so the user can read and run it.',
              _obj({'sql': S('the query')}, ['sql']), False, _ui_run, lambda b, a: 'Put this query in the editor', check=_ui_ok, **_U))
register(Tool('build_dashboard', 'Page action: add charts from the last result to the dashboard.',
              _obj({'charts': {'type': 'array', 'description': 'the charts to add', 'items': {'type': 'object', 'properties': {
                  'kind': E('chart; forecast = a line with its forecast band, x the date', ['bar', 'line', 'table', 'kpi', 'forecast']),
                  'title': S('title'), 'x': S('column'), 'y': S('column'),
                  'agg': E('how', ['sum', 'avg', 'count', 'min', 'max'])}, 'required': ['kind', 'title']}}}, ['charts']),
              False, _ui_run, lambda b, a: f'Add {len(a.get("charts") or [])} charts', check=_ui_ok, **_U))
