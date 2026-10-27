"""Helpers for the multi-session tests: small YOLO folders, visualizer sessions in the database, and the full app
(sign-in layer + /s/<id>/ routing) over a Ctx whose States index synchronously (no feature models)."""

from types import SimpleNamespace

import numpy as np
from sqlalchemy import text

from conftest import make_jpeg


def make_yolo(root, classes=('cat', 'dog'), boxes=(('a', '0 0.5 0.5 0.4 0.4\n1 0.3 0.3 0.2 0.2\n'),)):
    """A YOLO folder: classes.txt, images/<name>.jpg + labels/<name>.txt per (name, label text)."""
    (root / 'labels').mkdir(parents=True)
    (root / 'classes.txt').write_text('\n'.join(classes) + '\n')
    for k, (name, label) in enumerate(boxes):
        make_jpeg(root / 'images' / f'{name}.jpg', 64, 48, color=(20 + 40 * k, 90, 30))
        (root / 'labels' / f'{name}.txt').write_text(label)
    return root


def union_session(owner_id, name, parts, visibility='private'):
    """A combined dataset over the visualizer sessions `parts` ([(sid, alias)]) through the registry -> its id."""
    from dataviz.data import connections
    reg = connections.Registry('/nonexistent')
    ds = reg.upsert_dataset({'kind': 'union', 'name': name, 'parts': [{'id': i, 'alias': a} for i, a in parts]},
                            owner_id)
    if visibility != 'private':
        from dataviz.data import sessions
        sessions.set_sharing(ds['id'], visibility, [], owner_id)
    return ds['id']


def viz_session(owner_id, name, path, visibility='private', kind='local'):
    """A visualizer session (app.sessions + app.viz_sessions, a local or embeddings folder) -> its id."""
    from dataviz import db
    from dataviz.data import sessions
    with db.begin() as c:
        sid = sessions.create('viz', owner_id, name, visibility=visibility, conn=c)['id']
        c.execute(text("INSERT INTO app.viz_sessions(session_id, source_kind, path) VALUES (:s, :k, :p)"),
                  {'s': sid, 'k': kind, 'p': str(path)})
    return sid


def share(sid, owner_id, **members):
    """share(sid, owner, <user id>=<role>, ...) as a private session with these members."""
    from dataviz.data import sessions
    sessions.set_sharing(sid, 'private', [{'user_id': u, 'role': r} for u, r in members.items()], owner_id)


class _Job:
    def progress(self, *a, **k):
        pass


def geom_registry(calls=None):
    """A feature registry with one cheap feature 'geom' (calls: a list each computation is appended to)."""
    def fn(crops, samples, progress):
        if calls is not None:
            calls.append(len(samples))
        return np.asarray([[s['area'], s['aspect']] for s in samples], np.float32)
    return {'geom': SimpleNamespace(key='geom', label='Box geometry', group='basic', note='', text_model=None,
                                    kind=None, metric='euclidean', fn=fn)}


def make_viz_app(root, max_loaded=4):
    """(ctx, app): the real app with sign-in; ctx.builds counts the States built, each indexed at once."""
    from dataviz.core import config as vconfig
    from dataviz.core.jobs import JobManager
    from dataviz.data.embedding_source import EmbeddingSource
    from dataviz.data.sources import LocalSource
    from dataviz.server.app import Ctx, create_app
    from dataviz.state import State
    cfg = vconfig.load_config(str(vconfig.DEFAULT_CONFIG))
    cfg['logging']['http'] = False
    cfg['sessions']['max_loaded'] = max_loaded
    registry = geom_registry()
    from dataviz.data import connections
    ctx = Ctx(None, registry=registry, datasets=connections.Registry(root), cfg=cfg, jobs=JobManager())
    ctx.builds = []

    def build(ds, **kw):
        if ds.get('kind') == 'embeddings':          # its own vector files are its feature spaces (as in cli.py)
            src = EmbeddingSource(ds['path'], name=ds.get('name'))
            reg = src.feature_registry()
            st = State(src, None, reg, default_feature=next(iter(reg)), jobs=ctx.jobs, cfg=cfg, workspace_root=root,
                       dataset_id=ds['id'])
        elif ds.get('kind') == 'union':             # a combined dataset: its members through the registry
            st = State(ctx.datasets.source(ds), None, registry, default_feature='geom', jobs=ctx.jobs, cfg=cfg,
                       workspace_root=root, dataset_id=ds['id'])
        else:
            st = State(LocalSource(ds['path'], name=ds.get('name')), None, registry, default_feature='geom',
                       jobs=ctx.jobs, cfg=cfg, workspace_root=root, dataset_id=ds['id'])
        st._index_job(_Job())
        ctx.builds.append(ds['id'])
        return st
    ctx.open_dataset = build
    return ctx, create_app(ctx)
