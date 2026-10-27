"""
Fixtures for the I/O + database tests (imported by test_io.py / test_db.py; tests/conftest.py belongs to
the backend-core tests): in-process S3 (moto server), FTP (pyftpdlib) and SFTP (paramiko) servers over
temp folders, test images, and docker-run PostgreSQL / MSSQL containers (skipped with a reason when docker
or the image is unavailable).
"""

import io
import os
import time
import shutil
import socket
import threading
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

N_IMAGES = 50


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def jpeg_bytes(w=64, h=48, color=(200, 30, 30), orientation=None):
    im = Image.new('RGB', (w, h), color)
    buf = io.BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        im.save(buf, 'JPEG', quality=85, exif=exif.tobytes())
    else:
        im.save(buf, 'JPEG', quality=85)
    return buf.getvalue()


def make_image_tree(root, n=N_IMAGES):
    """n images (every 5th in sub/), img_001 rotated by EXIF 6, img_002 a PNG, plus files to ignore."""
    root = Path(root)
    names = []
    for i in range(n):
        rel = f'sub/img_{i:03d}.jpg' if i % 5 == 0 else f'img_{i:03d}.jpg'
        if i == 1:
            data = jpeg_bytes(80, 40, orientation=6)       # stored 80x40, shown 40x80
        elif i == 2:
            rel = 'img_002.png'
            buf = io.BytesIO()
            Image.new('RGBA', (30, 20), (0, 0, 255, 128)).save(buf, 'PNG')
            data = buf.getvalue()
        else:
            data = jpeg_bytes(64, 48, (i * 5 % 255, 100, 50))
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        names.append(rel)
    (root / 'notes.txt').write_text('not an image')
    (root / '.hidden').mkdir(exist_ok=True)
    (root / '.hidden' / 'skip.jpg').write_bytes(jpeg_bytes())
    return sorted(names)


@pytest.fixture
def image_tree(tmp_path):
    root = tmp_path / 'imgs'
    return root, make_image_tree(root)


# ---------------------------------------------------------------- S3 (moto server mode)

@pytest.fixture(scope='session')
def s3_server():
    from moto.server import ThreadedMotoServer
    port = free_port()
    server = ThreadedMotoServer(ip_address='127.0.0.1', port=port, verbose=False)
    server.start()
    conn = {'id': 's3test', 'name': 'moto', 'endpoint': f'http://127.0.0.1:{port}', 'region': 'us-east-1',
            'access_key': 'testing', 'secret_key': 'testing', 'verify_ssl': False}
    yield conn
    server.stop()


@pytest.fixture
def s3_bucket(s3_server, request):
    from dataviz.data import sources
    client = sources.make_client(s3_server)
    name = 'b-' + ''.join(c if c.isalnum() else '-' for c in request.node.name.lower())[:50].strip('-')
    client.create_bucket(Bucket=name)
    return client, name


# ---------------------------------------------------------------- FTP (pyftpdlib)

@pytest.fixture
def ftp_server(tmp_path):
    """{conn, root}: an FTP server on a temp folder (user / pw), MLSD supported."""
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer
    root = tmp_path / 'ftproot'
    root.mkdir()
    auth = DummyAuthorizer()
    auth.add_user('user', 'pw', str(root), perm='elradfmwMT')
    handler = type('H', (FTPHandler,), {'authorizer': auth, 'banner': 'test', 'use_encoding': 'utf8'})
    server = FTPServer(('127.0.0.1', 0), handler)
    port = server.address[1]
    t = threading.Thread(target=server.serve_forever, kwargs={'timeout': 0.2, 'handle_exit': False}, daemon=True)
    t.start()
    conn = {'id': 'ftptest', 'kind': 'ftp', 'name': 'ftp', 'host': '127.0.0.1', 'port': port, 'username': 'user',
            'secret_key': 'pw', 'tls': False, 'passive': True, 'base_path': '/'}
    yield {'conn': conn, 'root': root}
    server.close_all()
    t.join(5)


# ---------------------------------------------------------------- SFTP (in-process paramiko server)

def _sftp_server_classes():
    import paramiko
    from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface, ServerInterface

    class Handle(SFTPHandle):
        def stat(self):
            try:
                return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)

        def chattr(self, attr):
            return paramiko.SFTP_OK

    class FS(SFTPServerInterface):
        ROOT = None

        def _real(self, path):
            return os.path.join(self.ROOT, self.canonicalize(path).lstrip('/'))

        def list_folder(self, path):
            path = self._real(path)
            try:
                out = []
                for name in os.listdir(path):
                    a = SFTPAttributes.from_stat(os.stat(os.path.join(path, name)))
                    a.filename = name
                    out.append(a)
                return out
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)

        def stat(self, path):
            try:
                return SFTPAttributes.from_stat(os.stat(self._real(path)))
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)

        lstat = stat

        def open(self, path, flags, attr):
            path = self._real(path)
            try:
                fd = os.open(path, flags | getattr(os, 'O_BINARY', 0), 0o644)
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)
            if flags & os.O_WRONLY:
                mode = 'ab' if flags & os.O_APPEND else 'wb'
            elif flags & os.O_RDWR:
                mode = 'a+b' if flags & os.O_APPEND else 'r+b'
            else:
                mode = 'rb'
            f = os.fdopen(fd, mode)
            h = Handle(flags)
            h.filename = path
            h.readfile = h.writefile = f
            return h

        def remove(self, path):
            try:
                os.remove(self._real(path))
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def rename(self, old, new):
            try:
                os.rename(self._real(old), self._real(new))
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def mkdir(self, path, attr):
            try:
                os.mkdir(self._real(path))
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def rmdir(self, path):
            try:
                os.rmdir(self._real(path))
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def chattr(self, path, attr):
            return paramiko.SFTP_OK

    class Auth(ServerInterface):
        PUBKEY = None

        def check_auth_password(self, username, password):
            return paramiko.AUTH_SUCCESSFUL if (username, password) == ('user', 'pw') else paramiko.AUTH_FAILED

        def check_auth_publickey(self, username, key):
            ok = username == 'user' and self.PUBKEY is not None and key.get_base64() == self.PUBKEY.get_base64()
            return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

        def get_allowed_auths(self, username):
            return 'password,publickey'

        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    return FS, Auth


@pytest.fixture(scope='session')
def ssh_keys():
    import paramiko
    return paramiko.RSAKey.generate(2048), paramiko.RSAKey.generate(2048)      # host key, client key


@pytest.fixture
def sftp_server(tmp_path, ssh_keys):
    """{conn, root, client_key_pem}: a paramiko SFTP server on a temp folder (user / pw or the client key)."""
    import paramiko
    host_key, client_key = ssh_keys
    FS, Auth = _sftp_server_classes()
    root = tmp_path / 'sftproot'
    root.mkdir()
    fs_cls = type('FSRoot', (FS,), {'ROOT': str(root)})
    auth_cls = type('AuthKey', (Auth,), {'PUBKEY': client_key})
    lsock = socket.socket()
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(('127.0.0.1', 0))
    lsock.listen(32)
    lsock.settimeout(0.2)
    stop = threading.Event()
    transports = []

    def serve():
        while not stop.is_set():
            try:
                sock, _ = lsock.accept()
            except (socket.timeout, OSError):
                continue
            t = paramiko.Transport(sock)
            t.add_server_key(host_key)
            t.set_subsystem_handler('sftp', paramiko.SFTPServer, fs_cls)
            try:
                t.start_server(server=auth_cls())
            except Exception:  # noqa: BLE001 - a client that hung up mid-handshake
                continue
            transports.append(t)

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    buf = io.StringIO()
    client_key.write_private_key(buf)
    conn = {'id': 'sftptest', 'kind': 'sftp', 'name': 'sftp', 'host': '127.0.0.1', 'port': lsock.getsockname()[1],
            'username': 'user', 'secret_key': 'pw', 'base_path': '/'}
    yield {'conn': conn, 'root': root, 'client_key_pem': buf.getvalue()}
    stop.set()
    th.join(2)
    for t in transports:
        t.close()
    lsock.close()


# ---------------------------------------------------------------- docker databases

def _docker(*args, timeout=60):
    return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)


def _image_present(image):
    try:
        return _docker('image', 'inspect', image, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _published(name, port):
    """The host port docker mapped to `port` of container `name`, or None (not running / not published)."""
    r = _docker('port', name, f'{port}/tcp', timeout=20)
    for line in r.stdout.split():                       # '0.0.0.0:49153' (and '[::]:49153')
        tail = line.rsplit(':', 1)[-1]
        if tail.isdigit():
            return int(tail)
    return None


@contextmanager
def docker_db(name, image, env, port, ready, timeout, pull_timeout=600, args=()):
    """Start (or reuse) a container; yields the (host, port) that answers `ready(host, port)`. Skips with a reason.

    The host port is picked by docker (`-p <port>`), never a fixed number: two test databases, or a test database
    next to a dev one, used to fight over the same port and one of them skipped its tests.
    args: server arguments after the image. Under pytest -n every worker shares one container: the first starts it
    under a file lock while the rest wait and reuse it, and none removes it at the end (the next run reuses it),
    since one worker's teardown would pull the database out from under the others."""
    if not os.environ.get('PYTEST_XDIST_WORKER'):
        yield from _docker_db(name, image, env, port, ready, timeout, pull_timeout, args, keep=False)
        return
    import fcntl
    import tempfile
    with open(Path(tempfile.gettempdir()) / f'{name}.docker.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        gen = _docker_db(name, image, env, port, ready, timeout, pull_timeout, args, keep=True)
        found = next(gen)
        fcntl.flock(lock, fcntl.LOCK_UN)
    yield found
    gen.close()


def _docker_db(name, image, env, port, ready, timeout, pull_timeout, args, keep):
    if not shutil.which('docker'):
        pytest.skip('docker CLI is not installed')
    try:
        if _docker('version', '--format', '{{.Server.Version}}', timeout=20).returncode != 0:
            pytest.skip('docker daemon is not reachable')
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip('docker daemon is not reachable')
    left = _published(name, port)                       # a container of this name left from an earlier run
    if left:
        for h in ('127.0.0.1', '172.17.0.1'):
            if ready(h, left, quick=True):
                yield h, left
                return
    if not _image_present(image):
        try:
            r = _docker('pull', image, timeout=pull_timeout)
        except subprocess.TimeoutExpired:
            pytest.skip(f'docker pull {image} timed out after {pull_timeout}s')
        if r.returncode != 0:
            pytest.skip(f'docker pull {image} failed: {r.stderr.strip()[-300:]}')
    _docker('rm', '-f', name, timeout=60)
    cmd = ['run', '-d', '--name', name, '-p', str(port)]        # no --rm: logs survive a crash; docker picks the port
    for k, v in env.items():
        cmd += ['-e', f'{k}={v}']
    r = _docker(*cmd, image, *args, timeout=120)
    if r.returncode != 0:
        pytest.skip(f'docker run {image} failed: {r.stderr.strip()[-300:]}')
    try:
        ip = _docker('inspect', '-f', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', name).stdout.strip()
        host_port = _published(name, port)
        candidates = ([('127.0.0.1', host_port), ('172.17.0.1', host_port)] if host_port else []) + ([(ip, port)] if ip else [])
        deadline = time.time() + timeout
        found = None
        while time.time() < deadline and found is None:
            for h, p in candidates:
                if ready(h, p):
                    found = (h, p)
                    break
            else:
                if _docker('inspect', '-f', '{{.State.Running}}', name).stdout.strip() != 'true':
                    break                                 # the server crashed: no point waiting
                time.sleep(2)
        if found is None:
            r = _docker('logs', '--tail', '200', name)
            lines = (r.stdout + r.stderr).splitlines()
            why = [x.strip() for x in lines if any(k in x for k in ('Message:', 'errno text', 'ERROR', 'FATAL'))]
            pytest.skip(f'{image} did not start (waited up to {timeout}s on {candidates}): '
                        + ('; '.join(why[:3]) or ' | '.join(lines[-3:])))
        yield found
    finally:
        if not keep:
            _docker('rm', '-f', name, timeout=120)


def pg_ready(host, port, quick=False):
    import psycopg
    try:
        with psycopg.connect(host=host, port=port, user='postgres', password='pw', dbname='postgres',
                             connect_timeout=2 if quick else 5) as c:
            c.execute('SELECT 1')
        return True
    except Exception:  # noqa: BLE001
        return False


MSSQL_PW = 'Str0ng!Passw0rd'


def mssql_ready(host, port, quick=False):
    import pymssql
    try:
        with pymssql.connect(server=host, port=str(port), user='sa', password=MSSQL_PW, database='master',
                             login_timeout=2 if quick else 5, autocommit=True) as c:
            cur = c.cursor()
            cur.execute("IF DB_ID('lbltest') IS NULL CREATE DATABASE lbltest")
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope='session')
def pg_conn():
    with docker_db('lbl_test_pg', 'postgres:16', {'POSTGRES_PASSWORD': 'pw'}, 5432, pg_ready, 90) as (host, port):
        yield {'id': 'pg', 'kind': 'postgres', 'name': 'pg', 'host': host, 'port': port, 'database': 'postgres',
               'username': 'postgres', 'secret_key': 'pw', 'schema': 'lbltest', 'table_prefix': 'lbl_'}


@pytest.fixture(scope='session')
def mssql_conn():
    with docker_db('lbl_test_mssql', 'mcr.microsoft.com/mssql/server:2022-latest',
                   {'ACCEPT_EULA': 'Y', 'MSSQL_SA_PASSWORD': MSSQL_PW}, 1433, mssql_ready, 180,
                   pull_timeout=900) as (host, port):
        yield {'id': 'ms', 'kind': 'mssql', 'name': 'mssql', 'host': host, 'port': port, 'database': 'lbltest',
               'username': 'sa', 'secret_key': MSSQL_PW, 'schema': '', 'table_prefix': 'lbl_'}
