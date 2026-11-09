"""The favorites the ◆ assistant sees (analysis/favorites.py): the prefs blob is validated like the page's loadFavs,
the system-prompt line stays short, and a user's word resolves to a category, a favorite table, several, or none."""

from dataviz.analysis import favorites as fav


def blob(groups, items, **extra):
    return {fav.KEY: {'groups': groups, 'items': items, **extra}}


def it(t, g='', c='c1', d='db', s='dbo'):
    return {'c': c, 'd': d, 's': s, 't': t, 'g': g}


def test_load_validates_like_the_page():
    f = fav.load(blob(['Motion', 'Motion', ' Sales ', '', 7, 'x' * 99],
                      [it('a', 'Motion'), it('a', 'Motion'), it('b', 'Gone'), {'c': '', 't': 'z'}, 'junk', it('c', 'Sales')]))
    assert f['groups'] == ['Motion', 'Sales', 'x' * 60]
    assert [(x['t'], x['g']) for x in f['items']] == [('a', 'Motion'), ('b', ''), ('c', 'Sales')]
    for bad in (None, {}, {fav.KEY: 'x'}, {fav.KEY: {'groups': 'a', 'items': {}}}):
        assert fav.load(bad) == {'groups': [], 'items': []}
    many = fav.load(blob([f'g{i}' for i in range(99)], [it(f't{i}') for i in range(999)]))
    assert len(many['groups']) == 40 and len(many['items']) == 300


def test_index_line_is_short_and_empty_without_favorites():
    assert fav.index_line(fav.load({})) == ''
    line = fav.index_line(fav.load(blob(['Motion', 'Sales'], [it('a', 'Motion'), it('b', 'Motion'), it('c')])))
    assert line.startswith('Your categories (Favorites, 3 tables): Motion (2), Sales (0); 1 uncategorized.')
    assert 'list_favorites' in line
    big = fav.index_line(fav.load(blob([f'{"n" * 55}{i:02d}' for i in range(40)], [])))
    assert len(big) <= fav.INDEX_CHARS + 20 and '… +' in big


def test_resolve_category_table_ambiguous_none():
    f = fav.load(blob(['Motion', 'Motion capture', 'Sales EU', 'Sales US'],
                      [it('m1', 'Motion'), it('m2', 'Motion capture'), it('s1', 'Sales EU'), it('motion_log'), it('x', 'Sales US')]))
    r = fav.resolve(f, '  MOTION ')                         # exact wins over the prefix match
    assert r['match'] == 'category' and r['categories'] == ['Motion'] and [x['t'] for x in r['items']] == ['m1']
    assert fav.resolve(f, 'motion cap')['categories'] == ['Motion capture']
    r = fav.resolve(f, 'sales')
    assert r['match'] == 'ambiguous' and r['categories'] == ['Sales EU', 'Sales US'] and len(r['items']) == 2
    assert fav.resolve(f, 'eu')['categories'] == ['Sales EU']          # substring
    r = fav.resolve(f, 'log')
    assert r['match'] == 'table' and [x['t'] for x in r['items']] == ['motion_log']
    r = fav.resolve(f, 'weather')
    assert r['match'] == 'none' and r['items'] == [] and 'Motion' in r['categories']
    assert fav.resolve(f, '')['match'] == 'all' and len(fav.resolve(f)['items']) == 5


def test_lookup_one_database():
    f = fav.load(blob(['Motion'], [it('a', 'Motion'), it('b'), it('c', 'Motion', d='other')]))
    assert fav.lookup(f, 'c1', 'db') == {('dbo', 'a'): 'Motion', ('dbo', 'b'): ''}
