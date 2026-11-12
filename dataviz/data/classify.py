"""
What a statement does, decided on the server. This is the safety gate of the Analysis zone: a statement runs at once
only when it is proven to be a plain read. Anything else, including anything that cannot be parsed, needs the user's
confirmation.

    split(text)                  the statements of a script (quotes, comments, [names], `;` and T-SQL GO respected)
    classify(text, dialect)      [{sql, kind, op, tables, reason}], one per statement
    classify_mongo(text)         the same for one MongoDB command
    needs_confirm(statements)    the statements that must be confirmed first

    kind   read    returns rows and changes nothing
           temp    only touches a temp table of this session
           write   anything else
"""

import json
import re

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

READ, TEMP, WRITE = 'read', 'temp', 'write'
DIALECTS = {'mssql': 'tsql', 'postgres': 'postgres', 'workspace': 'duckdb'}

_READ_TOPS = (exp.Select, exp.SetOperation, exp.Subquery, exp.Values)
_WRITE_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Drop, exp.Create, exp.Alter, exp.TruncateTable,
                exp.Command, exp.Lock)
_OUTSIDE_READERS = {'OPENROWSET', 'OPENDATASOURCE'}      # reach files and servers no connection names
_GO = re.compile(r'^[ \t]*GO[ \t]*(?:\d+[ \t]*)?$', re.I)


def split(text):
    """The non-empty statements of `text`, each with its comments, in order."""
    out, start, i, n = [], 0, 0, len(text)

    def cut(end, resume):
        nonlocal start
        part = text[start:end]
        if _has_code(part):
            out.append(part.strip())
        start = resume

    while i < n:
        c = text[i]
        if c == '\n':
            eol = text.find('\n', i + 1)
            eol = n if eol < 0 else eol
            if _GO.match(text[i + 1:eol]):
                cut(i, eol)
                i = eol
                continue
            i += 1
        elif c == "'":
            i = _end_quote(text, i, "'")
        elif c == '"':
            i = _end_quote(text, i, '"')
        elif c == '[':
            i = _end_quote(text, i, ']')
        elif c == '$' and (m := re.match(r'\$[A-Za-z_]*\$', text[i:])):      # PostgreSQL dollar quoting
            end = text.find(m.group(0), i + len(m.group(0)))
            i = n if end < 0 else end + len(m.group(0))
        elif text.startswith('--', i):
            end = text.find('\n', i)
            i = n if end < 0 else end
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            i = n if end < 0 else end + 2
        elif c == ';':
            cut(i, i + 1)
            i += 1
        else:
            i += 1
    cut(n, n)
    return out


def _end_quote(text, i, close):
    """The index after the quoted run that opens at text[i]; a doubled closing quote stays inside."""
    j, n = i + 1, len(text)
    while j < n:
        if text[j] == close:
            if j + 1 < n and text[j + 1] == close:
                j += 2
                continue
            return j + 1
        j += 1
    return n


def _has_code(part):
    no_comments = re.sub(r'/\*.*?\*/|--[^\n]*', ' ', part, flags=re.S)
    return bool(no_comments.strip())


def _name(table):
    return '.'.join(p for p in (table.catalog, table.db, table.name) if p)


def _is_temp(table, temps):
    if table is None:
        return False
    flagged = isinstance(table.this, exp.Identifier) and table.this.args.get('temporary')     # T-SQL #name
    return bool(flagged) or table.name.startswith('#') or table.name.lower() in temps


def _tables(tree):
    seen = []
    for t in tree.find_all(exp.Table):
        n = _name(t)
        if n and n not in seen:
            seen.append(n)
    return seen


def _opaque(sql, why):
    return {'sql': sql, 'kind': WRITE, 'op': 'UNKNOWN', 'tables': [], 'reason': why}


def _remote_kind(tree, dialect, temps):
    """OPENQUERY runs its text on a linked server, so its text decides. -> (kind, reason)."""
    worst = READ, ''
    for call in tree.find_all(exp.Anonymous):
        name = str(call.this).upper()
        if name in _OUTSIDE_READERS:
            return WRITE, f'{name} reaches outside this connection'
        if name == 'OPENQUERY':
            inner = call.expressions[1] if len(call.expressions) > 1 else None
            if not isinstance(inner, exp.Literal) or not inner.is_string:
                return WRITE, 'OPENQUERY text is not a plain string'
            for s in classify(inner.this, dialect, temps):
                if s['kind'] != READ:
                    return WRITE, 'OPENQUERY text changes data on the linked server'
    return worst


def _one(sql, dialect, temps):
    try:
        tree = parse_one(sql, dialect=dialect)
    except (SqlglotError, ValueError, RecursionError) as e:
        return _opaque(sql, f'could not be parsed: {str(e).splitlines()[0][:120]}')
    if tree is None:
        return _opaque(sql, 'empty statement')
    out = {'sql': sql, 'op': tree.key.upper(), 'tables': _tables(tree), 'reason': ''}
    into = tree.args.get('into') if isinstance(tree, exp.Select) else None
    if isinstance(tree, exp.Create):
        props = tree.args.get('properties')
        temporary = bool(props and props.find(exp.TemporaryProperty)) or (
            isinstance(tree.this, (exp.Table, exp.Schema)) and _is_temp(tree.this.find(exp.Table), temps))
        return {**out, 'kind': TEMP if temporary else WRITE, 'target': _name(tree.this.find(exp.Table))}
    if isinstance(tree, (exp.Insert, exp.Update, exp.Delete, exp.TruncateTable, exp.Drop)):
        target = tree.find(exp.Table)
        only_temp = target is not None and _is_temp(target, temps) and not any(
            isinstance(n, _WRITE_NODES) and n is not tree for n in tree.walk())
        return {**out, 'kind': TEMP if only_temp else WRITE, 'target': _name(target) if target else ''}
    if not isinstance(tree, _READ_TOPS):
        return {**out, 'kind': WRITE, 'reason': f'{out["op"]} is not a plain read'}
    if into is not None:
        target = into.find(exp.Table)
        temporary = into.args.get('temporary') or (target is not None and _is_temp(target, temps))
        return {**out, 'kind': TEMP if temporary else WRITE, 'op': 'SELECT INTO', 'target': _name(target) if target else ''}
    for node in tree.walk():
        if isinstance(node, _WRITE_NODES):
            return {**out, 'kind': WRITE, 'reason': f'contains {node.key.upper()}'}
        if isinstance(node, exp.Into):
            return {**out, 'kind': WRITE, 'reason': 'contains INTO'}
    kind, why = _remote_kind(tree, dialect, temps)
    return {**out, 'kind': kind, 'reason': why}


def classify(text, dialect='tsql', temps=()):
    """temps: names of this session's temp tables, so changing one of them counts as temp."""
    dialect = DIALECTS.get(dialect, dialect)
    known = {t.lower() for t in temps}
    return [_one(s, dialect, known) for s in split(text)]


def needs_confirm(statements):
    return [s for s in statements if s['kind'] != READ]


# ---------------------------------------------------------------- MongoDB
_MONGO_CALL = re.compile(r'^\s*db\s*\.\s*(?:getCollection\(\s*["\']([^"\']+)["\']\s*\)|([A-Za-z_][\w.-]*?))\s*\.\s*([A-Za-z]+)\s*\(', re.S)
_MONGO_READS = {'find', 'findOne', 'aggregate', 'countDocuments', 'estimatedDocumentCount', 'distinct', 'count'}
_MONGO_WRITE_STAGES = {'$out', '$merge'}
_MONGO_CODE = {'$function', '$where', '$accumulator'}          # run server-side JavaScript
_MODIFIERS = {'limit', 'skip', 'sort', 'projection'}


def relaxed_json(text):
    """Shell-style values -> Python: unquoted keys, single quotes and trailing commas are accepted."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in '"\'':
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == '\\' else 1
            body = text[i + 1:j]
            out.append(json.dumps(body.replace("\\'", "'").replace('\\"', '"')) if c == "'" else f'"{body}"')
            i = j + 1
        elif re.match(r'[A-Za-z_$]', c):
            m = re.match(r'[\w$.]+', text[i:])
            word = m.group(0)
            after = text[i + len(word):].lstrip()
            out.append(json.dumps(word) if after.startswith(':') else word)
            i += len(word)
        elif c in '}]':
            while out and out[-1].strip() == '':
                out.pop()
            if out and out[-1] == ',':
                out.pop()
            out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return json.loads(''.join(out))


def _balanced(text, start):
    """text[start] is '(': -> the inside and the index after the matching ')'."""
    depth, i, quote = 0, start, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == '\\':
                i += 1
            elif c == quote:
                quote = None
        elif c in '"\'':
            quote = c
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
        i += 1
    raise ValueError('a bracket is not closed')


def parse_mongo(text):
    """'db.x.find({...}).limit(5)' -> {collection, method, args, modifiers}. ValueError when it is not that shape."""
    text = text.strip().rstrip(';').strip()
    m = _MONGO_CALL.match(text)
    if m is None:
        raise ValueError('write a command like db.collection.find({...}) or db.collection.aggregate([...])')
    inside, end = _balanced(text, m.end() - 1)
    args = relaxed_json(f'[{inside}]')
    mods, rest = {}, text[end:]
    while rest.strip():
        c = re.match(r'\s*\.\s*([A-Za-z]+)\s*\(', rest)
        if c is None or c.group(1) not in _MODIFIERS:
            raise ValueError(f'unsupported after the call: {rest.strip()[:40]}')
        body, stop = _balanced(rest, c.end() - 1)
        mods[c.group(1)] = relaxed_json(f'[{body}]')[0]
        rest = rest[stop:]
    return {'collection': m.group(1) or m.group(2), 'method': m.group(3), 'args': args, 'modifiers': mods}


def _keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from _keys(v)


def classify_mongo(text):
    try:
        cmd = parse_mongo(text)
    except (ValueError, json.JSONDecodeError) as e:
        return [_opaque(text, f'could not be parsed: {str(e)[:120]}')]
    out = {'sql': text, 'op': cmd['method'], 'tables': [cmd['collection']], 'reason': '', 'command': cmd}
    if cmd['method'] not in _MONGO_READS:
        return [{**out, 'kind': WRITE, 'reason': f'{cmd["method"]} is not a read'}]
    keys = set(_keys(cmd['args']))
    if keys & _MONGO_WRITE_STAGES:
        return [{**out, 'kind': WRITE, 'reason': f'{sorted(keys & _MONGO_WRITE_STAGES)[0]} writes to a collection'}]
    if keys & _MONGO_CODE:
        return [{**out, 'kind': WRITE, 'reason': f'{sorted(keys & _MONGO_CODE)[0]} runs code on the server'}]
    return [{**out, 'kind': READ}]
