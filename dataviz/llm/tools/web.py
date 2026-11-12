"""
Web search for both agents (domain 'web', offered only when the chat's web level is above Off): `web_search` and
`fetch_url`. Every query and URL passes llm/web_guard.py first; a blocked one is never sent.

Engine order (the first that answers wins):
    1. the user's own engine (Settings › AI › Web search): brave | tavily | serper | google_cse (key from
       store.web_key, cx for Google CSE) or searxng at the user's URL
    2. the bundled SearXNG at env SEARXNG_URL (default http://searxng:8080, the compose service), JSON API
    3. DuckDuckGo HTML (html.duckduckgo.com/html: no key)
'auto' (and duckduckgo / searxng without a URL) start at the matching step.

fetch_url goes through bucket/sources/fetch.py's SSRF guard (no private / loopback / link-local / metadata
address, every redirect checked), http(s) only, at most 2 MB in 15 s, and returns readable text (≤ 8000 characters).

Each search sent queues a 'search' event on ctx.events (the engine sends it after tool_end, the trace keeps it):
{query, engine, results: [{title, url}], level}; a blocked one {blocked: true, reasons, query: redacted}. After 3
blocked queries in one turn the web is off for the rest of it.
"""

import html
import os
import re
from urllib.parse import parse_qs, urlsplit

from .. import web_guard
from . import I, S, Tool, ToolError, _obj, register

MAX_RESULTS = 8
MAX_SNIPPET = 300
MAX_PAGE = 2 << 20              # bytes read of a fetched page
MAX_PAGE_TEXT = 8000
TIMEOUT = 15.0
MAX_BLOCKED = 3
DEFAULT_SEARXNG = 'http://searxng:8080'
DDG_URL = 'https://html.duckduckgo.com/html/'
UA = 'Mozilla/5.0 (compatible; dataviz-assistant/1)'


# ---------------------------------------------------------------- plumbing
def _fetch():
    from ...bucket.sources import fetch
    return fetch


def _client():
    """The HTTP client (tests patch fetch.make_client to an httpx.MockTransport)."""
    return _fetch().make_client()


def _max_level():
    try:
        from ...auth import settings
        return int(settings.get('llm_web_max_level'))
    except Exception:  # noqa: BLE001 - no database (tests, CLI)
        return 3


def level_of(ctx):
    """The level this run may use: the chat's level, capped by the admin's maximum."""
    try:
        lv = int(getattr(ctx, 'web_level', 0) or 0)
    except (TypeError, ValueError):
        lv = 0
    return max(0, min(3, lv, _max_level()))


def _key(ctx):
    """The user's web engine API key (server side only; '' when none / no store)."""
    st = getattr(getattr(ctx, 'app', None), 'llm', None)
    try:
        return st.web_key(ctx.user) if st is not None else ''
    except Exception:  # noqa: BLE001 - no database
        return ''


def _get(method, url, allow_private=None, **kw):
    """One request through the SSRF-safe helper -> response (ToolError-free: httpx / BucketError pass to the caller)."""
    f = _fetch()
    with _client() as c:
        resp, _ = f.request(c, method, url, kw.pop('headers', None), allow_private, timeout=TIMEOUT, **kw)
    if resp.status_code >= 400:
        raise f.BucketError(f'{urlsplit(url).hostname} answered {resp.status_code}', 502)
    return resp


def _clip(s, n=MAX_SNIPPET):
    s = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', str(s or '')))).strip()
    return s if len(s) <= n else s[:n - 1] + '…'


def _rows(items, title, url, snippet, n):
    out = []
    for it in items or []:
        if not isinstance(it, dict) or not it.get(url):
            continue
        out.append({'title': _clip(it.get(title), 200), 'url': str(it[url])[:500], 'snippet': _clip(it.get(snippet))})
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------- engines: (query, n, ctx, cfg) -> [{title, url, snippet}]
def _brave(q, n, ctx, cfg):
    r = _get('GET', 'https://api.search.brave.com/res/v1/web/search', params={'q': q, 'count': n},
             headers={'X-Subscription-Token': _need_key(ctx), 'Accept': 'application/json'})
    return _rows((r.json().get('web') or {}).get('results'), 'title', 'url', 'description', n)


def _tavily(q, n, ctx, cfg):
    r = _get('POST', 'https://api.tavily.com/search', json={'api_key': _need_key(ctx), 'query': q, 'max_results': n})
    return _rows(r.json().get('results'), 'title', 'url', 'content', n)


def _serper(q, n, ctx, cfg):
    r = _get('POST', 'https://google.serper.dev/search', json={'q': q, 'num': n},
             headers={'X-API-KEY': _need_key(ctx), 'Content-Type': 'application/json'})
    return _rows(r.json().get('organic'), 'title', 'link', 'snippet', n)


def _google_cse(q, n, ctx, cfg):
    if not cfg.get('cx'):
        raise ToolError('Google CSE needs its search engine id (cx) in Settings › AI › Web search')
    r = _get('GET', 'https://www.googleapis.com/customsearch/v1',
             params={'key': _need_key(ctx), 'cx': cfg['cx'], 'q': q, 'num': min(n, 10)})
    return _rows(r.json().get('items'), 'title', 'link', 'snippet', n)


def _searxng_at(base, allow_private):
    def go(q, n, ctx, cfg):
        r = _get('GET', base.rstrip('/') + '/search', allow_private=allow_private,
                 params={'q': q, 'format': 'json', 'safesearch': 1}, headers={'Accept': 'application/json'})
        return _rows(r.json().get('results'), 'title', 'url', 'content', n)
    return go


_DDG_A = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_DDG_SNIP = re.compile(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div|td)>', re.S | re.I)


def _ddg_url(href):
    """DuckDuckGo wraps result links as //duckduckgo.com/l/?uddg=<the url>."""
    href = html.unescape(href)
    if href.startswith('//'):
        href = 'https:' + href
    p = urlsplit(href)
    if p.hostname and p.hostname.endswith('duckduckgo.com') and p.path.startswith('/l/'):
        u = parse_qs(p.query).get('uddg')
        if u:
            return u[0]
    return href


def parse_ddg(page, n=MAX_RESULTS):
    """The results of a DuckDuckGo HTML page -> [{title, url, snippet}] (ads skipped)."""
    links = list(_DDG_A.finditer(page))
    snips = [m.group(1) for m in _DDG_SNIP.finditer(page)]
    out = []
    for i, m in enumerate(links):
        url = _ddg_url(m.group(1))
        if not url.startswith(('http://', 'https://')) or 'duckduckgo.com/y.js' in url:
            continue
        out.append({'title': _clip(m.group(2), 200), 'url': url[:500], 'snippet': _clip(snips[i] if i < len(snips) else '')})
        if len(out) >= n:
            break
    return out


def _duckduckgo(q, n, ctx, cfg):
    r = _get('POST', DDG_URL, data={'q': q, 'kl': 'wt-wt'}, headers={'User-Agent': UA})
    return parse_ddg(r.text, n)


def _need_key(ctx):
    k = _key(ctx)
    if not k:
        raise ToolError('no API key saved for this web search engine')
    return k


ENGINES = {'brave': _brave, 'tavily': _tavily, 'serper': _serper, 'google_cse': _google_cse, 'duckduckgo': _duckduckgo}


def chain(cfg):
    """[(engine name, fn)] in the order they are tried for these settings."""
    eng = (cfg or {}).get('engine') or 'auto'
    out = []
    if eng in ('brave', 'tavily', 'serper', 'google_cse'):
        out.append((eng, ENGINES[eng]))
    elif eng == 'searxng' and (cfg or {}).get('url'):
        out.append(('searxng', _searxng_at(cfg['url'], None)))           # a user's URL: no private address
    if eng != 'duckduckgo':
        base = os.environ.get('SEARXNG_URL', DEFAULT_SEARXNG).strip()
        if base:
            out.append(('searxng', _searxng_at(base, True)))             # the admin's own service (compose)
    out.append(('duckduckgo', _duckduckgo))
    return out


# ---------------------------------------------------------------- the guard, per run
def _guarded(ctx, text, what='query'):
    """The level, after the guard let `text` through; ToolError (and a blocked event) otherwise."""
    lv = level_of(ctx)
    if lv <= 0:
        raise ToolError('web search is off (the chat or the admin turned it off): answer without it')
    if ctx.web_blocked >= MAX_BLOCKED:
        raise ToolError(f'web search is off for the rest of this turn ({MAX_BLOCKED} queries were blocked): '
                        'answer without it')
    ok, reasons, redacted = web_guard.check(text, ctx, lv)
    if ok:
        return lv
    ctx.web_blocked += 1
    ctx.events.append({'type': 'search', 'blocked': True, 'reasons': list(dict.fromkeys(reasons)),
                       'count': len(reasons), what: redacted, 'level': lv})
    msg = web_guard.message(reasons)
    if what == 'url':
        msg = msg.replace('query contained', 'the URL contained').replace('rephrase it', 'use a public URL')
    if ctx.web_blocked >= MAX_BLOCKED:
        msg += f'. Web search is now off for the rest of this turn ({MAX_BLOCKED} blocked): answer without it'
    raise ToolError(msg)


def _ask_before(ctx, clean):
    """Ask before each search: only for queries the guard lets through (a blocked one goes back to the model)."""
    if not (getattr(ctx, 'web_cfg', None) or {}).get('ask_before'):
        return False
    if level_of(ctx) <= 0 or ctx.web_blocked >= MAX_BLOCKED:
        return False
    return web_guard.check(clean.get('query') or clean.get('url') or '', ctx, level_of(ctx))[0]


# ---------------------------------------------------------------- tools
def _web_search(ctx, a):
    q = re.sub(r'\s+', ' ', a['query']).strip()
    if not q:
        raise ToolError('query must not be empty')
    if len(q) > 400:
        raise ToolError('the query is too long: at most 400 characters of general words')
    lv = _guarded(ctx, q)
    n = max(1, min(MAX_RESULTS, int(a.get('max_results') or MAX_RESULTS)))
    errors = []
    for name, fn in chain(getattr(ctx, 'web_cfg', None) or {}):
        try:
            rows = fn(q, n, ctx, getattr(ctx, 'web_cfg', None) or {})
        except Exception as e:  # noqa: BLE001 - the next engine is tried
            errors.append(f'{name}: {str(e)[:120]}')
            continue
        ctx.events.append({'type': 'search', 'query': q, 'engine': name, 'level': lv,
                           'results': [{'title': r['title'], 'url': r['url']} for r in rows]})
        res = {'query': q, 'engine': name, 'results': rows}
        if not rows:
            res['note'] = 'no results: try other general words'
        return res, f'→ {len(rows)} result{"s" if len(rows) != 1 else ""} ({name})'
    raise ToolError('web search failed on every engine (' + '; '.join(errors) + '): answer without it')


_DROP = re.compile(r'<(script|style|noscript|svg|template|iframe|head)\b.*?</\1\s*>', re.S | re.I)
_BLOCK = re.compile(r'</?(?:p|div|br|li|ul|ol|h[1-6]|tr|table|section|article|header|footer|pre|blockquote)\b[^>]*>',
                    re.I)


def readable(page):
    """(title, text) of an HTML page: scripts / styles dropped, tags stripped, blank runs collapsed."""
    m = re.search(r'<title[^>]*>(.*?)</title>', page, re.S | re.I)
    title = _clip(m.group(1), 200) if m else ''
    body = re.sub(r'<!--.*?-->', ' ', page, flags=re.S)
    body = _DROP.sub(' ', body)
    body = _BLOCK.sub('\n', body)
    body = html.unescape(re.sub(r'<[^>]+>', ' ', body))
    lines = [re.sub(r'[ \t\r\f\v]+', ' ', x).strip() for x in body.split('\n')]
    text = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()
    return title, text


def _fetch_url(ctx, a):
    url = a['url'].strip()
    if not url.lower().startswith(('http://', 'https://')):
        raise ToolError('only http(s) URLs can be fetched')
    _guarded(ctx, url, 'url')
    f = _fetch()
    try:
        with _client() as c:
            resp, final = f.request(c, 'GET', url, {'User-Agent': UA, 'Accept': 'text/html,text/plain,*/*;q=0.5'},
                                    None, stream=True, timeout=TIMEOUT)
            try:
                if resp.status_code >= 400:
                    raise ToolError(f'{urlsplit(final).hostname} answered {resp.status_code}')
                ctype = resp.headers.get('content-type', '').split(';')[0].strip().lower()
                if ctype and not (ctype.startswith('text/') or ctype in ('application/json', 'application/xhtml+xml',
                                                                          'application/xml')):
                    raise ToolError(f'not a text page ({ctype}): only web pages can be read')
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if len(buf) >= MAX_PAGE:
                        break
            finally:
                resp.close()
    except f.BucketError as e:
        raise ToolError(str(e)) from None
    raw = bytes(buf[:MAX_PAGE]).decode(resp.encoding or 'utf-8', errors='replace')
    title, text = readable(raw) if 'html' in ctype or '<html' in raw[:2000].lower() else ('', raw.strip())
    cut = len(text) > MAX_PAGE_TEXT
    out = {'url': final, 'title': title, 'text': text[:MAX_PAGE_TEXT] + ('… (cut)' if cut else '')}
    return out, f'→ {title or urlsplit(final).hostname} ({len(text)} chars{", cut" if cut else ""})'


register(Tool('web_search', 'Search the public web. Use general words only: never bucket, folder, file, dataset or '
              'people names (the server blocks them). Returns [{title, url, snippet}].',
              _obj({'query': S('the search query, in general words'),
                    'max_results': I('how many results (default 8)', 1, MAX_RESULTS)}, ['query']),
              False, _web_search, domain='web', ctx=True, confirm_if=_ask_before,
              preview=lambda b, a: f'Search the web: {a.get("query", "")}'))
register(Tool('fetch_url', 'Read a public web page (from web_search results) as text, at most 8000 characters.',
              _obj({'url': S('the http(s) URL')}, ['url']),
              False, _fetch_url, domain='web', ctx=True,
              preview=lambda b, a: f'Read {a.get("url", "")}'))
