"""
Zones: the parts of the product an install can have. Core is always there; the others can be left out, so an analyst
can run Data Center + Data Analysis without the visualizer and its GPU libraries. See ARCHITECTURE.md.

    selected(cfg)        the zones of this install, in dependency order (core first)
    on(zone, cfg)        is this zone part of the install?
    route_modules(cfg)   the server/routes modules of those zones, in the order they are included

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


ZONES = {z.id: z for z in (
    Zone('core', 'Core'),
    Zone('datacenter', 'Data Center', requires=('core',)),
    Zone('labeling', 'Labeling', requires=('datacenter',)),
    Zone('visualizer', 'Dataset Visualizer', requires=('datacenter',)),
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
    ('agent', 'core'),
    ('sessions', 'core'),
    ('assets', 'core'),
)


def _named(cfg):
    env = os.environ.get('DATAVIZ_ZONES')
    if env is not None and env.strip():
        return [x.strip() for x in env.split(',') if x.strip()]
    value = (cfg or {}).get('zones')
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


def route_modules(cfg=None):
    chosen = selected(cfg)
    return [importlib.import_module(f'dataviz.server.routes.{name}') for name, zone in ROUTES if zone in chosen]
