"""
Which images the models were trained on: image name -> 'train' | 'val' | 'test'.

Read from the split dataset the models were trained with (config models.split_source, default
dataset_split/): its train/images, val/images folders are the truth; a split_report.json
({"groups": [{"split", "files"}]}) is used when the folders are not there. Without either every image
is unknown and the Model tab scores all images (with a warning: train images make metrics look too good).
"""

import json
from pathlib import Path

SPLITS = ('train', 'val', 'test')


def _from_folders(root):
    out = {}
    for split in SPLITS:
        d = root / split / 'images'
        if d.is_dir():
            for f in d.iterdir():
                out[f.name] = split
    return out


def _from_report(path):
    try:
        rep = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {Path(f).name: g.get('split') for g in rep.get('groups', []) for f in g.get('files', [])}


def load_splits(dataset_dir, split_source=None):
    """({image name: split}, where it came from). `split_source`: a split dataset folder or a split_report.json."""
    cands = []
    if split_source:
        cands.append(Path(split_source))
    if dataset_dir:
        d = Path(dataset_dir)
        cands += [d, d.parent / f'{d.name}_split']
    for p in cands:
        if p.is_dir():
            out = _from_folders(p)
            if out:
                return out, str(p)
            p = p / 'split_report.json'
        if p.is_file():
            out = _from_report(p)
            if out:
                return out, str(p)
    return {}, None
