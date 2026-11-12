"""
Map units: what one point of the visualizer is. A view wraps a source (sources.LocalSource / S3Source) and gives the
indexer (indexer.build_index) the label files it should read, so every unit reuses the same pipeline: S3 download-ahead,
worker processes, crops, features, projections, caches.

    ImageView   one point per IMAGE: every image gets one synthetic box covering it (`0 0.5 0.5 1 1`), cropped with
                no padding, so its crop is the whole image letterboxed. Works for images with no labels at all.
                class_from_folder: the class is the image's first sub-folder (cam1/f.jpg -> cam1), else one class.
                class_from_labels: a LABELLED source seen as full images (the visualizer's "Full images" points,
                /s/<sid>@images/): the class is the image's most frequent box class, '(no boxes)' without any.

A view has its own fingerprint (`unit|...|<source fingerprint>`) and uri (`<source uri>#images`), so it gets its own
workspace (workspace.resolve_workspace) next to the box workspace of the same data: switching unit never overwrites
anything, and switching back is a cache hit.

    UNITS        'boxes'  the source's own labels (the classic visualizer)
                 'images' ImageView
                 'auto'   images when the source has no label files, else boxes
"""

import hashlib

import numpy as np

UNITS = ('auto', 'boxes', 'images')
VIEW_VERSION = 1


class ImageView:
    unit = 'image'
    index_pad = 0.0                        # the crop is the image itself, letterboxed to a square
    index_draft = True                     # a crop is 224 px: decode big JPEGs at 1/2..1/8 scale
    label_writable = False

    def __init__(self, base, class_from_folder=False, class_from_labels=False):
        self.base = base
        self.class_from_folder = bool(class_from_folder)
        self.class_from_labels = bool(class_from_labels) and not self.class_from_folder
        self.uri = f'{base.uri}#images'
        self._classes = None
        self._major = None                 # class_from_labels: image -> class id

    def __getattr__(self, name):           # name, kind, root, mode, s3cfg, pool, local, open_image, image_ref, ...
        return getattr(self.base, name)

    def check(self):
        return self.base.check()

    def fingerprint(self):
        lab = '|labels' if self.class_from_labels else ''   # a new key only for the labelled mode: old caches stay valid
        seed = f'unit|images|{VIEW_VERSION}|{int(self.class_from_folder)}{lab}|{self.base.fingerprint()}'
        return hashlib.sha1(seed.encode()).hexdigest()

    # -- what the indexer reads: one label per image
    def list_images(self):
        return self.base.list_images()

    def list_labels(self):
        return [f'{name}.txt' for name in self.list_images()]

    def image_for_label(self, label_name):
        return label_name[:-4]

    @staticmethod
    def folder_of(name):
        return name.split('/', 1)[0] if '/' in name else ''

    def read_classes(self):
        if self._classes is None:
            if self.class_from_labels:
                self._majority()
            elif self.class_from_folder:
                self._classes = sorted({self.folder_of(n) or '(top folder)' for n in self.list_images()}, key=str.lower)
            else:
                self._classes = ['image']
        return self._classes

    def _majority(self):
        """image -> its most frequent box class (ties: the lower id), read once for every label file in one batch.
        Classes = the source's own, any id past them named class_<k> (as the box indexer does), then '(no boxes)'."""
        if self._major is not None:
            return self._major
        base = self.base
        names = base.list_labels()
        texts = base.read_labels(names) if hasattr(base, 'read_labels') else [base.read_label(n) for n in names]
        stem_cls = {}
        top = -1
        for n, t in zip(names, texts):
            t = t.decode('utf-8', 'replace') if isinstance(t, (bytes, bytearray)) else (t or '')
            ids = []
            for line in t.splitlines():
                head = line.split(None, 1)[0] if line.strip() else ''
                if head.lstrip('-').isdigit() and int(head) >= 0:
                    ids.append(int(head))
            if ids:
                c = int(np.bincount(ids).argmax())
                stem_cls[n.rsplit('/', 1)[-1].rsplit('.', 1)[0]] = c
                top = max(top, c)
        classes = [str(c) for c in base.read_classes()]
        classes += [f'class_{k}' for k in range(len(classes), top + 1)]
        none_id = len(classes)
        self._classes = classes + ['(no boxes)']
        self._major = {img: stem_cls.get(img.rsplit('/', 1)[-1].rsplit('.', 1)[0], none_id) for img in base.list_images()}
        return self._major

    def _cls(self, name):
        if self.class_from_labels:
            return self._majority().get(name, len(self.read_classes()) - 1)
        if not self.class_from_folder:
            return 0
        return self.read_classes().index(self.folder_of(name) or '(top folder)')

    def read_label(self, label_name):
        return f'{self._cls(self.image_for_label(label_name))} 0.5 0.5 1 1\n'

    def read_labels(self, names):
        return [self.read_label(n) for n in names]

    def label_ref(self, name):
        return ''


def has_labels(source):
    """True when the source has at least one label file (an empty labels/ or a flat folder of images: False)."""
    try:
        return bool(source.list_labels())
    except (OSError, ValueError):
        return False


def view_for(source, unit='auto', class_from_folder=False, class_from_labels=False):
    """The source as the visualizer should index it for `unit` (UNITS). Embeddings / labeling sessions are boxes or
    rows by nature: returned as they are. class_from_labels (the "Full images" points of a labelled session): each
    image takes its majority box class, when the source has labels."""
    if source.kind not in ('local', 's3') or getattr(source, 'unit', None) == 'image':
        return source
    if unit == 'images' or (unit != 'boxes' and not has_labels(source)):
        return ImageView(source, class_from_folder, class_from_labels and has_labels(source))
    return source
