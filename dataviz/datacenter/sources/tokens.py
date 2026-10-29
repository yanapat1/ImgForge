"""
"Test" for the token connections of the web dataset sources (Settings › Connections): one cheap authenticated call
each -> {ok, ms, user?, detail}; a refused token raises ValueError with what to fix.

    kaggle       GET  /api/v1/datasets/list?page=1           (Basic username:key, or Bearer for KGAT_ tokens)
    huggingface  HfApi.whoami()
    roboflow     GET  https://api.roboflow.com/?api_key=      -> {workspace}
    zenodo       GET  https://zenodo.org/api/records?size=1   (Bearer)
"""

import time

from ..store import DCError
from . import fetch, huggingface, kaggle, roboflow, zenodo


def test(conn):
    kind = conn.get('kind')
    t0 = time.time()
    try:
        if kind == 'kaggle':
            with fetch.make_client() as c:
                fetch.get_json(c, f'{kaggle.API}/datasets/list', kaggle.auth_headers(conn), params={'page': 1},
                               errors={401: kaggle._bad_token(), 403: kaggle._bad_token()})
            out = {'user': conn.get('username') or None, 'detail': 'Kaggle accepted the token'}
        elif kind == 'huggingface':
            tok = (conn.get('api_key') or '').strip()
            if not tok:
                raise DCError('add the token (huggingface.co › Settings › Access Tokens)')
            try:
                who = huggingface.hf_api(tok).whoami()
            except Exception as e:  # noqa: BLE001 - huggingface_hub's own error classes
                raise DCError(f'Hugging Face refused the token: {e}') from None
            out = {'user': (who or {}).get('name'), 'detail': 'Hugging Face accepted the token'}
        elif kind == 'roboflow':
            with fetch.make_client() as c:
                d = fetch.get_json(c, f'{roboflow.API}/', params={'api_key': (conn.get('api_key') or '').strip()},
                                   errors={401: roboflow._bad(), 403: roboflow._bad()})
            out = {'user': d.get('workspace'), 'detail': 'Roboflow accepted the key'}
        elif kind == 'zenodo':
            with fetch.make_client() as c:
                fetch.get_json(c, f'{zenodo.API}/records', zenodo._headers(conn), params={'size': 1},
                               errors={401: 'Zenodo refused the token (401)', 403: 'Zenodo refused the token (403)'})
            out = {'detail': 'Zenodo accepted the token'}
        else:
            raise DCError(f'{kind} is not a token connection')
    except DCError as e:
        raise ValueError(e.message) from None
    return {'ok': True, 'ms': round((time.time() - t0) * 1000), **out}
