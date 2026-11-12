"""The Analysis assistant knows the user's own categories: the scope line names them (and stays out when there are
none), list_favorites returns a category's tables under the result cap, and the analysis prompts name only real tools.
It reads the editor's SQL (read_editor), checks and fixes it for the connection (check_sql), and proposes SQL with
set_editor_query, which waits for the user's Apply in ask mode."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from dataviz.data import favorites as fav
from dataviz.data import service
from dataviz.llm import budget, prompting, tools
from dataviz.llm.engine import roles
from dataviz.llm.engine.react import Ctx

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / 'dataviz' / 'llm' / 'prompts'
ALICE = {'id': 'u-alice', 'username': 'alice'}
SRC = [{'id': 'c1', 'name': 'Plant DB', 'kind': 'postgres'}]


def favs(n_groups=2, n_items=3):
    groups = ['Motion', 'Sales'][:n_groups] if n_groups <= 2 else [f'Group {i}' for i in range(n_groups)]
    items = [{'c': 'c1', 'd': 'db', 's': 'public', 't': f'motion_{i}' if i % 2 == 0 else f'sales_{i}',
              'g': groups[i % len(groups)] if groups else ''} for i in range(n_items)]
    return {fav.KEY: {'groups': groups, 'items': items}}


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return SimpleNamespace(all=lambda: self.rows)


@pytest.fixture
def prefs(monkeypatch):
    box = {'p': favs()}
    monkeypatch.setattr(service, 'user_prefs', lambda user: box['p'])
    monkeypatch.setattr(service, 'sources', lambda user: SRC)
    monkeypatch.setattr(service, '_cfg', lambda: {})
    doc = SimpleNamespace(connection_id='c1', database='db', schema_name='public', table_name='motion_0', description='sensor events')
    monkeypatch.setattr(service.db, 'connect', lambda: _Conn([doc]))
    return box


def test_list_favorites_is_a_read_tool_of_the_analysis_role():
    t = tools.REGISTRY['list_favorites']
    assert t.domain == 'data' and t.ctx and not t.mutating
    assert 'list_favorites' in tools.for_role(roles.get('data').domains, 'plan')     # a read: plan mode keeps it


def test_scope_line_names_the_categories(prefs):
    s = roles.data_scope(ALICE, {'connection': 'c1', 'database': 'db'})
    assert 'Your categories (Favorites, 3 tables): Motion (2), Sales (1).' in s
    prefs['p'] = {}
    assert 'categories' not in roles.data_scope(ALICE, {'connection': 'c1', 'database': 'db'})


def test_list_favorites_resolves_the_users_word(prefs):
    ctx = Ctx(user=ALICE, role='data', scope={}, skills={})
    got = tools.REGISTRY['list_favorites'].run(ctx, {'name': 'motion'})[0]
    assert got['match'] == 'category' and got['connections'] == {'c1': 'Plant DB'}
    assert [t['table'] for t in got['tables']] == ['motion_0', 'motion_2']
    assert got['tables'][0]['description'] == 'sensor events' and got['tables'][0]['database'] == 'db'
    none = tools.REGISTRY['list_favorites'].run(ctx, {'name': 'weather'})[0]
    assert none['match'] == 'none' and 'ask_user' in none['note'] and none['categories'] == ['Motion', 'Sales']


def test_three_hundred_favorites_stay_under_the_cap(prefs):
    prefs['p'] = favs(40, 300)
    got = service.favorites_for_ai(ALICE)
    assert len(service.orjson.dumps(got)) <= service._ai_cfg('ai_result_chars', 4000)
    assert got['truncated'] and got['total'] == 300 and got['tables']


def test_analysis_prompts_name_only_real_tools():
    names = set(tools.REGISTRY)
    files = [*(PROMPTS / 'data').rglob('*.md'), *(PROMPTS / 'skills' / 'data').rglob('SKILL.md')]
    for f in files:
        for tok in re.findall(r'`([a-z]+_[a-z_]+)`', f.read_text()):
            if tok.endswith(('_favorites', '_catalog', '_table', '_query', '_result', '_rows', '_test', '_user',
                             '_description', '_change', '_dashboard', '_sources', '_docs', '_editor', '_sql')):
                assert tok in names, f'{f.name}: `{tok}` is not a tool'


def test_analysis_prompt_stays_small(prefs):
    prefs['p'] = favs(40, 300)
    r = roles.get('data')
    s = prompting.build('data', tools_catalog=tools.risk_card(tools.for_role(r.domains, 'ask')),
                        scope_text=roles.data_scope(ALICE, {'connection': 'c1', 'database': 'db'}),
                        mode='ask', effort='medium', pages_text=roles.other_pages('data'))
    assert 'Your categories' in s and budget.text_tokens(s) < 6500, budget.text_tokens(s)      # 40 categories, 300 tables


# ---------------------------------------------------------------- the editor's SQL
def _ctx(scope):
    return Ctx(user=ALICE, role='data', scope={'page': 'data', 'session': 's1', 'connection': 'c1', **scope}, skills={})


def test_scope_inlines_short_editor_sql_and_says_it_is_current(prefs):
    line = roles.data_scope(ALICE, {'page': 'data', 'connection': 'c1', 'sql': 'SELECT 1\nFROM t', 'tab_name': 'q1',
                                        'last_error': 'boom'})
    assert "tab 'q1'" in line and '```sql\nSELECT 1\nFROM t\n```' in line and 'last run failed: boom' in line
    assert 'earlier messages may describe an older editor' in line
    fence = roles.data_scope(ALICE, {'page': 'data', 'connection': 'c1', 'sql': "SELECT '```'"})
    assert fence.count('```') == 2                          # a fence in the SQL cannot close the prompt's block
    assert 'The editor is empty now' in roles.data_scope(ALICE, {'page': 'data', 'connection': 'c1', 'sql': ''})


def test_scope_points_to_read_editor_for_long_sql(prefs):
    long_sql = 'SELECT a\nFROM secret_t\n' + '-- x\n' * 600
    line = roles.data_scope(ALICE, {'page': 'data', 'connection': 'c1', 'sql': long_sql})
    assert 'read_editor shows it' in line and 'secret_t' not in line
    cut = roles.data_scope(ALICE, {'page': 'data', 'connection': 'c1', 'sql': 'SELECT 1', 'sql_truncated': True})
    assert 'read_editor shows it' in cut


def test_a_small_model_keeps_read_editor(prefs):
    from dataviz.llm.engine import react
    role = roles.get('data')
    picked = react.pick_tools(tools.for_role(role.domains, 'ask'), 'hello there', keep=role.always)
    assert {'read_editor', 'list_sources', 'search_catalog'} <= set(picked)


def test_read_editor_returns_the_text_syntax_and_error(prefs):
    got, summary = tools.REGISTRY['read_editor'].run(_ctx({'sql': 'SELECT a\nFROM t', 'tab_name': 'q1',
                                                          'last_error': 'relation "t" does not exist', 'sql_truncated': True}), {})
    assert got['sql'] == 'SELECT a\nFROM t' and got['lines'] == 2 and got['syntax'] == 'PostgreSQL'
    assert got['truncated'] and got['tab'] == 'q1' and 'failed' in summary
    assert tools.REGISTRY['read_editor'].run(_ctx({}), {})[1] == '→ the editor is empty'


def test_check_sql_fixes_the_editor_text_and_asks_the_database(prefs, monkeypatch):
    asked = []
    monkeypatch.setattr(service, 'explain_for_ai', lambda u, sid, cid, db, sql: asked.append(sql) or {'top': []})
    got, summary = tools.REGISTRY['check_sql'].run(_ctx({'sql': 'SELECT TOP 5 [a] FROM t'}), {})
    assert got['fixed'] == 'SELECT "a" FROM t LIMIT 5' and summary == '→ fixed'
    assert asked == ['SELECT "a" FROM t LIMIT 5'] and got['database_check'].startswith('ok')


def test_check_sql_reports_the_database_error_and_skips_writes(prefs, monkeypatch):
    def boom(*a):
        raise service.DataError('column "zz" does not exist')
    monkeypatch.setattr(service, 'explain_for_ai', boom)
    got, _ = tools.REGISTRY['check_sql'].run(_ctx({}), {'sql': 'SELECT zz FROM t'})
    assert got['ok'] and 'zz' in got['database_check']
    got, _ = tools.REGISTRY['check_sql'].run(_ctx({}), {'sql': 'DELETE FROM t'})
    assert got['database_check'].startswith('skipped')
    with pytest.raises(tools.ToolError, match='empty'):
        tools.REGISTRY['check_sql'].run(_ctx({'sql': ' '}), {})


def test_set_editor_query_is_a_ui_proposal_with_its_note(prefs):
    t = tools.REGISTRY['set_editor_query']
    assert t.risk == 'ui' and set(t.schema['properties']) >= {'sql', 'mode', 'note'}
    assert t.preview(None, {'sql': 'x', 'note': 'fixed LIMIT'}) == 'fixed LIMIT'
    assert t.preview(None, {'sql': 'x', 'mode': 'new_tab'}) == 'Open this query in a new tab'
    names = tools.for_role(roles.get('data').domains, 'plan')
    assert 'read_editor' in names and 'check_sql' in names and 'set_editor_query' not in names


def test_run_in_editor_runs_a_read_on_the_page_after_the_check(prefs, monkeypatch):
    """run_in_editor is a run action: confirmed in ask, run at once in auto, not offered in plan. Its check resolves the
    page's connection and refuses empty SQL, a missing connection and anything that is not a plain read."""
    t = tools.REGISTRY['run_in_editor']
    assert t.risk == 'run' and t.domain == 'data' and set(t.schema['properties']) >= {'sql', 'mode', 'name', 'note', 'connection'}
    assert 'run_in_editor' not in tools.for_role(roles.get('data').domains, 'plan')
    assert 'run_in_editor' in tools.for_role(roles.get('data').domains, 'ask')
    seen = []
    monkeypatch.setattr(service, 'assert_read_for_ai', lambda user, sid, cid, db, sql: seen.append((sid, cid, db, sql)))
    clean, label = tools.prepare(_ctx({'database': 'db'}), 'run_in_editor', {'sql': 'SELECT 1', 'mode': 'new_tab'})
    assert clean['connection'] == 'c1' and clean['database'] == 'db' and clean['mode'] == 'new_tab'
    assert seen == [('s1', 'c1', 'db', 'SELECT 1')] and label == 'Run in the editor (new tab): SELECT 1'
    assert tools.prepare(_ctx({}), 'run_in_editor', {'sql': 'SELECT 1', 'note': 'ten biggest orders'})[1] == 'ten biggest orders'
    with pytest.raises(tools.ToolError, match='empty'):
        tools.prepare(_ctx({}), 'run_in_editor', {'sql': '  '})
    with pytest.raises(tools.ToolError, match='no connection'):
        tools.prepare(Ctx(user=ALICE, role='data', scope={'page': 'data', 'session': 's1'}, skills={}), 'run_in_editor', {'sql': 'SELECT 1'})

    def write(*a):
        raise service.DataError('this is not a plain read: use run_change, and the user confirms it')
    monkeypatch.setattr(service, 'assert_read_for_ai', write)
    with pytest.raises(tools.ToolError, match='not a plain read'):
        tools.prepare(_ctx({}), 'run_in_editor', {'sql': 'UPDATE t SET a = 1'})


def test_edit_board_checks_its_ops_before_the_page_sees_them():
    import pytest
    from dataviz.llm.tools import ToolError
    from dataviz.llm.tools import data as an
    ok, _ = an._board_check(None, {'ops': [
        {'op': 'add', 'note': {'shape': 'rect', 'layer': 'back', 'x': -50, 'y': -99999, 'w': 9000, 'h': 300, 'fill': 'BLUE', 'alpha': 3, 'text': 'Sales'}},
        {'op': 'remove', 'id': 'n1'}]})
    note = ok['ops'][0]['note']
    assert note['x'] == -50 and note['y'] == -20000 and note['w'] == 2400 and note['alpha'] == 1 and note['fill'] == 'blue'   # left / above is fine; sizes clamped
    assert ok['ops'][1] == {'op': 'remove', 'id': 'n1'}
    for bad in ({'ops': []}, {'ops': [{'op': 'move'}]}, {'ops': [{'op': 'update', 'note': {}}]},
                {'ops': [{'op': 'add', 'note': {'shape': 'star'}}]}, {'ops': [{'op': 'add', 'note': {'fill': 'pink'}}]},
                {'ops': [{'op': 'add', 'note': {'locked': True}}]}, {'ops': [{'op': 'remove', 'id': 'x'}] * 61}):
        with pytest.raises(ToolError):
            an._board_check(None, bad)


def test_edit_board_zone_and_describe_ops_need_no_coordinates():
    import pytest
    from dataviz.llm.tools import ToolError
    from dataviz.llm.tools import data as an
    ok, _ = an._board_check(None, {'arrange': 1, 'ops': [
        {'op': 'describe', 'table': 'o', 'text': '  One row per order.  '},
        {'op': 'zone', 'label': 'Sales', 'tables': ['o', 'c'], 'fill': 'Green'}]})
    assert ok['arrange'] is True
    assert ok['ops'] == [{'op': 'describe', 'table': 'o', 'text': 'One row per order.'},
                         {'op': 'zone', 'label': 'Sales', 'tables': ['o', 'c'], 'fill': 'green'}]
    for bad in ({'ops': [{'op': 'zone', 'label': 'S', 'tables': []}]}, {'ops': [{'op': 'zone', 'tables': ['o']}]},
                {'ops': [{'op': 'zone', 'label': 'S', 'tables': ['o'], 'fill': 'pink'}]},
                {'ops': [{'op': 'describe', 'table': 'o'}]}):
        with pytest.raises(ToolError):
            an._board_check(None, bad)


def test_the_board_is_named_in_the_scope_only_when_it_has_something():
    from dataviz.llm.tools import data as an
    ctx = type('C', (), {'scope': {'board': {'tables': [{'id': 'o'}], 'notes': []}}})()
    got, line = an._read_board(ctx, {})
    assert got['tables'] == [{'id': 'o'}] and '1 tables' in line
    got, line = an._read_board(type('C', (), {'scope': {}})(), {})
    assert got['board'] is None and 'no board' in line
