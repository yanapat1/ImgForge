"""
The Data assistant's tools (domain 'data'). They act as the signed-in user, through data/service.py, on the
user's own connections.

Token budget: a result never travels to the model. run_query returns its size, columns and summary statistics; the
model answers with query_result, an aggregate the server computes on the saved file. Real rows reach the model only
when the user allowed it for this session, and then only a capped sample that says how much it leaves out. Everything
below is cut to `data_analysis.ai_result_chars`, so a million rows or two thousand columns cost the same as ten.

Changes go through the same gate as the page: run_change is 'danger', so the user confirms each one in every mode.
"""

from . import E, I, S, Tool, ToolError, _obj, register

_R = dict(domain='data', ctx=True)
_U = dict(domain='data', ctx=True, risk='ui')
RUN_WAIT_S = 120


def _svc():
    from ...data import service
    return service


def _call(fn, *a, **k):
    from ...data.service import DataError
    try:
        return fn(*a, **k)
    except (DataError, ValueError) as e:
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


def _favorites(ctx, a):
    got = _call(_svc().favorites_for_ai, ctx.user, a.get('name'))
    return got, f'→ {got.get("match", "?")}: {len(got.get("tables") or [])} tables'


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


def _change_check(ctx, a):
    """Before the confirm card: a change the tables' AI access does not allow goes back to the model."""
    cid, db = _target(ctx, a)
    _call(_svc().ai_access_check, ctx.user, _sid(ctx), cid, db, a['sql'])
    return a, None


def _change(ctx, a):
    cid, db = _target(ctx, a)
    got = _call(_svc().change_for_ai, ctx.user, _sid(ctx), cid, db, a['sql'], RUN_WAIT_S)
    return got, f'→ {got.get("status")}'


def _ui_ok(ctx, a):
    return dict(a), None


EDITOR_CHARS = 20000


def _editor(ctx):
    s = ctx.scope or {}
    sql = str(s.get('sql') or '')
    return sql, s


def _kind(ctx, cid):
    return next((x['kind'] for x in _call(_svc().sources, ctx.user) if x['id'] == cid), None)


def _read_editor(ctx, a):
    sql, s = _editor(ctx)
    cid = s.get('connection')
    kind = _kind(ctx, cid) if cid else None
    from ...data.sqlfix import NAMES
    from ...data.classify import DIALECTS
    out = {'tab': s.get('tab_name') or '', 'connection': cid, 'database': s.get('database'),
           'syntax': NAMES.get(DIALECTS.get(kind, kind), kind) if kind else None, 'sql': sql,
           'lines': sql.count('\n') + 1 if sql else 0, 'truncated': bool(s.get('sql_truncated')),
           'last_error': s.get('last_error') or None}
    return out, f'→ {out["lines"]} lines' + (' (last run failed)' if out['last_error'] else '') if sql else '→ the editor is empty'


def _check_sql(ctx, a):
    sql = a.get('sql') or _editor(ctx)[0]
    if not str(sql).strip():
        raise ToolError('nothing to check: the editor is empty; pass sql')
    cid, db = _target(ctx, a)
    kind = _kind(ctx, cid)
    if kind is None:
        raise ToolError(f'unknown connection {cid!r}: list_sources shows them')
    from ...data import sqlfix
    try:
        got = sqlfix.check(sql, kind, a.get('from_syntax') or None)
    except ValueError as e:
        raise ToolError(str(e)) from None
    final = got['fixed'] or sql
    if not got['errors'] and kind != 'mongo':      # the database itself confirms the names, without running anything
        from ...data import classify
        if all(x['kind'] == classify.READ for x in classify.classify(final, kind)):
            try:
                _svc().explain_for_ai(ctx.user, _sid(ctx), cid, db, final)
                got['database_check'] = 'ok: the database accepted it (EXPLAIN, not run)'
            except Exception as e:                  # the database's own words are the most useful hint
                got['database_check'] = f'error: {str(e)[:500]}'
        else:
            got['database_check'] = 'skipped: not a plain read'
    n = len(got['errors'])
    return got, f'→ {n} errors' if n else '→ fixed' if got['fixed'] else '→ ok'


def _ui_run(ctx, a):
    return {'ok': True}, 'shown on the page'


CONN = {'connection': S('connection id, default the page\'s'), 'database': S('database, default the page\'s')}
register(Tool('list_sources', 'The databases this user connected: id, name, kind.', _obj({}), False, _list_sources, **_R))
register(Tool('search_catalog', 'Find tables by words in their name, columns or descriptions. Start here: never ask for a '
                                'whole database.', _obj({'query': S('words to look for'), **CONN}, ['query']), False, _search, **_R))
register(Tool('list_favorites', 'The user\'s favorite tables in their own categories (the scope line names the categories). '
                                'Give the word the user used ("Motion"): it matches a category, else favorite table names. '
                                'Each table says its connection and database. match none / ambiguous: ask_user.',
              _obj({'name': S('a category or table word; empty = all categories')}), False, _favorites, **_R))
register(Tool('describe_table', 'One table: columns, types, keys, descriptions. Wide tables come in pages (offset).',
              _obj({'table': S('table name'), 'schema': S('schema'), 'offset': I('first column'), **CONN}, ['table']),
              False, _describe, **_R))
def _ask_query(ctx, clean):
    """A query against the user's database asks first unless the chat is in mode auto."""
    return getattr(ctx, 'mode', 'ask') != 'auto'


def _query_line(b, a):
    sql = ' '.join(str(a.get('sql') or '').split())
    return f'Run on {a["connection"]}: {sql[:240]}' if a.get('connection') else f'Run: {sql[:260]}'


register(Tool('run_query', 'Run one read-only query. Returns its size, columns and statistics plus result_id, never the rows. '
                           'Then use query_result. Changes are refused here. In mode ask the user approves (and may '
                           'edit) each query first; in auto it runs at once.',
              _obj({'sql': S('the query, in this database\'s syntax'), 'name': S('a short name'), **CONN}, ['sql']),
              False, _run_query, _query_line, confirm_if=_ask_query, confirm_risk='query', **_R))
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
              lambda b, a: f'Describe {a.get("column") or a.get("table")}', domain='data', ctx=True))
register(Tool('run_change', 'Change data or structure, or make a temp table. The user confirms it first, in every mode. '
                            'Each table has an AI access the user sets: Read only (default: temp tables only), Edit rows '
                            '(INSERT / UPDATE / DELETE / MERGE), Change structure (+ ALTER / TRUNCATE / DROP TABLE). '
                            'describe_table shows it; one change per statement, permanent new tables are refused.',
              _obj({'sql': S('the statements'), **CONN}, ['sql']), True, _change, lambda b, a: a.get('sql', '')[:200],
              domain='data', ctx=True, risk='danger', check=_change_check))
register(Tool('read_editor', 'The SQL in the user\'s editor on the open tab, its syntax and the last run\'s error. Call it '
                             'when the user means "my query" or "this SQL".', _obj({}), False, _read_editor, **_R))
register(Tool('check_sql', 'Check SQL (default: the editor\'s) against the connection\'s syntax: syntax errors with line and '
                           'column, a fixed version when it was written for another database, what changed, and the '
                           'database\'s own check (EXPLAIN, a read is never run). Show a fix with set_editor_query.',
              _obj({'sql': S('the SQL; empty = the editor\'s'), 'from_syntax': S('the syntax it was written in, if known: postgres, tsql, mysql, oracle ...'),
                    **CONN}), False, _check_sql, **_R))
register(Tool('set_editor_query', 'Page action: put a query in the SQL editor. The user sees the SQL and what changed against '
                                  'the editor before Apply; Ctrl+Z in the editor undoes it.',
              _obj({'sql': S('the whole query, in this database\'s syntax'), 'mode': E('replace = the open tab, new_tab = a new tab', ['replace', 'new_tab']),
                    'note': S('one line: what you wrote or fixed')}, ['sql']), False, _ui_run,
              lambda b, a: a.get('note') or ('Open this query in a new tab' if a.get('mode') == 'new_tab' else 'Put this query in the editor'),
              check=_ui_ok, **_U))


def _editor_run_check(ctx, a):
    """Before run_in_editor's card: a read on a known connection; the page then writes it in the editor and runs it."""
    sql = str(a.get('sql') or '')
    if not sql.strip():
        raise ToolError('sql: the query to run is empty')
    if len(sql) > EDITOR_CHARS:
        raise ToolError(f'sql: at most {EDITOR_CHARS} characters')
    if not (a.get('connection') or (ctx.scope or {}).get('connection')):
        raise ToolError('no connection on the page: ask the user to choose one (list_sources shows them), then pass connection')
    cid, db = _target(ctx, a)
    _call(_svc().assert_read_for_ai, ctx.user, _sid(ctx), cid, db, sql)
    out = {**a, 'sql': sql, 'connection': cid, 'mode': 'new_tab' if a.get('mode') == 'new_tab' else 'replace'}
    if db:
        out['database'] = db
    return out, None


def _editor_run_line(b, a):
    sql = ' '.join(str(a.get('sql') or '').split())
    return a.get('note') or f'Run in the editor{" (new tab)" if a.get("mode") == "new_tab" else ""}: {sql[:240]}'


register(Tool('run_in_editor', 'Page action that runs: write a read in the SQL editor and run it; rows show in the Results '
                               'tab. Returns rows, columns, result_id (never the rows) or the database error.',
              _obj({'sql': S('the whole query, in this database\'s syntax'),
                    'mode': E('replace = this tab, new_tab = a new one', ['replace', 'new_tab']),
                    'name': S('new tab name'), 'note': S('one line: what it shows'), **CONN}, ['sql']),
              False, _ui_run, _editor_run_line, check=_editor_run_check, domain='data', ctx=True, risk='run'))
NOTE_COLORS = ['blue', 'green', 'amber', 'red', 'violet', 'teal', 'grey', 'yellow']
DASH_KINDS = ['kpi', 'bar', 'hbar', 'line', 'area', 'donut', 'forecast', 'table', 'text']
register(Tool('build_dashboard', 'Page action: add charts from the last result to the dashboard.',
              _obj({'charts': {'type': 'array', 'description': 'the charts to add', 'items': {'type': 'object', 'properties': {
                  'kind': E('kpi = one number over the whole result (y, agg; x = optional trend sparkline by that column), bar = '
                            'groups side by side, hbar = a ranking (largest first), line / area = a trend over x, donut = each '
                            'group\'s share, forecast = a line with its forecast band (x the date), table = groups as rows, '
                            'text = a heading or note (its text in text)', DASH_KINDS),
                  'title': S('title: what it shows'), 'x': S('column: the groups, or the date'), 'y': S('column: the number (empty = row count)'),
                  'agg': E('how', ['sum', 'avg', 'count', 'min', 'max']), 'text': S('text widget: its words'),
                  'span': {'type': 'integer', 'description': 'width in 12ths: 3, 4, 6 (default), 8 or 12'},
                  'size': E('height: s short, m medium (default), l tall', ['s', 'm', 'l']),
                  'color': E('its colour (default the accent)', NOTE_COLORS),
                  'sort': E('key = x\'s own order (dates, numbers), desc / asc = by value (default desc for hbar and donut)', ['key', 'desc', 'asc']),
                  'top': {'type': 'integer', 'description': 'how many groups, 3..100 (default 30; hbar 10, donut 6)'}},
                  'required': ['kind', 'title']}}}, ['charts']),
              False, _ui_run, lambda b, a: f'Add {len(a.get("charts") or [])} charts', check=_ui_ok, **_U))


# ---------------------------------------------------------------- the visual builder's board: zones, text, sticky notes
BOARD_W, BOARD_H, BOARD_OPS = 2400, 1600, 60
NOTE_SHAPES = ['rect', 'round', 'pill', 'ellipse', 'diamond', 'hexagon', 'callout', 'none']
_NOTE_ENUMS = {'shape': NOTE_SHAPES, 'layer': ['back', 'front'], 'border': ['solid', 'dashed', 'none'],
               'ha': ['left', 'center', 'right'], 'va': ['top', 'middle', 'bottom']}
BOARD_SPAN = 20000                              # notes go anywhere on the board, like the table cards
_NOTE_NUM = {'x': (-BOARD_SPAN, BOARD_SPAN), 'y': (-BOARD_SPAN, BOARD_SPAN), 'w': (40, BOARD_W), 'h': (24, BOARD_H), 'alpha': (0, 1), 'size': (11, 48), 'rot': (-180, 180)}


def _note_fields(i, note):
    import re
    if not isinstance(note, dict):
        raise ToolError(f'ops[{i}].note must be an object')
    out = {}
    for k, v in note.items():
        if k in _NOTE_ENUMS:
            if v not in _NOTE_ENUMS[k]:
                raise ToolError(f'ops[{i}].note.{k} must be one of {", ".join(_NOTE_ENUMS[k])}')
            out[k] = v
        elif k in _NOTE_NUM:
            try:
                f = float(v)
            except (TypeError, ValueError):
                raise ToolError(f'ops[{i}].note.{k} must be a number') from None
            lo, hi = _NOTE_NUM[k]
            out[k] = round(min(hi, max(lo, f)), 3 if k == 'alpha' else 0)
        elif k == 'fill':
            v = str(v or '').strip().lower()
            if v and v not in NOTE_COLORS and not re.fullmatch(r'#[0-9a-f]{6}', v):
                raise ToolError(f'ops[{i}].note.fill: one of {", ".join(NOTE_COLORS)}, #rrggbb, or "" for none')
            out[k] = v
        elif k == 'text':
            out[k] = str(v)[:2000]
        elif k in ('bold', 'carry'):
            out[k] = bool(v)
        else:
            raise ToolError(f'ops[{i}].note.{k}: not a note field (locking is the user\'s)')
    return out


def _board_check(ctx, a):
    """Before the page applies edit_board: every op well-formed, sizes inside the board (the page checks locks and
    measures the tables for zone / describe)."""
    import re
    ops = a.get('ops')
    if not isinstance(ops, list) or not ops:
        raise ToolError('ops: give 1 to 60 operations')
    if len(ops) > BOARD_OPS:
        raise ToolError(f'at most {BOARD_OPS} operations in one edit_board')
    out = []
    for i, o in enumerate(ops):
        kind = o.get('op') if isinstance(o, dict) else None
        if kind not in ('zone', 'describe', 'add', 'update', 'remove'):
            raise ToolError(f'ops[{i}].op must be zone, describe, add, update or remove')
        x = {'op': kind}
        if o.get('id'):
            x['id'] = str(o['id'])[:64]
        if kind == 'zone':
            label, tables = str(o.get('label') or '').strip(), o.get('tables')
            if not label:
                raise ToolError(f'ops[{i}]: a zone needs its label')
            if not isinstance(tables, list) or not tables or not all(isinstance(t, str) and t for t in tables):
                raise ToolError(f'ops[{i}]: a zone needs tables, the table ids from read_board (e.g. ["o", "c"])')
            x.update(label=label[:120], tables=[t[:200] for t in tables[:40]])
            fill = str(o.get('fill') or '').strip().lower()
            if fill and fill not in NOTE_COLORS and not re.fullmatch(r'#[0-9a-f]{6}', fill):
                raise ToolError(f'ops[{i}].fill: one of {", ".join(NOTE_COLORS)} or #rrggbb')
            if fill:
                x['fill'] = fill
        elif kind == 'describe':
            table, text = str(o.get('table') or '').strip(), str(o.get('text') or '').strip()
            if not table or not text:
                raise ToolError(f'ops[{i}]: describe needs table (its id from read_board) and text')
            x.update(table=table[:200], text=text[:600])
        else:
            if kind != 'add' and not o.get('id'):
                raise ToolError(f'ops[{i}]: {kind} needs the note id (read_board lists them)')
            if kind != 'remove':
                x['note'] = _note_fields(i, o.get('note') or {})
        out.append(x)
    return {**a, 'ops': out, 'arrange': bool(a.get('arrange'))}, None


def _read_board(ctx, a):
    b = (ctx.scope or {}).get('board')
    if not b:
        return {'board': None, 'note': 'The builder is closed or its board is empty: open Query with the Builder pane.'}, '→ no board'
    return b, f'→ {len(b.get("tables") or [])} tables, {len(b.get("notes") or [])} notes'


_NOTE_PROPS = {
    'shape': E('rect, round (rounded), pill, ellipse, diamond, hexagon, callout (speech bubble), none (text only)', NOTE_SHAPES),
    'layer': E('back = a zone behind the tables, front = above them (text, sticky notes)', ['back', 'front']),
    'x': {'type': 'number', 'description': 'left, board px (the same space as the table cards, read_board)'}, 'y': {'type': 'number', 'description': 'top, board px'},
    'w': {'type': 'number', 'description': 'width px (≥ 40)'}, 'h': {'type': 'number', 'description': 'height px (≥ 24)'},
    'fill': S(f'one of {", ".join(NOTE_COLORS)}, #rrggbb, or "" for none'),
    'alpha': {'type': 'number', 'description': 'fill opacity 0..1: zones about 0.14, sticky notes 1'},
    'border': E('outline', ['solid', 'dashed', 'none']), 'text': S('plain text, new lines kept'),
    'size': {'type': 'number', 'description': 'font px 11..48'}, 'bold': {'type': 'boolean', 'description': 'bold text'},
    'ha': E('text across', ['left', 'center', 'right']), 'va': E('text up and down', ['top', 'middle', 'bottom']),
    'carry': {'type': 'boolean', 'description': 'zones: dragging the zone moves the tables inside it'},
    'rot': {'type': 'number', 'description': 'rotation, degrees clockwise about its centre, -180..180 (0 = straight); x, y, w, h stay the unrotated box'},
}
register(Tool('read_board', 'The visual builder\'s board: each table card (id, name, x, y, w, h, its description when the '
                            'catalog has one), the joins (with 1:N / N:M when known) and the notes (zones, text, sticky '
                            'notes; describes = the table a description note belongs to) with their ids. Read it before edit_board.',
              _obj({}), False, _read_board, **_R))
register(Tool('edit_board', 'Page action: group and explain the tables on the visual builder board. Easiest: '
                            'op zone {label, tables: [table ids]} draws a zone around those tables and op describe '
                            '{table, text} pins a short description note above a table (it moves with the card): the '
                            'page measures and places them, so never work out coordinates for these. arrange: true first '
                            'lays the tables out zone by zone so no zones overlap (moves the cards, not the SQL). add / '
                            'update / remove change one note by id (text, sticky notes, shapes; x, y in board px; rot turns it). Never '
                            'moves or deletes a locked note; never removes the user\'s notes unless they asked. One undo '
                            'step (Ctrl+Z).',
              _obj({'ops': {'type': 'array', 'description': 'the edits (at most 60); describe ops before the zones that hold those tables', 'items': {
                  'type': 'object', 'properties': {'op': E('zone = a zone around tables, describe = a table\'s description note, add / update / remove = one note by id',
                                                           ['zone', 'describe', 'add', 'update', 'remove']),
                                                   'label': S('zone: its title, a few words'),
                                                   'tables': {'type': 'array', 'items': {'type': 'string'}, 'description': 'zone: the table ids from read_board'},
                                                   'fill': S(f'zone: one of {", ".join(NOTE_COLORS)} (optional: a new colour per zone)'),
                                                   'table': S('describe: the table id from read_board'),
                                                   'text': S('describe: one or two short sentences (what one row is, what it is for)'),
                                                   'id': S('the note id (update, remove; optional on add / zone: a zone with this id or label is redrawn)'),
                                                   'note': {'type': 'object', 'description': 'add / update: the fields to set', 'properties': _NOTE_PROPS,
                                                            'additionalProperties': False}},
                  'required': ['op'], 'additionalProperties': False}},
                    'arrange': {'type': 'boolean', 'description': 'move the tables so each zone\'s tables sit together and zones do not overlap (use when tables of different zones are mixed on the board)'},
                    'note': S('one line: what this edit does to the board')}, ['ops']), False, _ui_run,
              lambda b, a: a.get('note') or f'Edit the board ({len(a.get("ops") or [])} changes{", tables arranged" if a.get("arrange") else ""})', check=_board_check, **_U))
