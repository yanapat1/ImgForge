"""
Join insight: what one join keeps and what it cuts. This asks the user's database for counts, so it runs only when the
user asks, never on each edit. One query answers everything: both sides are grouped by the join key and compared, so
the cost is one scan of each table.

    query(model, index, dialect)   -> the SQL, read-only by construction
    summary(row)                   -> {a_rows, b_rows, matched_a, matched_b, a_keys, b_keys, matched_keys, fan_out, kept}
"""

from . import querymodel


def _side(t, column, d):
    key = querymodel._quote(column, d)
    if t.get('kind') == 'sub':
        body = t['sql']
        return f'SELECT {key} AS k, COUNT(*) AS n FROM ({body}) AS s GROUP BY {key}'
    ref = querymodel._ref(t, d).rsplit(' AS ', 1)[0]
    return f'SELECT {key} AS k, COUNT(*) AS n FROM {ref} GROUP BY {key}'


def query(model, index, dialect):
    j = model['joins'][index]
    by_id = {t['id']: t for t in model['tables']}
    a, b = by_id[j['a'][0]], by_id[j['b'][0]]
    if a.get('kind') in ('remote', 'cte') or b.get('kind') in ('remote', 'cte'):
        raise ValueError('join insight needs tables or plain subqueries on both sides')
    d = querymodel._d(dialect)
    # NULL keys never match, so they count as rows on their own side
    return f"""SELECT COALESCE(SUM(a.n), 0) AS a_rows, COALESCE(SUM(b.n), 0) AS b_rows,
       COALESCE(SUM(CASE WHEN a.k IS NOT NULL AND b.k IS NOT NULL THEN a.n END), 0) AS matched_a,
       COALESCE(SUM(CASE WHEN a.k IS NOT NULL AND b.k IS NOT NULL THEN b.n END), 0) AS matched_b,
       COUNT(a.k) AS a_keys, COUNT(b.k) AS b_keys,
       SUM(CASE WHEN a.k IS NOT NULL AND b.k IS NOT NULL THEN 1 ELSE 0 END) AS matched_keys,
       COALESCE(SUM(CASE WHEN a.k IS NOT NULL AND b.k IS NOT NULL THEN a.n * b.n END), 0) AS joined
FROM ({_side(a, j['a'][1], d)}) AS a
FULL OUTER JOIN ({_side(b, j['b'][1], d)}) AS b ON a.k = b.k"""


def summary(row, join_type):
    a_rows, b_rows, matched_a, matched_b, a_keys, b_keys, matched_keys, joined = (int(x or 0) for x in row)
    lost_a, lost_b = a_rows - matched_a, b_rows - matched_b
    kept = joined + (lost_a if join_type in ('LEFT', 'FULL') else 0) + (lost_b if join_type in ('RIGHT', 'FULL') else 0)
    return {'a_rows': a_rows, 'b_rows': b_rows, 'matched_a': matched_a, 'matched_b': matched_b, 'lost_a': lost_a, 'lost_b': lost_b,
            'a_keys': a_keys, 'b_keys': b_keys, 'matched_keys': matched_keys, 'joined': joined, 'kept': kept,
            'fan_out': joined > max(matched_a, matched_b)}
