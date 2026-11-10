"""
Saved ◆ chats (app.llm_sessions + app.llm_messages): every chat of both assistants lives in PostgreSQL, so history
survives reloads, other devices and rebuilds.

    create(user, role, scope, title)      a new chat; the 21st first soft-deletes the one not opened longest
    list_(user, role, scope, q, cursor)   live chats (pinned first, then last opened), search over titles + messages
    get(user, sid)                        one chat + its messages (touches last_opened_at)
    update(user, sid, title, pinned)      rename / pin
    delete(user, ids | role + scope)      soft delete (delete_reason 'user'); restore(user, ids) undoes it
    append(user, sid, role, text, ...)    one message (the chat route: the user's at the start, the answer at the end),
                                          with the page it was written on
    history(user, sid)                    [{role, text, page}] for react.history (last HISTORY_TURNS after the summary)
    context(user, sid)                    {summary, summary_seq, plan, messages after it}: what the model is sent
    set_plan(user, sid, items)            the chat's cross-page checklist (the todo tool)
    for_summary(user, sid, after, upto)   the messages (with traces) a compaction folds into the summary
    save_summary(user, sid, ...)          store a new summary (only over the one it was built from)
    purge(now)                            hard-deletes chats deleted more than llm_chat_retention_days ago
    compact_trace(events)                 the NDJSON events of one turn -> the trace kept with the answer

Deleting only marks a chat deleted: every user-facing query filters `deleted_at IS NULL`, and a deleted chat's id
answers 404 like another user's. The rows stay until purge() (startup + every 24 h, start_purger()). Deleting the
account removes them at once (FK CASCADE).

One chat, every page: the assistant's chats are role 'app'. The page a message comes from picks the specialist
that answers it (engine/roles.py), not the chat, so the same chat continues on the home page, in the Data Center
and in any visualizer session. Each message keeps its page (llm_messages.page); list_(role 'app') lists every live
chat of the user, the older per-assistant chats ('data', 'visual') too, so they can be continued anywhere.

Limits: MAX_SESSIONS (Admin › Settings llm_max_sessions) live chats per user (role 'app' counts every chat; the
older roles count their own). Pinned chats are never removed automatically; when every chat is pinned, create() is
refused (409). create() runs in one transaction holding an advisory lock on (owner, role) plus FOR UPDATE on the
live rows, so two tabs cannot push the count past the limit. MAX_MESSAGES per chat: the oldest go first.
"""

import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from .. import db
from ..core import logging as vlog

MAX_SESSIONS = 20
MAX_MESSAGES = 400
HISTORY_TURNS = 40
TITLE_LEN = 60
RETENTION_DAYS = 730
PURGE_BATCH = 1000
PURGE_EVERY_S = 24 * 3600
ROLES = ('app', 'data', 'visual', 'analysis')
APP = 'app'                     # one chat across every page: lists and limits cover all roles
MAX_PLAN = 30
THINKING_MAX = 4000
PREVIEW_MAX = 1500
_PURGE_LOCK_KEY = 0x6c6c6d63686174      # any constant: the advisory lock id of "llm chat purge"

_log = vlog.get_logger('llm')
_J = {k: bindparam(k, type_=JSONB) for k in ('scope', 'trace', 'usage', 'page', 'plan')}
_COLS = ('id, role, title, scope, provider_id, model, mode, effort, pinned, n_messages, usage, created_at, '
         'updated_at, last_opened_at, summary_seq, summary_tokens, last_page')


class SessionError(ValueError):
    """A chat history problem with an HTTP status for the route."""

    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------- helpers
def max_sessions():
    try:
        from ..auth import settings
        return int(settings.get('llm_max_sessions'))
    except Exception:  # noqa: BLE001 - settings unreadable: the constant
        return MAX_SESSIONS


def retention_days():
    try:
        from ..auth import settings
        return int(settings.get('llm_chat_retention_days'))
    except Exception:  # noqa: BLE001
        return RETENTION_DAYS


def _uid(user):
    uid = (user or {}).get('id') if isinstance(user, dict) else user
    if not uid:
        raise SessionError('sign in first', 401)
    return uid


def _role(role):
    if role not in ROLES:
        raise SessionError(f'role must be one of {", ".join(ROLES)}', 400)
    return role


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def _row(r):
    d = dict(r._mapping)
    for k in ('created_at', 'updated_at', 'last_opened_at', 'deleted_at'):
        if k in d:
            d[k] = _iso(d[k])
    d['scope'] = d.get('scope') or {}
    d['usage'] = d.get('usage') or {}
    if 'last_page' in d:
        d['last_page'] = d['last_page'] or {}
    if 'page' in d:
        d['page'] = d['page'] or {}
    return d


def _brief(r):
    return {'id': r.id, 'title': r.title, 'last_opened_at': _iso(r.last_opened_at)} if r is not None else None


def _scope(scope):
    """Only the keys a scope may have ({bucket_id} | {dataset_id, name}), strings."""
    if not isinstance(scope, dict):
        return {}
    return {k: str(scope[k])[:200] for k in ('bucket_id', 'dataset_id', 'name') if scope.get(k) not in (None, '')}


def _filter(scope):
    """The scope keys a list / delete filters on (the name is only a label)."""
    return {k: v for k, v in _scope(scope).items() if k != 'name'}


def cut_title(t):
    t = re.sub(r'\s+', ' ', str(t or '')).strip()
    return t if len(t) <= TITLE_LEN else t[:TITLE_LEN - 1].rstrip() + '…'


def _role_sql(role, col='role'):
    """The WHERE part that picks one role's chats ('' for role app: every chat of the user)."""
    return '' if role == APP else f' AND {col} = :r'


def _lock_role(c, uid, role):
    """Serialise the writers of one user's chats of one role (the count check, inserts, restores); role app locks
    and returns every live chat of the user (it counts them all)."""
    c.execute(text('SELECT pg_advisory_xact_lock(hashtext(:k))'), {'k': f'llm_sessions:{uid}:{role}'})
    return c.execute(text(f"""SELECT id, title, pinned, last_opened_at FROM app.llm_sessions
                              WHERE owner_id = :u{_role_sql(role)} AND deleted_at IS NULL
                              ORDER BY last_opened_at, id FOR UPDATE"""), {'u': uid, 'r': role}).all()


def _oldest_unpinned(rows):
    return next((r for r in rows if not r.pinned), None)      # rows are ordered by last_opened_at


def _live(c, uid, sid, lock=False):
    r = c.execute(text(f"""SELECT {_COLS} FROM app.llm_sessions
                           WHERE id = :s AND owner_id = :u AND deleted_at IS NULL{' FOR UPDATE' if lock else ''}"""),
                  {'s': str(sid or ''), 'u': uid}).first()
    if r is None:
        raise SessionError('no such chat', 404)
    return r


# ---------------------------------------------------------------- sessions
def create(user, role, scope=None, title='', meta=None):
    """A new chat -> {session, removed: {id, title, last_opened_at} | None}. At the limit the unpinned chat of
    this role not opened longest is soft-deleted first (delete_reason 'limit'); 409 when all are pinned."""
    uid, role = _uid(user), _role(role)
    mx = max_sessions()
    meta = meta or {}
    removed = []
    with db.begin() as c:
        rows = list(_lock_role(c, uid, role))
        while len(rows) >= mx:
            old = _oldest_unpinned(rows)
            if old is None:
                raise SessionError(f'{len(rows)} pinned chats: unpin or delete one', 409)
            c.execute(text("""UPDATE app.llm_sessions SET deleted_at = now(), delete_reason = 'limit'
                              WHERE id = :s"""), {'s': old.id})
            removed.append(_brief(old))
            rows.remove(old)
        sid = uuid.uuid4().hex
        r = c.execute(text(f"""INSERT INTO app.llm_sessions(id, owner_id, role, title, scope, provider_id, model, mode,
                                                            effort)
                               VALUES (:id, :u, :r, :t, :scope, :p, :m, :mo, :e) RETURNING {_COLS}""")
                      .bindparams(_J['scope']),
                      {'id': sid, 'u': uid, 'r': role, 't': cut_title(title), 'scope': _scope(scope),
                       'p': meta.get('provider_id'), 'm': (meta.get('model') or None) and str(meta['model'])[:200],
                       'mo': meta.get('mode'), 'e': meta.get('effort')}).first()
    return {'session': _row(r), 'removed': removed[0] if removed else None, 'removed_all': removed}


def list_(user, role, scope=None, q='', cursor=None, limit=50):
    """Live chats of one role (role app: every chat of the user; pinned first, then last opened) -> {sessions,
    next_cursor, count, max, will_remove, all_pinned}. `scope` filters ({bucket_id} | {dataset_id}); `q` searches
    titles and message texts (ILIKE)."""
    uid, role = _uid(user), _role(role)
    try:
        off = max(0, int(cursor or 0))
    except (TypeError, ValueError):
        raise SessionError('bad cursor', 400) from None
    limit = max(1, min(200, int(limit or 50)))
    where = ['s.owner_id = :u', 's.deleted_at IS NULL'] + ([] if role == APP else ['s.role = :r'])
    args = {'u': uid, 'r': role, 'lim': limit + 1, 'off': off}
    f = _filter(scope)
    stmt_params = []
    if f:
        where.append('s.scope @> :f')
        args['f'] = f
        stmt_params.append(bindparam('f', type_=JSONB))
    q = str(q or '').strip()[:200]
    if q:
        args['q'] = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        where.append("""(s.title ILIKE :q ESCAPE '\\' OR EXISTS (SELECT 1 FROM app.llm_messages m
                          WHERE m.session_id = s.id AND m.text ILIKE :q ESCAPE '\\'))""")
    cols = ', '.join('s.' + x.strip() for x in _COLS.split(','))
    sql = text(f"""SELECT {cols} FROM app.llm_sessions s WHERE {' AND '.join(where)}
                   ORDER BY s.pinned DESC, s.last_opened_at DESC, s.id DESC LIMIT :lim OFFSET :off""")
    if stmt_params:
        sql = sql.bindparams(*stmt_params)
    mx = max_sessions()
    with db.connect() as c:
        rows = c.execute(sql, args).all()
        live = c.execute(text(f"""SELECT id, title, pinned, last_opened_at FROM app.llm_sessions
                                  WHERE owner_id = :u{_role_sql(role)} AND deleted_at IS NULL
                                  ORDER BY last_opened_at, id"""), {'u': uid, 'r': role}).all()
    full = len(live) >= mx
    old = _oldest_unpinned(live) if full else None
    return {'sessions': [_row(r) for r in rows[:limit]],
            'next_cursor': str(off + limit) if len(rows) > limit else None,
            'count': len(live), 'max': mx, 'will_remove': _brief(old), 'all_pinned': full and old is None}


def get(user, sid, touch=True):
    """One live chat + its messages (seq order) -> {session, messages}; opening it sets last_opened_at."""
    uid = _uid(user)
    with db.begin() as c:
        if touch:
            r = c.execute(text(f"""UPDATE app.llm_sessions SET last_opened_at = now()
                                   WHERE id = :s AND owner_id = :u AND deleted_at IS NULL RETURNING {_COLS}"""),
                          {'s': str(sid or ''), 'u': uid}).first()
            if r is None:
                raise SessionError('no such chat', 404)
        else:
            r = _live(c, uid, sid)
        msgs = c.execute(text("""SELECT id, seq, role, text, trace, usage, page, created_at FROM app.llm_messages
                                 WHERE session_id = :s ORDER BY seq"""), {'s': r.id}).all()
        extra = c.execute(text('SELECT summary, plan FROM app.llm_sessions WHERE id = :s'), {'s': r.id}).first()
    return {'session': {**_row(r), 'summary': extra.summary, 'plan': extra.plan or []},
            'messages': [_row(m) for m in msgs]}


def update(user, sid, title=None, pinned=None):
    """Rename and / or pin -> the session."""
    uid = _uid(user)
    sets, args = [], {'s': str(sid or ''), 'u': uid}
    if title is not None:
        t = re.sub(r'\s+', ' ', str(title)).strip()[:200]
        if not t:
            raise SessionError('the title cannot be empty', 400)
        sets.append('title = :t')
        args['t'] = t
    if pinned is not None:
        if not isinstance(pinned, bool):
            raise SessionError('pinned must be true or false', 400)
        sets.append('pinned = :p')
        args['p'] = pinned
    with db.begin() as c:
        if not sets:
            return _row(_live(c, uid, sid))
        r = c.execute(text(f"""UPDATE app.llm_sessions SET {', '.join(sets)}, updated_at = now()
                               WHERE id = :s AND owner_id = :u AND deleted_at IS NULL RETURNING {_COLS}"""),
                      args).first()
    if r is None:
        raise SessionError('no such chat', 404)
    return _row(r)


def delete(user, ids=None, role=None, scope=None, include_pinned=False):
    """Soft delete -> {deleted: [ids]}. `ids`: exactly those chats (pinned too). Else every live chat of `role`
    (only the ones in `scope` when given), pinned ones only with include_pinned."""
    uid = _uid(user)
    args = {'u': uid}
    where = ['owner_id = :u', 'deleted_at IS NULL']
    params = []
    if ids is not None:
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise SessionError('ids must be a list of chat ids', 400)
        if not ids:
            return {'deleted': []}
        where.append('id = ANY(:ids)')
        args['ids'] = ids[:1000]
    else:
        args['r'] = _role(role)
        if role != APP:
            where.append('role = :r')
        f = _filter(scope)
        if f:
            where.append('scope @> :f')
            args['f'] = f
            params.append(bindparam('f', type_=JSONB))
        if not include_pinned:
            where.append('NOT pinned')
    sql = text(f"""UPDATE app.llm_sessions SET deleted_at = now(), delete_reason = 'user'
                   WHERE {' AND '.join(where)} RETURNING id""")
    if params:
        sql = sql.bindparams(*params)
    with db.begin() as c:
        out = [r.id for r in c.execute(sql, args)]
    return {'deleted': out}


def delete_one(user, sid):
    if not delete(user, ids=[str(sid or '')])['deleted']:
        raise SessionError('no such chat', 404)
    return {'deleted': [sid]}


def restore(user, ids):
    """Undo a delete (by the user or the limit) -> {restored: [sessions]}; 409 when that would bring a role past
    the limit (delete another chat first). Purged chats are gone: 404."""
    uid = _uid(user)
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise SessionError('ids must be a non-empty list of chat ids', 400)
    mx = max_sessions()
    with db.begin() as c:
        rows = c.execute(text("""SELECT id, role FROM app.llm_sessions
                                 WHERE owner_id = :u AND id = ANY(:ids) AND deleted_at IS NOT NULL"""),
                         {'u': uid, 'ids': ids[:1000]}).all()
        if len(rows) != len(set(ids)):
            raise SessionError('no such deleted chat', 404)
        roles_ = {r.role for r in rows}
        for role in sorted(roles_ | ({APP} if roles_ - {APP} else set())):
            live = _lock_role(c, uid, role)             # role app counts every chat: restoring any one adds to it
            n = len(live) + sum(1 for r in rows if role == APP or r.role == role)
            if n > mx:
                raise SessionError(f'{len(live)} of {mx} chats are open: delete another chat first', 409)
        out = c.execute(text(f"""UPDATE app.llm_sessions SET deleted_at = NULL, delete_reason = NULL
                                 WHERE owner_id = :u AND id = ANY(:ids) RETURNING {_COLS}"""),
                        {'u': uid, 'ids': [r.id for r in rows]}).all()
    return {'restored': [_row(r) for r in out]}


# ---------------------------------------------------------------- messages
def append(user, sid, role, text_, trace=None, usage=None, meta=None, page=None):
    """One message at the end of a live chat -> {id, seq}. Updates n_messages, updated_at, last_opened_at, the
    usage totals, last_page and (for an untitled chat) the title; keeps the newest MAX_MESSAGES. `page`: where it
    was written ({role, screen, label, ...}, engine/roles.page_of)."""
    uid = _uid(user)
    if role not in ('user', 'assistant'):
        raise SessionError('role must be user or assistant', 400)
    # in / out: billed tokens (summed on the chat); ctx / limit: the context the answer ended at (the page's ring)
    usage = ({k: int((usage or {}).get(k) or 0) for k in ('in', 'out')}
             | {k: int(usage[k]) for k in ('ctx', 'limit') if isinstance(usage.get(k), (int, float))}) if usage else {}
    with db.begin() as c:
        s = _live(c, uid, sid, lock=True)
        seq = (c.execute(text('SELECT max(seq) FROM app.llm_messages WHERE session_id = :s'), {'s': s.id}).scalar()
               or 0) + 1
        mid = uuid.uuid4().hex
        page = _page(page)
        c.execute(text("""INSERT INTO app.llm_messages(id, session_id, seq, role, text, trace, usage, page)
                          VALUES (:id, :s, :seq, :r, :t, :trace, :usage, :page)""")
                  .bindparams(_J['trace'], _J['usage'], _J['page']),
                  {'id': mid, 's': s.id, 'seq': seq, 'r': role, 't': str(text_ or ''), 'trace': list(trace or []),
                   'usage': usage, 'page': page})
        c.execute(text('DELETE FROM app.llm_messages WHERE session_id = :s AND seq <= :old'),
                  {'s': s.id, 'old': seq - MAX_MESSAGES})
        tot = dict(s.usage or {})
        for k in ('in', 'out'):
            tot[k] = int(tot.get(k) or 0) + usage.get(k, 0)
        meta = meta or {}
        title = s.title or (cut_title(text_) if role == 'user' else '')
        c.execute(text("""UPDATE app.llm_sessions SET n_messages = (SELECT count(*) FROM app.llm_messages
                                                                    WHERE session_id = :s),
                                 updated_at = now(), last_opened_at = now(), usage = :usage, title = :t,
                                 provider_id = coalesce(:p, provider_id), model = coalesce(:m, model),
                                 mode = coalesce(:mo, mode), effort = coalesce(:e, effort),
                                 last_page = CASE WHEN :page = '{}'::jsonb THEN last_page ELSE :page END
                          WHERE id = :s""").bindparams(_J['usage'], _J['page']),
                  {'s': s.id, 'usage': tot, 't': title, 'p': meta.get('provider_id'), 'page': page,
                   'm': (meta.get('model') or None) and str(meta['model'])[:200], 'mo': meta.get('mode'),
                   'e': meta.get('effort')})
    return {'id': mid, 'seq': seq}


PAGE_KEYS = ('role', 'screen', 'label', 'session_id', 'bucket', 'path', 'tab', 'continued')


def _page(page):
    """Only the keys a message's page may have, short strings (continued: a bool)."""
    if not isinstance(page, dict):
        return {}
    out = {k: str(page[k])[:200] for k in PAGE_KEYS if k != 'continued' and page.get(k) not in (None, '')}
    if page.get('continued') is True:
        out['continued'] = True
    return out


def history(user, sid, turns=HISTORY_TURNS):
    """[{role, text, page}] of the last `turns` messages after the summary (react.history's input)."""
    return [{'role': m['role'], 'text': m['text'], 'page': m['page']}
            for m in context(user, sid)['messages'][-int(turns):]]


def context(user, sid):
    """What the model is sent of a chat: {summary, summary_seq, summary_tokens, plan, messages: [{seq, role, text,
    page}] after summary_seq} (summary None = never compacted). Bounded by MAX_MESSAGES."""
    uid = _uid(user)
    with db.connect() as c:
        r = c.execute(text("""SELECT id, summary, summary_seq, summary_tokens, plan FROM app.llm_sessions
                              WHERE id = :s AND owner_id = :u AND deleted_at IS NULL"""),
                      {'s': str(sid or ''), 'u': uid}).first()
        if r is None:
            raise SessionError('no such chat', 404)
        rows = c.execute(text("""SELECT seq, role, text, page FROM app.llm_messages WHERE session_id = :s
                                 AND seq > :a ORDER BY seq"""), {'s': r.id, 'a': int(r.summary_seq or 0)}).all()
    return {'summary': r.summary, 'summary_seq': r.summary_seq, 'summary_tokens': r.summary_tokens,
            'plan': r.plan or [],
            'messages': [{'seq': m.seq, 'role': m.role, 'text': m.text, 'page': m.page or {}} for m in rows]}


def set_plan(user, sid, items):
    """Store the chat's checklist (the todo tool's items: {text, state, page?}, at most MAX_PLAN)."""
    uid = _uid(user)
    clean = []
    for x in (items or [])[:MAX_PLAN]:
        if isinstance(x, dict) and str(x.get('text') or '').strip():
            it = {'text': str(x['text']).strip()[:200], 'state': str(x.get('state') or 'todo')[:20]}
            if x.get('page'):
                it['page'] = str(x['page'])[:20]
            clean.append(it)
    with db.begin() as c:
        n = c.execute(text("""UPDATE app.llm_sessions SET plan = :plan WHERE id = :s AND owner_id = :u
                              AND deleted_at IS NULL""").bindparams(_J['plan']),
                      {'plan': clean, 's': str(sid), 'u': uid}).rowcount
    if not n:
        raise SessionError('no such chat', 404)
    return clean


def for_summary(user, sid, after_seq, upto_seq):
    """[{seq, role, text, trace}] with after_seq < seq <= upto_seq: what a compaction folds into the summary."""
    uid = _uid(user)
    with db.connect() as c:
        _live(c, uid, sid)
        rows = c.execute(text("""SELECT seq, role, text, trace FROM app.llm_messages
                                 WHERE session_id = :s AND seq > :a AND seq <= :b ORDER BY seq"""),
                         {'s': str(sid), 'a': int(after_seq or 0), 'b': int(upto_seq)}).all()
    return [{'seq': m.seq, 'role': m.role, 'text': m.text, 'trace': m.trace or []} for m in rows]


def save_summary(user, sid, summary, seq, tokens, expect_seq):
    """Store a summary of every message up to `seq`, only when the chat still has the summary it was built from
    (summary_seq = expect_seq: two tabs compacting at once never step back) -> True when stored."""
    uid = _uid(user)
    with db.begin() as c:
        n = c.execute(text("""UPDATE app.llm_sessions SET summary = :t, summary_seq = :q, summary_tokens = :k
                              WHERE id = :s AND owner_id = :u AND deleted_at IS NULL
                              AND summary_seq IS NOT DISTINCT FROM :e"""),
                      {'t': str(summary), 'q': int(seq), 'k': int(tokens), 's': str(sid), 'u': uid,
                       'e': None if expect_seq is None else int(expect_seq)}).rowcount
    return bool(n)


def improve_title(user, sid, provider, question, answer, expect_title):
    """A short title from the model (<= 16 tokens, same provider) for a chat still titled `expect_title` (the cut
    first message: never over a rename). Runs in a background thread; every failure keeps the cut title."""
    def work():
        try:
            from . import providers
            msgs = [{'role': 'user', 'parts': [{'text': f'User: {question[:1500]}\n\nAssistant: {answer[:1500]}'}]}]
            t, _ = providers.complete(provider, 'Write a title of at most 6 words for this chat. Answer with the '
                                                'title only: no quotes, no full stop.', msgs, max_tokens=16)
            t = re.sub(r'\s+', ' ', str(t or '')).strip().strip('"\'`*#').strip()
            if not t:
                return
            t = cut_title(t)
            with db.begin() as c:
                c.execute(text("""UPDATE app.llm_sessions SET title = :t WHERE id = :s AND owner_id = :u
                                  AND title = :old AND deleted_at IS NULL"""),
                          {'t': t, 's': sid, 'u': _uid(user), 'old': expect_title})
        except Exception as e:  # noqa: BLE001 - the cut title stays
            _log.debug(f'chat title: {type(e).__name__}: {vlog.esc(str(e))}')
    th = threading.Thread(target=work, name='llm-title', daemon=True)
    th.start()
    return th


# ---------------------------------------------------------------- purge
def purge(now=None):
    """Hard-delete chats (cascading to messages) deleted more than llm_chat_retention_days ago, in batches of
    PURGE_BATCH, under an advisory lock (a second purger skips) -> the number removed, None when skipped."""
    now = now or datetime.now(timezone.utc)
    cut = now - timedelta(days=retention_days())
    total = 0
    with db.engine().connect() as c:
        got = c.execute(text('SELECT pg_try_advisory_lock(:k)'), {'k': _PURGE_LOCK_KEY}).scalar()
        c.commit()
        if not got:
            return None
        try:
            while True:
                n = c.execute(text("""DELETE FROM app.llm_sessions WHERE id IN (
                                          SELECT id FROM app.llm_sessions WHERE deleted_at IS NOT NULL
                                          AND deleted_at < :cut LIMIT :n)"""), {'cut': cut, 'n': PURGE_BATCH}).rowcount
                c.commit()
                total += n
                if n < PURGE_BATCH:
                    break
        finally:
            c.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': _PURGE_LOCK_KEY})
            c.commit()
    return total


def start_purger(log=None, every_s=PURGE_EVERY_S):
    """purge() now and every `every_s` in a daemon thread -> the threading.Event that stops it."""
    stop = threading.Event()

    def loop():
        while True:
            try:
                n = purge()
                if n and log:
                    log(f'purged {n} deleted ◆ chats older than {retention_days()} days')
            except Exception as e:  # noqa: BLE001 - try again next time
                _log.warning(f'chat purge failed: {type(e).__name__}: {vlog.esc(str(e))}')
            if stop.wait(every_s):
                return
    threading.Thread(target=loop, name='llm-chat-purge', daemon=True).start()
    return stop


# ---------------------------------------------------------------- traces
def _cut(s, n):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + '…'


def compact_trace(events):
    """The events of one turn -> the list stored with the answer: meta (model, mode, effort), the thinking as one
    item (cut to THINKING_MAX), one 'tool' item per call (start + end merged), one 'confirm' per confirmation (with
    its outcome), search, skill, the last todo, ui_action, ask, cannot, plan_proposal, a retry count, notices,
    error, done. Dropped: ping, step, text (the text is the message itself), session, context."""
    out, tools_, confirms = [], {}, {}
    think, think_item, todo_item, retry_item = [], None, None, None
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        t = ev.get('type')
        if t in ('ping', 'step', 'text', 'session', 'context', None):
            continue
        if t == 'meta':
            out.append({'type': 'meta', **{k: ev.get(k) for k in ('model', 'provider', 'mode', 'effort')}})
        elif t == 'thinking':
            think.append(str(ev.get('text') or ''))
            if think_item is None:
                think_item = {'type': 'thinking', 'text': '', 'ms': 0}
                out.append(think_item)
        elif t == 'thinking_end':
            if think_item is not None:
                think_item['ms'] += int(ev.get('ms') or 0)
        elif t == 'tool_start':
            item = {'type': 'tool', 'id': ev.get('id'), 'name': ev.get('name'), 'args': ev.get('args'),
                    'risk': ev.get('risk')}
            if ev.get('summary'):
                item['summary'] = ev['summary']
            out.append(item)
            if ev.get('id'):
                tools_[ev['id']] = item
        elif t == 'tool_end':
            item = tools_.get(ev.get('id'))
            if item is None:
                item = {'type': 'tool', 'id': ev.get('id'), 'name': ev.get('name')}
                out.append(item)
            item.update(ok=ev.get('ok'), summary=ev.get('summary', item.get('summary')), ms=ev.get('ms'))
            if ev.get('preview') is not None:
                item['preview'] = _cut(ev['preview'], PREVIEW_MAX)
            for k in ('undo_id', 'url', 'retagged', 'errors'):
                if ev.get(k):
                    item[k] = ev[k]
        elif t == 'confirm':
            item = {'type': 'confirm', 'confirm_id': ev.get('confirm_id'), 'actions': ev.get('actions') or [],
                    'status': 'pending'}
            out.append(item)
            confirms[ev.get('confirm_id')] = item
        elif t == 'confirmed':
            item = confirms.get(ev.get('confirm_id'))
            if item is None:
                item = {'type': 'confirm', 'confirm_id': ev.get('confirm_id'), 'actions': []}
                out.append(item)
            item.update(results=ev.get('results') or [], status=ev.get('status'))
        elif t == 'todo':
            if todo_item is None:
                todo_item = {'type': 'todo'}
                out.append(todo_item)
            todo_item['items'] = ev.get('items') or []
        elif t == 'retry':
            if retry_item is None:
                retry_item = {'type': 'retry', 'count': 0}
                out.append(retry_item)
            retry_item['count'] += 1
            retry_item['reason'] = str(ev.get('reason') or '')[:300]
        elif t == 'done':
            out.append({'type': 'done', **{k: ev.get(k) for k in ('usage', 'ms', 'steps')}})
        else:       # search, skill, ui_action, ask, cannot, plan_proposal, notice, error (and anything newer)
            out.append(dict(ev))
    if think_item is not None:
        think_item['text'] = _cut(''.join(think), THINKING_MAX)
    return out
