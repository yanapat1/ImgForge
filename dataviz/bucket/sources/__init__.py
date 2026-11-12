"""
Web dataset sources for the Bucket page's "From the web" import: Kaggle, Hugging Face, Roboflow, a direct link,
Zenodo and OpenML. Each is a Source (base.py); fetch.py holds the shared downloader (SSRF guard, resume, ranged
parallel parts, progress, disk check). service.import_source runs one as an io-lane job and unpacks the result.

    SOURCES          {key: Source}
    get(key)         the Source (BucketError 400 on an unknown key)
    describe()       [{key, label, needs, token, fields, examples, help}] for GET /api/bucket/sources
"""

from ..store import BucketError
from .huggingface import HuggingFace
from .kaggle import Kaggle
from .openml import OpenML
from .roboflow import Roboflow
from .url import Url
from .zenodo import Zenodo

SOURCES = {s.key: s for s in (Kaggle, HuggingFace, Roboflow, Url, Zenodo, OpenML)}
# connection kinds that hold a source's token (Settings › Connections)
TOKEN_KINDS = tuple(sorted({s.needs for s in SOURCES.values() if s.needs}))


def get(key):
    s = SOURCES.get(str(key or '').strip().lower())
    if s is None:
        raise BucketError(f'unknown source {key!r} (one of {", ".join(SOURCES)})')
    return s


def describe():
    return [s.describe() for s in SOURCES.values()]
