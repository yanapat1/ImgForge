"""
`python -m dataviz dc <tool> --args '{json}' --user <username>`: run one Data Center tool (llm/tools.py, the same
registry the in-app agent uses) as a user and print its JSON result. Changes (move, delete, tag, send ...) need
--yes; without it the CLI prints what it would do and exits 3, like a plan the user has not confirmed yet.
`python -m dataviz dc list` prints the tool catalogue. Exit codes: 0 ok, 1 tool / service error, 2 usage, 3 not
confirmed. It connects to the app's database (config.yaml / DATABASE_URL) without migrating it.
"""

import argparse
import json
import sys

from .core import config as vconfig


def _connect(cfg):
    from . import db
    from .db import crypto
    url = str((cfg.get('database') or {}).get('url') or '').strip()
    if not url or url.startswith('${'):
        sys.exit('No database configured: set DATABASE_URL (or database.url in the config)')
    db.init(url, pool_size=2)
    crypto.init(key_file=vconfig.resolve(cfg['dataset']['workspace_root']) / 'secret.key')


def _user(username):
    from .auth import users
    r = users.by_login(username)
    if r is None or not r.get('is_active', True):
        sys.exit(f'no active user {username!r}')
    return users.public(r)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m dataviz dc', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('tool', help='a tool name, or "list" for the catalogue')
    ap.add_argument('--args', default='{}', help='the tool arguments as a JSON object')
    ap.add_argument('--user', help='act as this username (or email)')
    ap.add_argument('--yes', action='store_true', help='really run a tool that changes data')
    ap.add_argument('--config', default=str(vconfig.DEFAULT_CONFIG), help='YAML settings file')
    a = ap.parse_args(argv)
    from .llm import tools
    if a.tool == 'list':
        print(tools.catalog())
        return 0
    t = tools.TOOLS.get(a.tool)
    if t is None or t.control:
        print(f'unknown tool {a.tool!r}; tools: {", ".join(k for k, v in tools.TOOLS.items() if not v.control)}',
              file=sys.stderr)
        return 2
    try:
        args = json.loads(a.args)
    except ValueError as e:
        print(f'--args is not JSON: {e}', file=sys.stderr)
        return 2
    if not a.user:
        print('--user is required', file=sys.stderr)
        return 2
    cfg = vconfig.load_config(a.config)
    _connect(cfg)
    from .datacenter import service
    if hasattr(service, 'configure'):
        service.configure(cfg)
    user = _user(a.user)
    from .auth import context
    token = context.USER.set(user)          # anything that reads the signed-in user sees this one
    try:
        if t.mutating:
            clean, summary = tools.prepare(user, a.tool, args)
            if not a.yes:
                print(json.dumps({'ok': False, 'confirm': summary, 'args': clean,
                                  'hint': 'run again with --yes to do it'}, indent=2, ensure_ascii=False))
                return 3
            args = clean
        result, summary = tools.run(user, a.tool, args)
    except Exception as e:  # noqa: BLE001 - printed as JSON for scripts
        print(json.dumps({'ok': False, 'error': str(e), 'status': getattr(e, 'status', None)}, ensure_ascii=False))
        return 1
    finally:
        context.USER.reset(token)
    print(json.dumps({'ok': True, 'summary': summary, 'result': result}, indent=2, ensure_ascii=False, default=str))
    return 0
