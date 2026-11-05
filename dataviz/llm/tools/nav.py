"""
The app's pages for every ◆ assistant (domain common): find a session, and open the page that answers the user.

    list_sessions {q?, kind?, bucket?, path?}
                                         read: the visualize / labeling sessions the user can open (id, name, kind,
                                         tags, folder); bucket + path: only the sessions made from that folder
    navigate {screen, goal, session_id?, bucket?, path?, tab?}
                                         ui: open another page (home | data | viz | label). The page reloads, so it
                                         ends the turn: the SAME chat continues on the new page with `goal` (the dock
                                         sends it there as a continued message; that page's specialist answers it).

    data_location {}                     read, Visualizer only: the Data Center bucket + folder of the open dataset
                                         (datacenter/service.locate) and the folders above it, for navigate

navigate is checked here (the session must exist and be visible to the user, the bucket must resolve, the target
page's assistant must be on) and applied by the page (core/dock.ts withNav builds the URL with core/nav.ts). A
folder with no session yet is sent first (the Data Center's send_to_session: confirmed in ask, a labeling project
always), then navigate opens it.
"""

import os

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


def _sources(ctx):
    """{viz session id: its source} ({path} | {s3_bucket, prefix}) of the sessions the user sees (one query)."""
    reg = getattr(getattr(ctx, 'app', None), 'datasets', None)
    if reg is None:
        return {}
    try:
        rows = reg.datasets(ctx.user)
    except Exception:  # noqa: BLE001 - no database: no sources, the list still works
        return {}
    out = {}
    for d in rows:
        if d.get('kind') in ('local', 'embeddings') and d.get('path'):
            out[d['id']] = {'path': os.path.realpath(d['path'])}
        elif d.get('kind') == 's3':
            out[d['id']] = {'s3_bucket': d.get('bucket'), 'prefix': str(d.get('prefix') or '').strip('/')}
    return out


def _folder_text(src):
    return src['path'] if 'path' in src else f's3://{src["s3_bucket"]}/{src["prefix"]}'.rstrip('/')


def _list_sessions(ctx, a):
    kinds = [a['kind']] if a.get('kind') else ['viz', 'label']
    q = str(a.get('q') or '').strip().lower()
    want = None
    if a.get('bucket'):                         # the sessions made from this Data Center folder (send_to_session)
        from . import _svc
        from .datacenter import resolve_bucket
        b = resolve_bucket(ctx.user, a['bucket'])
        want = _svc().folder_source(ctx.user, b['id'], a.get('path') or '')
        kinds = ['viz']
    src = _sources(ctx) if 'viz' in kinds else {}
    out = []
    for k in kinds:
        for s in _sessions(ctx.user, k):
            if q and q not in s['name'].lower() and not any(q in t.lower() for t in s.get('tags') or []):
                continue
            if want is not None and src.get(s['id']) != want:
                continue
            row = {'id': s['id'], 'name': s['name'], 'kind': k, 'tags': s.get('tags') or [], 'role': s.get('my_role')}
            if s['id'] in src:
                row['folder'] = _folder_text(src[s['id']])
            out.append(row)
    return {'sessions': out[:MAX_SESSIONS], 'total': len(out)}, f'→ {len(out)} session{"" if len(out) == 1 else "s"}'


def _check_nav(ctx, a):
    from ..engine import roles
    scr = a['screen']
    out = {'screen': scr, 'goal': str(a.get('goal') or '').strip()[:500]}
    if not out['goal']:
        raise ToolError('goal is required: what to continue doing on that page (the chat continues there with it)')
    role = roles.get(roles.SCREEN_ROLE[scr])
    if (getattr(ctx, 'roles_on', None) or {}).get(role.name) is False:
        raise ToolError(f'the assistant of the {SCREEN_LABEL[scr]} is switched off for this user (Settings › AI '
                        f'assistant › {role.feature}): tell the user, and that they can open the page themselves')
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


def _data_location(ctx, a):
    did = getattr(ctx.state, 'dataset_id', None)
    reg = getattr(ctx.app, 'datasets', None)
    if not did or reg is None:
        raise ToolError('this session was not opened from a saved dataset, so it has no Data Center folder')
    try:
        ds = reg.dataset(did)
    except KeyError:
        raise ToolError('this session has no saved dataset row: it has no Data Center folder') from None
    from . import _svc
    kind = ds.get('kind')
    if kind in ('local', 'embeddings'):
        loc = _svc().locate(ctx.user, path=ds.get('path'))
    elif kind == 's3':
        loc = _svc().locate(ctx.user, s3_bucket=ds.get('bucket'), prefix=ds.get('prefix'))
    else:
        raise ToolError(f'this session is a {kind} dataset, not a folder: it has no Data Center folder')
    if loc is None:
        raise ToolError('no Data Center bucket you can see holds this dataset\'s folder (the user can add it as '
                        'a bucket in the Data Center)')
    return loc, f'→ {loc["bucket_name"]}/{loc["path"] or "(root)"}'


_C = dict(domain='common', ctx=True)
register(Tool('list_sessions', 'Sessions the user can open (viz, label), for navigate; bucket + path: the ones '
                               'of that folder.',
              _obj({'q': S('words in the name or a tag (default: all)'), 'kind': E('only this kind', ['viz', 'label']),
                    'bucket': S('Data Center bucket'), 'path': S('folder in it')}),
              False, _list_sessions, **_C))
register(Tool('navigate', 'Open another page (home, data = Data Center, viz / label = a session). Ends the turn: '
                          'call it last; `goal` is what to do there (this chat continues there with it).',
              _obj({'screen': E('the page', SCREENS), 'goal': S('the next request there'),
                    'session_id': S('viz / label: from list_sessions'), 'bucket': S('data: bucket'),
                    'path': S('data: folder'), 'tab': E('viz: view', VIZ_TABS)}, ['screen', 'goal']),
              False, _nav_run, _nav_label, check=_check_nav, risk='ui', **_C))
# the visualizer's way back to its folder (domain visual: only the Visual specialist has it; it only reads)
register(Tool('data_location', 'The Data Center bucket + folder of this dataset; parents[0] = "..", '
                               'parents[1] = "../..".',
              _obj({}), False, _data_location, domain='visual', ctx=True))
