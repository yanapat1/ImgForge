# `dataviz/auth/` — accounts, sign-in, access

Who is asking (cookie → user), what they may do with a session, and the account / admin plumbing behind
`/api/auth/*`, `/api/me/*`, `/api/users/*`, `/api/admin/*` and `/api/sessions/{sid}/sharing`
(route modules in `server/routes/auth.py`, `me.py`, `users.py`, `admin.py`, `sharing.py`).

| Module | What it does |
|---|---|
| `context.py` | contextvars `USER` (user dict) and `BOUND` (the `/s/<id>/` session + role) for the length of a request |
| `access.py` | the role rule: owner (owner or any admin) > labeler > viewer (member, or anyone on a global session); `check()` → 401 / 404 / 403 |
| `middleware.py` | `AuthMiddleware` (pure ASGI, outermost): cookie → `USER`, CSRF, sign-in wall, security headers; `install_auth(app, cfg)` adds it and the routers |
| `deps.py` | route guards (`current_user`, `admin_user`, `require_session(need, param)`, `require_bound(need)`, `can_add_connections`); each has `__dataviz_auth__ = True` for the route-guard test |
| `tokens.py` | login sessions (`app.auth_sessions`): create / resolve / revoke; stores sha256(token) only; 30 s in-process cache with explicit eviction |
| `users.py` | the user repository: create, look up by username-or-email, edit, list, search, delete (with transfer), lockout bookkeeping, last-admin guard |
| `passwords.py` | argon2id hashing (re-hash on login when parameters change), policy (10–256 chars, not the username / email), dummy verify for timing |
| `ratelimit.py` | per-IP token bucket for login / sign-up (20 per minute, bounded memory) |
| `settings.py` | `app.app_settings`: `signup_enabled` (default on), `who_can_add_connections` (`everyone` \| `admins`) |
| `audit.py` | `log(action, target, meta, user_id, ip)` (never raises), `list_events(limit)` |
| `avatars.py` | upload → Pillow decode (≤ 5 MB, ≤ 40 MP) → EXIF upright → centre square → 256 px WebP |
| `bootstrap.py` | start-up: `ensure_first_admin(env, log)` → `(user, generated_password or None)`; `ensure_defaults()` |

## Rules worth knowing

- **Login** answers one message, `Invalid username or password`, for unknown login, wrong password, locked
  and deactivated accounts; an unknown login still runs one argon2 verification. 5 failures in a row lock the
  account for 1, 2, 4, 8, then 15 minutes; a success resets the count.
- **Cookie** `dv_session`: HttpOnly, SameSite=Lax, Path=/, Max-Age = `auth.session_max_days`; Secure when
  `auth.cookie_secure` is `true`, or `auto` and the request is https (or `X-Forwarded-Proto: https`).
  Sessions end after `auth.session_idle_days` idle or `auth.session_max_days` in total.
- **What evicts the cache at once**: logout, logout-others, password change / reset, deactivation, any
  profile or admin-flag change (`users.update` → `tokens.evict_user`).
- **CSRF**: non-GET/HEAD/OPTIONS under `/api` or `/s/<id>/api` need `X-Requested-With: dataviz`; an
  `Origin` header must name the `Host` (or `X-Forwarded-Host`).
- **Redirects are relative** (`../../login?next=…` from `/s/<id>/`): the app often runs behind a path-prefix
  proxy, so an absolute `/login` would drop the prefix.
- **Last admin**: demoting, deactivating or deleting the last active admin is a `ValueError`, checked with the
  admin rows locked (`SELECT … FOR UPDATE`) in the same transaction.
- **Deleting a user** with `transfer_to` moves their sessions *and* their connections, models and LLM
  providers (a moved visualizer session still points at its owner's connection); names that clash become
  `name (2)`. Without it their sessions are deleted first, then the user (the rest cascades).
- The rate limiter keys on the socket peer address; behind a reverse proxy every user shares the proxy's
  address and therefore one bucket (20 attempts a minute).
