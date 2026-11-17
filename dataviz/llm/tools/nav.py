"""
The app's pages for every ◆ assistant (domain common): find a session, and open the page that answers the user.

    list_sessions {q?, kind?, bucket?, path?}
                                         read: the visualize / labeling sessions the user can open (id, name, kind,
                                         tags, folder); bucket + path: only the sessions made from that folder
    navigate {screen, goal, session_id?, bucket?, path?, tab?}
                                         ui: open another page (home | bucket | image | viz | label | data). The page
                                         reloads, so it ends the turn: the SAME chat continues on the new page with
                                         `goal` (the dock sends it there as a continued message; that page's
                                         specialist answers it).

    consult {page, question, session_id?}
                                         read: the other page's specialist answers one question on the server
                                         (read-only tools, <= 6 steps, its answer cut to 1500 characters; its tokens
                                         count for this turn): no page change, no extra tools in this prompt
    data_location {}                     read, Visualizer only: the bucket + folder of the open dataset
                                         (bucket/service.locate) and the folders above it, for navigate

navigate is checked here (the session must exist and be visible to the user, the bucket must resolve, the target
page's assistant must be on) and applied by the page (core/dock.ts withNav builds the URL with core/nav.ts). A
folder with no session yet is sent first (the Bucket page's send_to_session: confirmed in ask, a labeling project
always), then navigate opens it.
"""

import os

from . import E, S, Tool, ToolError, _obj, register

SCREENS = ('home', 'bucket', 'image', 'viz', 'label', 'data')
SCREEN_ZONE = {'bucket': 'bucket', 'viz': 'visualizer', 'label': 'labeling', 'data': 'data'}
SCREEN_LABEL = {'home': 'Home', 'bucket': 'Bucket', 'image': 'Image', 'viz': 'Visualizer', 'label': 'Labeling', 'data': 'Data'}
VIZ_TABS = ('explore', 'stats', 'curation', 'aug', 'model', 'eval', 'compare')     # = tools/visual.py TABS without 'ai'
DATA_TABS = ('query', 'catalog', 'scripts', 'analyze', 'dash', 'history')      # the Data page's workspaces (data/state.ts)
IMAGE_TABS = ('viz', 'label')                                                   # the Image page's two session lists
TABS = {'viz': VIZ_TABS, 'data': DATA_TABS, 'image': IMAGE_TABS}                # the tab navigate may open, per screen
MAX_SESSIONS = 40


def _sessions(user, kind, ids=None):
    from ...image import sessions as data_sessions
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
    if a.get('bucket'):                         # the sessions made from this Bucket folder (send_to_session)
        from . import _svc
        from .bucket import resolve_bucket
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


def _need_zone(screen):
    from ... import zones
    if screen == 'image' and not (zones.on('visualizer') or zones.on('labeling')):
        raise ToolError('this install has no Image page (no visualize or label sessions): tell the user')
    if screen in SCREEN_ZONE and not zones.on(SCREEN_ZONE[screen]):
        raise ToolError(f'this install has no {SCREEN_LABEL[screen]}: tell the user')


def _check_nav(ctx, a):
    from ..engine import roles
    scr = a['screen']
    _need_zone(scr)
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
    elif scr == 'bucket':
        if a.get('bucket'):
            from .bucket import resolve_bucket
            b = resolve_bucket(ctx.user, a['bucket'])
            out['bucket'] = b['id']
            if a.get('path'):
                out['path'] = str(a['path']).strip('/')
    if a.get('tab'):
        if scr not in TABS:
            raise ToolError(f'{SCREEN_LABEL[scr]} has no tabs: leave tab out')
        if a['tab'] not in TABS[scr]:
            raise ToolError(f'tab of {scr} must be one of {", ".join(TABS[scr])}')
        out['tab'] = a['tab']
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
        raise ToolError('this session was not opened from a saved dataset, so it has no Bucket folder')
    try:
        ds = reg.dataset(did)
    except KeyError:
        raise ToolError('this session has no saved dataset row: it has no Bucket folder') from None
    from . import _svc
    kind = ds.get('kind')
    if kind in ('local', 'embeddings'):
        loc = _svc().locate(ctx.user, path=ds.get('path'))
    elif kind == 's3':
        loc = _svc().locate(ctx.user, s3_bucket=ds.get('bucket'), prefix=ds.get('prefix'))
    else:
        raise ToolError(f'this session is a {kind} dataset, not a folder: it has no Bucket folder')
    if loc is None:
        raise ToolError('no bucket you can see holds this dataset\'s folder (the user can add it as '
                        'a bucket in the Bucket page)')
    return loc, f'→ {loc["bucket_name"]}/{loc["path"] or "(root)"}'


CONSULT_STEPS = 6
CONSULT_CHARS = 1500
CONSULT_NOTE = ('(Asked by the assistant on the {here} page, for the user. Answer it with your read-only tools: short, '
                'with the exact names and numbers. You cannot change anything or open pages here.)')


def _consult(ctx, a):
    """Another page's specialist answers one question, read-only, on the server: its tools and steps stay in its
    own run, only the answer comes back (no page change, no extra tools in this prompt)."""
    from ..engine import roles, subrun
    if a['page'] == 'data':
        raise ToolError('Data has no consult (its tools need the Data page: the open connection and session): call '
                        'navigate(screen="data", goal=<the question>) as your last call, the chat continues there')
    target = 'visual' if a['page'] == 'viz' else 'bucket'
    _need_zone(a['page'])
    if getattr(ctx, 'depth', 0):
        raise ToolError('a consult cannot consult again: answer with your tools')
    if target == ctx.role:
        raise ToolError('the user is on that page already: answer with your own tools')
    if (getattr(ctx, 'roles_on', None) or {}).get(target) is False:
        raise ToolError(f'the {SCREEN_LABEL[a["page"]]} assistant is switched off for this user (Settings › AI '
                        'assistant): tell the user')
    if not getattr(ctx, 'provider', None):
        raise ToolError('no model to ask here')
    state = None
    if target == 'visual':
        sid = str(a.get('session_id') or '').strip()
        if not sid:
            raise ToolError('session_id is required for viz: find it with list_sessions')
        if not _sessions(ctx.user, 'viz', ids=[sid]):
            raise ToolError(f'no visualize session {sid!r} you can open: list_sessions shows them')
        try:
            state = ctx.app.state_for(sid, ctx.user)
        except Exception as e:  # noqa: BLE001 - a session that cannot open (folder gone ...): say why
            raise ToolError(f'session {sid!r} cannot be opened: {str(e)[:200]}') from None
        if getattr(state, 'index', True) is None:
            raise ToolError(f'session {sid!r} is still being indexed: try again in a moment, or navigate there')
    here = roles.SCREEN_LABEL.get((ctx.scope or {}).get('page') or ('viz' if ctx.role == 'visual' else 'bucket'), 'other')
    q = f'{a["question"].strip()[:1500]}\n\n{CONSULT_NOTE.format(here=here)}'
    got = subrun.run_sub(ctx, target, q, state=state, steps=CONSULT_STEPS, chars=CONSULT_CHARS,
                         label=f'{SCREEN_LABEL[a["page"]]} assistant')
    for k in ('in', 'out'):
        ctx.sub_usage[k] = ctx.sub_usage.get(k, 0) + got['usage'][k]
    answer, tools_used, steps = got['answer'], got['tools'], got['steps']
    ctx.events.append({'type': 'consult', 'page': a['page'], 'steps': steps, 'tools': tools_used, 'ok': got['ok']})
    return ({'page': a['page'], 'answer': answer, 'tools_used': tools_used},
            f'→ {SCREEN_LABEL[a["page"]]} answered ({steps} step{"" if steps == 1 else "s"})')


_C = dict(domain='common', ctx=True)
register(Tool('list_sessions', 'Sessions the user can open (viz, label), for navigate; bucket + path: the ones '
                               'of that folder.',
              _obj({'q': S('words in the name or a tag (default: all)'), 'kind': E('only this kind', ['viz', 'label']),
                    'bucket': S('bucket'), 'path': S('folder in it')}),
              False, _list_sessions, **_C))
register(Tool('navigate', 'Open another page (home = the overview, bucket = Bucket, image = the list of '
                          'visualize / label sessions, viz / label = one session, data = Data: SQL / MongoDB '
                          'queries, statistics, dashboards). Ends the turn: '
                          'call it last; `goal` is what to do there (this chat continues there with it).',
              _obj({'screen': E('the page', SCREENS), 'goal': S('the next request there'),
                    'session_id': S('viz / label: from list_sessions'), 'bucket': S('bucket: bucket id or name'),
                    'path': S('bucket: folder'),
                    'tab': S('the tab to open there. viz: ' + ' | '.join(VIZ_TABS) + '. data: ' + ' | '.join(DATA_TABS)
                             + '. image: ' + ' | '.join(IMAGE_TABS))}, ['screen', 'goal']),
              False, _nav_run, _nav_label, check=_check_nav, risk='ui', **_C))
register(Tool('consult', "Ask another page's specialist one question, read-only, without leaving (viz needs "
                         'session_id). To show or change something there: navigate.',
              _obj({'page': E('whose specialist (data: not by consult, navigate there)', ['bucket', 'viz', 'data']), 'question': S('the full question, with names'),
                    'session_id': S('viz: from list_sessions')}, ['page', 'question']),
              False, _consult, **_C))
# the visualizer's way back to its folder (domain visual: only the Visual specialist has it; it only reads)
register(Tool('data_location', 'The bucket + folder of this dataset; parents[0] = "..", '
                               'parents[1] = "../..".',
              _obj({}), False, _data_location, domain='visual', ctx=True))
