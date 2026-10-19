"""Sign-in, cookies, CSRF, the sign-in wall, lockout / rate limit, own account (password, avatar, prefs),
password hashing and the first-admin bootstrap (dataviz/auth/, server/routes/auth.py, me.py)."""

import io

import pytest
from sqlalchemy import text

from authutil import PW, account, client, login, make_app, signed_in
from conftest import jpeg_bytes


@pytest.fixture
def app(pg):
    return make_app()


def _set_cookie(r):
    return r.headers.get('set-cookie', '')


def test_signup_and_login_by_username_or_email(app):
    c = client(app)
    assert c.get('/api/auth/config').json() == {'signup_enabled': True, 'password_min': 10}
    r = c.post('/api/auth/signup', json={'username': 'Alice', 'email': 'Alice@Example.test',
                                         'display_name': '  Alice  A ', 'password': PW})
    assert r.status_code == 200, r.text
    u = r.json()['user']
    assert u['username'] == 'alice' and u['display_name'] == 'Alice A' and u['is_admin'] is False
    assert set(u) == {'id', 'username', 'email', 'display_name', 'is_admin', 'avatar_ver'}
    assert c.get('/api/ping').json()['user']['id'] == u['id']            # signed in straight away
    me = c.get('/api/auth/me').json()
    assert me['user']['id'] == u['id'] and me['prefs'] == {}

    for login_ in ('ALICE', 'alice@example.TEST'):
        c2 = client(app)
        assert login(c2, login_).status_code == 200
        assert c2.get('/api/ping').status_code == 200

    # duplicates (any case), bad usernames, weak passwords: 400 with a human message
    for body in ({'username': 'ALICE', 'email': 'x@y.z'}, {'username': 'bob', 'email': 'alice@EXAMPLE.test'},
                 {'username': 'a!', 'email': 'q@y.z'}, {'username': 'bob', 'email': 'nope'},
                 {'username': 'bob', 'email': 'b@y.z', 'password': 'short'},
                 {'username': 'bobbybobby', 'email': 'b@y.z', 'password': 'BobbyBobby'}):
        r = client(app).post('/api/auth/signup', json={'display_name': 'B', 'password': PW, **body})
        assert r.status_code == 400 and isinstance(r.json()['detail'], str), body


def test_wrong_password_is_generic(app):
    account('alice')
    for login_, pw in (('alice', 'wrong password!'), ('nobody', PW), ('', '')):
        r = login(client(app), login_, pw)
        assert r.status_code == 401 and r.json() == {'detail': 'Invalid username or password'}
        assert 'dv_session' not in _set_cookie(r)


def test_lockout_is_per_account_and_address(app, monkeypatch):
    """5 wrong passwords lock (alice, that address) — the right password from there gets the same 401 — while
    alice signs in fine from another address, so a stranger cannot lock her out."""
    from dataviz import db
    from dataviz.server.routes import auth as auth_routes
    monkeypatch.setattr(auth_routes, 'client_ip', lambda request: request.headers.get('x-test-ip'))  # TestClient: one peer
    account('alice')
    attacker = client(app)
    attacker.headers['x-test-ip'] = '203.0.113.9'
    for _ in range(5):
        assert login(attacker, 'alice', 'wrong password!').status_code == 401
    r = login(attacker, 'alice')                            # correct password, but locked there: same answer
    assert r.status_code == 401 and r.json()['detail'] == 'Invalid username or password'
    owner = client(app)
    owner.headers['x-test-ip'] = '198.51.100.7'
    assert login(owner, 'alice').status_code == 200
    with db.connect() as conn:
        assert conn.execute(text('SELECT failed_logins FROM app.users')).scalar() == 0   # reset by the success


def test_failure_lock_schedule():
    from dataviz.auth.ratelimit import FailureLock
    t = [0.0]
    lock = FailureLock(clock=lambda: t[0])
    for _ in range(4):
        lock.fail('k')
    assert not lock.locked('k')
    lock.fail('k')                                          # 5th: 1 minute
    assert lock.locked('k') and not lock.locked('other')
    t[0] = 61
    assert not lock.locked('k')
    lock.fail('k')                                          # 6th: 2 minutes
    t[0] = 61 + 119
    assert lock.locked('k')
    for _ in range(10):
        lock.fail('k')
    t[0] += 15 * 60 + 1                                     # capped at 15 minutes
    assert not lock.locked('k')
    lock.clear('k')
    lock.fail('k')
    assert not lock.locked('k')


def test_per_ip_rate_limit(app):
    c = client(app)
    codes = [login(c, 'nobody', 'whatever!!').status_code for _ in range(21)]
    assert codes[:20] == [401] * 20 and codes[20] == 429
    assert 'Too many' in login(c, 'nobody', 'x').json()['detail']


def test_rate_limiter_refills():
    from dataviz.auth.ratelimit import RateLimiter
    t = [0.0]
    rl = RateLimiter(rate=20, per=60, burst=20, max_keys=3, clock=lambda: t[0])
    assert all(rl.allow('a') for _ in range(20)) and not rl.allow('a')
    t[0] += 3.0                                              # one token per 3 s
    assert rl.allow('a') and not rl.allow('a')
    for k in 'bcde':
        rl.allow(k)
    assert len(rl._buckets) == 3                             # bounded memory


def test_cookie_flags(pg):
    account('alice')
    r = login(client(make_app()), 'alice')
    ck = _set_cookie(r).lower()
    assert 'dv_session=' in ck and 'httponly' in ck and 'samesite=lax' in ck and 'path=/' in ck
    assert 'secure' not in ck and 'max-age=2592000' in ck
    r = client(make_app()).post('/api/auth/login', json={'login': 'alice', 'password': PW},
                                headers={'X-Forwarded-Proto': 'https'})
    assert 'secure' in _set_cookie(r).lower()
    r = login(client(make_app({'cookie_secure': 'true'})), 'alice')
    assert 'secure' in _set_cookie(r).lower()
    r = client(make_app({'cookie_secure': 'false'})).post('/api/auth/login', json={'login': 'alice', 'password': PW},
                                                          headers={'X-Forwarded-Proto': 'https'})
    assert 'secure' not in _set_cookie(r).lower()


def test_sign_in_wall(app):
    c = client(app, follow_redirects=False)
    r = c.get('/api/ping')
    assert r.status_code == 401 and r.json() == {'detail': 'sign in first'}
    assert c.get('/s/abc/api/points').status_code == 401
    r = c.get('/s/abc/', params={'x': '1'})
    assert r.status_code == 302 and r.headers['location'] == '../../login?next=%2Fs%2Fabc%2F%3Fx%3D1'
    assert c.get('/').headers['location'] == 'login?next=%2F'
    assert not c.get('/').headers['location'].startswith('/')
    for path in ('/login', '/api/auth/config'):               # public
        assert c.get(path).status_code == 200, path
    r = c.get('/api/ping')
    assert r.headers['x-content-type-options'] == 'nosniff' and r.headers['referrer-policy'] == 'same-origin'
    forged = client(app)
    forged.cookies.set('dv_session', 'forged')
    assert forged.get('/api/ping').status_code == 401


def test_csrf(app):
    c, _ = signed_in(app, 'alice')
    assert c.post('/api/ping').status_code == 200
    r = c.post('/api/ping', headers={'X-Requested-With': ''})
    assert r.status_code == 403 and 'X-Requested-With' in r.json()['detail']
    assert c.post('/api/ping', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert c.post('/api/ping', headers={'Origin': 'null'}).status_code == 403
    assert c.post('/api/ping', headers={'Origin': 'http://testserver'}).status_code == 200
    # login itself is a POST: also needs the header
    assert client(app).post('/api/auth/login', json={'login': 'alice', 'password': PW},
                            headers={'X-Requested-With': 'nope'}).status_code == 403
    assert c.get('/api/ping', headers={'X-Requested-With': ''}).status_code == 200      # GET is exempt


def test_logout_and_logout_others(app):
    c1, u = signed_in(app, 'alice')
    c2, c3 = client(app), client(app)
    assert login(c2, 'alice').status_code == 200 and login(c3, 'alice').status_code == 200
    old = c1.cookies.get('dv_session')
    r = c1.post('/api/auth/logout')
    assert r.json() == {'ok': True}
    assert c1.get('/api/ping').status_code == 401
    replay = client(app)
    replay.cookies.set('dv_session', old)
    assert replay.get('/api/ping').status_code == 401                 # revoked, not only cleared
    assert c2.post('/api/auth/logout-others').json() == {'ok': True, 'n': 1}
    assert c2.get('/api/ping').status_code == 200 and c3.get('/api/ping').status_code == 401


def test_password_change_revokes_other_sessions(app):
    c1, u = signed_in(app, 'alice')
    c2 = client(app)
    login(c2, 'alice')
    r = c1.put('/api/me/password', json={'current': 'nope nope nope', 'password': 'a brand new secret'})
    assert r.status_code == 400
    r = c1.put('/api/me/password', json={'current': PW, 'password': 'short'})
    assert r.status_code == 400
    r = c1.put('/api/me/password', json={'current': PW, 'password': 'a brand new secret'})
    assert r.json() == {'ok': True}
    assert c1.get('/api/ping').status_code == 200 and c2.get('/api/ping').status_code == 401
    assert login(client(app), 'alice').status_code == 401
    assert login(client(app), 'alice', 'a brand new secret').status_code == 200


def test_deactivated_user(app):
    from dataviz.auth import users
    c, u = signed_in(app, 'alice')
    users.update(u['id'], is_active=False)
    assert c.get('/api/ping').status_code == 401
    assert login(client(app), 'alice').status_code == 401


def test_session_expiry(app):
    from dataviz import db
    from dataviz.auth import tokens
    c, u = signed_in(app, 'alice')
    tokens.clear_cache()
    with db.begin() as conn:
        conn.execute(text("UPDATE app.auth_sessions SET last_seen_at = now() - interval '8 days'"))
    assert c.get('/api/ping').status_code == 401
    c, u = signed_in(app, 'bob')
    tokens.clear_cache()
    with db.begin() as conn:
        conn.execute(text("UPDATE app.auth_sessions SET expires_at = now() - interval '1 second'"))
    assert c.get('/api/ping').status_code == 401


def test_signup_disabled(app):
    from dataviz.auth import settings
    settings.update({'signup_enabled': False})
    c = client(app)
    assert c.get('/api/auth/config').json()['signup_enabled'] is False
    r = c.post('/api/auth/signup', json={'username': 'bob', 'email': 'b@y.z', 'display_name': 'B', 'password': PW})
    assert r.status_code == 403 and r.json()['detail'] == 'sign-up is turned off'


def test_profile_update(app):
    c, u = signed_in(app, 'alice')
    account('bob')
    r = c.patch('/api/me', json={'display_name': 'Alice Liddell', 'username': 'Alice2'})
    assert r.status_code == 200 and r.json()['user']['username'] == 'alice2'
    assert c.get('/api/ping').json()['user']['display_name'] == 'Alice Liddell'   # cache evicted
    assert c.patch('/api/me', json={'username': 'BOB'}).status_code == 400
    assert c.patch('/api/me', json={'email': 'bob@example.test'}).status_code == 400


def test_avatar(app):
    from PIL import Image
    c, u = signed_in(app, 'alice')
    other, _ = signed_in(app, 'bob')
    assert other.get(f'/api/users/{u["id"]}/avatar').status_code == 404
    r = c.put('/api/me/avatar', files={'file': ('x.jpg', b'hello, not an image', 'image/jpeg')})
    assert r.status_code == 400
    r = c.put('/api/me/avatar', files={'file': ('x.jpg', b'\0' * (5 * 1024 * 1024 + 10), 'image/jpeg')})
    assert r.status_code == 413
    big = io.BytesIO()
    Image.new('1', (7000, 6000)).save(big, 'PNG')             # tiny file, 42 megapixels
    r = c.put('/api/me/avatar', files={'file': ('x.png', big.getvalue(), 'image/png')})
    assert r.status_code == 413
    r = c.put('/api/me/avatar', files={'file': ('x.jpg', jpeg_bytes(300, 200), 'image/jpeg')})
    assert r.status_code == 200, r.text
    assert r.json()['user']['avatar_ver'] == 1
    r = other.get(f'/api/users/{u["id"]}/avatar', params={'v': 1})
    assert r.status_code == 200 and r.headers['content-type'] == 'image/webp'
    assert r.headers['cache-control'] == 'private, max-age=86400' and r.headers['x-content-type-options'] == 'nosniff'
    im = Image.open(io.BytesIO(r.content))
    assert im.format == 'WEBP' and im.size == (256, 256)
    assert c.delete('/api/me/avatar').json()['user']['avatar_ver'] == 2
    assert other.get(f'/api/users/{u["id"]}/avatar').status_code == 404


def test_avatar_exif_rotation():
    from PIL import Image
    from dataviz.auth import avatars
    im = Image.new('RGB', (400, 100), (255, 0, 0))
    im.paste((0, 0, 255), (0, 0, 200, 100))                   # left half blue
    exif = Image.Exif()
    exif[0x0112] = 6                                          # rotate 90° clockwise to display
    b = io.BytesIO()
    im.save(b, 'JPEG', exif=exif)
    out = Image.open(io.BytesIO(avatars.process(b.getvalue()))).convert('RGB')
    assert out.size == (256, 256)
    top, bottom = out.getpixel((128, 5)), out.getpixel((128, 250))
    assert top[2] > 200 and bottom[0] > 200                   # rotated: blue on top, red below


def test_prefs(app):
    c, u = signed_in(app, 'alice')
    assert c.get('/api/me/prefs').json() == {'prefs': {}}
    assert c.put('/api/me/prefs', json={'prefs': {'theme': 'dark', 'filter': {'a': 1}}}).json() == \
        {'prefs': {'theme': 'dark', 'filter': {'a': 1}}}
    r = c.put('/api/me/prefs', json={'prefs': {'filter': {'b': 2}, 'theme': None, 'x': [1]}})
    assert r.json() == {'prefs': {'filter': {'b': 2}, 'x': [1]}}            # shallow merge, None removes
    assert c.get('/api/auth/me').json()['prefs'] == {'filter': {'b': 2}, 'x': [1]}
    assert c.put('/api/me/prefs', json={'prefs': {'big': 'x' * 70000}}).status_code == 413
    assert c.put('/api/me/prefs', json={'prefs': 'nope'}).status_code == 400
    other, _ = signed_in(app, 'bob')
    assert other.get('/api/me/prefs').json() == {'prefs': {}}


def test_password_policy_and_hash():
    from dataviz.auth import passwords
    with pytest.raises(ValueError):
        passwords.check_policy('x' * 9)
    with pytest.raises(ValueError):
        passwords.check_policy('x' * 257)
    with pytest.raises(ValueError):
        passwords.check_policy('Alice12345', username='alice12345')
    with pytest.raises(ValueError):
        passwords.check_policy('A@example.test', email='a@EXAMPLE.test')
    passwords.check_policy('x' * 10)
    h = passwords.hash_password(PW)
    assert h.startswith('$argon2id$') and passwords.verify(h, PW) and not passwords.verify(h, PW + '!')
    assert not passwords.verify('x', PW) and not passwords.verify(None, PW)


def test_rehash_on_login(app, monkeypatch):
    from argon2 import PasswordHasher
    from dataviz import db
    from dataviz.auth import passwords
    account('alice')
    with db.connect() as c:
        before = c.execute(text('SELECT password_hash FROM app.users')).scalar()
    monkeypatch.setattr(passwords, '_ph', PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1))
    assert passwords.needs_rehash(before)
    assert login(client(app), 'alice').status_code == 200
    with db.connect() as c:
        after = c.execute(text('SELECT password_hash FROM app.users')).scalar()
    assert after != before and 'm=8192,t=1,p=1' in after and not passwords.needs_rehash(after)
    assert login(client(app), 'alice').status_code == 200


def test_audit_rows(app):
    from dataviz.auth import audit
    account('alice')
    login(client(app), 'alice', 'wrong password!')
    login(client(app), 'alice')
    ev = audit.list_events(10)
    assert [e['action'] for e in ev][:2] == ['login', 'login_failed']
    assert ev[1]['target'] == 'alice' and ev[0]['user']['username'] == 'alice' and ev[0]['ip']
    audit.log('x' * 100, target='t' * 500, meta={'a': object()})        # never raises, truncates
    audit.log('boom', user_id='no-such-user')                           # FK error: swallowed


def test_bootstrap(pg):
    from dataviz.auth import bootstrap, settings, users
    logs = []
    u, pw = bootstrap.ensure_first_admin(env={}, log=logs.append)
    assert u['username'] == 'admin' and u['is_admin'] and pw and len(pw) >= 16
    assert users.by_login('admin@localhost')['id'] == u['id']
    assert login(client(make_app()), 'admin', pw).status_code == 200
    assert bootstrap.ensure_first_admin(env={}, log=logs.append) == (None, None)       # second call: no-op
    assert len(users.list_all()) == 1

    bootstrap.ensure_defaults()
    assert settings.all_settings() == {'signup_enabled': True, 'who_can_add_connections': 'everyone'}


def test_bootstrap_env_and_promote(pg):
    from dataviz.auth import bootstrap
    u, pw = bootstrap.ensure_first_admin(env={'DATAVIZ_ADMIN_USERNAME': 'Root', 'DATAVIZ_ADMIN_EMAIL': 'r@x.y',
                                              'DATAVIZ_ADMIN_PASSWORD': 'given password 1'}, log=lambda m: None)
    assert u['username'] == 'root' and pw is None
    assert login(client(make_app()), 'root', 'given password 1').status_code == 200


def test_bootstrap_promotes_existing(pg):
    from dataviz.auth import bootstrap
    account('admin')
    u, pw = bootstrap.ensure_first_admin(env={}, log=lambda m: None)
    assert u['is_admin'] and pw is None
