"""
Session names and tags, shared by the Visualize sessions (data/connections.py) and the Label sessions
(labeling/projects.py). Names are unique within each list, compared case-insensitively; tags are short
free-text labels the user types, at most MAX_TAGS per session.
"""

MAX_TAGS = 20
MAX_TAG_LEN = 32


def clean_tags(tags):
    """The stored form of a tag list: trimmed, inner whitespace collapsed, empty ones dropped, de-duplicated
    case-insensitively (first spelling and input order kept). ValueError with a human message when invalid."""
    if tags is None:
        return []
    if not isinstance(tags, (list, tuple)):
        raise ValueError('tags must be a list of strings')
    out, seen = [], set()
    for t in tags:
        if not isinstance(t, str):
            raise ValueError('tags must be a list of strings')
        t = ' '.join(t.split())
        if not t:
            continue
        if len(t) > MAX_TAG_LEN:
            raise ValueError(f'a tag can be at most {MAX_TAG_LEN} characters: “{t[:MAX_TAG_LEN]}…”')
        if t.casefold() in seen:
            continue
        seen.add(t.casefold())
        out.append(t)
    if len(out) > MAX_TAGS:
        raise ValueError(f'a session can have at most {MAX_TAGS} tags')
    return out


def name_taken(name, others):
    """True when `name` equals one of `others` ignoring case."""
    key = (name or '').strip().casefold()
    return any((o or '').strip().casefold() == key for o in others)


def unique_name(base, others, nth=lambda base, n: f'{base} ({n})'):
    """`base`, else `base (2)`, `base (3)` … (`nth(base, n)` names the n-th try): the first one not in `others` (for
    auto-generated names)."""
    taken = {(o or '').strip().casefold() for o in others}
    name, n = base, 2
    while name.casefold() in taken:
        name, n = nth(base, n), n + 1
    return name
