"""
Command line: read the config (flags win over config.yaml), bind the port, connect to PostgreSQL (migrate,
secret key, default settings, the first admin, the one-time import of the old JSON / SQLite stores), start the
worker pools and serve. The page opens on the Sessions home; sessions load on first use at /s/<id>/ (up to
sessions.max_loaded at once). --dataset (or config.yaml dataset.path) saves that folder as a session of the
first admin and loads it at start.
"""

import os
import sys
import argparse

import yaml

from .core import config as vconfig
from .core import hardware
from .core import logging as vlog
from .core import workers as compute
from .core.jobs import JobManager
from . import db
from .data import connections
from .labeling.projects import ProjectRegistry
from .llm.store import LLMStore
from .ml import analysis
from .models.registry import ModelRegistry
from .models.service import ModelService
from .ml import features as feats
from .paths import PROJECT_ROOT
from .server.app import Ctx, create_app
from .state import State

DESCRIPTION = """FiftyOne-style dataset visualizer for YOLO datasets.

Each bounding box becomes a dot in a 2D projection (UMAP / t-SNE / PCA) of a
feature space you pick (DINOv2, CLIP, your YOLO weights, colour histogram,
HOG, box geometry). Lasso dots to see their crops in the gallery, compare
classes, show group bubbles and outliers, and find suspicious labels and
near-duplicates. Slow work runs as background jobs with progress in the
terminal and in the page.

The first page lists your sessions (one dataset each): local folders (read in place), S3 / RustFS
buckets (streamed, or synced to a local cache) or a task of a labeling session (read live). Create one there, then click its card to explore it.

Usage (from the project root):
    .venv/bin/python app.py --dataset dataset --port 5151
"""


def main():
    ap = argparse.ArgumentParser(prog='app.py', description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', default=str(vconfig.DEFAULT_CONFIG),
                    help='YAML settings file (default: config.yaml in this folder); the flags below override it')
    ap.add_argument('--dataset', help='open this YOLO folder (images/, labels/, classes.txt) at start, saved as a '
                                      'session  [dataset.path]')
    ap.add_argument('--cache', help='use exactly this workspace folder  [dataset.workspace]')
    ap.add_argument('--workspace-root', help='where auto-named workspaces <dataset>__<fingerprint> are kept  '
                                             '[dataset.workspace_root]')
    ap.add_argument('--feature', help='feature space shown first  [startup.feature]')
    ap.add_argument('--precompute', help='comma-separated feature keys to compute in the background, e.g. '
                                         'openclip-h14,hog  [startup.precompute]')
    ap.add_argument('--yolo-weights', action='append', default=[], help='extra ultralytics .pt to offer as features  '
                                                                        '[features.yolo.extra_weights]')
    ap.add_argument('--host', help='bind address  [server.host]')
    ap.add_argument('--port', type=int, help='[server.port]')
    ap.add_argument('--rebuild', action='store_true', default=None, help='ignore caches and recompute everything  '
                                                                         '[dataset.rebuild]')
    ap.add_argument('--list-features', action='store_true', help='print the available feature keys and exit')
    ap.add_argument('--show-config', action='store_true', help='print the effective settings and exit')
    args = ap.parse_args()

    cfg = vconfig.load_config(args.config)
    hardware.configure(cfg.get('hardware'))
    vlog.get_logger('hardware').info(hardware.describe())
    # command-line flags win over the file
    for flag, (section, key) in {'dataset': ('dataset', 'path'), 'cache': ('dataset', 'workspace'),
                                 'workspace_root': ('dataset', 'workspace_root'), 'feature': ('startup', 'feature'),
                                 'host': ('server', 'host'), 'port': ('server', 'port'),
                                 'rebuild': ('dataset', 'rebuild')}.items():
        if getattr(args, flag) is not None:
            cfg[section][key] = getattr(args, flag)
    if args.precompute is not None:
        cfg['startup']['precompute'] = [k.strip() for k in args.precompute.split(',') if k.strip()]
    if args.show_config:
        print(yaml.safe_dump({k: v for k, v in cfg.items() if not k.startswith('_')}, sort_keys=False,
                             allow_unicode=True, width=110))
        return
    vlog.setup_logging(cfg['logging']['level'])
    clog = vlog.get_logger('config')
    analysis.configure(cfg['projections'])

    registry = feats.build_registry(PROJECT_ROOT, args.yolo_weights, cfg['features'])
    if args.list_features:
        for s in registry.values():
            print(f'{s.key:28s} {s.label}  {s.note}')
        return
    for key in cfg['startup']['precompute']:
        if key not in registry:
            clog.warning(f'startup.precompute: unknown feature {key!r} (see --list-features)')
    if cfg['startup']['feature'] not in registry:
        clog.warning(f'startup.feature {cfg["startup"]["feature"]!r} is not offered; using {next(iter(registry))!r}')

    dataset_dir = vconfig.resolve(cfg['dataset']['path']) if cfg['dataset']['path'] else None
    host, port = str(cfg['server']['host']), int(cfg['server']['port'])
    clog.info(vlog.esc(cfg['_path'] or 'built-in defaults'))

    import socket
    import uvicorn
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)  # also accept IPv4 (127.0.0.1)
    try:
        sock.bind((host, port))
    except OSError as e:
        sys.exit(f'\nPort {port} is busy ({e.strerror}). The visualizer may already be running: open '
                 f'http://localhost:{port}, or stop it with\n'
                 f'  kill $(ps -eo pid,args | grep "[p]ython .*app.py" | awk \'{{print $1}}\')\n'
                 f'or start on another port with --port {port + 1} (or change server.port in the config)')

    from .paths import APP_DIR, LEGACY_ROOT
    if PROJECT_ROOT == LEGACY_ROOT and APP_DIR.parent.name != 'scripts' and not os.environ.get('DATAVIZ_PROJECT_ROOT'):
        vlog.get_logger('app').warning(
            f'project root is {LEGACY_ROOT} (two folders above the app, kept because its .dataviz_cache holds '
            f'secret.key): storage.allowed_roots "." lets users open folders anywhere under it. Move '
            f'{LEGACY_ROOT / ".dataviz_cache"} to {APP_DIR / ".dataviz_cache"}, or set DATAVIZ_PROJECT_ROOT.')
    # only after the port is ours: never touch (or migrate) a workspace another running instance is using
    ws_root = vconfig.resolve(cfg['dataset']['workspace_root'])
    admin, admin_pw = start_database(cfg, ws_root)
    datasets = connections.Registry(ws_root)

    w = cfg['workers']
    # heavy CPU work runs in worker processes, never blocking the web server
    pools = compute.Pools(warm=bool(w['warmup']), log=vlog.log_fn('workers'),
                          settings={'omp_threads': hardware.workers('omp') if str(w['omp_threads']) == 'auto' else int(w['omp_threads']), 'projections': cfg['projections'],
                                    'warm_method': cfg['startup'].get('method', 'umap')}) \
        if w['enabled'] else compute.InlinePools()
    jobs = JobManager()
    model_reg = ModelRegistry(vconfig.resolve(cfg['models']['store_dir']) if cfg['models']['store_dir'] else ws_root,
                              allowed_roots=[PROJECT_ROOT, *connections.allowed_roots(cfg),
                                             *[vconfig.resolve(p) for p in cfg['models']['allowed_roots']]])
    ctx = Ctx(None, registry=registry, datasets=datasets, cfg=cfg, models=model_reg,
              model_service=ModelService(model_reg, cfg), jobs=jobs,
              labeling=ProjectRegistry(ws_root / 'labeling'), llm=LLMStore(ws_root))

    def open_dataset(ds, cache_dir=None, rebuild=False, refresh=False):
        """A new State for `ds` (a saved dataset); indexing / syncing runs as a background job. Ctx keeps it
        loaded under the session id (Ctx.open_dataset / state_for)."""
        src = datasets.source(ds, cfg['s3'], ctx.labeling)
        if src.kind in ('local', 'labeling'):       # labeling: its session + task still exist and have boxes
            src.check()
        # an embeddings session offers only its own vector files, never the image models
        reg = src.feature_registry() if src.kind == 'embeddings' else registry
        if not reg:
            raise ValueError(f'{src.uri} has no .npy vector files')
        state = State(src, cache_dir, reg, default_feature=cfg['startup']['feature'], jobs=jobs, rebuild=rebuild,
                      pools=pools, cfg=cfg, workspace_root=ws_root, dataset_id=ds['id'], refresh=refresh)
        vlog.get_logger('datasets').info(f'opening [bold]{vlog.esc(src.name)}[/] [dim]{vlog.esc(src.uri)}'
                                         + {'s3': f' · {getattr(src, "mode", "")}', 'labeling': ' · live from the labeling session',
                                            'embeddings': ' · vectors + metadata'}
                                         .get(src.kind, ' · read in place') + '[/]')
        state.start(precompute=cfg['startup']['precompute'], auto_compute=cfg['startup'].get('auto_compute', False))
        return state

    ctx.open_dataset = open_dataset

    # no dataset at start: the page opens on the Sessions home. --dataset / dataset.path opens one (saved as a
    # session, matched by its folder, so restarting with the same flag never adds duplicates)
    state = None
    if dataset_dir is not None:
        ws = cfg['dataset']['workspace']
        try:
            ds = start_session(datasets, dataset_dir, admin)
            state = ctx.open_dataset(ds, cache_dir=vconfig.resolve(ws) if ws else None,
                                     rebuild=bool(cfg['dataset']['rebuild']))
        except (ValueError, KeyError) as e:
            sys.exit(str(e))

    n_sessions = count_sessions()
    rows = [('sessions', f'{n_sessions} saved' + (' · create one in the page' if not n_sessions else ''))]
    if admin_pw:
        rows.append(('admin', f'{admin["username"]} / {admin_pw}  (generated: shown once, change it in the page)'))
    vlog.banner('Dataset Visualizer', rows + [
        ('open', f'{state.source.name}  ({state.source.uri}) · /s/{ds["id"]}/' if state else
                 'none (pick a session in the page)'),
        ('workspaces', ws_root),
        ('features', f'{len(registry)} offered · start: {cfg["startup"]["feature"]} / {cfg["startup"].get("method", "umap")}'),
        ('workers', 'process pools (heavy + fast)' if w['enabled'] else 'inline (debug)'),
        ('config', cfg['_path'] or 'built-in defaults'),
    ], f'http://localhost:{port}')
    server = uvicorn.Server(uvicorn.Config(create_app(ctx), log_level='warning', ws='none'))   # no websocket routes: skips websockets.legacy
    try:
        server.run(sockets=[sock])
    finally:
        pools.shutdown()


def start_database(cfg, ws_root):
    """Connect + migrate, the secret key, default settings, the first admin and the one-time legacy import.
    -> (an admin user dict or None, the admin password when one was generated now)."""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError
    from .auth import bootstrap
    from .db import crypto, legacy, migrate
    dlog = vlog.get_logger('database')
    url = str((cfg.get('database') or {}).get('url') or '').strip()
    if not url or url.startswith('${'):
        sys.exit('\nNo database configured: set DATABASE_URL (or database.url in the config) to a PostgreSQL URL,\n'
                 '  e.g. postgresql://dataviz:secret@127.0.0.1:5432/dataviz  (docker compose up -d starts one)')
    try:
        db.init(url, pool_size=int((cfg.get('database') or {}).get('pool_size', 10)))
        migrate.upgrade(log=dlog.info)
    except (ValueError, SQLAlchemyError, OSError) as e:
        where = db.normalize_url(url).split('@')[-1]          # never print the password
        sys.exit(f'\nCannot use the database at {where}: {type(e).__name__}: {str(e).strip().splitlines()[0]}\n'
                 f'Is PostgreSQL running and is DATABASE_URL right?')
    try:
        crypto.init(key_file=ws_root / 'secret.key', log=dlog.info)
    except RuntimeError as e:
        sys.exit(f'\n{e}')
    bootstrap.ensure_defaults()
    admin, password = bootstrap.ensure_first_admin(log=dlog.info)
    if admin is None:
        with db.connect() as c:
            r = c.execute(text("""SELECT id, username FROM app.users WHERE is_admin AND is_active
                                  ORDER BY created_at LIMIT 1""")).mappings().first()
        admin = dict(r) if r else None
    if admin is not None:
        try:
            legacy.import_legacy(ws_root, admin['id'], log=dlog.info)
        except Exception as e:  # noqa: BLE001 - the server still starts; the import runs again next start
            dlog.exception(f'importing the old files failed ({type(e).__name__}: {e}); it is retried at the next start')
    return admin, password


def start_session(datasets, dataset_dir, admin):
    """--dataset: the first admin's saved local dataset reading `dataset_dir` (matched by folder: restarting with
    the same flag never adds duplicates), created as their visualizer session when new -> the dataset dict."""
    if admin is None:
        raise ValueError('--dataset needs an admin account (set DATAVIZ_ADMIN_* or create one first)')
    return (datasets.find_local(dataset_dir, admin['id'])
            or datasets.upsert_dataset({'kind': 'local', 'path': str(dataset_dir)}, admin['id']))


def count_sessions():
    from sqlalchemy import text
    try:
        with db.connect() as c:
            return c.execute(text("SELECT count(*) FROM app.sessions WHERE kind = 'viz'")).scalar() or 0
    except Exception:  # noqa: BLE001
        return 0
