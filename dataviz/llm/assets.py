"""
A user's own additions to the ◆ assistants (Settings › Prompts & Skills), per user in PostgreSQL:

    app.llm_user_prompts   kind prompt (an instruction appended to the system prompt) | skill (steps the agent loads
                           with use_skill), role data | visual | all, name, description, body, enabled; plugin_id set
                           for the rows a plugin installed (removed with it, ON DELETE CASCADE). A skill's `when`
                           is kept as a `when:` front matter line of its body (the SKILL.md format).
    app.llm_plugins        declarative bundles: {name, version, description, skills, prompts, tools, secrets};
                           secrets = Fernet-encrypted {NAME: value}, write-only (the page sees only which are set)

Limits: 100 prompts and 100 skills per user (their own rows; a plugin's do not count), body <= 20000 characters,
name <= 120; a plugin has <= 20 HTTP tools named `<plugin>__<tool>` ([a-z0-9_]). No plugin code runs: its tools are
HTTP calls (llm/tools/http.py), offered only to its owner's runs through `for_run(user, role)`, which the agent
route (server/routes/agent.py `_assets`) reads per chat.
"""

import io
import json
import re
import uuid
import zipfile

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from .. import db
from ..core import logging as vlog

KINDS = ('prompt', 'skill')
ROLES = ('bucket', 'visual', 'data', 'all')
MAX_ITEMS = 100
MAX_BODY = 20000
MAX_NAME = 120
MAX_DESC = 1000
MAX_WHEN = 500
MAX_PLUGINS = 50
MAX_TOOLS = 20
MAX_PLUGIN_ITEMS = 50           # skills / prompts in one plugin
MAX_SECRETS = 20
MAX_ZIP = 2 << 20               # bytes, packed and unpacked
PLUGIN_NAME = re.compile(r'^[a-z][a-z0-9_]{0,39}$')
TOOL_NAME = re.compile(r'^[a-z0-9_]{1,64}$')
SECRET_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
_log = vlog.get_logger('llm')
_J = bindparam('m', type_=JSONB)


class AssetError(ValueError):
    """Bad input (400) with a message for the page."""


class NotFound(KeyError):
    """No such item / plugin of this user (404)."""


def _need(user):
    if not user or not user.get('id'):
        raise AssetError('sign in first')
    return user


def _ts(v):
    return v.timestamp() if hasattr(v, 'timestamp') else v


# ---------------------------------------------------------------- prompts and skills
def _split_when(body):
    """(when, body) of a stored skill body: a leading `---\\nwhen: ...\\n---\\n` block."""
    from .prompting import front
    meta, rest = front(body or '')
    if meta:
        return str(meta.get('when') or ''), rest.lstrip('\n')
    return '', body or ''


def _join_when(when, body):
    when = ' '.join(str(when or '').split())
    return f'---\nwhen: {when}\n---\n{body}' if when else body


def _item(r):
    when, body = _split_when(r.body) if r.kind == 'skill' else ('', r.body)
    return {'id': r.id, 'kind': r.kind, 'role': r.role, 'name': r.name, 'description': r.description or '',
            'when': when, 'body': body, 'enabled': bool(r.enabled), 'plugin_id': r.plugin_id,
            'created_at': _ts(r.created_at), 'updated_at': _ts(r.updated_at)}


def _clean_item(body, old=None):
    """The fields of a prompt / skill from a form body over the saved one (AssetError for bad values)."""
    if not isinstance(body, dict):
        raise AssetError('send the item as an object')
    old = dict(old or {})
    out = {k: old.get(k) for k in ('kind', 'role', 'name', 'description', 'when', 'body', 'enabled')}
    for k in out:
        if k in body and body[k] is not None:
            out[k] = body[k]
    if old.get('kind') and out['kind'] != old['kind']:
        raise AssetError('the kind of an item cannot change')
    if out['kind'] not in KINDS:
        raise AssetError('kind must be prompt or skill')
    out['role'] = out['role'] or 'all'
    if out['role'] not in ROLES:
        raise AssetError('role must be data, visual or all')
    out['name'] = ' '.join(str(out['name'] or '').split())
    if not out['name']:
        raise AssetError('give it a name')
    if len(out['name']) > MAX_NAME:
        raise AssetError(f'the name is longer than {MAX_NAME} characters')
    out['description'] = str(out['description'] or '').strip()
    if len(out['description']) > MAX_DESC:
        raise AssetError(f'the description is longer than {MAX_DESC} characters')
    out['when'] = ' '.join(str(out['when'] or '').split())
    if len(out['when']) > MAX_WHEN:
        raise AssetError(f'"when" is longer than {MAX_WHEN} characters')
    out['body'] = str(out['body'] or '').replace('\r\n', '\n').strip()
    if len(out['body']) > MAX_BODY:
        raise AssetError(f'the text is longer than {MAX_BODY} characters')
    if not out['body']:
        raise AssetError('the text is empty')
    if out['kind'] == 'skill' and not out['description']:
        raise AssetError('a skill needs a description: the agent picks skills by it')
    if out['kind'] == 'prompt':
        out['when'] = ''
    out['enabled'] = True if out['enabled'] is None else bool(out['enabled'])
    return out


def list_items(user, kind=None):
    """The user's own prompts and skills (not the ones a plugin installed), newest first."""
    _need(user)
    q = 'SELECT * FROM app.llm_user_prompts WHERE owner_id = :o AND plugin_id IS NULL'
    args = {'o': user['id']}
    if kind:
        q += ' AND kind = :k'
        args['k'] = kind
    with db.connect() as c:
        rows = c.execute(text(q + ' ORDER BY kind, lower(name), created_at'), args).all()
    return [_item(r) for r in rows]


def _get(c, user, iid):
    r = c.execute(text('SELECT * FROM app.llm_user_prompts WHERE id = :i AND owner_id = :o AND plugin_id IS NULL'),
                  {'i': str(iid), 'o': user['id']}).first()
    if r is None:
        raise NotFound(f'no item {iid!r}')
    return r


def create_item(user, body):
    _need(user)
    v = _clean_item(body)
    with db.begin() as c:
        c.execute(text('SELECT pg_advisory_xact_lock(hashtext(:k))'), {'k': f'llm_assets:{user["id"]}'})
        n = c.execute(text("""SELECT count(*) FROM app.llm_user_prompts WHERE owner_id = :o AND kind = :k
                              AND plugin_id IS NULL"""), {'o': user['id'], 'k': v['kind']}).scalar()
        if n >= MAX_ITEMS:
            raise AssetError(f'you already have {MAX_ITEMS} {v["kind"]}s: delete one first')
        iid = uuid.uuid4().hex
        c.execute(text("""INSERT INTO app.llm_user_prompts(id, owner_id, kind, role, name, description, body, enabled)
                          VALUES (:i, :o, :k, :r, :n, :d, :b, :e)"""),
                  {'i': iid, 'o': user['id'], 'k': v['kind'], 'r': v['role'], 'n': v['name'], 'd': v['description'],
                   'b': _join_when(v['when'], v['body']), 'e': v['enabled']})
        return _item(_get(c, user, iid))


def update_item(user, iid, body):
    _need(user)
    with db.begin() as c:
        old = _item(_get(c, user, iid))
        v = _clean_item(body, old)
        c.execute(text("""UPDATE app.llm_user_prompts SET role = :r, name = :n, description = :d, body = :b,
                          enabled = :e, updated_at = now() WHERE id = :i AND owner_id = :o"""),
                  {'i': old['id'], 'o': user['id'], 'r': v['role'], 'n': v['name'], 'd': v['description'],
                   'b': _join_when(v['when'], v['body']), 'e': v['enabled']})
        return _item(_get(c, user, iid))


def delete_item(user, iid):
    _need(user)
    with db.begin() as c:
        n = c.execute(text('DELETE FROM app.llm_user_prompts WHERE id = :i AND owner_id = :o AND plugin_id IS NULL'),
                      {'i': str(iid), 'o': user['id']}).rowcount
    if not n:
        raise NotFound(f'no item {iid!r}')


# ---------------------------------------------------------------- plugin manifests
def _str(v, n, what, need=False):
    s = str(v or '').strip() if not isinstance(v, (dict, list)) else ''
    if need and not s:
        raise AssetError(f'{what} is missing')
    if len(s) > n:
        raise AssetError(f'{what} is longer than {n} characters')
    return s


def _role_of(v, where):
    v = v or 'all'
    if v not in ROLES:
        raise AssetError(f'{where}: role must be data, visual or all')
    return v


def parse_manifest(m, admin=False):
    """A plugin manifest checked and normalised -> clean dict (AssetError with what is wrong)."""
    from .tools import REGISTRY
    from .tools.http import PluginError, ROLES as TOOL_ROLES, RISKS, clean_http, clean_params
    if not isinstance(m, dict):
        raise AssetError('the manifest must be a JSON object')
    name = _str(m.get('name'), 40, 'name', need=True)
    if not PLUGIN_NAME.match(name) or '__' in name:
        raise AssetError('name must be lowercase letters, digits and _ (starting with a letter, at most 40, no "__")')
    out = {'name': name, 'version': _str(m.get('version'), 40, 'version') or '0',
           'description': _str(m.get('description'), MAX_DESC, 'description')}
    secrets = m.get('secrets') or []
    if not isinstance(secrets, list) or len(secrets) > MAX_SECRETS \
            or not all(isinstance(s, str) and SECRET_NAME.match(s) for s in secrets):
        raise AssetError(f'secrets must be a list of at most {MAX_SECRETS} names (letters, digits, _)')
    out['secrets'] = list(dict.fromkeys(secrets))
    for key, kind in (('skills', 'skill'), ('prompts', 'prompt')):
        items = m.get(key) or []
        if not isinstance(items, list) or len(items) > MAX_PLUGIN_ITEMS:
            raise AssetError(f'{key} must be a list of at most {MAX_PLUGIN_ITEMS}')
        clean = []
        for i, s in enumerate(items):
            where = f'{key}[{i}]'
            if not isinstance(s, dict):
                raise AssetError(f'{where} must be an object')
            try:
                v = _clean_item({'kind': kind, 'role': _role_of(s.get('role'), where), 'name': s.get('name'),
                                 'description': s.get('description') or ('' if kind == 'prompt' else None),
                                 'when': s.get('when'), 'body': s.get('body')})
            except AssetError as e:
                raise AssetError(f'{where}: {e}') from None
            clean.append({k: v[k] for k in ('name', 'role', 'description', 'when', 'body')})
        out[key] = clean
    tools = m.get('tools') or []
    if not isinstance(tools, list):
        raise AssetError('tools must be a list')
    if len(tools) > MAX_TOOLS:
        raise AssetError(f'a plugin has at most {MAX_TOOLS} tools ({len(tools)} given)')
    seen, clean_tools = set(), []
    for i, t in enumerate(tools):
        where = f'tools[{i}]'
        if not isinstance(t, dict):
            raise AssetError(f'{where} must be an object')
        tn = _str(t.get('name'), 64, f'{where}.name', need=True)
        full = tn if tn.startswith(f'{name}__') else f'{name}__{tn}'
        if not TOOL_NAME.match(tn) or not TOOL_NAME.match(full):
            raise AssetError(f'{where}: tool names are lowercase letters, digits and _ (at most 64 with the '
                             f'"{name}__" prefix)')
        if full in seen or full in REGISTRY:
            raise AssetError(f'{where}: the tool name {full} is used twice')
        seen.add(full)
        risk = t.get('risk') or 'read'
        if risk not in RISKS:
            raise AssetError(f'{where}: risk must be read, change or danger')
        roles = t.get('roles', t.get('role'))
        if roles in (None, '', 'all', [], ['all']):
            roles = list(TOOL_ROLES)
        elif isinstance(roles, str):
            roles = [roles]
        if not isinstance(roles, list) or any(r not in TOOL_ROLES for r in roles):
            raise AssetError(f'{where}: roles must be bucket and / or visual')
        desc = _str(t.get('description'), MAX_DESC, f'{where}.description', need=True)
        try:
            params = clean_params(t.get('parameters'), where)
            http = clean_http(t.get('http'), params, out['secrets'], admin=admin, where=where)
        except PluginError as e:
            raise AssetError(str(e)) from None
        clean_tools.append({'name': full, 'description': desc, 'parameters': params, 'risk': risk,
                            'roles': list(dict.fromkeys(roles)), 'http': http})
    out['tools'] = clean_tools
    if not (clean_tools or out['skills'] or out['prompts']):
        raise AssetError('the plugin has no tools, skills or prompts')
    return out


def read_package(data):
    """A manifest from what was uploaded: a dict, JSON text / bytes, or a .zip with plugin.json (+ skills/*/SKILL.md,
    each with name / description / when / role front matter)."""
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        data = data.encode('utf-8')
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise AssetError('send a plugin.json or a .zip')
    if len(data) > MAX_ZIP:
        raise AssetError('the plugin file is larger than 2 MB')
    if not data.startswith(b'PK'):
        try:
            return json.loads(bytes(data).decode('utf-8-sig'))
        except (ValueError, UnicodeDecodeError) as e:
            raise AssetError(f'the manifest is not valid JSON: {e}') from None
    from .prompting import front
    try:
        z = zipfile.ZipFile(io.BytesIO(bytes(data)))
    except zipfile.BadZipFile:
        raise AssetError('not a valid .zip file') from None
    infos = [i for i in z.infolist() if not i.is_dir()]
    if len(infos) > 200 or sum(i.file_size for i in infos) > MAX_ZIP:
        raise AssetError('the zip is too large (at most 200 files, 2 MB unpacked)')
    names = {i.filename.replace('\\', '/'): i for i in infos}
    root = next((n for n in sorted(names, key=len) if n.rsplit('/', 1)[-1] == 'plugin.json' and n.count('/') <= 1), None)
    if root is None:
        raise AssetError('the zip has no plugin.json')
    prefix = root[:-len('plugin.json')]
    try:
        m = json.loads(z.read(names[root]).decode('utf-8-sig'))
    except (ValueError, UnicodeDecodeError) as e:
        raise AssetError(f'plugin.json is not valid JSON: {e}') from None
    if not isinstance(m, dict):
        raise AssetError('plugin.json must be a JSON object')
    skills = list(m.get('skills') or []) if isinstance(m.get('skills'), list) else []
    for n in sorted(names):
        rel = n[len(prefix):] if n.startswith(prefix) else None
        parts = rel.split('/') if rel else []
        if len(parts) == 3 and parts[0] == 'skills' and parts[2] == 'SKILL.md':
            try:
                meta, body = front(z.read(names[n]).decode('utf-8-sig'))
            except UnicodeDecodeError:
                raise AssetError(f'{n} is not UTF-8 text') from None
            skills.append({'name': meta.get('name') or parts[1], 'description': meta.get('description') or '',
                           'when': meta.get('when') or '', 'role': meta.get('role') or 'all', 'body': body.strip()})
    m['skills'] = skills
    return m


# ---------------------------------------------------------------- plugins
def _secret_dict(raw):
    from ..db import crypto
    return dict(crypto.decrypt_json(raw) or {}) if raw else {}


def _public_plugin(r, rows=()):
    m = dict(r.manifest or {})
    have = _secret_dict(r.secrets)
    mine = [x for x in rows if x.plugin_id == r.id]
    return {'id': r.id, 'name': r.name, 'version': r.version, 'description': m.get('description') or '',
            'enabled': bool(r.enabled), 'installed_at': _ts(r.installed_at),
            'tools': [{'name': t['name'], 'description': t['description'], 'risk': t['risk'], 'roles': t['roles'],
                       'method': t['http']['method'], 'url': t['http']['url']} for t in m.get('tools') or []],
            'skills': [{'id': x.id, 'name': x.name, 'role': x.role, 'description': x.description or '',
                        'enabled': bool(x.enabled)} for x in mine if x.kind == 'skill'],
            'prompts': [{'id': x.id, 'name': x.name, 'role': x.role, 'enabled': bool(x.enabled)}
                        for x in mine if x.kind == 'prompt'],
            'secrets': [{'name': s, 'set': bool(have.get(s))} for s in m.get('secrets') or []]}


def list_plugins(user):
    _need(user)
    with db.connect() as c:
        rows = c.execute(text('SELECT * FROM app.llm_plugins WHERE owner_id = :o ORDER BY lower(name)'),
                         {'o': user['id']}).all()
        items = c.execute(text("""SELECT id, plugin_id, kind, name, role, description, enabled FROM app.llm_user_prompts
                                  WHERE owner_id = :o AND plugin_id IS NOT NULL ORDER BY lower(name)"""),
                          {'o': user['id']}).all()
    return [_public_plugin(r, items) for r in rows]


def _plugin_row(c, user, pid):
    r = c.execute(text('SELECT * FROM app.llm_plugins WHERE id = :i AND owner_id = :o'),
                  {'i': str(pid), 'o': user['id']}).first()
    if r is None:
        raise NotFound(f'no plugin {pid!r}')
    return r


def _one(c, user, pid):
    r = _plugin_row(c, user, pid)
    items = c.execute(text("""SELECT id, plugin_id, kind, name, role, description, enabled FROM app.llm_user_prompts
                              WHERE owner_id = :o AND plugin_id = :p ORDER BY lower(name)"""),
                      {'o': user['id'], 'p': r.id}).all()
    return _public_plugin(r, items)


def install(user, data):
    """Install (or upgrade, by name) a plugin from a manifest dict, JSON text or a .zip -> its public view.
    An upgrade keeps the enabled switch and the secrets the new version still declares."""
    from ..db import crypto
    _need(user)
    m = parse_manifest(read_package(data), admin=bool(user.get('is_admin')))
    with db.begin() as c:
        c.execute(text('SELECT pg_advisory_xact_lock(hashtext(:k))'), {'k': f'llm_plugins:{user["id"]}'})
        old = c.execute(text('SELECT * FROM app.llm_plugins WHERE owner_id = :o AND lower(name) = lower(:n)'),
                        {'o': user['id'], 'n': m['name']}).first()
        if old is None:
            n = c.execute(text('SELECT count(*) FROM app.llm_plugins WHERE owner_id = :o'), {'o': user['id']}).scalar()
            if n >= MAX_PLUGINS:
                raise AssetError(f'you already have {MAX_PLUGINS} plugins: remove one first')
            pid = uuid.uuid4().hex
            c.execute(text("""INSERT INTO app.llm_plugins(id, owner_id, name, version, manifest)
                              VALUES (:i, :o, :n, :v, :m)""").bindparams(_J),
                      {'i': pid, 'o': user['id'], 'n': m['name'], 'v': m['version'], 'm': m})
        else:
            pid = old.id
            keep = {k: v for k, v in _secret_dict(old.secrets).items() if k in m['secrets']}
            c.execute(text("""UPDATE app.llm_plugins SET name = :n, version = :v, manifest = :m, secrets = :s
                              WHERE id = :i""").bindparams(_J),
                      {'i': pid, 'n': m['name'], 'v': m['version'], 'm': m, 's': crypto.encrypt_json(keep)})
            c.execute(text('DELETE FROM app.llm_user_prompts WHERE plugin_id = :p'), {'p': pid})
        for kind, key in (('skill', 'skills'), ('prompt', 'prompts')):
            for s in m[key]:
                c.execute(text("""INSERT INTO app.llm_user_prompts(id, owner_id, kind, role, name, description, body,
                                  plugin_id) VALUES (:i, :o, :k, :r, :n, :d, :b, :p)"""),
                          {'i': uuid.uuid4().hex, 'o': user['id'], 'k': kind, 'r': s['role'], 'n': s['name'],
                           'd': s['description'], 'b': _join_when(s['when'], s['body']), 'p': pid})
        return _one(c, user, pid)


def update_plugin(user, pid, body):
    """{enabled?, secrets?: {NAME: value ('' removes it)}} -> its public view (secret values never come back)."""
    from ..db import crypto
    _need(user)
    if not isinstance(body, dict):
        raise AssetError('send an object')
    with db.begin() as c:
        r = _plugin_row(c, user, pid)
        if 'enabled' in body:
            c.execute(text('UPDATE app.llm_plugins SET enabled = :e WHERE id = :i'), {'e': bool(body['enabled']), 'i': r.id})
        if body.get('secrets') is not None:
            if not isinstance(body['secrets'], dict):
                raise AssetError('secrets must be an object {NAME: value}')
            declared = set((r.manifest or {}).get('secrets') or [])
            cur = _secret_dict(r.secrets)
            for k, v in body['secrets'].items():
                if k not in declared:
                    raise AssetError(f'{k} is not a secret of this plugin')
                v = str(v or '').strip()
                if len(v) > 4000:
                    raise AssetError(f'{k} is too long')
                if v:
                    cur[k] = v
                else:
                    cur.pop(k, None)
            c.execute(text('UPDATE app.llm_plugins SET secrets = :s WHERE id = :i'),
                      {'s': crypto.encrypt_json(cur), 'i': r.id})
        return _one(c, user, pid)


def uninstall(user, pid):
    _need(user)
    with db.begin() as c:
        n = c.execute(text('DELETE FROM app.llm_plugins WHERE id = :i AND owner_id = :o'),
                      {'i': str(pid), 'o': user['id']}).rowcount
    if not n:
        raise NotFound(f'no plugin {pid!r}')


def plugin_secrets(user, pid):
    """The decrypted secrets of one of the user's plugins (server side only: the HTTP tools)."""
    with db.connect() as c:
        raw = c.execute(text('SELECT secrets FROM app.llm_plugins WHERE id = :i AND owner_id = :o'),
                        {'i': pid, 'o': user['id']}).scalar()
    return _secret_dict(raw)


# ---------------------------------------------------------------- what one run gets
def for_run(user, role):
    """The enabled prompts, skills and plugin tools of `user` for one assistant ('bucket' | 'visual' | 'data'):
    {'prompts': [{name, body, role}], 'skills': [{name, description, when, body, source}], 'tools': [Tool]}.
    Never raises (no database, a broken row): the run goes on without them, and the log says why."""
    out = {'prompts': [], 'skills': [], 'tools': []}
    if not user or not user.get('id'):
        return out
    try:
        from .tools import http
        with db.connect() as c:
            plugins = c.execute(text('SELECT id, name, manifest, enabled FROM app.llm_plugins WHERE owner_id = :o'),
                                {'o': user['id']}).all()
            rows = c.execute(text("""SELECT kind, role, name, description, body, plugin_id FROM app.llm_user_prompts
                                     WHERE owner_id = :o AND enabled AND role IN (:r, 'all')
                                     ORDER BY plugin_id NULLS FIRST, kind, lower(name)"""),
                             {'o': user['id'], 'r': role}).all()
        on = {p.id: p for p in plugins if p.enabled}
        names = {p.id: p.name for p in plugins}
        for r in rows:
            if r.plugin_id and r.plugin_id not in on:
                continue
            if r.kind == 'prompt':
                out['prompts'].append({'name': r.name, 'body': r.body, 'role': r.role})
            else:
                when, body = _split_when(r.body)
                out['skills'].append({'name': r.name, 'description': r.description or '', 'when': when, 'body': body,
                                      'source': f'plugin {names[r.plugin_id]}' if r.plugin_id else 'yours'})
        for p in on.values():
            def secrets(_pid=p.id, _cache={}):
                if _pid not in _cache:
                    _cache[_pid] = plugin_secrets(user, _pid)
                return _cache[_pid]
            for t in http.build({'id': p.id, 'name': p.name, 'manifest': p.manifest}, user, secrets):
                if role in t.roles:
                    out['tools'].append(t)
    except Exception as e:  # noqa: BLE001 - the assistant still answers without the user's additions
        _log.warning(f'llm assets for {user.get("id")}: {type(e).__name__}: {vlog.esc(str(e))}')
        return {'prompts': [], 'skills': [], 'tools': []}
    return out
