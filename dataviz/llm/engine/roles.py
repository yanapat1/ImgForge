"""
The specialists behind the one ◆ chat. The chat follows the user over every page (llm/sessions.py role 'app'); the
page a message comes from picks the specialist that answers it (the router, no model call): the Bucket specialist on
the Home page, the Bucket page, the Image page and the labeling page, the Visual specialist in a visualizer session,
the Data specialist on the Data page. A role fixes
which tool domains its specialist may call, which feature switch gates it, whether it needs the visualizer session,
and how "where the user is" is described in its prompt. The specialists never share page tools: 'bucket' is not among
the visual role's domains (and back), the route builds the tool set from the role on the server, and the prompt
only names what the other pages can do (other_pages), so the model navigates there instead of guessing.

    page_of(role, scope, bound)   where a message was written: {role, screen, label, session_id?, bucket?, ...}
    other_pages(role)             the short "Other pages" card of a specialist's prompt
"""

from dataclasses import dataclass

from ... import zones


@dataclass(frozen=True)
class Role:
    name: str
    title: str
    domains: tuple
    feature: str                    # the Settings › AI switch that gates the route
    needs_session: bool
    zone: str                       # the zone that brings this specialist: without it, it does not exist
    screen: str                     # the page navigate opens for it
    always: tuple = ()              # tools a small model keeps whatever the question's words (react.pick_tools)


ROLES = {
    'bucket': Role('bucket', 'Bucket', ('bucket', 'web', 'common', 'control', 'plugin'), 'bucket_data', False,
                   'bucket', 'bucket'),
    'visual': Role('visual', 'Visualizer', ('visual', 'web', 'common', 'control', 'plugin'), 'assistant', True,
                   'visualizer', 'viz'),
    'data': Role('data', 'Data', ('data', 'web', 'common', 'control', 'plugin'), 'bucket_data', False,
                 'data', 'data', always=('read_editor', 'list_sources', 'search_catalog')),
}


SCREEN_ROLE = {'home': 'bucket', 'bucket': 'bucket', 'image': 'bucket', 'label': 'bucket', 'viz': 'visual', 'data': 'data'}
SCREEN_LABEL = {'home': 'Home', 'bucket': 'Bucket', 'image': 'Image', 'viz': 'Visualizer', 'label': 'Labeling', 'data': 'Data'}
# what each specialist's pages do, for the other one's prompt (one line: the tools stay with their page)
CAN = {
    'bucket': 'Bucket (screen bucket; image = the sessions, home = overview): buckets, folders, files, tags, S3, '
              'import / export, send a folder to the visualizer or labeling',
    'visual': 'Visualizer (screen viz, one session): maps (t-SNE, UMAP, PCA ...), colour / filter, class stats, '
              'curation, label errors, model runs + evaluation, compare, augmentation (Albumentations) preview and coverage',
    'data': 'Data (screen data): SQL and MongoDB queries on the user\'s databases, joins, statistics, '
            'forecasts, dashboards, zones and notes on the visual builder board',
}


def installed():
    """The specialists this install has."""
    return {k: r for k, r in ROLES.items() if zones.on(r.zone)}


def get(name):
    r = ROLES.get(str(name or ''))
    if r is None:
        raise KeyError(f'no agent {name!r}: bucket, visual or data')
    return r


def page_of(role, scope, bound=None):
    """Where a message was written (llm_messages.page): the router's input and the history's page markers."""
    scope = scope if isinstance(scope, dict) else {}
    if role == 'visual':
        out = {'role': 'visual', 'screen': 'viz', 'label': 'Visualizer'}
        if bound is not None:
            out['session_id'] = str(bound.get('dataset_id') or '')
            if bound.get('name'):
                out['label'] = f'Visualizer · {bound["name"]}'
        tab = ((scope.get('view') or {}) if isinstance(scope.get('view'), dict) else {}).get('tab')
        if isinstance(tab, str) and tab:
            out['tab'] = tab
        return out
    screen = scope.get('page') if scope.get('page') in ('home', 'image', 'label') else 'bucket'
    if role == 'data':
        return {'role': 'data', 'screen': 'data', 'label': SCREEN_LABEL['data']}
    out = {'role': 'bucket', 'screen': screen, 'label': SCREEN_LABEL[screen]}
    if screen == 'label' and scope.get('label_project'):
        out['session_id'] = str(scope['label_project'])
    if screen == 'bucket' and scope.get('bucket'):
        out['bucket'] = str(scope['bucket'])
        if scope.get('path'):
            out['path'] = str(scope['path'])
            out['label'] = f'Bucket · {str(scope["path"]).rstrip("/").rsplit("/", 1)[-1]}'
    return out


def other_pages(role):
    """The "Other pages" card: one chat follows the user, only this page's tools are here."""
    here = installed()
    others = [f'Elsewhere: {CAN[k]}.' for k in CAN if k != role and k in here]
    chosen = zones.selected()
    missing = [z.title for k, z in zones.ZONES.items() if k not in chosen]
    return ('## Other pages\nThis chat follows the user across the app; you have only this page\'s tools. '
            + ' '.join(others)
            + (' A question about it: `consult`; to see or change it: `navigate` (`list_sessions` finds ids). Never '
               'call another page\'s tools.' if others else '')
            + (f' This install has no {", ".join(missing)}: never offer it, and say so if the user asks for it.'
               if missing else ''))


# ---------------------------------------------------------------- "where the user is"
def bucket_scope(user, scope):
    """The open bucket / folder / selection, where it is read from and whether it can be changed (looked up, best
    effort): makes "this folder" resolvable and keeps the agent from proposing a full S3 load for one prefix."""
    from .. import tools
    scope = scope if isinstance(scope, dict) else {}
    bid, path = scope.get('bucket'), str(scope.get('path') or '')
    sel = [str(x) for x in scope.get('selection') or [] if isinstance(x, str)]
    page = scope.get('page')
    if page == 'home':
        return ('The user is on the Home page (the overview: one card each for Bucket, Image and Data, with counts and '
                'recent items), not in the Bucket page.')
    if page == 'image':
        return 'The user is on the Image page (the list of visualize and label sessions), not in the Bucket page.'
    if page == 'label':
        pid = str(scope.get('label_project') or '')[:64]
        return (f'The user is on the labeling page of project {pid!r} (editing boxes; its own Label assistant handles '
                'boxes there). You can answer about the data behind it.')
    if not bid:
        return 'The user is on the Bucket page overview (no bucket open).'
    st = None
    try:
        st = tools._svc().folder_state(user, bid, path)
    except Exception:  # noqa: BLE001 - a stale scope: the model still has the id and the tools
        pass
    kind = st and (st['kind'] + (f', mode {st["mode"]}' if st.get('mode') else ''))
    out = [f'The user has bucket {bid!r}' + (f' ({st["name"]!r}, {kind})' if st else '') + ' open, folder '
           + (repr(path) if path else 'the root') + (f' (location {st["location"]})' if st else '') + '.']
    if st and st.get('s3_url') and st['s3_url'] != st['location']:
        out.append(f'In S3: {st["s3_url"]}.')
    if st and st['kind'] == 's3' and not st['writable']:          # streamed (a loaded-whole link is writable)
        if st['editable']:
            out.append(f'This folder is downloaded (in {st["downloaded_in"] or "the whole bucket"!r}, state '
                       f'{st["local"]}) and editable: create_folder / move / rename / delete / upload here change only '
                       'the local copy (not S3). Do NOT propose loading the whole bucket for it.')
        elif st.get('local') == 'fetching':
            out.append('This folder is being downloaded right now: changes wait until the download finished.')
        else:
            down = st.get('downloaded') or []
            out.append('This folder is streamed (read-only). To change it, propose download_s3_folders for just '
                       'this folder (only that prefix is downloaded), then the changes inside it; set_s3_mode local '
                       'only when the user wants the whole bucket.'
                       + (f' Downloaded folders: {", ".join(repr(p) for p in down)}.' if down else ''))
    if sel:
        shown = sel[:50]
        out.append(f'Selected ({len(sel)}): ' + ', '.join(shown) + (' ...' if len(sel) > len(shown) else '')
                   + '. "These" / "the selection" means these paths.')
    return ' '.join(out)


VIEW_KEYS = (('tab', 'open tab'), ('dataset', 'dataset'), ('feature', 'features (embedding)'),
             ('method', 'projection method'), ('params', 'method parameters'), ('color_by', 'colour by'),
             ('classes', 'classes shown'), ('filter', 'filter'), ('selection', 'selected boxes'),
             ('issue', 'Find issues view'), ('quality', 'map quality'))


def visual_scope(user, scope):
    """The visualizer's state as the page reports it (scope.view): which features / method / colour are on."""
    v = (scope or {}).get('view') if isinstance(scope, dict) else None
    if not isinstance(v, dict) or not v:
        return 'The page did not report its state: call view_state if you need it.'
    rows = []
    for k, label in VIEW_KEYS:
        x = v.get(k)
        if x in (None, '', [], {}):
            continue
        if isinstance(x, (dict, list)):
            import json
            x = json.dumps(x, ensure_ascii=False)[:400]
        rows.append(f'- {label}: {x}')
    return 'The visualizer shows:\n' + '\n'.join(rows) if rows else 'The visualizer state is empty.'


INLINE_SQL = 1500                   # editor SQL up to this many characters is in the prompt itself (no read_editor step)


def data_scope(user, scope):
    """What the page has open: the connection and database, the provider's syntax, the last result."""
    from ...data import service
    scope = scope if isinstance(scope, dict) else {}
    cid, db, rid = scope.get('connection'), scope.get('database'), scope.get('result')
    kind = next((s['kind'] for s in service.sources(user) if s['id'] == cid), None)
    out = [f'Connection {cid!r} ({kind}), database {db!r}.' if cid else 'No connection is chosen on the page.']
    if rid:
        out.append(f'The open tab\'s last result is {rid!r}.')
    sql = str(scope.get('sql') or '')
    failed = f' Its last run failed: {str(scope["last_error"])[:300]}' if scope.get('last_error') else ''
    if sql.strip():
        n = sql.count('\n') + 1
        if len(sql) <= INLINE_SQL and not scope.get('sql_truncated'):
            tab_name = str(scope.get('tab_name') or '')[:80]
            out.append(f'The editor{f" (tab {tab_name!r})" if tab_name else ""} holds this SQL now:\n```sql\n'
                       f'{sql.replace("```", chr(39) * 3)}\n```{failed}')
        else:
            out.append(f'The editor holds {n} line{"s" if n > 1 else ""} of SQL now (read_editor shows it).{failed}')
    elif scope.get('page') == 'data':
        out.append('The editor is empty now.')
    if scope.get('page') == 'data':
        out.append('This is the page as of this message: earlier messages may describe an older editor.')
    board = scope.get('board') if isinstance(scope.get('board'), dict) else None
    if board:
        out.append(f'The visual builder board shows {len(board.get("tables") or [])} tables and {len(board.get("notes") or [])} notes '
                   '(read_board lists them; edit_board adds zones, text and sticky notes).')
    if scope.get('rows_ok'):
        out.append('The user allowed row samples for this session.')
    else:
        out.append('Row samples are off: answer from aggregates.')
    from ...data import aiaccess, favorites
    prefs = service.user_prefs(user)
    line = favorites.index_line(favorites.load(prefs))
    access = aiaccess.index_line(aiaccess.items(prefs), cid)
    return ' '.join(out) + ''.join(f'\n{x}' for x in (line, access) if x)


SCOPES = {'bucket': bucket_scope, 'visual': visual_scope, 'data': data_scope}
