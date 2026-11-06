"""
Start-up of the visualizer zone: feature spaces, worker pools and opening a saved dataset as a State. Only cli.py imports
this, and only when the install has the visualizer, because this is where torch-sized dependencies come in.

    feature_registry(cfg, yolo_weights)              the feature spaces offered
    check_features(cfg, registry)                    warn about config keys that name a feature not on offer
    start_workers(cfg)                               the process pools heavy work runs in
    dataset_opener(cfg, ctx, registry, pools, root)  callable(ds, cache_dir=, rebuild=, refresh=) -> a started State
"""

from .core import hardware
from .core import logging as vlog
from .core import workers as compute
from .ml import analysis
from .ml import features as feats
from .ml import user_features
from .paths import PROJECT_ROOT
from .state import State


def feature_registry(cfg, yolo_weights):
    analysis.configure(cfg['projections'])
    return feats.build_registry(PROJECT_ROOT, yolo_weights, cfg['features'])


def check_features(cfg, registry):
    clog = vlog.get_logger('config')
    for key in cfg['startup']['precompute']:
        if key not in registry:
            clog.warning(f'startup.precompute: unknown feature {key!r} (see --list-features)')
    if cfg['startup']['feature'] not in registry:
        clog.warning(f'startup.feature {cfg["startup"]["feature"]!r} is not offered; using {next(iter(registry))!r}')


def start_workers(cfg):
    """Heavy CPU work runs in worker processes, never blocking the web server."""
    w = cfg['workers']
    if not w['enabled']:
        return compute.InlinePools()
    return compute.Pools(warm=bool(w['warmup']), log=vlog.log_fn('workers'),
                         settings={'omp_threads': hardware.workers('omp') if str(w['omp_threads']) == 'auto'
                                   else int(w['omp_threads']), 'projections': cfg['projections'],
                                   'warm_method': cfg['startup'].get('method', 'umap')})


def dataset_opener(cfg, ctx, registry, pools, ws_root):
    def open_dataset(ds, cache_dir=None, rebuild=False, refresh=False):
        """A new State for `ds` (a saved dataset); indexing / syncing runs as a background job. Ctx keeps it
        loaded under the session id (Ctx.open_dataset / state_for)."""
        src = ctx.datasets.source(ds, cfg['s3'], ctx.labeling)
        if src.kind in ('local', 'labeling'):       # labeling: its session + task still exist and have boxes
            src.check()
        if src.kind == 'embeddings':                 # an embeddings session offers only its own vector files, never the image models
            reg = src.feature_registry()
            if not reg:
                raise ValueError(f'{src.uri} has no .npy vector files')
        else:                                        # the config's features + the session owner's own models that embed (Settings › Models)
            reg = feats.FeatureRegistry(registry, user_features.OwnerModels(ctx.models, ds.get('owner_id'), ctx.model_service.workdir)
                                        if ds.get('owner_id') else None)
        state = State(src, cache_dir, reg, default_feature=cfg['startup']['feature'], jobs=ctx.jobs, rebuild=rebuild,
                      pools=pools, cfg=cfg, workspace_root=ws_root, dataset_id=ds['id'], refresh=refresh,
                      unit=ds.get('unit', 'auto'), class_from_folder=ds.get('class_from_folder', False),
                      layer=ds.get('layer'))
        vlog.get_logger('datasets').info(f'opening [bold]{vlog.esc(src.name)}[/] [dim]{vlog.esc(src.uri)}'
                                         + {'s3': f' · {getattr(src, "mode", "")}', 'labeling': ' · live from the labeling session',
                                            'embeddings': ' · vectors + metadata'}
                                         .get(src.kind, ' · read in place') + '[/]')
        state.start(precompute=cfg['startup']['precompute'], auto_compute=cfg['startup'].get('auto_compute', False))
        return state
    return open_dataset
