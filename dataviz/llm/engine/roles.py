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
    """The open bucket / folder / selection (the bucket's name looked up, best effort): makes "this folder" resolvable."""
    from .. import tools
    scope = scope if isinstance(scope, dict) else {}
    bid, path = scope.get('bucket'), str(scope.get('path') or '')
    sel = [str(x) for x in scope.get('selection') or [] if isinstance(x, str)]
    if not bid:
        return 'The user is on the Data Center overview (no bucket open).'
    name = kind = None
    try:
        b = tools._svc().get_bucket(user, bid)
        name, kind = b.get('name'), b.get('kind')
    except Exception:  # noqa: BLE001 - a stale scope: the model still has the id and the tools
        pass
    out = [f'The user has bucket {bid!r}' + (f' ({name!r}, {kind})' if name else '') + ' open, folder '
           + (repr(path) if path else 'the root') + '.']
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
