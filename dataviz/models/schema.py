"""
The one output structure every model is turned into, and the spec shown to the user for Custom models.

    Detections: float32 (N, 6) = [x1, y1, x2, y2, score, class_id] in original-image pixels.
"""

import numpy as np

CUSTOM_SPEC = {
    'input': {
        'layout': 'float32 tensor (B, 3, H, W), NCHW',
        'color': 'RGB',
        'range': '0..1, then (x - mean) / std if mean/std are set (default: no mean/std)',
        'resize': 'letterbox to imgsz x imgsz (keep aspect, pad with 114/255, centered)',
        'size': 'imgsz from the form (default 640)',
    },
    'output_a': {
        'title': 'Tensor after NMS',
        'shape': '(N, 6) or (B, N, 6)',
        'columns': ['x1', 'y1', 'x2', 'y2', 'score', 'class_id'],
        'units': 'pixels of the letterboxed input tensor (the server maps them back to the original image)',
        'example': [[34.0, 120.5, 210.0, 300.0, 0.91, 3], [400.2, 88.0, 480.9, 170.4, 0.42, 11]],
    },
    'output_b': {
        'title': 'Dict (or a list of dicts, one per image)',
        'example': {'boxes': [[34.0, 120.5, 210.0, 300.0]], 'scores': [0.91], 'labels': [3]},
        'units': 'boxes are xyxy in letterboxed-input pixels',
    },
    'adapter': {
        'title': 'Optional Python adapter (.py)',
        'functions': {
            'build_model(weights_path, device)': 'needed for .pth / .safetensors state dicts: build the network, '
                                                 'load the weights, return a callable',
            'preprocess(pil_images, meta)': 'optional: return the model input; default is the input spec above',
            'postprocess(outputs, meta)': 'optional: return a list (one per image) of {"boxes","scores","labels"} '
                                          'in ORIGINAL image pixels',
        },
        'meta': {'orig_sizes': '[(w, h), ...]', 'input_size': 'imgsz', 'scale': '[r, ...]', 'pad': '[(left, top), ...]',
                 'device': 'cuda | cpu'},
    },
    'classes': 'class_id indexes the class names of the model. Names are matched to the dataset classes by name; '
               'without names, ids are used as dataset class ids.',
    'api_generic': {
        'request': 'POST <url> with the image as multipart field "file" (or raw bytes, or JSON {"image_b64": ...})',
        'response_example': {'detections': [{'box': [34, 120, 210, 300], 'score': 0.91, 'class_id': 3,
                                             'class_name': 'ring-top-box'}]},
        'mapping': {'detections': 'dotted path to the list (e.g. "detections" or "result.objects")',
                    'box': 'key of the box inside one item', 'score': 'key of the score',
                    'class_id': 'key of the class id (or leave empty and set class_name)',
                    'class_name': 'key of the class name', 'box_format': 'xyxy | xywh | cxcywh',
                    'normalized': 'true when boxes are 0..1 of the image size'},
    },
}

ADAPTER_TEMPLATE = '''"""Adapter for a Custom model in the Dataset Visualizer.

Every function is optional except build_model for .pth / .safetensors state dicts.
"""
import numpy as np
import torch


def build_model(weights_path, device):
    """Build the network, load the weights, return a callable model."""
    from my_project.model import MyDetector          # your architecture
    model = MyDetector(num_classes=16)
    if str(weights_path).endswith('.safetensors'):
        from safetensors.torch import load_file
        model.load_state_dict(load_file(weights_path))
    else:
        model.load_state_dict(torch.load(weights_path, map_location='cpu'))
    return model.to(device).eval()


def preprocess(pil_images, meta):
    """Return the model input. meta: orig_sizes, input_size, device (fill scale/pad if you letterbox)."""
    size = meta['input_size']
    batch = []
    for im in pil_images:
        im = im.convert('RGB').resize((size, size))
        batch.append(np.asarray(im, np.float32).transpose(2, 0, 1) / 255.0)
    meta['scale'] = [(size / w, size / h) for w, h in meta['orig_sizes']]
    return torch.from_numpy(np.stack(batch)).to(meta['device'])


def postprocess(outputs, meta):
    """Return one dict per image: boxes (N,4 xyxy, ORIGINAL image pixels), scores (N,), labels (N,)."""
    results = []
    for i, out in enumerate(outputs):
        sx, sy = meta['scale'][i]
        boxes = out['boxes'].cpu().numpy() / np.array([sx, sy, sx, sy])
        results.append({'boxes': boxes, 'scores': out['scores'].cpu().numpy(), 'labels': out['labels'].cpu().numpy()})
    return results
'''


class OutputError(ValueError):
    pass


def _np(x):
    if hasattr(x, 'detach'):
        x = x.detach().float().cpu().numpy()
    return np.asarray(x, dtype=np.float32)


def describe(x):
    if isinstance(x, dict):
        return '{' + ', '.join(f'{k}: {describe(v)}' for k, v in x.items()) + '}'
    if isinstance(x, (list, tuple)):
        return f'{type(x).__name__}[{len(x)}]' + (f' of {describe(x[0])}' if x else '')
    if hasattr(x, 'shape'):
        return f'{type(x).__name__}{tuple(x.shape)}'
    return type(x).__name__


def to_detections(out, batch_size):
    """Model output (spec A or B) -> list of (N, 6) arrays, one per image, in model-input pixels."""
    if isinstance(out, dict) and 'boxes' in out:
        out = [out]
    if isinstance(out, (list, tuple)) and out and isinstance(out[0], dict):
        if len(out) != batch_size:
            raise OutputError(f'got {len(out)} result dicts for {batch_size} images')
        res = []
        for d in out:
            missing = {'boxes', 'scores', 'labels'} - set(d)
            if missing:
                raise OutputError(f'result dict is missing {sorted(missing)} (got keys {sorted(d)})')
            b, s, l = _np(d['boxes']).reshape(-1, 4), _np(d['scores']).reshape(-1), _np(d['labels']).reshape(-1)
            res.append(np.concatenate([b, s[:, None], l[:, None]], 1) if len(b) else np.zeros((0, 6), np.float32))
        return res
    if isinstance(out, (list, tuple)) and len(out) == 1:
        out = out[0]
    a = _np(out)
    if a.ndim == 2 and a.shape[-1] == 6 and batch_size == 1:
        return [a]
    if a.ndim == 3 and a.shape[-1] == 6 and a.shape[0] == batch_size:
        return [x for x in a]
    hint = ''
    if a.ndim == 3 and a.shape[1] in range(5, 400) and a.shape[2] > 1000:
        hint = ' This looks like a raw YOLO head (B, 4+nc, anchors): choose family YOLO, or add NMS / an adapter.'
    elif a.ndim == 3 and a.shape[1] in (100, 300) and a.shape[2] == 4:
        hint = ' This looks like DETR boxes (B, queries, 4): choose family RF-DETR, or add an adapter postprocess.'
    raise OutputError(f'expected (N,6), (B,N,6) or {{boxes,scores,labels}}, got {describe(out)}.{hint}')


def clean(det, w, h):
    """Clip to the image, drop empty boxes, float32 (N, 6)."""
    det = np.asarray(det, np.float32).reshape(-1, 6)
    if not len(det):
        return det
    det[:, [0, 2]] = det[:, [0, 2]].clip(0, w)
    det[:, [1, 3]] = det[:, [1, 3]].clip(0, h)
    keep = (det[:, 2] > det[:, 0]) & (det[:, 3] > det[:, 1]) & np.isfinite(det).all(1)
    return det[keep]
