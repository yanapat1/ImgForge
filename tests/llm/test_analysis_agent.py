"""The Analysis assistant knows the user's own categories: the scope line names them (and stays out when there are
none), list_favorites returns a category's tables under the result cap, and the analysis prompts name only real tools.
It reads the editor's SQL (read_editor), checks and fixes it for the connection (check_sql), and proposes SQL with
set_editor_query, which waits for the user's Apply in ask mode."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from dataviz.analysis import favorites as fav
from dataviz.analysis import service
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
    assert t.domain == 'analysis' and t.ctx and not t.mutating
    assert 'list_favorites' in tools.for_role(roles.get('analysis').domains, 'plan')     # a read: plan mode keeps it


def test_scope_line_names_the_categories(prefs):
    s = roles.analysis_scope(ALICE, {'connection': 'c1', 'database': 'db'})
    assert 'Your categories (Favorites, 3 tables): Motion (2), Sales (1).' in s
    prefs['p'] = {}
    assert 'categories' not in roles.analysis_scope(ALICE, {'connection': 'c1', 'database': 'db'})


def test_list_favorites_resolves_the_users_word(prefs):
    ctx = Ctx(user=ALICE, role='analysis', scope={}, skills={})
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
    files = [*(PROMPTS / 'analysis').rglob('*.md'), *(PROMPTS / 'skills' / 'analysis').rglob('SKILL.md')]
    for f in files:
        for tok in re.findall(r'`([a-z]+_[a-z_]+)`', f.read_text()):
            if tok.endswith(('_favorites', '_catalog', '_table', '_query', '_result', '_rows', '_test', '_user',
                             '_description', '_change', '_dashboard', '_sources', '_docs', '_editor', '_sql')):
                assert tok in names, f'{f.name}: `{tok}` is not a tool'


def test_analysis_prompt_stays_small(prefs):
    prefs['p'] = favs(40, 300)
    r = roles.get('analysis')
    s = prompting.build('analysis', tools_catalog=tools.catalog(tools.for_role(r.domains, 'ask')),
                        scope_text=roles.analysis_scope(ALICE, {'connection': 'c1', 'database': 'db'}),
                        mode='ask', effort='medium', pages_text=roles.other_pages('analysis'))
    assert 'Your categories' in s and budget.text_tokens(s) < 9000, budget.text_tokens(s)


# ---------------------------------------------------------------- the editor's SQL
def _ctx(scope):
    return Ctx(user=ALICE, role='analysis', scope={'page': 'analysis', 'session': 's1', 'connection': 'c1', **scope}, skills={})


def test_scope_line_says_the_editor_holds_sql_not_the_sql(prefs):
    line = roles.analysis_scope(ALICE, {'page': 'analysis', 'connection': 'c1', 'sql': 'SELECT 1\nFROM secret_t', 'last_error': 'boom'})
    assert '2 lines of SQL and its last run failed' in line and 'secret_t' not in line
    assert 'The editor is empty' in roles.analysis_scope(ALICE, {'page': 'analysis', 'connection': 'c1', 'sql': ''})


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
        raise service.AnalysisError('column "zz" does not exist')
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
    names = tools.for_role(roles.get('analysis').domains, 'plan')
    assert 'read_editor' in names and 'check_sql' in names and 'set_editor_query' not in names
