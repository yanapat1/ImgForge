"""
Map units: what one point of the visualizer is. A view wraps a source (sources.LocalSource / S3Source) and gives the
indexer (indexer.build_index) the label files it should read, so every unit reuses the same pipeline: S3 download-ahead,
worker processes, crops, features, projections, caches.

    ImageView   one point per IMAGE: every image gets one synthetic box covering it (`0 0.5 0.5 1 1`), cropped with
                no padding, so its crop is the whole image letterboxed. Works for images with no labels at all.
                class_from_folder: the class is the image's first sub-folder (cam1/f.jpg -> cam1), else one class.

A view has its own fingerprint (`unit|...|<source fingerprint>`) and uri (`<source uri>#images`), so it gets its own
workspace (workspace.resolve_workspace) next to the box workspace of the same data: switching unit never overwrites
anything, and switching back is a cache hit.

    UNITS        'boxes'  the source's own labels (the classic visualizer)
                 'images' ImageView
                 'auto'   images when the source has no label files, else boxes
"""

import hashlib

UNITS = ('auto', 'boxes', 'images')
VIEW_VERSION = 1


class ImageView:
    unit = 'image'
    index_pad = 0.0                        # the crop is the image itself, letterboxed to a square
    index_draft = True                     # a crop is 224 px: decode big JPEGs at 1/2..1/8 scale
    label_writable = False

    def __init__(self, base, class_from_folder=False):
        self.base = base
        self.class_from_folder = bool(class_from_folder)
        self.uri = f'{base.uri}#images'
        self._classes = None

    def __getattr__(self, name):           # name, kind, root, mode, s3cfg, pool, local, open_image, image_ref, ...
        return getattr(self.base, name)

    def check(self):
        return self.base.check()

    def fingerprint(self):
        seed = f'unit|images|{VIEW_VERSION}|{int(self.class_from_folder)}|{self.base.fingerprint()}'
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
            if self.class_from_folder:
                self._classes = sorted({self.folder_of(n) or '(top folder)' for n in self.list_images()}, key=str.lower)
            else:
                self._classes = ['image']
        return self._classes

    def _cls(self, name):
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


def view_for(source, unit='auto', class_from_folder=False):
    """The source as the visualizer should index it for `unit` (UNITS). Embeddings / labeling sessions are boxes or
    rows by nature: returned as they are."""
    if source.kind not in ('local', 's3') or getattr(source, 'unit', None) == 'image':
        return source
    if unit == 'images' or (unit != 'boxes' and not has_labels(source)):
        return ImageView(source, class_from_folder)
    return source
