"""
The app's pages for every ◆ assistant (domain common): find a session, and open the page that answers the user.

    list_sessions {q?, kind?}            read: the visualize / labeling sessions the user can open (id, name, kind, tags)
    navigate {screen, goal, session_id?, bucket?, path?, tab?}
                                         ui: open another page (home | data | viz | label). The page reloads, so it
                                         ends the turn: the chat continues on the new page with `goal` (the dock sends
                                         it as the first message there, with this chat's summary handed over).

navigate is checked here (the session must exist and be visible to the user, the bucket must resolve) and applied
by the page (core/dock.ts withNav builds the URL with core/nav.ts). Creating sessions or labeling projects is not
something the assistant does (Home › New session; the Data Center's Send to…).
"""

from . import E, S, Tool, ToolError, _obj, register

SCREENS = ('home', 'data', 'viz', 'label')
SCREEN_LABEL = {'home': 'Home', 'data': 'Data Center', 'viz': 'Visualizer', 'label': 'Labeling'}
VIZ_TABS = ('explore', 'stats', 'curation', 'model', 'eval', 'compare')
MAX_SESSIONS = 40


def _sessions(user, kind, ids=None):
    from ...data import sessions as data_sessions
    try:
        return data_sessions.listing(user, kind, 'all', ids=ids)
    except Exception as e:  # noqa: BLE001 - no database (tests without accounts): nothing to list
        raise ToolError(f'the sessions cannot be read here ({type(e).__name__})') from None


def _list_sessions(ctx, a):
    kinds = [a['kind']] if a.get('kind') else ['viz', 'label']
    q = str(a.get('q') or '').strip().lower()
    out = []
    for k in kinds:
        for s in _sessions(ctx.user, k):
            if q and q not in s['name'].lower() and not any(q in t.lower() for t in s.get('tags') or []):
                continue
            out.append({'id': s['id'], 'name': s['name'], 'kind': k, 'tags': s.get('tags') or [],
                        'role': s.get('my_role')})
    return {'sessions': out[:MAX_SESSIONS], 'total': len(out)}, f'→ {len(out)} session{"" if len(out) == 1 else "s"}'


def _check_nav(ctx, a):
    scr = a['screen']
    out = {'screen': scr, 'goal': str(a.get('goal') or '').strip()[:500]}
    if not out['goal']:
        raise ToolError('goal is required: what to continue doing on that page (it becomes the first message there)')
    if scr in ('viz', 'label'):
        sid = str(a.get('session_id') or '').strip()
        if not sid:
            raise ToolError(f'session_id is required for {scr}: find it with list_sessions')
        if not _sessions(ctx.user, scr, ids=[sid]):
            raise ToolError(f'no {"visualize" if scr == "viz" else "labeling"} session {sid!r} you can open: '
                            'list_sessions shows them')
        out['session_id'] = sid
        if scr == 'viz' and a.get('tab'):
            if a['tab'] not in VIZ_TABS:
                raise ToolError(f'tab must be one of {", ".join(VIZ_TABS)}')
            out['tab'] = a['tab']
    elif scr == 'data':
        if a.get('bucket'):
            from .datacenter import resolve_bucket
            b = resolve_bucket(ctx.user, a['bucket'])
            out['bucket'] = b['id']
            if a.get('path'):
                out['path'] = str(a['path']).strip('/')
    return out, None


def _nav_label(b, a):
    where = SCREEN_LABEL.get(a.get('screen'), a.get('screen'))
    extra = a.get('tab') or a.get('path') or ''
    return f'Open {where}' + (f' › {extra}' if extra else '')


def _nav_run(ctx, a):
    clean, _ = _check_nav(ctx, a)
    return {'ui_action': True, 'args': clean}, '→ opening the page'


_C = dict(domain='common', ctx=True)
register(Tool('list_sessions', 'Visualize (viz) and labeling (label) sessions the user can open, for navigate.',
              _obj({'q': S('words in the name or a tag (default: all)'), 'kind': E('only this kind', ['viz', 'label'])}),
              False, _list_sessions, **_C))
register(Tool('navigate', 'Open another page (home, data = Data Center, viz / label = a session). Ends the turn: '
                          'call it last; `goal` is what to do there (the chat continues with it).',
              _obj({'screen': E('the page', SCREENS), 'goal': S('the next request there'),
                    'session_id': S('viz / label: from list_sessions'), 'bucket': S('data: bucket'),
                    'path': S('data: folder'), 'tab': E('viz: view', VIZ_TABS)}, ['screen', 'goal']),
              False, _nav_run, _nav_label, check=_check_nav, risk='ui', **_C))
