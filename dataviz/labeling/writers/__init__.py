"""
The label format registry, shared by the session export (export.py) and the Data Center's "Label with model".

    REGISTRY                         {key: Writer class}
    get(key)                         the class (KeyError on an unknown key)
    for_task(task_type, media_type)  [meta] of the writers that can write that task (GET /api/label/formats)
    check(keys, task_type, media)    validated keys (ValueError on an unknown one); the unsupported ones are dropped

Image: yolo, yolo_obb, coco, voc, labelme, createml, csv, imagefolder (image.py). Text: text_jsonl, text_csv,
conll (text.py). Audio: audio_jsonl, commonvoice, segments_csv (audio.py). Embeddings folders convert between
npy+meta.csv, parquet and csv through vector.py (not a Writer: it reads a folder, not annotation rows).
"""

from . import audio, image, text
from .base import Writer, bucket_ctx, export_ctx  # noqa: F401 - re-exported

REGISTRY = {w.key: w for w in image.WRITERS + text.WRITERS + audio.WRITERS}
DEFAULT = ('yolo',)


def get(key):
    return REGISTRY[key]


def supports(key, task_type, media_type='image'):
    w = REGISTRY.get(key)
    return bool(w) and task_type in w.tasks and (media_type or 'image') in w.media


def for_task(task_type=None, media_type=None):
    return [w.meta() for w in REGISTRY.values()
            if (not task_type or task_type in w.tasks) and (not media_type or media_type in w.media)]


def check(keys, task_type=None, media_type=None):
    """keys in registry order, unknown -> ValueError; with task_type, only the ones that support it."""
    keys = [str(k).strip().lower().replace('-', '_') for k in (keys or []) if str(k).strip()]
    bad = [k for k in keys if k not in REGISTRY]
    if bad:
        raise ValueError(f'unknown label format {bad[0]!r} (one of {", ".join(REGISTRY)})')
    return [k for k in REGISTRY if k in keys and (task_type is None or supports(k, task_type, media_type))]
