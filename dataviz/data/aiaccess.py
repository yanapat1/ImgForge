"""
What the ◆ assistant may change in a table, per user and per table. The user sets it in the object explorer (a
table's right-click menu › AI access); the assistant cannot change it. Kept in the user prefs (key
`analysis_ai_access`: {"items": [{c, d, s, t, level}]}, only tables above the default).

    Read only         (read, the default)   SELECT, CAST / CONVERT inside a query, this session's temp tables
    Edit rows         (rows)                + INSERT / UPDATE / DELETE / MERGE on that table
    Change structure  (schema)              + ALTER TABLE (add, drop, rename a column, change its type),
                                              TRUNCATE and DROP of that table

    load(prefs)                       -> {(c, d, s, t) lower-cased: level}
    required(statements, conn_kind)   -> [(schema, table, level needed, op)]; AccessError for what no level allows
    gate(levels, cid, db, statements, conn_kind)   AccessError when a table's level is below what a statement needs

Every change of the assistant is still confirmed by the user (run_change is 'danger'); the level decides what it may
propose at all. The check runs on the server, on the parsed statements, never on the model's word.
"""

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

from . import classify

KEY = 'analysis_ai_access'
LEVELS = ('read', 'rows', 'schema')
LABEL = {'read': 'Read only', 'rows': 'Edit rows', 'schema': 'Change structure'}
MAX_ITEMS = 500
DEFAULT_SCHEMA = {'postgres': 'public', 'mssql': 'dbo', 'mongo': ''}

_ROWS_OPS = (exp.Insert, exp.Update, exp.Delete, exp.Merge)
_SCHEMA_OPS = (exp.Alter, exp.TruncateTable, exp.Drop)
_MONGO_ROWS = {'insertOne', 'insertMany', 'updateOne', 'updateMany', 'replaceOne', 'deleteOne', 'deleteMany',
               'findOneAndUpdate', 'findOneAndReplace', 'findOneAndDelete', 'bulkWrite'}
_MONGO_SCHEMA = {'drop', 'renameCollection', 'createIndex', 'createIndexes', 'dropIndex', 'dropIndexes'}


class AccessError(ValueError):
    """The assistant may not do this: the message says which table and level, and how the user can raise it."""


def _s(v, n=200):
    return v.strip()[:n] if isinstance(v, str) else ''


def key(c, d, s, t):
    return (_s(c, 64), _s(d).lower(), _s(s).lower(), _s(t).lower())


def items(prefs):
    """The stored rows [{c, d, s, t, level}] (valid ones, above the default, deduplicated)."""
    raw = (prefs or {}).get(KEY) if isinstance(prefs, dict) else None
    rows = raw.get('items') if isinstance(raw, dict) and isinstance(raw.get('items'), list) else []
    out, seen = [], set()
    for x in rows:
        if not isinstance(x, dict) or x.get('level') not in LEVELS[1:]:
            continue
        f = {k: _s(x.get(k), 64 if k == 'c' else 200) for k in ('c', 'd', 's', 't')}
        k = key(f['c'], f['d'], f['s'], f['t'])
        if not f['c'] or not f['t'] or k in seen:
            continue
        seen.add(k)
        out.append({**f, 'level': x['level']})
        if len(out) >= MAX_ITEMS:
            break
    return out


def load(prefs):
    return {key(x['c'], x['d'], x['s'], x['t']): x['level'] for x in items(prefs)}


def put(prefs, c, d, s, t, level):
    """The new prefs value for KEY after setting one table's level ('read' removes it)."""
    if level not in LEVELS:
        raise AccessError(f'level must be one of {", ".join(LEVELS)}')
    if not _s(c, 64) or not _s(t):
        raise AccessError('connection and table are required')
    k = key(c, d, s, t)
    rows = [x for x in items(prefs) if key(x['c'], x['d'], x['s'], x['t']) != k]
    if level != 'read':
        if len(rows) >= MAX_ITEMS:
            raise AccessError(f'at most {MAX_ITEMS} tables can have a raised AI access')
        rows.append({'c': _s(c, 64), 'd': _s(d), 's': _s(s), 't': _s(t), 'level': level})
    return {'items': rows}


INDEX_CHARS = 400


def index_line(rows, cid=None):
    """One short line for the system prompt: which tables of this connection the assistant may change."""
    mine = [x for x in rows if not cid or x['c'] == cid]
    if not mine:
        return 'AI access: every table is Read only for you (temp tables are fine); the user can raise a table to Edit rows or Change structure.'
    parts = []
    for lv in LEVELS[:0:-1]:
        names = ['.'.join(p for p in (x['d'], x['s'], x['t']) if p) for x in mine if x['level'] == lv]
        if names:
            parts.append(f'{LABEL[lv]}: {", ".join(names)}')
    text = f'AI access ({"; ".join(parts)}); every other table is Read only.'
    return text if len(text) <= INDEX_CHARS else text[:INDEX_CHARS - 40].rsplit(',', 1)[0] + ' … (describe_table says each).'


def level_of(levels, cid, db, schema, table):
    return levels.get(key(cid, db, schema, table), 'read')


def _target(node):
    t = node.this if isinstance(node.this, exp.Table) else node.find(exp.Table)
    if isinstance(t, exp.Schema):
        t = t.this
    return t if isinstance(t, exp.Table) else None


def _own_part(tree, node):
    """A write node that belongs to the statement itself: an ALTER's actions (DROP COLUMN ...), a MERGE's WHEN
    MATCHED THEN UPDATE / INSERT / DELETE (they change the same target)."""
    if isinstance(tree, exp.Alter):
        return node.parent is tree
    return isinstance(tree, exp.Merge) and node.find_ancestor(exp.When) is not None


def _sql_needs(st, dialect):
    """(schema, table, level, op) of one write statement (classify's dict); AccessError when no level allows it."""
    sql, op = st['sql'], st.get('op') or 'UNKNOWN'
    try:
        tree = parse_one(sql, dialect=classify.DIALECTS.get(dialect, dialect))
    except (SqlglotError, ValueError, RecursionError):
        tree = None
    if tree is None or isinstance(tree, exp.Command):
        raise AccessError(f'the assistant may not run {op} statements it cannot read: write it as plain '
                          'INSERT / UPDATE / DELETE / ALTER TABLE')
    if isinstance(tree, _ROWS_OPS):
        level = 'rows'
    elif isinstance(tree, exp.Drop):
        kind = str(tree.args.get('kind') or '').upper()
        if kind != 'TABLE':
            raise AccessError(f'the assistant may not DROP {kind or "this"}: only a table, at Change structure')
        level = 'schema'
    elif isinstance(tree, _SCHEMA_OPS):
        if isinstance(tree, exp.Alter) and str(tree.args.get('kind') or 'TABLE').upper() != 'TABLE':
            raise AccessError('the assistant may only ALTER a table')
        level = 'schema'
    elif isinstance(tree, exp.Create) or (isinstance(tree, exp.Select) and tree.args.get('into') is not None):
        raise AccessError('the assistant may not create permanent tables, views or indexes: use a temp table '
                          '(#name / CREATE TEMP TABLE), or the user runs it')
    else:
        raise AccessError(f'the assistant may not run {op}: only INSERT / UPDATE / DELETE / MERGE (Edit rows) and '
                          'ALTER / TRUNCATE / DROP TABLE (Change structure)')
    if any(isinstance(n, classify._WRITE_NODES) and n is not tree and not _own_part(tree, n) for n in tree.walk()):
        raise AccessError('one change per statement: split it')
    t = _target(tree)
    if t is None or not t.name:
        raise AccessError(f'which table {op} changes could not be read: name it plainly')
    return t.catalog or '', t.db or '', t.name, level, op


def required(statements, conn_kind):
    """[(database, schema, table, level, op)] the write statements of `statements` (classify's output) need."""
    out = []
    for st in statements:
        if st['kind'] != classify.WRITE:
            continue                                    # reads and this session's temp tables: always allowed
        if conn_kind == 'mongo':
            m = (st.get('command') or {}).get('method') or st.get('op')
            coll = (st.get('tables') or [''])[0]
            if m in _MONGO_ROWS:
                out.append(('', '', coll, 'rows', m))
            elif m in _MONGO_SCHEMA:
                out.append(('', '', coll, 'schema', m))
            else:
                raise AccessError(f'the assistant may not run {m}: {st.get("reason") or "not an allowed change"}')
            continue
        out.append(_sql_needs(st, conn_kind))
    return out


def gate(levels, cid, database, statements, conn_kind):
    """AccessError when a statement needs more than its table's level for this user."""
    dflt = DEFAULT_SCHEMA.get(conn_kind, '')
    for db, schema, table, need, op in required(statements, conn_kind):
        db, schema = db or database or '', schema or dflt
        have = level_of(levels, cid, db, schema, table)
        if LEVELS.index(have) < LEVELS.index(need):
            name = '.'.join(p for p in (schema, table) if p)
            raise AccessError(
                f'{op} on {name} needs AI access "{LABEL[need]}", and this table is "{LABEL[have]}" for the assistant. '
                f'Tell the user: they can raise it in Data Analysis (right-click the table in the object explorer › '
                f'AI access). If the table lives in another schema, write its full schema.table name.')
