"""
Tags of the Data Center: app.dc_tags rows on (bucket, path), path '' = the bucket itself.

Two kinds: user tags (cleaned like session tags, core/names.clean_tags) and the one AUTO tag `format:<key>` on the
bucket root, written only here from format detection. revalidate() runs after every change to a bucket (ops,
upload, undo): it re-detects the root and, when the format moved, swaps the auto tag and says so, so the page /
the assistant can tell the user "this is no longer a YOLO dataset" right after the move that broke it.
"""

from sqlalchemy import text

from .. import db
from ..core.names import clean_tags
from .store import DCError

AUTO_PREFIX = 'format:'


def auto_tag(fmt):
    return f'{AUTO_PREFIX}{fmt}'


def _shape(rows):
    out = [{'tag': r.tag, 'auto': bool(r.auto)} for r in rows]
    return sorted(out, key=lambda t: (not t['auto'], t['tag'].casefold()))


def get(bid, path='', conn=None):
    if conn is None:
        with db.connect() as c:
            return get(bid, path, c)
    return _shape(conn.execute(text('SELECT tag, auto FROM app.dc_tags WHERE bucket_id = :b AND path = :p'),
                               {'b': bid, 'p': path}).all())


def for_buckets(bids, conn):
    """{bucket id: [Tag]} of the bucket roots."""
    out = {b: [] for b in bids}
    if not bids:
        return out
    rows = conn.execute(text("SELECT bucket_id, tag, auto FROM app.dc_tags WHERE bucket_id = ANY(:b) AND path = ''"),
                        {'b': list(bids)}).all()
    by = {}
    for r in rows:
        by.setdefault(r.bucket_id, []).append(r)
    for b, rs in by.items():
        out[b] = _shape(rs)
    return out


def set_user(bid, path, add=(), remove=()):
    """Add / remove user tags on (bid, path) -> the path's tags. Auto tags cannot be removed or typed by hand."""
    add = clean_tags(list(add or []))
    remove = [' '.join(str(t).split()) for t in (remove or []) if str(t).strip()]
    for t in add:
        if t.casefold().startswith(AUTO_PREFIX):
            raise DCError(f'“{t}”: format:* tags are set automatically by format detection')
    with db.begin() as c:
        cur = c.execute(text('SELECT tag, auto FROM app.dc_tags WHERE bucket_id = :b AND path = :p'),
                        {'b': bid, 'p': path}).all()
        autos = {r.tag.casefold() for r in cur if r.auto}
        for t in remove:
            if t.casefold() in autos or t.casefold().startswith(AUTO_PREFIX):
                raise DCError(f'“{t}” is an automatic format tag: it cannot be removed')
        c.execute(text('DELETE FROM app.dc_tags WHERE bucket_id = :b AND path = :p AND NOT auto '
                       'AND lower(tag) = ANY(:t)'), {'b': bid, 'p': path, 't': [t.casefold() for t in remove]})
        users = {r.tag.casefold() for r in cur if not r.auto} - {t.casefold() for t in remove}
        new = [t for t in add if t.casefold() not in users | autos]
        try:
            clean_tags(sorted(users) + new)            # the 20-tag limit, counted per path
        except ValueError as e:
            raise DCError(str(e).replace('a session', 'a path')) from None
        for t in new:
            c.execute(text('INSERT INTO app.dc_tags(bucket_id, path, tag, auto) VALUES (:b, :p, :t, false) '
                           'ON CONFLICT DO NOTHING'), {'b': bid, 'p': path, 't': t})
        return get(bid, path, c)


def current_format(bid, conn):
    r = conn.execute(text("SELECT tag FROM app.dc_tags WHERE bucket_id = :b AND path = '' AND auto"),
                     {'b': bid}).first()
    return r.tag[len(AUTO_PREFIX):] if r is not None else None


def set_auto(bid, fmt, conn):
    """Make `format:<fmt>` the bucket's only auto tag -> the previous format (None when there was none)."""
    old = current_format(bid, conn)
    if old != fmt:
        c = conn
        c.execute(text("DELETE FROM app.dc_tags WHERE bucket_id = :b AND path = '' AND auto"), {'b': bid})
        c.execute(text("DELETE FROM app.dc_tags WHERE bucket_id = :b AND path = '' AND lower(tag) = lower(:t)"),
                  {'b': bid, 't': auto_tag(fmt)})
        c.execute(text("INSERT INTO app.dc_tags(bucket_id, path, tag, auto) VALUES (:b, '', :t, true)"),
                  {'b': bid, 't': auto_tag(fmt)})
    return old


def apply_format(bid, fmt_result):
    """Store the root's detected format as its auto tag -> retagged {path, from, to, reason} or None."""
    fmt = fmt_result['format']
    with db.begin() as c:
        old = set_auto(bid, fmt, c)
    if old is None or old == fmt:
        return None
    return {'path': '', 'from': old, 'to': fmt, 'reason': fmt_result.get('reason')}


def move_paths(bid, old, new, conn):
    """Tags (and cached detections) below `old` follow a move / rename to `new`."""
    for table in ('dc_tags', 'dc_format_cache'):
        conn.execute(text(f"""UPDATE app.{table} SET path = :new || substr(path, length(:old) + 1)
                              WHERE bucket_id = :b AND path <> '' AND (path = :old OR path LIKE :like)"""),
                     {'b': bid, 'old': old, 'new': new, 'like': _like(old)})


def take_paths(bid, path, conn):
    """Remove the user tags at / below `path` (a delete) -> the rows, so an undo can put them back."""
    rows = conn.execute(text("""DELETE FROM app.dc_tags WHERE bucket_id = :b AND NOT auto AND path <> ''
                                AND (path = :p OR path LIKE :like) RETURNING path, tag"""),
                        {'b': bid, 'p': path, 'like': _like(path)}).all()
    conn.execute(text("DELETE FROM app.dc_format_cache WHERE bucket_id = :b AND path <> '' "
                      "AND (path = :p OR path LIKE :like)"), {'b': bid, 'p': path, 'like': _like(path)})
    return [[r.path, r.tag] for r in rows]


def put_back(bid, rows, conn):
    for p, t in rows or []:
        conn.execute(text('INSERT INTO app.dc_tags(bucket_id, path, tag, auto) VALUES (:b, :p, :t, false) '
                          'ON CONFLICT DO NOTHING'), {'b': bid, 'p': p, 't': t})


def _like(p):
    return p.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '/%'


def all_tags(bucket_ids):
    """[{tag, count, auto}] over the given buckets (count = how many (bucket, path) carry it)."""
    if not bucket_ids:
        return []
    with db.connect() as c:
        rows = c.execute(text("""SELECT min(tag) AS tag, bool_or(auto) AS auto, count(*) AS n FROM app.dc_tags
                                 WHERE bucket_id = ANY(:b) GROUP BY lower(tag) ORDER BY count(*) DESC, min(tag)"""),
                         {'b': list(bucket_ids)}).all()
    return [{'tag': r.tag, 'count': int(r.n), 'auto': bool(r.auto)} for r in rows]


def paths_with(bucket_ids, wanted):
    """[(bucket id, path)] carrying ALL the `wanted` tags (case-insensitive)."""
    wanted = [t.casefold() for t in wanted]
    with db.connect() as c:
        rows = c.execute(text("""SELECT bucket_id, path FROM app.dc_tags WHERE bucket_id = ANY(:b)
                                 AND lower(tag) = ANY(:t) GROUP BY bucket_id, path
                                 HAVING count(DISTINCT lower(tag)) = :n"""),
                         {'b': list(bucket_ids), 't': wanted, 'n': len(set(wanted))}).all()
    return [(r.bucket_id, r.path) for r in rows]
