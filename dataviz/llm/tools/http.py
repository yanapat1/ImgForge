"""
A user plugin's HTTP tools (llm/assets.py installs the manifests): declarative calls, no code runs. Nothing here is
registered in the global REGISTRY: `build(plugin, owner, secrets)` makes the Tool objects of one installed plugin,
and llm/assets.py `for_run` hands them to the engine for the owner's runs only.

A manifest tool:

    {name, description, parameters (JSON schema object), risk read | change | danger, roles [data, visual],
     http: {method GET | POST | PUT | PATCH | DELETE, url, query {k: template}, headers {k: template},
            body (JSON template)}}

Templates: `{{arg}}` is a validated argument (URL-encoded in the URL path; a string that is exactly "{{arg}}" in the
body / query keeps the argument's JSON type, and is dropped when the optional argument is missing); `${secret:NAME}`
is one of the plugin's secrets (headers, query and body only, never the URL itself). The scheme and host of the URL
are fixed by the manifest (no placeholder before the path) and https only, http for admins.

A call goes through datacenter/sources/fetch.py (`request`: redirects followed by hand, every hop SSRF-checked;
private / loopback hosts are refused unless the plugin's owner is an admin), 15 s timeout, at most 1 MB read, and
returns compact JSON or text of at most 6000 characters. change / danger tools go through the engine's confirm.
"""

import json
import re
from urllib.parse import quote, urlsplit

import httpx

from . import Tool, ToolError

TIMEOUT = 15.0
MAX_BYTES = 1 << 20
MAX_OUT = 6000
METHODS = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')
RISKS = ('read', 'change', 'danger')
ROLES = ('data', 'visual')
UA = 'dataviz-plugin/1 (+httpx)'
_ARG = re.compile(r'\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}')
_SECRET = re.compile(r'\$\{secret:([A-Za-z_][A-Za-z0-9_]*)\}')
_HDR = re.compile(r'^[A-Za-z0-9-]{1,80}$')


class PluginError(ValueError):
    """A manifest that cannot be installed (its message goes back to the page as a 400)."""


def _client():
    """The httpx client of one call (patchable: tests swap in a MockTransport client)."""
    return httpx.Client(follow_redirects=False, headers={'User-Agent': UA}, timeout=httpx.Timeout(TIMEOUT))


# ---------------------------------------------------------------- manifest validation
def _walk_strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk_strings(v)


def clean_http(h, params, secrets, admin=False, where='tool'):
    """The `http` block of one manifest tool, checked -> clean dict (PluginError otherwise)."""
    if not isinstance(h, dict):
        raise PluginError(f'{where}: http must be an object')
    method = str(h.get('method') or 'GET').upper()
    if method not in METHODS:
        raise PluginError(f'{where}: http.method must be one of {", ".join(METHODS)}')
    url = str(h.get('url') or '').strip()
    parts = urlsplit(url)
    if parts.scheme not in ('https', 'http') or not parts.hostname:
        raise PluginError(f'{where}: http.url must be an absolute https:// URL')
    if parts.scheme == 'http' and not admin:
        raise PluginError(f'{where}: http.url must use https (plain http is for administrators only)')
    if '{{' in parts.netloc or '${' in parts.netloc or '{{' in parts.scheme or parts.username or parts.password:
        raise PluginError(f'{where}: the host of http.url is fixed: no placeholder or credentials before the path')
    if _SECRET.search(url):
        raise PluginError(f'{where}: secrets go in headers, query or body, not in the URL')
    if len(url) > 2000:
        raise PluginError(f'{where}: http.url is too long')
    query = h.get('query') or {}
    headers = h.get('headers') or {}
    if not isinstance(query, dict) or not all(isinstance(v, (str, int, float, bool)) for v in query.values()):
        raise PluginError(f'{where}: http.query must be an object of strings / numbers')
    if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
        raise PluginError(f'{where}: http.headers must be an object of strings')
    for k in headers:
        if not _HDR.match(str(k)) or str(k).lower() in ('host', 'content-length', 'transfer-encoding', 'connection'):
            raise PluginError(f'{where}: header {str(k)[:40]!r} is not allowed')
    body = h.get('body')
    if body is not None and method == 'GET':
        raise PluginError(f'{where}: a GET request has no body')
    if body is not None and len(json.dumps(body)) > 20000:
        raise PluginError(f'{where}: http.body is too long')
    known = set((params.get('properties') or {}).keys())
    for s in _walk_strings([url, query, headers, body]):
        for a in _ARG.findall(s):
            if a not in known:
                raise PluginError(f'{where}: placeholder {{{{{a}}}}} is not one of its parameters')
        for n in _SECRET.findall(s):
            if n not in secrets:
                raise PluginError(f'{where}: ${{secret:{n}}} is not declared in the manifest secrets')
    out = {'method': method, 'url': url}
    if query:
        out['query'] = {str(k): v for k, v in query.items()}
    if headers:
        out['headers'] = {str(k): v for k, v in headers.items()}
    if body is not None:
        out['body'] = body
    return out


_TYPES = ('string', 'integer', 'number', 'boolean', 'array', 'object')


def clean_params(p, where='tool'):
    """A tool's `parameters`: a JSON schema object (properties of known types), unknown keys refused at call time."""
    p = p if p is not None else {'type': 'object', 'properties': {}}
    if not isinstance(p, dict) or p.get('type', 'object') != 'object':
        raise PluginError(f'{where}: parameters must be a JSON schema object ({{"type": "object", "properties": ...}})')
    props = p.get('properties') or {}
    if not isinstance(props, dict) or len(props) > 30:
        raise PluginError(f'{where}: parameters.properties must be an object of at most 30 parameters')
    clean = {}
    for k, v in props.items():
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$', str(k)) or not isinstance(v, dict):
            raise PluginError(f'{where}: bad parameter {str(k)[:40]!r}')
        if v.get('type') not in _TYPES:
            raise PluginError(f'{where}: parameter {k!r} needs a type ({", ".join(_TYPES)})')
        clean[k] = {kk: vv for kk, vv in v.items() if kk in ('type', 'description', 'enum', 'minimum', 'maximum', 'items', 'default')}
    req = p.get('required') or []
    if not isinstance(req, list) or any(r not in clean for r in req):
        raise PluginError(f'{where}: parameters.required must list parameters it declares')
    return {'type': 'object', 'properties': clean, 'required': list(req), 'additionalProperties': False}


# ---------------------------------------------------------------- rendering
def _sub(s, args, secrets, url=False):
    """One template string: {{arg}} -> the argument (URL-quoted in a URL), ${secret:NAME} -> the secret."""
    def arg(m):
        v = args.get(m.group(1))
        v = '' if v is None else v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        return quote(v, safe='') if url else v

    def sec(m):
        n = m.group(1)
        v = secrets().get(n) if callable(secrets) else (secrets or {}).get(n)
        if not v:
            raise ToolError(f'the plugin secret {n} is not set: Settings › Prompts & Skills › Plugins')
        return v
    return _SECRET.sub(sec, _ARG.sub(arg, s))


_MISSING = object()


def _whole(s):
    m = _ARG.fullmatch(s.strip()) if isinstance(s, str) else None
    return m.group(1) if m else None


def _tpl(x, args, secrets):
    """A JSON template: "{{arg}}" alone keeps the argument's type (missing -> dropped), other strings are filled in."""
    if isinstance(x, str):
        a = _whole(x)
        if a is not None:
            return args[a] if a in args else _MISSING
        return _sub(x, args, secrets)
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            r = _tpl(v, args, secrets)
            if r is not _MISSING:
                out[k] = r
        return out
    if isinstance(x, list):
        return [r for r in (_tpl(v, args, secrets) for v in x) if r is not _MISSING]
    return x


def render(http, args, secrets=None):
    """(method, url, params, headers, json body | None) for one call with validated `args`."""
    url = _sub(http['url'], args, secrets, url=True)
    params = {}
    for k, v in (http.get('query') or {}).items():
        r = _tpl(v, args, secrets) if isinstance(v, str) else v
        if r is _MISSING or r is None:
            continue
        params[k] = r if isinstance(r, str) else json.dumps(r) if isinstance(r, (dict, list)) else str(r).lower() \
            if isinstance(r, bool) else str(r)
    headers = {k: _sub(v, args, secrets).replace('\r', ' ').replace('\n', ' ') for k, v in (http.get('headers') or {}).items()}
    body = _tpl(http['body'], args, secrets) if 'body' in http else None
    if body is _MISSING:
        body = None
    if urlsplit(url).netloc != urlsplit(http['url']).netloc:      # the host is the manifest's, whatever the args
        raise ToolError('the request URL left the plugin host')
    return http['method'], url, params, headers, body


def _redact(text, secrets):
    try:
        vals = [v for v in (secrets() if callable(secrets) else secrets or {}).values() if v and len(v) >= 4]
    except Exception:  # noqa: BLE001 - nothing to redact then
        vals = []
    for v in vals:
        text = text.replace(v, '••••')
    return text


def _brief(raw, ctype):
    text = raw.decode('utf-8', 'replace')
    if 'json' in ctype or text[:1] in '{[':
        try:
            data = json.loads(text)
            s = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
            return {'json': data} if len(s) <= MAX_OUT else {'text': s[:MAX_OUT] + '… (cut)'}
        except ValueError:
            pass
    text = ' '.join(text.split()) if 'html' in ctype else text
    return {'text': text if len(text) <= MAX_OUT else text[:MAX_OUT] + '… (cut)'}


def call(http, args, secrets=None, admin=False):
    """Run one HTTP tool -> (result, summary). ToolError for anything the model or user can act on."""
    from ...bucket.sources import fetch
    from ...bucket.store import BucketError
    method, url, params, headers, body = render(http, args, secrets)
    if urlsplit(url).scheme != 'https' and not admin:
        raise ToolError('plugin calls must use https')
    host = urlsplit(url).hostname
    kw = {'params': params or None}
    if body is not None:
        kw['json'] = body
    client = _client()
    try:
        resp, _ = fetch.request(client, method, url, headers, allow_private=bool(admin), stream=True, **kw)
        try:
            raw = bytearray()
            for chunk in resp.iter_bytes():
                raw += chunk
                if len(raw) > MAX_BYTES:
                    raw = raw[:MAX_BYTES]
                    break
            status, ctype = resp.status_code, resp.headers.get('content-type', '').lower()
        finally:
            resp.close()
    except BucketError as e:
        raise ToolError(_redact(str(e), secrets)) from None
    except httpx.TimeoutException:
        raise ToolError(f'{host} did not answer within {TIMEOUT:.0f} s') from None
    except httpx.HTTPError as e:
        raise ToolError(_redact(f'{host}: {e}', secrets)) from None
    finally:
        client.close()
    out = _brief(bytes(raw), ctype)
    if status >= 400:
        detail = json.dumps(out.get('json'), ensure_ascii=False)[:300] if 'json' in out else out.get('text', '')[:300]
        raise ToolError(_redact(f'{host} answered {status}: {detail}', secrets))
    return {'status': status, **out}, f'→ HTTP {status} · {len(raw) / 1000:.1f} kB'


# ---------------------------------------------------------------- Tool objects
def build(plugin, owner, secrets=None):
    """The Tool objects of one installed plugin ({id, name, manifest}) for its owner; `secrets` () -> {NAME: value}
    is only called when a tool runs. Each Tool has `roles` (the agents it is offered to)."""
    admin = bool((owner or {}).get('is_admin'))
    out = []
    for spec in (plugin.get('manifest') or {}).get('tools') or []:
        http = spec['http']

        def run(ctx, args, _h=http):
            return call(_h, args, secrets, admin)

        def preview(_b, args, _h=http, _n=spec['name']):
            u = urlsplit(_h['url'])
            a = json.dumps(args, ensure_ascii=False)
            return f'{_n}: {_h["method"]} {u.hostname}{u.path} {a if len(a) <= 200 else a[:200] + "…"}'

        risk = spec.get('risk') if spec.get('risk') in RISKS else 'read'
        t = Tool(name=spec['name'], description=f'[plugin {plugin.get("name")}] {spec.get("description") or ""}'[:1200],
                 schema=spec['parameters'], mutating=risk != 'read', run=run, preview=preview, domain='plugin',
                 risk=risk, ctx=True)
        t.roles = tuple(r for r in spec.get('roles') or ROLES if r in ROLES) or ROLES
        t.plugin = plugin.get('name')
        out.append(t)
    return out
