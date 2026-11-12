"""
Zones: the parts of the product an install can have. Core is always there; the others can be left out, so an analyst
can run Bucket + Data without the visualizer and its GPU libraries. See ARCHITECTURE.md.

    configure(cfg)       remember the server's config: code without a cfg at hand (the assistant) then gets the same answer
    selected(cfg)        the zones of this install, in dependency order (core first)
    on(zone, cfg)        is this zone part of the install?
    route_modules(cfg)   the server/routes modules of those zones, in the order they are included
    send_targets(cfg)    the Bucket "Send to" targets of those zones
    bucket_session_finders(cfg)   what lists each zone's sessions on a bucket card
    perms_off(cfg)       the tier permissions that mean nothing in this install

The choice comes from DATAVIZ_ZONES (comma separated), else `zones:` in config.yaml, else every zone. Core is added
when it is not named. A zone needs the zones in its `requires`: a choice that misses one is refused at start-up, so a
broken install stops with a clear message instead of failing on the first request.

Nothing here imports a zone: importing dataviz.zones must stay free of torch, cv2 and the like.
"""

import importlib
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Zone:
    id: str
    title: str
    requires: tuple = ()
    send_target: str = ''           # the Bucket's "Send to" target this zone receives
    bucket_sessions: str = ''       # 'module:function': (rows, conn, out) adds this zone's sessions to the bucket cards
    perms: tuple = ()               # the tier permissions (auth/tiers.py) that only mean something with this zone


ZONES = {z.id: z for z in (
    Zone('core', 'Core'),
    Zone('bucket', 'Bucket', requires=('core',), perms=('bucket.manage',)),
    Zone('visualizer', 'Image · Visualize', requires=('bucket',), send_target='visualize',
         bucket_sessions='dataviz.image.bucket_sessions:find', perms=('dataset.create', 'model.add', 'model.upload')),
    Zone('labeling', 'Image · Label', requires=('bucket',), send_target='label',
         bucket_sessions='dataviz.labeling.bucket_sessions:find', perms=('label.create', 'model.add', 'model.upload')),
    Zone('data', 'Data', requires=('bucket',), perms=('data.use', 'data.write')),
)}

# (server/routes module, its zone), in include order. auth.py, users.py, admin.py, me.py and sharing.py are not here:
# the sign-in layer (auth/middleware.py) installs those.
ROUTES = (
    ('system', 'core'),
    ('session_data', 'visualizer'),
    ('projections', 'visualizer'),
    ('samples', 'visualizer'),
    ('datasets', 'visualizer'),
    ('connections', 'core'),
    ('models', 'visualizer'),
    ('labeling', 'labeling'),
    ('label_import', 'labeling'),
    ('curation', 'visualizer'),
    ('llm', 'core'),
    ('compare', 'visualizer'),
    ('augment', 'visualizer'),
    ('live', 'visualizer'),
    ('promote', 'visualizer'),
    ('bucket', 'bucket'),
    ('data', 'data'),
    ('agent', 'core'),
    ('sessions', 'core'),
    ('assets', 'core'),
)


_active = None


def configure(cfg):
    global _active
    _active = cfg


def _named(cfg):
    env = os.environ.get('DATAVIZ_ZONES')
    if env is not None and env.strip():
        return [x.strip() for x in env.split(',') if x.strip()]
    value = (_active if cfg is None else cfg or {}) or {}
    value = value.get('zones')
    return list(value) if value else None


def selected(cfg=None):
    """The ids of this install's zones, in ZONES order. ValueError: an unknown zone, or a zone's requirement is left out."""
    named = _named(cfg)
    if named is None:
        return tuple(ZONES)
    unknown = [z for z in named if z not in ZONES]
    if unknown:
        raise ValueError(f'unknown zone {", ".join(unknown)} (the zones are {", ".join(ZONES)})')
    chosen = {'core', *named}
    for z in chosen:
        missing = [r for r in ZONES[z].requires if r not in chosen]
        if missing:
            raise ValueError(f'zone {z} needs {", ".join(missing)}: add it to DATAVIZ_ZONES or `zones:` in the config')
    return tuple(z for z in ZONES if z in chosen)


def on(zone, cfg=None):
    return zone in selected(cfg)


def send_targets(cfg=None):
    return tuple(ZONES[z].send_target for z in selected(cfg) if ZONES[z].send_target)


def perms_off(cfg=None):
    chosen = selected(cfg)
    keep = {p for z in chosen for p in ZONES[z].perms}
    return {p for z in ZONES if z not in chosen for p in ZONES[z].perms} - keep


def bucket_session_finders(cfg=None):
    found = []
    for z in selected(cfg):
        if ZONES[z].bucket_sessions:
            module, name = ZONES[z].bucket_sessions.split(':')
            found.append(getattr(importlib.import_module(module), name))
    return found


def route_modules(cfg=None):
    chosen = selected(cfg)
    return [importlib.import_module(f'dataviz.server.routes.{name}') for name, zone in ROUTES if zone in chosen]


# ---------------------------------------------------------------------------------------------------- what an install holds
# The files that only a zone uses, so an install without that zone can leave them out: `python -m dataviz.zones prune`
# deletes them (the Docker image does it for ZONES=bucket,data). A row's zones are the ones that need it: the
# file goes only when none of them is installed. tests/zones/test_install.py checks that the kept code never imports a
# pruned file at load time and that a pruned copy starts and serves its pages.
#
# Frontend rows name the TypeScript source under static/ts/; the built static/js/ copy (.js and .js.map) goes with it.
# Most of dataviz/labeling stays in every install: the Bucket page reuses its sinks, writers, media, formats and projects.
VIZ = ('visualizer',)
VIZ_OR_LABEL = ('visualizer', 'labeling')
FILES = (
    # backend
    ('dataviz/state.py', VIZ), ('dataviz/visualizer_boot.py', VIZ), ('dataviz/core/workers.py', VIZ),
    ('dataviz/server/binary.py', VIZ), ('dataviz/llm/tools/visual.py', VIZ),
    ('dataviz/llm/prompts/visual/', VIZ), ('dataviz/llm/prompts/skills/visual/', VIZ),
    *((f'dataviz/image/{m}.py', VIZ) for m in ('curation', 'compare', 'diff', 'imgstats', 'promote', 'shift', 'stress', 'augment', 'augment_cover', 'augment_run', 'augment_model',
                                               'indexer', 'views', 'union', 'reuse', 'live', 'patching', 'bucket_sessions')),
    *((f'dataviz/server/routes/{m}.py', VIZ) for m, z in ROUTES if z == 'visualizer'),
    ('dataviz/ml/', VIZ_OR_LABEL), ('dataviz/models/', VIZ_OR_LABEL), ('dataviz/server/models.py', VIZ_OR_LABEL),
    *((f'dataviz/server/routes/{m}.py', ('labeling',)) for m, z in ROUTES if z == 'labeling'),
    *((f'dataviz/labeling/{m}.py', ('labeling',)) for m in ('export', 'importers', 'bucket_sessions')),
    # frontend (static/ts and its build in static/js)
    ('static/ts/viz/', VIZ), ('static/ts/plot/', VIZ),
    *((f'static/ts/views/{m}.ts', VIZ) for m in (
        'assistant', 'box-editor', 'chart-util', 'compare', 'compare-shift', 'compare-stress', 'compare-viewer', 'controls',
        'curation', 'eval', 'ev-util', 'explore-bind', 'filter-bar', 'gallery', 'image-grid', 'layout', 'modal',
        'model-images', 'model', 'pick-bar', 'preview', 'stat-chip', 'stats', 'viewer', 'augment', 'augment-check', 'augment-cover', 'augment-model', 'augment-ghost')),
    *((f'static/ts/core/{m}.ts', VIZ) for m in ('live', 'compare-core', 'cmp-keys')),
    *((f'static/css/{m}.css', VIZ) for m in ('curation', 'compare', 'compare-shift', 'augment')),
)


def prunable(cfg=None):
    """The paths (relative to the app folder) this install can leave out: no zone of theirs is installed."""
    chosen = set(selected(cfg))
    out = []
    for path, need in FILES:
        if chosen.isdisjoint(need):
            out.append(path)
            if path.startswith('static/ts/'):
                js = 'static/js/' + path.removeprefix('static/ts/')
                out += [js] if js.endswith('/') else [js[:-3] + '.js', js[:-3] + '.js.map']
    return out


def prune(root, cfg=None):
    """Delete the files of the zones this install leaves out, under the app folder `root`. Returns what went."""
    import shutil
    from pathlib import Path
    root, gone = Path(root), []
    for rel in prunable(cfg):
        p = root / rel
        if p.is_dir():
            shutil.rmtree(p)
        elif p.is_file():
            p.unlink()
        else:
            continue
        gone.append(rel)
    return gone


if __name__ == '__main__':                                  # python -m dataviz.zones prune [--root <app folder>]
    import argparse
    from pathlib import Path
    ap = argparse.ArgumentParser(prog='python -m dataviz.zones', description='Leave out the files of zones this install '
                                 'does not have (DATAVIZ_ZONES or `zones:` in config.yaml).')
    ap.add_argument('command', choices=['prune', 'list'])
    ap.add_argument('--root', default=str(Path(__file__).resolve().parent.parent), help='the app folder (default: this one)')
    a = ap.parse_args()
    print(f'zones: {", ".join(selected())}')
    if a.command == 'list':
        print('\n'.join(prunable()) or 'nothing to leave out')
    else:
        gone = prune(a.root)
        print(f'removed {len(gone)} paths' + ''.join(f'\n  {g}' for g in gone))
