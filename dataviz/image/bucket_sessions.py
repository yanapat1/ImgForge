"""Visualize sessions whose source lies inside a bucket, for the bucket cards."""

from sqlalchemy import text


def _inside(path, root):
    return bool(path) and (path == root or path.startswith(root.rstrip('/') + '/'))


def find(rows, conn, out):
    """Add {id, kind, name, url} to out[bucket id] (cheap: one query for every bucket at once, matched by path prefix)."""
    owners = list({r.owner_id for r in rows})
    viz = conn.execute(text("""SELECT s.id, s.name, s.owner_id, v.source_kind, v.path, v.bucket, v.prefix
                               FROM app.sessions s JOIN app.viz_sessions v ON v.session_id = s.id
                               WHERE s.owner_id = ANY(:o)"""), {'o': owners}).all()
    for r in rows:
        for v in viz:
            if v.owner_id != r.owner_id:
                continue
            hit = _inside(v.path, r.root) if r.kind != 's3' else (
                v.source_kind == 's3' and v.bucket == r.s3_bucket and
                (not r.prefix or _inside((v.prefix or '').strip('/'), r.prefix.strip('/'))))
            if hit:
                out[r.id].append({'id': v.id, 'kind': 'visualize', 'name': v.name, 'url': f's/{v.id}/'})
