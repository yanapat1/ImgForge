"""
Your own pre-trained models as feature spaces: a Settings › Models entry with "Use for embeddings" becomes a
FeatureSpec (key model:<id>:<hash>) offered next to the config's features, so its embeddings are projected
(UMAP / PaCMAP / t-SNE / PCA), searched with k-NN and scored by cleanlab like any other feature.

    family / format              embedding taken from
    hf     (Hugging Face id/dir) AutoModel: get_image_features, else pooler_output / CLS / token mean
    yolo   pt/pth/pkl/sft        ultralytics YOLO.embed (the backbone's pooled output)
    rfdetr torch formats         forward hook on `layer` (default: backbone), pooled
    custom torch formats         adapter embed(model, images, meta), else a forward hook on `layer`, else the output
    any    onnx / openvino       the graph output named (or numbered) in `output`, pooled

Detector runners are reused (models/runners.py: loading, letterbox, device fallback, one model loaded at a time),
so a model scores in the Model tab and embeds in the plot from the same file. Every embedding is L2-normalized
(metric cosine). Loading a file model runs code (pickle / torch.load / adapters): runners.load_runner still refuses
one whose owner may not upload model files (admins, tiers with model.upload). A Hugging Face id is loaded from safetensors without remote code, so any user
may add one.
"""

import re
import time
import hashlib
import json
import threading
from pathlib import Path

import numpy as np

from . import features as feats

POOLS = ('avg', 'cls', 'flatten')
HF_ID = re.compile(r'^[A-Za-z0-9][\w.-]*/[\w.-]+$')
DEFAULT_EMBED = {'enabled': False, 'layer': '', 'pool': 'avg', 'output': '', 'imgsz': None, 'batch_size': None}


def embed_settings(m):
    """The model's `embed` dict with defaults filled in (hf models always embed: that is all they do)."""
    e = {**DEFAULT_EMBED, **(m.get('embed') or {})}
    if m.get('family') == 'hf':
        e['enabled'] = True
    return e


def feature_key(m, fingerprint):
    """model:<id>:<8 hex> — the hash covers the weights and the embed settings, so retraining or picking another
    layer recomputes instead of reusing a stale cache."""
    e = {k: v for k, v in embed_settings(m).items() if k != 'enabled'}
    h = hashlib.sha1(f'{fingerprint}|{json.dumps(e, sort_keys=True)}'.encode()).hexdigest()[:8]
    return f'model:{m["id"]}:{h}'


# ---------------------------------------------------------------- pooling
def _tensorish(o):
    """The array-like inside a model output: tensors / arrays as they are, NestedTensor.tensors, HF ModelOutput
    (pooler_output, else last_hidden_state), dicts (same keys, else the first value), lists (the last item:
    multi-scale backbones list their deepest map last)."""
    for _ in range(8):
        if hasattr(o, 'shape') and hasattr(o, 'ndim'):
            return o
        if hasattr(o, 'tensors'):
            o = o.tensors
        elif isinstance(o, dict) or hasattr(o, 'keys'):
            keys = list(o.keys())
            pick = next((k for k in ('pooler_output', 'image_embeds', 'embeddings', 'last_hidden_state') if k in keys
                         and o[k] is not None), keys[0] if keys else None)
            if pick is None:
                break
            o = o[pick]
        elif isinstance(o, (list, tuple)) and o:
            o = o[-1]
        elif hasattr(o, 'last_hidden_state'):
            o = o.last_hidden_state
        else:
            break
    raise ValueError(f'cannot read an embedding from a model output of type {type(o).__name__}')


def pool(out, how='avg'):
    """(B, D) float32 numpy from a model output: 4-D maps are averaged over H, W (or flattened); 3-D token
    sequences averaged over tokens, or the first (CLS) token with how='cls'."""
    x = _tensorish(out)
    if hasattr(x, 'detach'):
        x = x.detach().float().cpu().numpy()
    x = np.asarray(x, np.float32)
    if x.ndim == 1:
        x = x[None]
    if how == 'flatten':
        return x.reshape(len(x), -1)
    if x.ndim == 4:
        return x.mean((2, 3))
    if x.ndim == 3:
        return x[:, 0] if how == 'cls' else x.mean(1)
    if x.ndim > 4:
        return x.reshape(len(x), x.shape[1], -1).mean(2)
    return x


# ---------------------------------------------------------------- extractors
def _read_pil(paths):
    from PIL import Image
    out = []
    for p in paths:
        with Image.open(p) as im:
            out.append(im.convert('RGB'))
    return out


def _hook_module(runner):
    """The torch module a hook goes on: rfdetr's network, a custom model, YOLO's nn.Module."""
    for attr in ('net', 'model'):
        mod = getattr(runner, attr, None)
        if mod is None:
            continue
        inner = getattr(mod, 'model', None)          # ultralytics YOLO wraps the nn.Module in .model
        if attr == 'model' and inner is not None and hasattr(inner, 'named_modules') and not hasattr(mod, 'named_modules'):
            return inner
        if hasattr(mod, 'named_modules'):
            return mod
        if inner is not None and hasattr(inner, 'named_modules'):
            return inner
    return None


def _runner_fn(m, fingerprint, workdir):
    """fn(paths, samples, progress) for a detector file / exported model, through its runner."""
    e = embed_settings(m)

    def fn(paths, samples, progress):
        from ..models import runners
        progress(stage=f'loading {m["name"]}')
        runner = runners.get_runner(m, fingerprint, workdir)
        bs = int(e.get('batch_size') or max(8, getattr(runner, 'batch_size', 8)))
        if getattr(runner, 'max_batch', None):
            bs = min(bs, runner.max_batch)
        embed_batch = _embedder(runner, m, e)
        n, out = len(paths), []
        progress(0, n, 'embedding crops')
        for i in range(0, n, bs):
            out.append(embed_batch(_read_pil(paths[i:i + bs])))
            progress(min(n, i + bs), n)
        if not out:
            return np.zeros((0, 1), np.float32)
        return feats.l2_normalize(np.concatenate(out))
    return fn


def _embedder(runner, m, e):
    """images (PIL list) -> (B, D) for a loaded runner, by the rules in the module docstring."""
    adapter = getattr(runner, 'adapter', None)
    if adapter is not None and hasattr(adapter, 'embed'):
        def by_adapter(images):
            import torch
            meta = {'input_size': getattr(runner, 'size', None), 'device': getattr(runner, 'device', 'cpu')}
            with torch.inference_mode():
                return pool(adapter.embed(runner.model, images, meta), e['pool'])
        return by_adapter
    exported = getattr(runner, 'exported', None)
    if exported is not None:
        return _exported_embedder(runner, exported, e)
    layer = e.get('layer') or ('backbone' if m['family'] == 'rfdetr' else '')
    yolo = getattr(runner, 'model', None)
    if m['family'] == 'yolo' and not layer and hasattr(yolo, 'embed'):
        imgsz = int(e.get('imgsz') or 224)

        def by_yolo(images):
            arrays = [np.asarray(im)[:, :, ::-1].copy() for im in images]        # ultralytics wants BGR arrays
            emb = yolo.embed(arrays, imgsz=imgsz, verbose=False)
            return np.stack([pool(t.flatten(), 'flatten')[0] for t in emb])
        return by_yolo
    module = _hook_module(runner)
    if layer:
        if module is None or not hasattr(module, 'get_submodule'):
            raise ValueError(f'{m["name"]}: this model has no named layers to hook (TorchScript?): leave "layer" '
                             'empty and export a model that returns the embedding, or add embed() to an adapter')
        try:
            target = module.get_submodule(layer)
        except AttributeError:
            names = [n for n, _ in module.named_children()][:12]
            raise ValueError(f'{m["name"]}: no layer {layer!r}; top-level layers: {", ".join(names)}') from None
        return _hooked(runner, target, e['pool'])
    if module is None:
        raise ValueError(f'{m["name"]}: cannot embed with this model; set an output / layer, or add embed() to an adapter')

    def by_output(images):                         # the model's own output is the embedding (a backbone / classifier)
        import torch
        x = _letterbox_batch(images, int(e.get('imgsz') or getattr(runner, 'size', 224) or 224))
        with torch.inference_mode():
            p = next(module.parameters(), None)
            xt = torch.from_numpy(x).to(p.device if p is not None else 'cpu',
                                        p.dtype if p is not None and p.is_floating_point() else torch.float32)
            return pool(module(xt), e['pool'])
    return by_output


def _hooked(runner, target, how):
    lock = threading.Lock()

    def by_hook(images):
        got = []
        h = target.register_forward_hook(lambda _m, _i, o: got.append(pool(o, how)))
        try:
            with lock:
                try:
                    runner.predict(images)        # the runner's own preprocessing; the detections are dropped
                except Exception:  # noqa: BLE001 - a backbone / classifier is no detector: its decode may fail
                    if not got:
                        raise
        finally:
            h.remove()
        if not got:
            raise ValueError('the hooked layer did not run for these images')
        return got[-1] if len(got[-1]) == len(images) else np.concatenate(got)[-len(images):]
    return by_hook


def _letterbox_batch(images, size):
    from ..models import runners
    return np.stack([runners.letterbox(im, size)[0] for im in images]).transpose(0, 3, 1, 2).astype(np.float32) / 255.0


def _exported_embedder(runner, exported, e):
    want = str(e.get('output') or '').strip()
    if not want:
        raise ValueError('an ONNX / OpenVINO model embeds through one of its graph outputs: set "output" '
                         '(its name or number, 0 = the first)')
    names = []
    if getattr(exported, 'fmt', '') == 'onnx':
        names = [o.name for o in exported.sess.get_outputs()]
    else:
        names = [o.get_any_name() for o in exported.compiled.outputs]
    idx = int(want) if want.isdigit() else (names.index(want) if want in names else None)
    if idx is None or idx >= len(names):
        raise ValueError(f'no output {want!r}; the model has {", ".join(names) or "no named outputs"}')
    size = int(e.get('imgsz') or exported.input_size() or getattr(runner, 'size', 224) or 224)
    imagenet = type(runner).__name__ == 'RfdetrRunner'       # RF-DETR graphs take ImageNet-normalized input

    def by_output(images):
        x = _letterbox_batch(images, size)
        if imagenet:
            from ..models.runners import MEAN, STD
            x = (x - MEAN) / STD
        return pool(exported.run(x)[idx], e['pool'])
    return by_output


def _hf_fn(m, trusted):
    e = embed_settings(m)
    name = m['path']

    def build():
        import torch
        from transformers import AutoModel
        from ..core import hardware
        dev = hardware.device()
        dtype = torch.float16 if dev == 'cuda' else torch.float32
        kw = {'dtype': dtype, 'trust_remote_code': False}
        if not trusted:
            kw['use_safetensors'] = True              # no pickle from a user's hub id
        return AutoModel.from_pretrained(name, **kw).to(dev).eval()

    def preprocessing():
        size, mean, std = 224, feats.IMAGENET_MEAN, feats.IMAGENET_STD
        try:
            from transformers import AutoImageProcessor
            p = AutoImageProcessor.from_pretrained(name)
            mean = np.asarray(getattr(p, 'image_mean', None) or mean, np.float32)
            std = np.asarray(getattr(p, 'image_std', None) or std, np.float32)
            s = getattr(p, 'crop_size', None) or getattr(p, 'size', None) or {}
            size = int(s.get('height') or s.get('shortest_edge') or size) if isinstance(s, dict) else int(s)
        except Exception:  # noqa: BLE001 - a plain backbone without a processor config: ImageNet defaults
            pass
        return int(e.get('imgsz') or size), mean, std

    def fn(paths, samples, progress):
        progress(stage=f'loading {m["name"]} (downloads on first use)')
        model = feats._resident(('hf-user', name, trusted), build)
        size, mean, std = preprocessing()

        def forward(x):
            feats._to_current(model)
            if hasattr(model, 'get_image_features'):
                out = model.get_image_features(pixel_values=x)
            else:
                out = model(pixel_values=x)
                if e['pool'] != 'avg' or getattr(out, 'pooler_output', None) is None:
                    out = out.last_hidden_state
            import torch
            t = _tensorish(out)
            if t.ndim == 4:
                return t.mean((2, 3)) if e['pool'] != 'flatten' else t.flatten(1)
            if t.ndim == 3:
                return t[:, 0] if e['pool'] == 'cls' else t.mean(1)
            return t if torch.is_tensor(t) else torch.as_tensor(t)
        return feats._batched_torch(paths, progress, forward, mean, std, int(e.get('batch_size') or 64), size)
    return fn


def _trusted(owner_admin, m):
    return bool(owner_admin) and not (m.get('family') == 'hf' and HF_ID.match(str(m.get('path') or '')))


def spec_for(m, fingerprint, workdir, owner_admin=False):
    """FeatureSpec of one saved model (the caller checked embed_settings(m)['enabled'])."""
    if m['format'] == 'api':
        raise ValueError('API models have no embedding endpoint')
    fn = _hf_fn(m, _trusted(owner_admin, m)) if m['family'] == 'hf' else _runner_fn(m, fingerprint, workdir)
    note = {'hf': 'Hugging Face model'}.get(m['family'], f'{m["family"]} · {m["format"]}')
    return feats.FeatureSpec(feature_key(m, fingerprint), f'{m["name"]} · my model', 'model', fn, metric='cosine',
                             note=f'your model ({note})', kind='user')


class OwnerModels:
    """The extra feature spaces of one session: its owner's models that embed (read from the database at most
    every `ttl` seconds; FeatureRegistry calls this on every lookup)."""

    def __init__(self, registry, owner_id, workdir, ttl=5.0):
        self.reg, self.owner_id, self.workdir, self.ttl = registry, owner_id, workdir, ttl
        self._at, self._specs = 0.0, {}
        self._lock = threading.Lock()

    def _owner_admin(self):
        """Whether the owner's model files / folders are trusted (admin, or a tier with model.upload)."""
        from ..auth import tiers
        return tiers.owner_has(self.owner_id, 'model.upload')

    def __call__(self):
        with self._lock:
            if time.monotonic() - self._at < self.ttl:
                return self._specs
            specs = {}
            try:
                admin = self._owner_admin()
                for m in self.reg.models(owner_id=self.owner_id):
                    if m.get('format') == 'api' or not embed_settings(m)['enabled']:
                        continue
                    try:
                        s = spec_for(m, self.reg.fingerprint(m), self.workdir, admin)
                    except ValueError:
                        continue
                    specs[s.key] = s
            except Exception:  # noqa: BLE001 - no database (scripts / tests): no extra features
                specs = self._specs
            # a spec object per key is kept, so a job holding one keeps working while the list refreshes
            self._specs = {k: self._specs.get(k, s) for k, s in specs.items()}
            self._at = time.monotonic()
            return self._specs

    def refresh(self):
        with self._lock:
            self._at = 0.0


def check_hf_path(path):
    """A Hugging Face hub id (owner/name) — or None when `path` is not one."""
    p = str(path or '').strip()
    return p if HF_ID.match(p) and not Path(p).exists() else None
