"""
The Query Model: what the Visual builder edits, and how it stays in step with SQL.

    parse(sql, dialect)      SQL -> model. Parsing never touches a database.
    generate(model, dialect) model -> SQL, or a MongoDB pipeline for dialect 'mongo'

The model:

    tables  [{id, schema, name, database, alias, kind, sql, raw, show, sel: [column], star, cast: {column: {type, safe}}}]
            kind: table | sub (a subquery) | cte | remote (OPENQUERY)
            cols: for sub / cte / remote, the column names known from the SQL alone (its own SELECT list, and every
                  column the outer query uses through its alias), so the card has rows and join lines a place to end
    joins   [{a: [table id, column], b: [table id, column], type: INNER | LEFT | RIGHT | FULL}]
    calc    [{alias, t: table id, kind: case | window | sub | cast | expr, expr}]
    parts   {where, group, having, order, top, limit, distinct}
    locked  [why the builder cannot edit this query]: empty when it can

What the model cannot hold is never dropped: `locked` names it and the SQL text stays the source. Each group of joined
shown tables becomes one SELECT, so unjoined tables give one result each.
"""

import json

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

from . import classify

_NO_BUILDER = ((exp.Pivot, 'PIVOT'), (exp.Lateral, 'APPLY / LATERAL'), (exp.Rollup, 'ROLLUP'), (exp.Cube, 'CUBE'),
               (exp.GroupingSets, 'GROUPING SETS'))
_TYPES = {'INNER': 'INNER', '': 'INNER', 'LEFT': 'LEFT', 'RIGHT': 'RIGHT', 'FULL': 'FULL'}


def _d(dialect):
    return classify.DIALECTS.get(dialect, dialect)


def _table_id(t, used):
    base = (t.get('alias') or t['name'] or 'x').lower()
    i, name = 1, base
    while name in used:
        i += 1
        name = f'{base}{i}'
    used.add(name)
    return name


def _outputs(tree):
    """The column names a SELECT (or the left side of a UNION) gives, without its stars."""
    while isinstance(tree, exp.SetOperation):
        tree = tree.left
    if not isinstance(tree, exp.Select):
        return []
    return [e.alias_or_name for e in tree.expressions if e.alias_or_name and e.alias_or_name != '*']


def _remote_outputs(text):
    """The column names of the query inside OPENQUERY, when it reads as SQL (the remote server's dialect is unknown)."""
    try:
        return _outputs(parse_one(text, dialect='tsql'))
    except (SqlglotError, ValueError):
        return []


def _source(node, dialect, locked, cte_names):
    """A FROM / JOIN item -> a model table, or None when it is outside what the model holds."""
    if isinstance(node, exp.Subquery):
        alias = node.alias or 'sub'
        return {'schema': '', 'name': alias, 'database': '', 'alias': alias, 'kind': 'sub', 'sql': node.this.sql(dialect=dialect, pretty=True),
                'cols': _outputs(node.this)}
    if isinstance(node, exp.Table):
        fn = node.this
        if isinstance(fn, exp.Anonymous):
            if str(fn.this).upper() == 'OPENQUERY':
                alias = node.alias or 'remote'
                arg = fn.expressions[1] if len(fn.expressions) > 1 else None
                text = arg.this if isinstance(arg, exp.Literal) else ''
                return {'schema': '', 'name': alias, 'database': '', 'alias': alias, 'kind': 'remote',
                        'sql': text, 'raw': fn.sql(dialect=dialect), 'cols': _remote_outputs(text) if text else []}
            locked.append(f'{fn.this} in FROM')
            return None
        name = node.name
        kind = 'cte' if name.lower() in cte_names else 'table'
        temp = isinstance(node.this, exp.Identifier) and node.this.args.get('temporary')
        out = {'schema': node.db, 'name': f'#{name}' if temp else name, 'database': node.catalog, 'alias': node.alias or '', 'kind': kind}
        if kind == 'cte':
            out['cols'] = list(cte_names[name.lower()])
        return out
    locked.append('this FROM form')
    return None


def _select_item(item, tables, model, dialect, counter):
    """One entry of the SELECT list. A plain column goes to its table; everything else is a computed column."""
    alias = item.alias if isinstance(item, exp.Alias) else ''
    e = item.this if isinstance(item, exp.Alias) else item
    if isinstance(e, exp.Star):
        for t in tables:
            t['star'] = True
        return
    if isinstance(e, exp.Column) and isinstance(e.this, exp.Star):
        t = _find(tables, e.table)
        if t:
            t['star'] = True
            return
    if isinstance(e, exp.Column) and (not alias or alias == e.name):
        t = _find(tables, e.table) if e.table else None
        model['loose' if t is None and len(tables) > 1 else 'sel'].append([t['id'] if t else (tables[0]['id'] if tables else ''), e.name])
        return
    if isinstance(e, exp.Cast) and isinstance(e.this, exp.Column) and (not alias or alias == e.this.name):
        t = _find(tables, e.this.table) or (tables[0] if len(tables) == 1 else None)
        if t:
            t['sel'].append(e.this.name)
            t['cast'][e.this.name] = {'type': e.to.sql(dialect=dialect), 'safe': isinstance(e, exp.TryCast)}
            return
    counter[0] += 1
    name = alias or f'Column{counter[0]}'
    if isinstance(e, exp.Case):
        kind = 'case'
    elif isinstance(e, exp.Window):
        kind = 'window'
    elif isinstance(e, (exp.Subquery, exp.Select)):
        kind = 'sub'
    elif isinstance(e, exp.Cast):
        kind = 'cast'
    else:
        kind = 'expr'
    col = e.find(exp.Column)
    owner = _find(tables, col.table) if col is not None and col.table else None
    model['calc'].append({'alias': name, 't': (owner or (tables[0] if tables else {'id': ''}))['id'], 'kind': kind,
                          'expr': e.sql(dialect=dialect)})


def _find(tables, ref):
    ref = (ref or '').lower()
    return next((t for t in tables if ref and (t['alias'].lower() == ref or t['name'].lower() == ref)), None)


def _parse_select(tree, dialect, model, used, cte_names, counter):
    locked = model['locked']
    tables = []

    def add(node):
        src = _source(node, dialect, locked, cte_names)
        if src is None:
            return None
        if any(t['name'].lower() == src['name'].lower() and t['schema'].lower() == src['schema'].lower() and src['kind'] == 'table' for t in tables):
            locked.append('a self join')
            return None
        src.update(id=_table_id(src, used), show=True, sel=[], star=False, cast={})
        tables.append(src)
        return src

    frm = tree.args.get('from_') or tree.args.get('from')
    if frm is not None:
        add(frm.this)
    for join in tree.args.get('joins') or []:
        right = add(join.this)
        if right is None:
            continue
        side, kind = (join.side or '').upper(), (join.kind or '').upper()
        on = join.args.get('on')
        if kind == 'CROSS' or join.args.get('using') or on is None:
            locked.append('CROSS JOIN' if kind == 'CROSS' or on is None else 'JOIN USING')
            continue
        pairs = _column_pairs(on, tables)
        if pairs is None:
            locked.append('a join condition the builder cannot draw')
            continue
        model['joins'].extend({'a': [a['id'], ca], 'b': [b['id'], cb], 'type': _TYPES.get(side, 'INNER')} for a, ca, b, cb in pairs)

    bucket = {'sel': [], 'loose': [], 'calc': model['calc']}
    for item in tree.expressions:
        _select_item(item, tables, bucket, dialect, counter)
    for tid, col in bucket['sel']:
        t = next((x for x in tables if x['id'] == tid), None)
        if t is not None:
            t['sel'].append(col)
    model.setdefault('loose', []).extend(c for _, c in bucket['loose'])
    _known_columns(tree, tables)
    model['tables'].extend(tables)
    return tables


def _column_pairs(on, tables):
    """ON a.x = b.x [AND a.y = b.y ...] between two tables -> [(a, x, b, x), ...]; None for any other condition."""
    terms = list(on.flatten()) if isinstance(on, exp.And) else [on]
    out = []
    for e in terms:
        if not isinstance(e, exp.EQ) or not isinstance(e.left, exp.Column) or not isinstance(e.right, exp.Column):
            return None
        a, b = _find(tables, e.left.table), _find(tables, e.right.table)
        if a is None or b is None or a is b:
            return None
        out.append((a, e.left.name, b, e.right.name))
    return out if len({frozenset((a['id'], b['id'])) for a, _, b, _ in out}) == 1 else None


def _known_columns(tree, tables):
    """A subquery, CTE or OPENQUERY card has no catalog entry: add every column this SELECT uses through its alias."""
    derived = [t for t in tables if t['kind'] != 'table']
    if not derived:
        return
    for col in tree.find_all(exp.Column):
        if not col.table or col.find_ancestor(exp.Select) is not tree:
            continue
        t = _find(derived, col.table)
        if t is not None and col.name not in t['cols']:
            t['cols'].append(col.name)


def _parts(tree, dialect, one):
    parts = {}
    if not one:
        return parts
    where, group, having, order = (tree.args.get(k) for k in ('where', 'group', 'having', 'order'))
    if where is not None:
        parts['where'] = where.this.sql(dialect=dialect)
    if group is not None:
        parts['group'] = ', '.join(e.sql(dialect=dialect) for e in group.expressions)
    if having is not None:
        parts['having'] = having.this.sql(dialect=dialect)
    if order is not None:
        parts['order'] = ', '.join(e.sql(dialect=dialect) for e in order.expressions)
    limit = tree.args.get('limit')
    if limit is not None and isinstance(limit.expression, exp.Literal):
        parts['top'] = int(limit.expression.this)
    if tree.args.get('distinct') is not None:
        parts['distinct'] = True
    return parts


def parse(sql, dialect):
    """SQL -> model. {error} when it is not SQL the parser can read; a write gives {statement: 'write'}."""
    d = _d(dialect)
    statements = classify.split(sql)
    if not statements:
        return {'error': 'the query is empty'}
    model = {'tables': [], 'joins': [], 'calc': [], 'parts': {}, 'locked': [], 'adv': ''}
    used, counter = set(), [0]
    for text in statements:
        try:
            tree = parse_one(text, dialect=d)
        except (SqlglotError, ValueError) as e:
            return {'error': str(e).splitlines()[0][:200]}
        if isinstance(tree, exp.SetOperation):
            model['locked'].append(tree.key.upper())
            model['adv'] = text
            left = tree.left
            while isinstance(left, exp.SetOperation):
                left = left.left
            tree = left
        if not isinstance(tree, exp.Select):
            return {'statement': 'write', 'tables': [], 'joins': [], 'calc': [], 'parts': {}, 'locked': []}
        for node, why in _NO_BUILDER:
            if tree.find(node) is not None:
                model['locked'].append(why)
        cte_names = {}
        withs = tree.args.get('with_') or tree.args.get('with')
        if withs is not None:
            if withs.recursive:
                model['locked'].append('a recursive CTE')
            cte_names = {c.alias.lower(): _outputs(c.this) for c in withs.expressions}
            model['cte'] = [{'name': c.alias, 'sql': c.this.sql(dialect=d, pretty=True)} for c in withs.expressions]
        _parse_select(tree, d, model, used, cte_names, counter)
        model['parts'] = {**_parts(tree, d, len(statements) == 1), 'parsed': True}
        model['output'] = [e.alias_or_name for e in tree.expressions if e.alias_or_name != '*']
        if tree.args.get('into') is not None:
            return {'statement': 'write', 'tables': [], 'joins': [], 'calc': [], 'parts': {}, 'locked': []}
    if len(statements) > 1 and any(tree_has_clauses(s, d) for s in statements):
        model['locked'].append('filters across several statements')
    model['locked'] = sorted(set(model['locked']))
    return model


def tree_has_clauses(text, d):
    tree = parse_one(text, dialect=d)
    return any(tree.args.get(k) is not None for k in ('where', 'group', 'having', 'order'))


# ---------------------------------------------------------------- generating
def _quote(name, d):
    return exp.to_identifier(name, quoted=True).sql(dialect=d)


def _ref(t, d):
    if t.get('kind') == 'remote':
        return f'{t["raw"]} AS {_quote(_alias(t), d)}'
    if t.get('kind') == 'sub':
        body = t['sql'].replace('\n', '\n  ')
        return f'(\n  {body}\n) AS {_quote(_alias(t), d)}'
    parts = [p for p in (t.get('database'), t.get('schema')) if p]
    name = t['name']
    if name.startswith('#'):
        return f'{name} AS {_quote(_alias(t), d)}'
    return '.'.join([*(_quote(p, d) for p in parts), _quote(name, d)]) + f' AS {_quote(_alias(t), d)}'


def _alias(t):
    return t.get('alias') or t['id']


def _groups(model):
    shown = [t for t in model['tables'] if t.get('show', True)]
    parent = {t['id']: t['id'] for t in shown}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    live = [j for j in model['joins'] if j['a'][0] in parent and j['b'][0] in parent]
    for j in live:
        parent[find(j['a'][0])] = find(j['b'][0])
    groups = {}
    for t in shown:
        groups.setdefault(find(t['id']), {'tables': [], 'joins': []})['tables'].append(t)
    for j in live:
        groups[find(j['a'][0])]['joins'].append(j)
    return list(groups.values())


def _one_select(group, model, d, solo, default_top):
    by_id = {t['id']: t for t in group['tables']}
    parts = model.get('parts') or {} if solo else {}
    items = []
    for t in group['tables']:
        a = _quote(_alias(t), d)
        if t.get('star'):
            items.append(f'{a}.*')
            continue
        for c in t.get('sel') or []:
            ref = f'{a}.{_quote(c, d)}'
            cast = (t.get('cast') or {}).get(c)
            items.append(f'{"TRY_CAST" if cast.get("safe") else "CAST"}({ref} AS {cast["type"]}) AS {_quote(c, d)}' if cast else ref)
    for c in model.get('calc') or []:
        if c.get('t') in by_id:
            items.append(f'{c["expr"]} AS {_quote(c["alias"], d)}')
    top = parts.get('top', default_top)
    head = 'SELECT ' + ('DISTINCT ' if parts.get('distinct') else '') + (f'TOP ({int(top)}) ' if d == 'tsql' and top else '')
    lines = [head + (',\n       '.join(items) or '*'), 'FROM ' + _ref(group['tables'][0], d)]
    done, pending = {group['tables'][0]['id']}, list(group['joins'])
    line_of = {group['tables'][0]['id']: 1}             # the FROM / JOIN line that brought each table in
    while pending:
        i = next((n for n, j in enumerate(pending) if j['a'][0] in done or j['b'][0] in done), 0)
        j = pending.pop(i)
        left, right = by_id[j['a'][0]], by_id[j['b'][0]]
        cond = f'{_quote(_alias(left), d)}.{_quote(j["a"][1], d)} = {_quote(_alias(right), d)}.{_quote(j["b"][1], d)}'
        if j['a'][0] in done and j['b'][0] in done:
            at = max(line_of[j['a'][0]], line_of[j['b'][0]])
            lines[at] += f'\n  AND {cond}'
            continue
        new = right if j['a'][0] in done else left
        done.add(new['id'])
        line_of[new['id']] = len(lines)
        lines.append(f'{"INNER" if j["type"] == "INNER" else j["type"] + " OUTER"} JOIN {_ref(new, d)}\n  ON {cond}')
    for key, word in (('where', 'WHERE'), ('group', 'GROUP BY'), ('having', 'HAVING'), ('order', 'ORDER BY')):
        if parts.get(key):
            lines.append(f'{word} {parts[key]}')
    if d != 'tsql' and top:
        lines.append(f'LIMIT {int(top)}')
    return '\n'.join(lines) + ';'


def generate(model, dialect, default_top=1000):
    """model -> query text. An empty builder gives ''. A CTE's name stays a plain table in the FROM list."""
    if dialect == 'mongo':
        return _pipeline(model, default_top)
    d = _d(dialect)
    groups = _groups(model)
    if not groups:
        return ''
    top = None if (model.get('parts') or {}).get('parsed') else default_top
    with_ = ''
    if model.get('cte'):
        with_ = 'WITH ' + ',\n'.join(f'{_quote(c["name"], d)} AS (\n  {c["sql"]}\n)' for c in model['cte']) + '\n'
    out = []
    for g in groups:
        head = f'-- {" + ".join(t["name"] for t in g["tables"])}\n' if len(groups) > 1 else ''
        out.append(head + with_ + _one_select(g, model, d, len(groups) == 1, top))
    return '\n\n'.join(out)


def _pipeline(model, limit):
    """MongoDB: joins are $lookup. One command per group of joined collections."""
    out = []
    for g in _groups(model):
        first = g['tables'][0]
        stages = []
        for j in g['joins']:
            near, far = (j['a'], j['b']) if j['a'][0] == first['id'] else (j['b'], j['a'])
            other = next(t for t in g['tables'] if t['id'] == far[0])
            stages.append({'$lookup': {'from': other['name'], 'localField': near[1], 'foreignField': far[1], 'as': other['name']}})
            stages.append({'$unwind': {'path': f'${other["name"]}', **({} if j['type'] == 'INNER' else {'preserveNullAndEmptyArrays': True})}})
        fields = {}
        for t in g['tables']:
            prefix = '' if t is first else f'{t["name"]}.'
            for c in t.get('sel') or []:
                cast = (t.get('cast') or {}).get(c)
                fields[f'{prefix}{c}'] = {'$convert': {'input': f'${prefix}{c}', 'to': cast['type'], 'onError': None}} if cast else 1
        if fields:
            stages.append({'$project': fields})
        stages.append({'$limit': limit})
        body = ',\n  '.join(json.dumps(s) for s in stages)
        out.append(f'db.getCollection("{first["name"]}").aggregate([\n  {body}\n])')
    return '\n\n'.join(out)
