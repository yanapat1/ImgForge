"""
The user's favorite tables and their categories, as the ◆ assistant sees them. The page keeps them in the user prefs
(key `analysis_favorites`, written by static/ts/analysis/favorites.ts); these helpers read that blob, validated the same
way as the page's loadFavs, and stay free of the database so they are cheap and easy to test.

Token budget: the system prompt carries only `index_line` (category names and counts, capped); a category's tables come
with the list_favorites tool when the model needs them.
"""

import unicodedata

KEY = 'analysis_favorites'
LIMITS = {'items': 300, 'groups': 40, 'name': 60}
INDEX_CHARS = 500


def _str(v, n=200):
    return v[:n] if isinstance(v, str) else ''


def load(prefs):
    """{'groups': [names in order], 'items': [{c, d, s, t, g}]} from the prefs blob; bad shapes give empty lists."""
    raw = (prefs or {}).get(KEY) if isinstance(prefs, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    groups = []
    for g in raw.get('groups') if isinstance(raw.get('groups'), list) else []:
        g = _str(g).strip()[:LIMITS['name']]
        if g and g not in groups:
            groups.append(g)
    groups = groups[:LIMITS['groups']]
    known, seen, items = set(groups), set(), []
    for x in raw.get('items') if isinstance(raw.get('items'), list) else []:
        if not isinstance(x, dict) or len(items) >= LIMITS['items']:
            continue
        f = {'c': _str(x.get('c'), 64), 'd': _str(x.get('d')), 's': _str(x.get('s')), 't': _str(x.get('t'))}
        g = _str(x.get('g'))
        f['g'] = g if g in known else ''
        k = (f['c'], f['d'], f['s'], f['t'])
        if not f['c'] or not f['t'] or k in seen:
            continue
        seen.add(k)
        items.append(f)
    return {'groups': groups, 'items': items}


def index_line(favs):
    """One short line for the system prompt: the categories and how many tables each holds. '' when there are none."""
    items, groups = favs['items'], favs['groups']
    if not items and not groups:
        return ''
    count = {g: 0 for g in groups}
    for f in items:
        if f['g']:
            count[f['g']] += 1
    loose = sum(1 for f in items if not f['g'])
    head = f'Your categories (Favorites, {len(items)} tables): '
    tail = (f'; {loose} uncategorized' if loose else '') + '. list_favorites gives a category\'s tables.'
    parts, used = [], len(head) + len(tail)
    for i, g in enumerate(groups):
        p = f'{g} ({count[g]})'
        if used + len(p) + 2 > INDEX_CHARS:
            parts.append(f'… +{len(groups) - i} more')
            break
        parts.append(p)
        used += len(p) + 2
    return head + (', '.join(parts) if parts else 'none') + tail


def _norm(s):
    return ' '.join(unicodedata.normalize('NFKC', str(s or '')).casefold().split())


def resolve(favs, name=None):
    """What `name` means among the favorites: a category (exact, then prefix, then substring), else favorite tables whose
    name holds it. Returns {'match': 'all'|'category'|'table'|'ambiguous'|'none', 'categories': [...], 'items': [...]}."""
    groups, items = favs['groups'], favs['items']
    q = _norm(name)
    if not q:
        return {'match': 'all', 'categories': list(groups), 'items': list(items)}
    names = {g: _norm(g) for g in groups}
    for test in (lambda n: n == q, lambda n: n.startswith(q), lambda n: q in n):
        hit = [g for g in groups if test(names[g])]
        if len(hit) == 1:
            return {'match': 'category', 'categories': hit, 'items': [f for f in items if f['g'] == hit[0]]}
        if hit:
            return {'match': 'ambiguous', 'categories': hit, 'items': [f for f in items if f['g'] in hit]}
    tables = [f for f in items if q in _norm(f['t']) or q in _norm(f'{f["s"]}.{f["t"]}')]
    if tables:
        return {'match': 'table', 'categories': sorted({f['g'] for f in tables if f['g']}), 'items': tables}
    return {'match': 'none', 'categories': list(groups), 'items': []}


def lookup(favs, cid, database):
    """{(schema, table): category} of the favorites on one connection and database ('' = no category)."""
    return {(f['s'], f['t']): f['g'] for f in favs['items'] if f['c'] == cid and f['d'] == (database or '')}
