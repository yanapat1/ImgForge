"""
Check SQL against a connection's syntax, and fix text written for another database. Pure and fast: no database call
(the AI tool adds the database's own EXPLAIN on top).

    check(text, kind, source=None) -> {dialect, ok, statements, errors, warnings, fixed, changes, source}

    errors    [{stmt, line, col, message}]   line/col in the whole text, 1-based
    fixed     the whole text with only the statements that needed it rewritten, or None when nothing did
    changes   short notes of what a fix changed ("LIMIT 10 → TOP 10", "added TOP 10")
    source    the syntax the text was read as, when it was not the connection's

A statement is rewritten only when it needs it: it was written in another syntax, or it holds something the
connection's database lacks (FOREIGN). Valid text keeps the user's formatting.
"""

import difflib

import sqlglot
from sqlglot import ErrorLevel, exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import ParseError, SqlglotError
from sqlglot.tokens import TokenType

from . import classify

MAX_TEXT = 200000
NAMES = {'tsql': 'SQL Server (T-SQL)', 'postgres': 'PostgreSQL', 'duckdb': 'DuckDB', 'mongo': 'MongoDB shell'}
# other syntaxes text may be written in, tried in this order when the connection's own does not read it cleanly
SOURCES = ('postgres', 'tsql', 'mysql', 'duckdb', 'sqlite', 'oracle', 'snowflake', 'bigquery', 'spark')
_VALUE_TOKENS = {TokenType.VAR, TokenType.IDENTIFIER, TokenType.STRING, TokenType.NUMBER, TokenType.NATIONAL_STRING}
_SKIP_TOKENS = {TokenType.ALIAS, TokenType.SEMICOLON}
# what a syntax's parser accepts but its database does not: text read cleanly as the connection's own syntax is
# rewritten only when it holds one of these (anything else the generator would change is only spelling: TOP (5), ::)
FOREIGN = {'tsql': {TokenType.LIMIT, TokenType.ILIKE, TokenType.DCOLON}, 'postgres': {TokenType.TOP}}
MAX_CHANGES = 20
MERGE_GAP = 3


def _parse(sql, dialect):
    """-> (tree, None) or (None, ParseError-like dict)."""
    try:
        trees = sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE)
    except ParseError as e:
        first = (e.errors or [{}])[0]
        return None, {'line': first.get('line') or 1, 'col': first.get('col') or 1,
                      'message': str(first.get('description') or e).splitlines()[0][:200]}
    except (SqlglotError, ValueError, RecursionError) as e:
        return None, {'line': 1, 'col': 1, 'message': str(e).splitlines()[0][:200]}
    trees = [t for t in trees if t is not None]
    if len(trees) != 1:
        return None, {'line': 1, 'col': 1, 'message': 'expected one statement'}
    return trees[0], None


def _unknown_functions(tree):
    return {a.name.upper() for a in tree.find_all(exp.Anonymous)}


def _target_fixes(tree, target):
    """Rewrites the generator does not make by itself."""
    if target == 'tsql':
        def ilike(n):
            if isinstance(n, exp.ILike):         # SQL Server has no ILIKE: compare both sides lower-cased
                return exp.Like(this=exp.Lower(this=n.this), expression=exp.Lower(this=n.expression))
            return n
        tree = tree.transform(ilike)
    return tree


def _generate(tree, target, pretty):
    g = Dialect.get_or_raise(target).generator(unsupported_level=ErrorLevel.IGNORE, pretty=pretty)
    return g.generate(tree), list(dict.fromkeys(g.unsupported_messages))


def _tokens(sql, dialect):
    try:
        toks = Dialect.get_or_raise(dialect).tokenize(sql)
    except (SqlglotError, ValueError):
        return []
    return [t for t in toks if t.token_type not in _SKIP_TOKENS]


def _key(t):
    if t.token_type in _VALUE_TOKENS:
        return t.token_type.name, t.text.upper() if t.token_type == TokenType.VAR else t.text
    return t.token_type.name, ''


def _span(sql, toks):
    return ' '.join(sql[toks[0].start:toks[-1].end + 1].split())[:80] if toks else ''


def _diff(a_sql, a_dialect, b_sql, b_dialect):
    """The changes between two statements, by tokens. [] = the same apart from spelling."""
    ta, tb = _tokens(a_sql, a_dialect), _tokens(b_sql, b_dialect)
    m = difflib.SequenceMatcher(None, [_key(t) for t in ta], [_key(t) for t in tb], autojunk=False)
    runs = []                                      # edits a few tokens apart read as one change
    for op, i1, i2, j1, j2 in m.get_opcodes():
        if op == 'equal':
            continue
        if runs and i1 - runs[-1][1] <= MERGE_GAP and j1 - runs[-1][3] <= MERGE_GAP:
            runs[-1][1], runs[-1][3] = i2, j2
        else:
            runs.append([i1, i2, j1, j2])
    out = []
    for i1, i2, j1, j2 in runs:
        a, b = _span(a_sql, ta[i1:i2]), _span(b_sql, tb[j1:j2])
        out.append(f'{a} → {b}' if a and b else f'removed {a}' if a else f'added {b}')
    return out


def _fix_one(stmt, target, source):
    """-> (new_sql | None, changes, warnings, error | None, read_as)."""
    tree, err = _parse(stmt, source or target)
    read_as = source if source and source != target else None
    if source is None and (err is not None or (tree is not None and _unknown_functions(tree))):
        unknown = len(_unknown_functions(tree)) if tree is not None else None
        for d in SOURCES:                          # read it the way it was most likely written
            if d == target:
                continue
            t2, e2 = _parse(stmt, d)
            if t2 is None:
                continue
            n2 = len(_unknown_functions(t2))
            if unknown is None or n2 < unknown:
                tree, err, unknown, read_as = t2, None, n2, d
                if n2 == 0:
                    break
    if tree is None:
        return None, [], [], err, None
    if isinstance(tree, exp.Command):
        return None, [], [f'not checked, passed as is: {stmt.split(None, 1)[0][:40]}'], None, None
    tree = _target_fixes(tree, target)
    new, unsupported = _generate(tree, target, pretty='\n' in stmt)
    warnings = [f'{NAMES.get(target, target)} has no direct form for: {u[:160]}' for u in unsupported]
    unknown = sorted(_unknown_functions(tree))
    if unknown:
        warnings.append(f'functions not known to the parser (may not exist in {NAMES.get(target, target)}): {", ".join(unknown[:10])}')
    if read_as is None and not any(t.token_type in FOREIGN.get(target, ()) for t in _tokens(stmt, target)):
        return None, [], warnings, None, None
    changes = _diff(stmt, read_as or target, new, target)
    return (new if changes else None), changes, warnings, None, read_as


def _place(text, off, line, col):
    """A statement-relative line/col -> the whole text's."""
    first_line = text.count('\n', 0, off) + 1
    line_start = text.rfind('\n', 0, off) + 1
    return first_line + line - 1, col + (off - line_start if line == 1 else 0)


def check(text, kind, source=None):
    text = str(text or '')
    if not text.strip():
        raise ValueError('the query is empty')
    if len(text) > MAX_TEXT:
        raise ValueError('the query is too long')
    if kind == 'mongo':
        try:
            classify.parse_mongo(text)
            errors = []
        except ValueError as e:
            errors = [{'stmt': 1, 'line': 1, 'col': 1, 'message': str(e)[:200]}]
        return {'dialect': NAMES['mongo'], 'ok': not errors, 'statements': 1, 'errors': errors, 'warnings': [],
                'fixed': None, 'changes': [], 'source': None}
    target = classify.DIALECTS.get(kind, kind)
    if source:
        source = classify.DIALECTS.get(source, source)
        try:
            Dialect.get_or_raise(source)
        except (ValueError, SqlglotError):
            raise ValueError(f'unknown syntax {source!r}') from None
    errors, warnings, changes, read_as = [], [], [], set()
    parts, cursor, out, changed = classify.split(text), 0, [], False
    for n, stmt in enumerate(parts, 1):
        off = text.find(stmt, cursor)
        off = cursor if off < 0 else off
        out.append(text[cursor:off])
        new, ch, warn, err, ra = _fix_one(stmt, target, source)
        if err:
            line, col = _place(text, off, err['line'], err['col'])
            errors.append({'stmt': n, 'line': line, 'col': col, 'message': err['message']})
        warnings += [f'statement {n}: {w}' if len(parts) > 1 else w for w in warn]
        if ra:
            read_as.add(ra)
        if new is not None:
            changed = True
            changes += [f'statement {n}: {c}' if len(parts) > 1 else c for c in ch]
        out.append(new if new is not None else stmt)
        cursor = off + len(stmt)
    out.append(text[cursor:])
    return {'dialect': NAMES.get(target, target), 'ok': not errors and not changed, 'statements': len(parts),
            'errors': errors, 'warnings': warnings[:MAX_CHANGES], 'fixed': ''.join(out) if changed else None,
            'changes': changes[:MAX_CHANGES] + ([f'… {len(changes) - MAX_CHANGES} more'] if len(changes) > MAX_CHANGES else []),
            'source': ', '.join(NAMES.get(d, d) for d in sorted(read_as)) or None}
