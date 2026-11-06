"""Labeling sessions whose source lies inside a Data Center bucket, for the bucket cards."""

from sqlalchemy import text


def _inside(path, root):
    return bool(path) and (path == root or path.startswith(root.rstrip('/') + '/'))


def find(rows, conn, out):
    """Add {id, kind, name, url} to out[bucket id] (cheap: one query for every bucket at once, matched by path prefix)."""
    owners = list({r.owner_id for r in rows})
    lab = conn.execute(text("""SELECT s.id, s.name, s.owner_id, p.source FROM app.sessions s
                               JOIN app.label_projects p ON p.session_id = s.id WHERE s.owner_id = ANY(:o)"""),
                       {'o': owners}).all()
    for r in rows:
        for p in lab:
            src = p.source or {}
            if p.owner_id != r.owner_id:
                continue
            hit = (src.get('kind') == 'local' and _inside(src.get('path'), r.root)) if r.kind != 's3' else (
                src.get('kind') == 's3' and src.get('bucket') == r.s3_bucket)
            if hit:
                out[r.id].append({'id': p.id, 'kind': 'label', 'name': p.name, 'url': f'?label={p.id}'})
