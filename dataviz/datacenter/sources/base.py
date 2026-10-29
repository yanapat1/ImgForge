"""
The shape every web dataset source has (datacenter/sources/*.py).

    class Source:
        key       'kaggle' | 'huggingface' | 'roboflow' | 'url' | 'zenodo' | 'openml'
        label     shown in the page
        needs     the connection kind holding its token (Settings › Connections), or None
        token     'required' | 'optional' | None
        fields    the extra options the import dialog shows: [{key, label, type: text|select|bool, ...}]
        parse_ref(ref)                                  -> dict (DCError 400 for a ref it cannot read); 'name' is a
                                                           default bucket name, 'ref' the canonical text form
        inspect(conn, ref, opts)                        -> {title, size, files, license, versions, formats?, url?}
        download(conn, ref, opts, dest, progress, cancelled)
                                                        -> {files: [absolute paths under dest], bytes}

`conn` is the decrypted connection dict (or None), `ref` what parse_ref returned. A download writes only under
`dest` (a private .import-<id>/ folder the service then unpacks / moves into the bucket and removes).
"""

from ..store import DCError
from . import fetch


class Source:
    key = ''
    label = ''
    needs = None
    token = None
    fields = ()
    examples = ()
    help = ''

    def parse_ref(self, ref):
        raise NotImplementedError

    def inspect(self, conn, ref, opts):
        raise NotImplementedError

    def download(self, conn, ref, opts, dest, progress, cancelled):
        raise NotImplementedError

    def clean_opts(self, opts):
        """The options this source knows (from `fields`), type-checked; unknown keys dropped."""
        out = {}
        opts = dict(opts or {})
        for f in self.fields:
            k = f['key']
            if k not in opts or opts[k] is None or opts[k] == '':
                if 'default' in f:
                    out[k] = f['default']
                continue
            v = opts[k]
            if f['type'] == 'bool':
                v = v if isinstance(v, bool) else str(v).lower() in ('1', 'true', 'yes', 'on')
            elif f['type'] == 'select':
                v = str(v)
                if v not in [o[0] for o in f['options']]:
                    raise DCError(f'{f["label"]}: one of {", ".join(o[0] for o in f["options"])}')
            elif f['type'] == 'list':
                v = [s.strip() for s in (v if isinstance(v, list) else str(v).replace('\n', ',').split(','))
                     if str(s).strip()]
            else:
                v = str(v).strip()
            out[k] = v
        return out

    def describe(self):
        return {'key': self.key, 'label': self.label, 'needs': self.needs, 'token': self.token,
                'fields': [dict(f) for f in self.fields], 'examples': list(self.examples), 'help': self.help}

    # helpers
    @staticmethod
    def client():
        return fetch.make_client()


def need_token(src, conn, what='this'):
    if conn is None or not (conn.get('api_key') or '').strip():
        raise DCError(f'{src.label} needs a token for {what}: add one in Settings › Connections '
                      f'({src.label} kind) and pick it here')
    return conn['api_key'].strip()


def slug_name(*parts):
    s = '-'.join(str(p) for p in parts if p)
    return ''.join(ch if ch.isalnum() or ch in '-_.' else '-' for ch in s).strip('-.')[:80] or 'dataset'
