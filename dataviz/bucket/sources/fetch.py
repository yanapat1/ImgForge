"""
Fast, careful HTTP downloads for the web dataset sources (Kaggle, Hugging Face, Roboflow, URL, Zenodo, OpenML).

    make_client()            the one httpx.Client factory: tests monkeypatch it to an httpx.MockTransport client
    resolve_host(host)       DNS lookup used by the SSRF guard (tests monkeypatch it too: no real DNS in tests)
    check_url(url, ...)      refuse private / loopback / link-local / reserved targets (after DNS), unless the config
                             says sources.allow_private_urls
    request(...)             one request with redirects followed BY HAND: every hop goes through check_url, and the
                             Authorization header is dropped when a redirect leaves the original host (a Kaggle
                             download redirects to a signed storage URL that must not get the Kaggle key)
    download(url, dest_dir)  -> {path, bytes, filename}: streamed to <name>.part, resumed with Range on retry
                             (3 retries, backoff), split over 8 ranged connections (os.pwrite into one preallocated
                             file) when the server accepts ranges and the file is over 64 MB; progress as bytes with
                             rate / ETA; a free-disk check against Content-Length x 2.2 (archive + its contents, 507);
                             cancel removes the partial file.
"""

import email.message
import ipaddress
import os
import re
import shutil
import socket
import threading
import time
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from ..store import Cancelled, BucketError  # noqa: F401 - Cancelled is re-exported (fetch.Cancelled)

UA = 'dataviz-datacenter/1 (+httpx)'
CHUNK = 1 << 20
PARALLEL_MIN = 64 << 20             # files above this are split over PARTS ranged connections
PARTS = 8
RETRIES = 3
RETRY_SLEEP = 0.5                   # first backoff (s), doubled per retry; tests set 0
MAX_REDIRECTS = 10
DISK_FACTOR = 2.2                   # an archive and what it unpacks to




def make_client(**kw):
    """The httpx client every source uses (patchable: tests return a client on an httpx.MockTransport)."""
    return httpx.Client(follow_redirects=False, headers={'User-Agent': UA},
                        timeout=httpx.Timeout(30.0, read=120.0), **kw)


def resolve_host(host):
    """Every IP `host` resolves to (patchable in tests)."""
    return list({ai[4][0] for ai in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})


def private_allowed():
    """config sources.allow_private_urls (the server's config through the Data Center service)."""
    try:
        from .. import service
        return bool(((service._cfg() or {}).get('sources') or {}).get('allow_private_urls'))
    except Exception:  # noqa: BLE001 - no config: the safe answer
        return False


def _bad_ip(ip):
    a = ipaddress.ip_address(ip.split('%', 1)[0])
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    return a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified


def check_url(url, allow_private=None):
    """BucketError 400 unless `url` is http(s) and (allow_private, or) every address of its host is public."""
    parts = urlsplit(str(url))
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise BucketError(f'only http(s) links can be downloaded, not {str(url)[:80]!r}')
    if allow_private is None:
        allow_private = private_allowed()
    if allow_private:
        return
    host = parts.hostname
    try:
        ips = [host] if _is_ip(host) else resolve_host(host)
    except OSError as e:
        raise BucketError(f'cannot resolve {host}: {e}', 502) from None
    if not ips or any(_bad_ip(ip) for ip in ips):
        raise BucketError(f'{host} is a private, loopback or link-local address: refused (the admin can allow it with '
                      'config sources.allow_private_urls)')


def _is_ip(h):
    try:
        ipaddress.ip_address(h.strip('[]'))
        return True
    except ValueError:
        return False


def _hdrs(headers, url, origin_host):
    """`headers` minus Authorization once the request left the host it was meant for."""
    h = dict(headers or {})
    if origin_host and urlsplit(url).hostname != origin_host:
        h = {k: v for k, v in h.items() if k.lower() != 'authorization'}
    return h


def request(client, method, url, headers=None, allow_private=None, stream=False, **kw):
    """One request, redirects followed by hand (SSRF check + auth dropped across hosts) -> (response, final url).
    With stream=True the caller closes the response."""
    origin = urlsplit(url).hostname
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url, allow_private)
        req = client.build_request(method, url, headers=_hdrs(headers, url, origin), **kw)
        resp = client.send(req, stream=True)
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get('location'):
            resp.close()
            url = urljoin(str(resp.url), resp.headers['location'])
            if resp.status_code == 303:
                method = 'GET'
            continue
        if not stream:
            try:
                resp.read()
            finally:
                resp.close()
        return resp, url
    raise BucketError(f'too many redirects from {urlsplit(url).hostname}', 502)


def get_json(client, url, headers=None, allow_private=None, errors=None, params=None):
    """GET -> parsed JSON; an HTTP error is BucketError with errors[status] (or a generic message) as its text."""
    try:
        resp, _ = request(client, 'GET', url, headers, allow_private, params=params)
    except httpx.HTTPError as e:
        raise BucketError(f'{urlsplit(url).hostname}: {e}', 502) from None
    if resp.status_code >= 400:
        msg = (errors or {}).get(resp.status_code)
        raise BucketError(msg or f'{urlsplit(url).hostname} answered {resp.status_code}: {_brief(resp)}',
                      resp.status_code if resp.status_code in (401, 403, 404) else 502)
    try:
        return resp.json()
    except ValueError:
        raise BucketError(f'{urlsplit(url).hostname} did not answer JSON', 502) from None


def _brief(resp):
    try:
        j = resp.json()
        if isinstance(j, dict):
            for k in ('message', 'error', 'detail', 'title'):
                if j.get(k):
                    return str(j[k])[:200]
    except Exception:  # noqa: BLE001 - body is not JSON
        pass
    return (resp.text or '')[:200].strip() or resp.reason_phrase


# ---------------------------------------------------------------- file names
_SAFE = re.compile(r'[^A-Za-z0-9._ ()+-]+')


def safe_name(name, fallback='download'):
    name = os.path.basename(unquote(str(name or '')).replace('\\', '/')).strip().lstrip('.')
    name = _SAFE.sub('_', name)[:200].strip()
    return name or fallback


def filename_of(resp, url, fallback='download'):
    cd = resp.headers.get('content-disposition') or ''
    if cd:
        m = email.message.Message()
        m['content-disposition'] = cd
        fn = m.get_filename()
        if fn:
            return safe_name(fn, fallback)
    return safe_name(urlsplit(str(url)).path.rsplit('/', 1)[-1], fallback)


# ---------------------------------------------------------------- progress
class Meter:
    """Bytes done -> progress(done, total, stage, extra='12.3 MB/s · ETA 41 s'), at most 4 calls a second.
    `base` / `grand` let several files report as one download."""

    def __init__(self, progress, total, stage='downloading', base=0, grand=None):
        self.progress = progress or (lambda *a, **k: None)
        self.total = total
        self.grand = grand
        self.base = base
        self.stage = stage
        self.done = 0
        self.t0 = time.time()
        self.last = 0.0
        self.lock = threading.Lock()

    def add(self, n, force=False):
        with self.lock:
            self.done += n
            now = time.time()
            if not force and now - self.last < 0.25:
                return
            self.last = now
            done = self.done
        rate = done / max(1e-3, now - self.t0)
        total = self.grand if self.grand is not None else self.total
        extra = f'{fmt_bytes(rate)}/s'
        if self.total and rate > 0:
            extra += f' · ETA {fmt_secs(max(0, self.total - done) / rate)}'
        self.progress(self.base + done, total or None, self.stage, extra=extra)


def fmt_bytes(n):
    n = float(n or 0)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1000:
            return f'{n:.0f} {u}' if u == 'B' else f'{n:.1f} {u}'
        n /= 1000
    return f'{n:.1f} TB'


def fmt_secs(s):
    s = int(s)
    return f'{s} s' if s < 90 else f'{s // 60} min' if s < 5400 else f'{s // 3600} h {s % 3600 // 60} min'


def check_disk(folder, need, factor=DISK_FACTOR):
    if not need:
        return
    free = shutil.disk_usage(folder).free
    if need * factor > free:
        raise BucketError(f'this download is {need / 1e9:.1f} GB (about {need * factor / 1e9:.1f} GB with its unpacked '
                      f'files) but only {free / 1e9:.1f} GB are free', 507)


# ---------------------------------------------------------------- download
def _cancel_check(cancelled):
    if cancelled is not None and cancelled():
        raise Cancelled()


def _retrying(fn, cancelled):
    """fn() with RETRIES retries on network errors / 5xx (BucketError 502/503/504 from inside)."""
    delay = RETRY_SLEEP
    for attempt in range(RETRIES + 1):
        _cancel_check(cancelled)
        try:
            return fn()
        except (httpx.TransportError, _Retry) as e:
            if attempt == RETRIES:
                raise BucketError(f'download failed after {RETRIES + 1} tries: {e}', 502) from None
            time.sleep(delay)
            delay *= 2


class _Retry(Exception):
    pass


def download(url, dest_dir, headers=None, progress=None, cancelled=None, client=None, allow_private=None,
             filename=None, size_hint=None, meter=None, disk_factor=DISK_FACTOR, parallel=True):
    """Download `url` into folder dest_dir -> {path, bytes, filename}. See the module docstring."""
    own = client is None
    client = client or make_client()
    os.makedirs(dest_dir, exist_ok=True)
    part = None
    try:
        resp, final = request(client, 'GET', url, headers, allow_private, stream=True)
        try:
            if resp.status_code >= 400:
                resp.read()
                raise BucketError(f'{urlsplit(final).hostname} answered {resp.status_code}: {_brief(resp)}',
                              resp.status_code if resp.status_code in (401, 403, 404) else 502)
            name = safe_name(filename) if filename else filename_of(resp, final)
            total = int(resp.headers.get('content-length') or 0) or int(size_hint or 0) or None
            ranges = resp.headers.get('accept-ranges', '').lower() == 'bytes'
            check_disk(dest_dir, total, disk_factor)
            path = os.path.join(dest_dir, name)
            part = path + '.part'
            m = meter or Meter(progress, total)
            if parallel and ranges and total and total > PARALLEL_MIN:
                resp.close()
                _parallel(client, final, headers, part, total, m, cancelled, allow_private)
            else:
                _single(client, final, headers, part, resp, total, ranges, m, cancelled, allow_private)
        finally:
            resp.close()
        os.replace(part, path)
        part = None
        m.add(0, force=True)
        return {'path': path, 'bytes': os.path.getsize(path), 'filename': name}
    except BaseException:
        if part is not None:
            try:
                os.remove(part)
            except OSError:
                pass
        raise
    finally:
        if own:
            client.close()


def _single(client, url, headers, part, first, total, ranges, m, cancelled, allow_private):
    """One stream into `part`; on a broken connection resume from its size (Range) when the server allows."""
    state = {'resp': first}

    def go():
        resp = state.pop('resp', None)
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if resp is None:
            h = dict(headers or {})
            if have and ranges:
                h['Range'] = f'bytes={have}-'
            resp, _ = request(client, 'GET', url, h, allow_private, stream=True)
        try:
            if resp.status_code >= 500:
                raise _Retry(f'HTTP {resp.status_code}')
            if resp.status_code >= 400:
                resp.read()
                raise BucketError(f'{urlsplit(url).hostname} answered {resp.status_code}: {_brief(resp)}', 502)
            mode = 'ab' if resp.status_code == 206 and have else 'wb'
            if mode == 'wb' and have:
                m.add(-have)
            with open(part, mode) as f:
                for chunk in resp.iter_bytes():
                    _cancel_check(cancelled)
                    f.write(chunk)
                    m.add(len(chunk))
        finally:
            resp.close()
        if total and os.path.getsize(part) < total:
            raise _Retry(f'connection closed at {os.path.getsize(part)} of {total} bytes')

    _retrying(go, cancelled)


def _parallel(client, url, headers, part, total, m, cancelled, allow_private):
    """PARTS ranged GETs written in place with os.pwrite; each range resumes from where it stopped."""
    from concurrent.futures import ThreadPoolExecutor
    with open(part, 'wb') as f:
        f.truncate(total)
    step = -(-total // PARTS)
    spans = [(s, min(total, s + step) - 1) for s in range(0, total, step)]
    fd = os.open(part, os.O_WRONLY)
    stop = threading.Event()

    def one(span):
        start, end = span
        pos = [start]

        def go():
            if stop.is_set():
                raise Cancelled()
            h = {**(headers or {}), 'Range': f'bytes={pos[0]}-{end}'}
            resp, _ = request(client, 'GET', url, h, allow_private, stream=True)
            try:
                if resp.status_code >= 500:
                    raise _Retry(f'HTTP {resp.status_code}')
                if resp.status_code != 206:
                    resp.read()
                    raise BucketError(f'{urlsplit(url).hostname} ignored a byte range ({resp.status_code})', 502)
                for chunk in resp.iter_bytes():
                    if stop.is_set():
                        raise Cancelled()
                    _cancel_check(cancelled)
                    os.pwrite(fd, chunk, pos[0])
                    pos[0] += len(chunk)
                    m.add(len(chunk))
            finally:
                resp.close()
            if pos[0] <= end:
                raise _Retry(f'range {start}-{end} stopped at {pos[0]}')

        try:
            _retrying(go, cancelled)
        except BaseException:
            stop.set()
            raise

    try:
        with ThreadPoolExecutor(PARTS) as ex:
            for fut in [ex.submit(one, s) for s in spans]:
                fut.result()
    finally:
        os.close(fd)
