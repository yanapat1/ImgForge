"""
The two agents. A role fixes which tool domains its agent may call, which feature switch gates it, whether it needs
the visualizer session, and how "where the user is" is described in its prompt. The Data agent and the Visual agent
never share data tools: 'data' is not among the visual role's domains, and the route builds the tool set from the
role on the server.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Role:
    name: str
    title: str
    domains: tuple
    feature: str                    # the Settings › AI switch that gates the route
    needs_session: bool


ROLES = {
    'data': Role('data', 'Data Center', ('data', 'web', 'common', 'control', 'plugin'), 'data_center', False),
    'visual': Role('visual', 'Visualizer', ('visual', 'web', 'common', 'control', 'plugin'), 'assistant', True),
}


def get(name):
    r = ROLES.get(str(name or ''))
    if r is None:
        raise KeyError(f'no agent {name!r}: data or visual')
    return r


# ---------------------------------------------------------------- "where the user is"
def data_scope(user, scope):
    """The open bucket / folder / selection, where it is read from and whether it can be changed (looked up, best
    effort): makes "this folder" resolvable and keeps the agent from proposing a full S3 load for one prefix."""
    from .. import tools
    scope = scope if isinstance(scope, dict) else {}
    bid, path = scope.get('bucket'), str(scope.get('path') or '')
    sel = [str(x) for x in scope.get('selection') or [] if isinstance(x, str)]
    if not bid:
        return 'The user is on the Data Center overview (no bucket open).'
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


SCOPES = {'data': data_scope, 'visual': visual_scope}
