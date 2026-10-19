"""
Workspace folders: one auto-named cache folder per dataset version.

    <workspace_root>/<dataset name>__<fingerprint[:8]>/   crops/, features/, projections/, scores/, samples.json, ...

A changed dataset gets a new fingerprint, so a new folder: earlier results are never overwritten.
"""

import json
import time
from pathlib import Path

from .core import logging as vlog
from .data import sources


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def resolve_workspace(root, source, fp=None, log=None):
    """Auto-named folder per dataset version: <root>/<dataset name>__<fingerprint[:8]>.

    A changed dataset gets a new folder, so earlier results are never overwritten. A legacy
    <root>/<dataset name>/ folder with the same fingerprint is renamed into place.
    `source` is a sources.LocalSource / S3Source (a folder path is accepted too).
    """
    if not hasattr(source, 'fingerprint'):
        source = sources.LocalSource(source)
    lg = vlog.get_logger('workspace')
    root = Path(root)
    fp = fp or source.fingerprint()
    ws = root / f'{source.name}__{fp[:8]}'
    legacy = root / source.name
    if source.kind == 'local' and not ws.exists() and not legacy.is_symlink() and (legacy / 'samples.json').exists():
        try:
            if json.loads((legacy / 'samples.json').read_text()).get('fingerprint') == fp:
                legacy.rename(ws)
                lg.info(f'moved {vlog.esc(legacy)} -> {vlog.esc(ws.name)} (kept all computed results)')
        except (OSError, ValueError) as e:
            lg.warning(f'could not migrate {vlog.esc(legacy)}: {vlog.esc(e)}')
    new = not ws.exists()
    ws.mkdir(parents=True, exist_ok=True)
    meta_path = ws / 'workspace.json'
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {'created': _now()}
    meta.update({'dataset': source.uri, 'fingerprint': fp, 'last_opened': _now()})
    meta_path.write_text(json.dumps(meta, indent=1))
    lg.info(f'{"[green]new[/]" if new else "using"} {vlog.esc(ws)}')
    return ws


def list_workspaces(root, name):
    out = []
    for d in sorted(Path(root).glob(f'{name}__*')):
        m = d / 'workspace.json'
        if m.exists():
            meta = json.loads(m.read_text())
            out.append({'name': d.name, 'created': meta.get('created'), 'last_opened': meta.get('last_opened')})
    return out


_meta_memo = {}     # workspace.json path -> (mtime_ns, parsed): the Sessions home re-reads only changed files


def _read_meta(path):
    try:
        mt = path.stat().st_mtime_ns
    except OSError:
        return None
    hit = _meta_memo.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        meta = json.loads(path.read_text())
    except (OSError, ValueError):
        meta = None
    _meta_memo[path] = (mt, meta)
    return meta


def find_workspace(root, uri):
    """(folder, workspace.json dict) of the most recently opened workspace of the dataset at `uri`, or None.

    Matched by the dataset uri, not the folder name, so a renamed session still finds its results.
    Reads only the small workspace.json files (never samples.json): cheap enough for every page load.
    """
    best = None
    for m in Path(root).glob('*/workspace.json'):
        meta = _read_meta(m)
        if meta and meta.get('dataset') == uri and (best is None or
                                                    (meta.get('last_opened') or '') > (best[1].get('last_opened') or '')):
            best = (m.parent, meta)
    return best
