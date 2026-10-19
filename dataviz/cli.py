"""
Command line: read the config (flags win over config.yaml), bind the port, start the worker pools and
serve. The page opens on the Sessions home; no dataset is opened at startup unless --dataset (or
config.yaml dataset.path) names one, which is then saved as a session and opened.
"""

import sys
import argparse

import yaml

from .core import config as vconfig
from .core import hardware
from .core import logging as vlog
from .core import workers as compute
from .core.jobs import JobManager
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
    .venv/bin/python scripts/image-tools/app.py --dataset dataset --port 5151
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
                 f'  kill $(ps -eo pid,args | grep "[p]ython scripts/image-tools/app.py" | awk \'{{print $1}}\')\n'
                 f'or start on another port with --port {port + 1} (or change server.port in the config)')

    # only after the port is ours: never touch (or migrate) a workspace another running instance is using
    ws_root = vconfig.resolve(cfg['dataset']['workspace_root'])
    datasets = connections.Registry(ws_root)

    w = cfg['workers']
    # heavy CPU work runs in worker processes, never blocking the web server
    pools = compute.Pools(warm=bool(w['warmup']), log=vlog.log_fn('workers'),
                          settings={'omp_threads': hardware.workers('omp') if str(w['omp_threads']) == 'auto' else int(w['omp_threads']), 'projections': cfg['projections'],
                                    'warm_method': cfg['startup'].get('method', 'umap')}) \
        if w['enabled'] else compute.InlinePools()
    jobs = JobManager()
    model_reg = ModelRegistry(vconfig.resolve(cfg['models']['store_dir']) if cfg['models']['store_dir'] else ws_root,
                              allowed_roots=[PROJECT_ROOT, *[vconfig.resolve(p) for p in cfg['models']['allowed_roots']]])
    ctx = Ctx(None, registry=registry, datasets=datasets, cfg=cfg, models=model_reg,
              model_service=ModelService(model_reg, cfg), jobs=jobs,
              labeling=ProjectRegistry(ws_root / 'labeling'), llm=LLMStore(ws_root))

    def open_dataset(ds, cache_dir=None, rebuild=False, refresh=False):
        """Make `ds` (a saved dataset) the one served; indexing / syncing runs as a background job."""
        src = datasets.source(ds, cfg['s3'], ctx.labeling)
        if src.kind in ('local', 'labeling'):       # labeling: its session + task still exist and have boxes
            src.check()
        state = State(src, cache_dir, registry, default_feature=cfg['startup']['feature'], jobs=jobs, rebuild=rebuild,
                      pools=pools, cfg=cfg, workspace_root=ws_root, dataset_id=ds['id'], refresh=refresh)
        ctx.state = state
        datasets.set_active(ds['id'])
        vlog.get_logger('datasets').info(f'opening [bold]{vlog.esc(src.name)}[/] [dim]{vlog.esc(src.uri)}'
                                         + {'s3': f' · {getattr(src, "mode", "")}', 'labeling': ' · live from the labeling session'}
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
            ds = datasets.find_local(dataset_dir) or datasets.upsert_dataset({'kind': 'local', 'path': str(dataset_dir)})
            state = open_dataset(ds, cache_dir=vconfig.resolve(ws) if ws else None, rebuild=bool(cfg['dataset']['rebuild']))
        except (ValueError, KeyError) as e:
            sys.exit(str(e))

    n_sessions = len(datasets.datasets())
    vlog.banner('Dataset Visualizer', [
        ('sessions', f'{n_sessions} saved' + (' · create one in the page' if not n_sessions else '')),
        ('open', f'{state.source.name}  ({state.source.uri})' if state else 'none (pick a session in the page)'),
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
