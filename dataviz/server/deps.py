"""
Small helpers shared by the route modules: guards that turn "not ready / unknown / not computed"
into the right HTTP status, and a raw-bytes JSON response.

    need_index      503 while the dataset index is being built (the page shows its progress)
    sample_or_404   a box record by id
    feature_or_400  a known feature key (None = the default feature)
    scores_or_409   k-NN scores, loading them from disk if cached; 409 = "compute them first"
    boxes_only      409 in an embeddings session (no images / boxes: curation, models, the image viewer)
    owned           call a ModelRegistry method as the signed-in user (owner_id=) when it takes one
"""

from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.responses import Response


def _r(arr, nd=4):
    return [round(float(v), nd) for v in arr]


def owned(method, *args, **kw):
    """method(*args, owner_id=<the signed-in user>, **kw): a per-user repository call scoped to the caller. Never
    unscoped from a request: owner_id=None means "every owner" to the repositories (internal callers only)."""
    from ..auth import context
    uid = context.user_id()
    if uid is None:
        raise HTTPException(401, 'sign in first')
    return method(*args, owner_id=uid, **kw)


def json_bytes(data):
    return Response(data, media_type='application/json')



def guards(state):
    """The four guards bound to `state` (the proxy of the current dataset)."""

    def need_index():
        if not state.ready:
            raise HTTPException(503, 'dataset index is still being built')

    def sample_or_404(sid):
        need_index()
        if sid < 0 or sid >= len(state.samples):
            raise HTTPException(404, f'no sample {sid}')
        return state.samples[sid]

    def feature_or_400(key):
        key = key or state.default_feature
        if key not in state.registry:
            raise HTTPException(400, f'unknown feature {key!r}')
        return key

    def scores_or_409(key):
        need_index()
        sc = state.scores.get(key)
        if sc is None and state.feature_cached(key):
            sc = state.get_scores(key)
        if sc is None:
            raise HTTPException(409, f'features {key!r} not computed yet')
        return sc

    def boxes_only():
        if getattr(getattr(state, 'source', None), 'kind', None) == 'embeddings':
            raise HTTPException(409, 'not available in an embeddings session (it has no images or boxes)')

    return SimpleNamespace(need_index=need_index, sample_or_404=sample_or_404, feature_or_400=feature_or_400,
                           scores_or_409=scores_or_409, boxes_only=boxes_only)


def kind_of(state):
    """The open session's source kind ('local', 's3', 'labeling', 'embeddings'), None when there is none."""
    return getattr(getattr(state, 'source', None), 'kind', None)


def capabilities(state):
    """What the page may offer for the open session: one place instead of kind checks spread over the page."""
    emb = kind_of(state) == 'embeddings'
    # one point per image (data/views.py ImageView): no boxes to edit / fix / score a model against (yet)
    img = getattr(getattr(state, 'source', None), 'unit', None) == 'image'
    src = getattr(state, 'source', None)
    union = kind_of(state) == 'union'
    return {'union': union, 'live': bool(getattr(src, 'live', False)) or kind_of(state) == 'labeling',
            'boxes': not emb and not img, 'images': not emb,
            'curation': not emb and not img and (not union or bool(getattr(src, 'label_writable', False))),
            'models': not emb and not img, 'meta': emb, 'unit': 'embeddings' if emb else 'image' if img else 'box',
            # an S3 layout needs the bucket listing: read once the index job has made it
            'layout': getattr(state.source, 'layout', None) if not emb and getattr(state, 'ready', False) else None}
