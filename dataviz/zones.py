"""
Zones: the parts of the product an install can have. Core is always there; the others can be left out, so an analyst
can run Data Center + Data Analysis without the visualizer and its GPU libraries. See ARCHITECTURE.md.

    configure(cfg)       remember the server's config: code without a cfg at hand (the assistant) then gets the same answer
    selected(cfg)        the zones of this install, in dependency order (core first)
    on(zone, cfg)        is this zone part of the install?
    route_modules(cfg)   the server/routes modules of those zones, in the order they are included
    send_targets(cfg)    the Data Center "Send to" targets of those zones
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
    send_target: str = ''           # the Data Center's "Send to" target this zone receives
    bucket_sessions: str = ''       # 'module:function': (rows, conn, out) adds this zone's sessions to the bucket cards
    perms: tuple = ()               # the tier permissions (auth/tiers.py) that only mean something with this zone


ZONES = {z.id: z for z in (
    Zone('core', 'Core'),
    Zone('datacenter', 'Data Center', requires=('core',), perms=('data.manage',)),
    Zone('visualizer', 'Dataset Visualizer', requires=('datacenter',), send_target='visualize',
         bucket_sessions='dataviz.data.bucket_sessions:find', perms=('dataset.create', 'model.add', 'model.upload')),
    Zone('analysis', 'Data Analysis', requires=('datacenter',), perms=('analysis.use', 'analysis.write')),
    Zone('labeling', 'Labeling', requires=('datacenter',), send_target='label',
         bucket_sessions='dataviz.labeling.bucket_sessions:find', perms=('label.create', 'model.add', 'model.upload')),
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
    ('live', 'visualizer'),
    ('promote', 'visualizer'),
    ('datacenter', 'datacenter'),
    ('analysis', 'analysis'),
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
