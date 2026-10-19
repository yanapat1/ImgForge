"""
Turn a saved model into a Runner: predict(list of PIL images) -> list of (N, 6) float32 arrays
[x1, y1, x2, y2, score, class_id] in original-image pixels, plus `names` (the model's class names or None).

    family × format             loaded with
    yolo   pt/pth/pkl/onnx/ov   ultralytics.YOLO (pth/pkl are re-saved as a .pt it accepts)
    yolo   safetensors          ultralytics.YOLO(arch yaml / base .pt) + load_state_dict
    rfdetr pt/pth/pkl/sft       RFDETR<variant>(num_classes, resolution) + load_state_dict, our letterbox + PostProcess
    rfdetr onnx/openvino        letterbox + (dets cxcywh 0..1, logits) -> sigmoid top-k
    custom any                  TorchScript / pickled module / onnxruntime / OpenVINO, output per schema.CUSTOM_SPEC,
                                or an adapter .py (build_model / preprocess / postprocess)
    *      api                  remote.ApiRunner

Only one runner is kept loaded (GPU memory); `get_runner` reloads when another model is asked for.
"""

import threading
import importlib.util
from pathlib import Path

import numpy as np

from ..core import hardware
from . import schema

MEAN = np.array([0.485, 0.456, 0.406], np.float32).reshape(1, 3, 1, 1)
STD = np.array([0.229, 0.224, 0.225], np.float32).reshape(1, 3, 1, 1)
LOW_CONF = 0.001          # predictions are kept down to here so PR curves / the conf slider work afterwards


def _fp16_kwargs():
    """fp16 predict argument: newer ultralytics deprecates `half` (and warns on every call) in favour of `quantize`."""
    try:
        from ultralytics.cfg import DEFAULT_CFG_DICT
        return {'quantize': 16} if 'quantize' in DEFAULT_CFG_DICT else {'half': True}
    except Exception:
        return {'half': True}


def _device():
    return hardware.device()


def letterbox(im, size):
    """PIL -> uint8 (size, size, 3) RGB, long side = size, centered, padded with 114 (as train_rfdetr.py / ultralytics)."""
    im = im.convert('RGB')
    w0, h0 = im.size
    r = size / max(w0, h0)
    w, h = max(1, round(w0 * r)), max(1, round(h0 * r))
    from PIL import Image
    im = im.resize((w, h), Image.BILINEAR)
    canvas = np.full((size, size, 3), 114, np.uint8)
    left, top = (size - w) // 2, (size - h) // 2
    canvas[top:top + h, left:left + w] = np.asarray(im)
    return canvas, r, (left, top)


def unletterbox(det, r, pad):
    det = np.asarray(det, np.float32).reshape(-1, 6).copy()
    det[:, [0, 2]] = (det[:, [0, 2]] - pad[0]) / r
    det[:, [1, 3]] = (det[:, [1, 3]] - pad[1]) / r
    return det


def _load_adapter(path):
    if not path:
        return None
    spec = importlib.util.spec_from_file_location(f'dataviz_adapter_{Path(path).stem}', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _names_list(names):
    if names is None:
        return None
    if isinstance(names, dict):
        return [str(names[k]) for k in sorted(names)]
    return [str(n) for n in names]


class Runner:
    names = None
    batch_size = 8          # default images per call
    max_batch = None        # hard limit (exported graphs with a fixed batch dimension)
    draft_size = None       # model input size when boxes come back un-letterboxed from any decode scale (predict.py
                            # may then decode big JPEGs at reduced size and rescale the boxes); None = full decode

    def sync_device(self):
        """Called at the start of predict(): follow hardware.device() (a CUDA failure demotes the process to CPU)."""

    def predict(self, images):
        raise NotImplementedError

    def close(self):
        pass


# ------------------------------------------------------------------ YOLO (ultralytics)
class YoloRunner(Runner):
    def __init__(self, m, workdir):
        from ultralytics import YOLO
        fmt, path = m['format'], m['path']
        self.imgsz = int(m.get('imgsz') or 0) or None
        self.fmt = fmt
        self.device = _device()
        self.iou = float(m.get('iou') or 0.7)
        sd = None
        if fmt == 'safetensors':
            from safetensors.torch import load_file
            sd = load_file(path)
        elif fmt in ('pth', 'pkl'):
            path, sd = self._as_pt(path, workdir)
        if sd is not None:                          # weights only: build the network from arch, then load them
            if not m.get('arch'):
                raise ValueError(f'this {fmt} file holds weights only (a state dict): set "arch" to the model yaml '
                                 '(e.g. yolo11n.yaml) or the .pt it was trained from')
            self.model = YOLO(m['arch'])
            res = self.model.model.load_state_dict(sd, strict=False)
            if res.missing_keys:
                raise ValueError(f'{len(res.missing_keys)} weights missing for this arch, e.g. {res.missing_keys[:3]}')
        else:
            self.model = YOLO(str(path), task='detect')
        self.names = _names_list(getattr(self.model, 'names', None))
        if fmt in ('onnx', 'openvino'):            # ultralytics exports have a fixed batch of 1 unless dynamic=True
            self.batch_size = self.max_batch = 1
        else:
            self.batch_size = 16
        self.draft_size = self.imgsz or 640        # ultralytics letterboxes whatever image it gets, boxes map back to it

    def sync_device(self):
        self.device = _device()

    @staticmethod
    def _as_pt(path, workdir):
        """ultralytics only opens *.pt checkpoints: re-save a .pth / .pkl checkpoint or bare model as one.
        Returns (path, None), or (path, state_dict) when the file holds weights only."""
        import torch
        obj = torch.load(path, map_location='cpu', weights_only=False)
        if isinstance(obj, torch.nn.Module):
            obj = {'model': obj}
        if isinstance(obj, dict) and obj and all(hasattr(v, 'shape') for v in obj.values()):
            return path, obj
        if not (isinstance(obj, dict) and isinstance(obj.get('model') or obj.get('ema'), torch.nn.Module)):
            raise ValueError('this file is neither an ultralytics checkpoint (dict with "model"), a model, '
                             'nor a state dict')
        out = Path(workdir) / (Path(path).stem + '.pt')
        out.parent.mkdir(parents=True, exist_ok=True)
        torch.save(obj, out)
        return out, None

    def predict(self, images):
        self.sync_device()
        kw = dict(conf=LOW_CONF, iou=self.iou, verbose=False, max_det=300)
        if self.fmt in ('pt', 'pth', 'pkl', 'safetensors'):      # torch weights: explicit device, fp16 on the GPU
            kw['device'] = self.device
            if self.device == 'cuda':
                kw.update(_fp16_kwargs())
        if self.imgsz:
            kw['imgsz'] = self.imgsz
        res = self.model.predict(images, **kw)
        out = []
        for r in res:
            b = r.boxes
            if b is None or not len(b):
                out.append(np.zeros((0, 6), np.float32))
                continue
            out.append(np.concatenate([b.xyxy.cpu().numpy(), b.conf.cpu().numpy()[:, None],
                                       b.cls.cpu().numpy()[:, None]], 1).astype(np.float32))
        return out

    def close(self):
        del self.model


# ------------------------------------------------------------------ RF-DETR
def _rfdetr_class(variant):
    if variant in ('2xlarge', 'xlarge'):
        from rfdetr_plus import RFDETR2XLarge, RFDETRXLarge
        return {'2xlarge': RFDETR2XLarge, 'xlarge': RFDETRXLarge}[variant], {'accept_platform_model_license': True}
    import rfdetr
    return {'large': rfdetr.RFDETRLarge, 'medium': rfdetr.RFDETRMedium, 'small': rfdetr.RFDETRSmall,
            'nano': rfdetr.RFDETRNano, 'base': rfdetr.RFDETRBase}[variant], {}


def _rfdetr_topk(logits, boxes, size, k=300):
    """ONNX / OpenVINO RF-DETR outputs (B,Q,C) logits + (B,Q,4) cxcywh 0..1 -> per image (N,6) in input pixels."""
    prob = 1 / (1 + np.exp(-logits))
    out = []
    for p, b in zip(prob, boxes):
        flat = p.reshape(-1)
        k_ = min(k, flat.size)
        idx = np.argpartition(-flat, k_ - 1)[:k_]
        q, c = idx // p.shape[1], idx % p.shape[1]
        cx, cy, w, h = (b[q] * size).T
        out.append(np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, flat[idx], c], 1).astype(np.float32))
    return out


class RfdetrRunner(Runner):
    batch_size = 4

    def __init__(self, m):
        import torch
        self.device = _device()
        fmt, path = m['format'], m['path']
        self.size = int(m.get('imgsz') or 0) or None
        if fmt in ('onnx', 'openvino'):
            self.exported = _Exported(path, fmt)
            self.size = self.size or self.exported.input_size() or 704
            self.batch_size = self.max_batch = self.exported.fixed_batch() or 4
            self.draft_size = self.size
            return
        self.exported = None
        if fmt == 'safetensors':
            from safetensors.torch import load_file
            ckpt = {'model': load_file(path)}
        else:
            ckpt = torch.load(path, map_location='cpu', weights_only=False)
        if isinstance(ckpt, torch.nn.Module):
            ckpt = {'model': ckpt.state_dict()}
        sd = ckpt.get('model') if isinstance(ckpt, dict) else None
        if not isinstance(sd, dict):
            raise ValueError('expected an RF-DETR checkpoint: a dict with "model" (state dict)')
        args = ckpt.get('args')
        variant = m.get('variant') or ckpt.get('model_size') or _guess_variant(args, path)
        if not variant:
            raise ValueError('cannot tell the RF-DETR size from this checkpoint: choose a variant')
        nc = ckpt.get('num_classes') or _num_classes(sd)
        res = self.size or ckpt.get('resolution') or getattr(args, 'resolution', None) or 704
        self.size = int(res)
        names = ckpt.get('class_names') or getattr(args, 'class_names', None)
        self.names = _names_list(names)
        cls, extra = _rfdetr_class(variant)
        rf = cls(num_classes=nc, resolution=self.size, pretrain_weights=None, **extra)
        net = rf.model.model
        r = net.load_state_dict(sd, strict=False)
        if r.missing_keys:
            raise ValueError(f'{len(r.missing_keys)} weights missing for RF-DETR {variant} '
                             f'(wrong variant?), e.g. {r.missing_keys[:3]}')
        from rfdetr.config import TrainConfig
        from rfdetr.models.lwdetr import build_criterion_from_config
        _, self.post = build_criterion_from_config(rf.model_config, TrainConfig(dataset_dir='.', output_dir='.'))
        self.net = net.to(self.device).eval()
        self.variant = variant
        self.draft_size = self.size

    def sync_device(self):
        dev = _device()
        if dev != self.device:
            self.device = dev
            if self.exported is not None:
                self.exported.to_device(dev)
            elif getattr(self, 'net', None) is not None:
                self.net.to(dev)

    def predict(self, images):
        self.sync_device()
        boxes = [letterbox(im, self.size) for im in images]
        x = np.stack([b[0] for b in boxes]).transpose(0, 3, 1, 2).astype(np.float32) / 255.0
        x = (x - MEAN) / STD
        if self.exported is not None:
            outs = self.exported.run(x)
            dets = sorted(outs, key=lambda a: a.shape[-1])          # (B,Q,4) boxes, (B,Q,C) logits
            raw = _rfdetr_topk(dets[-1], dets[0], self.size)
        else:
            import torch
            from rfdetr.utilities.tensors import NestedTensor
            amp = hardware.dtype() if self.device == 'cuda' else torch.bfloat16
            with torch.inference_mode(), torch.autocast(self.device, dtype=amp, enabled=self.device == 'cuda'):
                xt = torch.from_numpy(x).to(self.device)
                mask = torch.zeros((xt.shape[0], self.size, self.size), dtype=torch.bool, device=self.device)
                o = self.net(NestedTensor(xt, mask))
            o = {k: v.float() for k, v in o.items() if k in ('pred_logits', 'pred_boxes')}
            sizes = torch.tensor([[self.size, self.size]] * len(images), device=self.device)
            raw = []
            for d in self.post(o, sizes):
                raw.append(np.concatenate([d['boxes'].cpu().numpy(), d['scores'].cpu().numpy()[:, None],
                                           d['labels'].cpu().numpy()[:, None]], 1).astype(np.float32))
        out = []
        for det, (_, r, pad) in zip(raw, boxes):
            det = det[det[:, 4] >= LOW_CONF]
            if self.names:
                det = det[det[:, 5] < len(self.names)]
            out.append(unletterbox(det, r, pad))
        return out

    def close(self):
        import torch
        self.net = None
        torch.cuda.empty_cache()


def _num_classes(sd):
    for k, v in sd.items():
        if k.endswith('class_embed.bias'):
            return int(v.shape[0]) - 1          # rfdetr adds one "no object" slot
    raise ValueError('cannot find class_embed in the state dict')


def _guess_variant(args, path):
    """Best guess from the training args or names like wd-rfdetr-l-3 / rf-detr-xxlarge / best-rfdetr-l.pt."""
    text = (str(getattr(args, 'encoder', '')) + ' ' + Path(path).as_posix()).lower()
    for pat, v in (('2xl', '2xlarge'), ('xxl', '2xlarge'), ('xlarge', 'xlarge'), ('-xl', 'xlarge'),
                   ('large', 'large'), ('medium', 'medium'), ('small', 'small'), ('nano', 'nano'), ('base', 'base')):
        if pat in text:
            return v
    import re
    m = re.search(r'rf-?detr-([lmsn])\b', text)
    return {'l': 'large', 'm': 'medium', 's': 'small', 'n': 'nano'}[m.group(1)] if m else None


# ------------------------------------------------------------------ exported graphs (ONNX / OpenVINO)
class _Exported:
    def __init__(self, path, fmt):
        self.fmt = fmt
        self.path = path
        if fmt == 'onnx':
            self._make_session(_device())
            self.inp = self.sess.get_inputs()[0]
            self.shape = self.inp.shape
        else:
            import openvino as ov
            p = Path(path)
            xml = p if p.suffix == '.xml' else next(p.glob('*.xml'))
            core = ov.Core()
            model = core.read_model(str(xml))
            self.shape = [d.get_length() if d.is_static else None for d in model.inputs[0].get_partial_shape()]
            self.compiled = core.compile_model(model, 'CPU')

    def _make_session(self, dev):
        import onnxruntime as ort
        avail = ort.get_available_providers()
        prov = [p for p in (('CUDAExecutionProvider',) if dev == 'cuda' else ()) + ('CPUExecutionProvider',) if p in avail]
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = hardware.workers('omp')
        self.sess = ort.InferenceSession(str(self.path), sess_options=opts, providers=prov)

    def to_device(self, dev):
        """After a CUDA failure: rebuild the ONNX session on CPU only (OpenVINO always runs on CPU)."""
        if self.fmt == 'onnx':
            self._make_session(dev)

    def input_size(self):
        s = self.shape[-1] if self.shape else None
        return s if isinstance(s, int) else None

    def fixed_batch(self):
        b = self.shape[0] if self.shape else None
        return b if isinstance(b, int) else None

    def run(self, x):
        """x float32 (B,3,H,W) -> list of output arrays (batched per image when the graph has a fixed batch of 1)."""
        if self.fixed_batch() == 1 and len(x) > 1:
            parts = [self.run(x[i:i + 1]) for i in range(len(x))]
            return [np.concatenate([p[j] for p in parts]) for j in range(len(parts[0]))]
        if self.fmt == 'onnx':
            return [np.asarray(o) for o in self.sess.run(None, {self.inp.name: x})]
        res = self.compiled(x)
        return [np.asarray(res[o]) for o in self.compiled.outputs]


# ------------------------------------------------------------------ Custom
class CustomRunner(Runner):
    batch_size = 8

    def __init__(self, m):
        import torch
        self.device = _device()
        self.size = int(m.get('imgsz') or 640)
        self.adapter = _load_adapter(m.get('adapter'))
        fmt, path = m['format'], m['path']
        self.exported = None
        self.model = None
        if self.adapter is not None and hasattr(self.adapter, 'build_model'):
            self.model = self.adapter.build_model(path, self.device)
        elif fmt in ('onnx', 'openvino'):
            self.exported = _Exported(path, fmt)
            self.size = int(m.get('imgsz') or self.exported.input_size() or 640)
            self.batch_size = self.max_batch = self.exported.fixed_batch() or 8
        elif fmt == 'safetensors':
            raise ValueError('.safetensors holds weights only: attach an adapter with build_model(weights_path, device)')
        else:
            try:
                self.model = torch.jit.load(path, map_location=self.device)
            except (RuntimeError, ValueError):
                obj = torch.load(path, map_location='cpu', weights_only=False)
                if isinstance(obj, dict) and isinstance(obj.get('model'), torch.nn.Module):
                    obj = obj['model']
                if not callable(obj):
                    raise ValueError('this file holds a state dict / data, not a model: attach an adapter with '
                                     'build_model(weights_path, device)')
                self.model = obj.to(self.device).eval() if hasattr(obj, 'to') else obj
        if self.adapter is None:
            self.draft_size = self.size           # our own letterbox + unletterbox; an adapter may use orig_sizes
        names = getattr(self.adapter, 'CLASS_NAMES', None) or getattr(self.model, 'names', None)
        self.names = _names_list(names) if isinstance(names, (list, tuple, dict)) else None

    def sync_device(self):
        dev = _device()
        if dev != self.device:
            self.device = dev
            if self.exported is not None:
                self.exported.to_device(dev)
            elif self.model is not None and hasattr(self.model, 'to'):
                self.model.to(dev)

    def predict(self, images):
        self.sync_device()
        meta = {'orig_sizes': [im.size for im in images], 'input_size': self.size, 'device': self.device}
        own_pre = self.adapter is not None and hasattr(self.adapter, 'preprocess')
        if own_pre:
            x = self.adapter.preprocess(images, meta)
        else:
            boxes = [letterbox(im, self.size) for im in images]
            meta['scale'] = [b[1] for b in boxes]
            meta['pad'] = [b[2] for b in boxes]
            x = np.stack([b[0] for b in boxes]).transpose(0, 3, 1, 2).astype(np.float32) / 255.0
        if self.exported is not None:
            outs = self.exported.run(np.asarray(x, np.float32))
            out = outs[0] if len(outs) == 1 else {'boxes': outs[0], 'scores': outs[1], 'labels': outs[2]} \
                if len(outs) == 3 else outs
        else:
            import torch
            if isinstance(x, np.ndarray):
                x = torch.from_numpy(x).to(self.device)
            with torch.inference_mode():
                out = self.model(x)
        if self.adapter is not None and hasattr(self.adapter, 'postprocess'):
            import torch
            with torch.inference_mode():                         # outputs are inference tensors
                res = self.adapter.postprocess(out, meta)        # original-image pixels
            return [schema.to_detections(d, 1)[0] for d in res]
        dets = schema.to_detections(out, len(images))
        if own_pre:
            if 'scale' not in meta:
                raise schema.OutputError('adapter preprocess() must set meta["scale"] (and "pad") or define postprocess()')
            res = []
            for i, d in enumerate(dets):
                s = meta['scale'][i]
                sx, sy = (s, s) if np.isscalar(s) else s
                px, py = (meta.get('pad') or [(0, 0)] * len(dets))[i]
                d = np.asarray(d, np.float32).copy()
                d[:, [0, 2]] = (d[:, [0, 2]] - px) / sx
                d[:, [1, 3]] = (d[:, [1, 3]] - py) / sy
                res.append(d)
            return res
        return [unletterbox(d, r, p) for d, r, p in zip(dets, meta['scale'], meta['pad'])]

    def close(self):
        import torch
        self.model = None
        torch.cuda.empty_cache()


# ------------------------------------------------------------------ factory + single loaded runner
def load_runner(m, workdir):
    if m['format'] == 'api':
        from .remote import ApiRunner, LabelApiRunner
        return LabelApiRunner(m) if (m.get('api') or {}).get('preset') == 'label' else ApiRunner(m)
    if m['family'] == 'yolo':
        return YoloRunner(m, workdir)
    if m['family'] == 'rfdetr':
        return RfdetrRunner(m)
    return CustomRunner(m)


_lock = threading.RLock()
_current = {'key': None, 'runner': None}


def get_runner(m, fingerprint, workdir):
    """The runner for model `m`, loading it (and unloading the previous one) when needed."""
    with _lock:
        key = (m['id'], fingerprint)
        if _current['key'] != key:
            release()
            _current['runner'] = load_runner(m, workdir)
            _current['key'] = key
        return _current['runner']


def release():
    with _lock:
        if _current['runner'] is not None:
            try:
                _current['runner'].close()
            except Exception:  # noqa: BLE001 - best effort
                pass
        _current['key'], _current['runner'] = None, None
        try:
            import torch
            torch.cuda.empty_cache()
        except ImportError:
            pass
