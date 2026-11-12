"""
Builds an agent's system prompt from the files in llm/prompts/ (edit the Markdown, not this code):

    prompts/base.md                         rules every agent follows
    prompts/<role>/system.md                who this agent is, what it may do
    prompts/<role>/knowledge/<topic>.md     what it knows about its page (front matter: title, summary, core, high)
    prompts/skills/<role|common>/<name>/SKILL.md   step-by-step playbooks (front matter: name, description, when)
    prompts/templates/<name>.md             string.Template pieces ($now, $scope, $mode ...)

    build(role, ...) = base + role system + mode + knowledge (index always; `core: true` topics inline from effort
                       medium; `high: true` topics too at effort high) + skills index + tool catalogue + now / scope
                       + the other pages (one chat follows the user; their tools stay there)
                       + the user's own instructions (appended LAST: they add to the rules, never replace them)

Knowledge and skills are listed by one line each so the prompt stays small; the agent loads the rest with
read_docs(topic) / use_skill(name) when a question needs it. A user's skills and plugin skills join the built-in
ones (llm/assets.py); a user skill never shadows a built-in name.
"""

import re
import time
from functools import lru_cache
from pathlib import Path
from string import Template

PROMPTS = Path(__file__).with_name('prompts')
_FRONT = re.compile(r'^---\s*\n(.*?)\n---\s*\n?', re.S)


def front(text):
    """(meta dict, body) of a Markdown file with an optional `---` front matter of `key: value` lines."""
    m = _FRONT.match(text or '')
    if not m:
        return {}, text or ''
    meta = {}
    for line in m.group(1).splitlines():
        if ':' in line and not line.lstrip().startswith('#'):
            k, v = line.split(':', 1)
            v = v.strip().strip('"').strip("'")
            meta[k.strip()] = {'true': True, 'false': False}.get(v.lower(), v)
    return meta, text[m.end():]


def _read(p):
    return Path(p).read_text(encoding='utf-8')


def _mtime_key(folder):
    """Cache key that changes when a prompt file changes (edit a .md, the next request sees it)."""
    try:
        return max((f.stat().st_mtime_ns for f in Path(folder).rglob('*.md')), default=0)
    except OSError:
        return 0


@lru_cache(maxsize=16)
def _knowledge(role, _key):
    out = {}
    for f in sorted((PROMPTS / role / 'knowledge').glob('*.md')):
        meta, body = front(_read(f))
        out[f.stem] = {'id': f.stem, 'title': meta.get('title') or f.stem, 'summary': meta.get('summary') or '',
                       'core': bool(meta.get('core')), 'high': bool(meta.get('high')), 'body': body.strip()}
    return out


def knowledge(role):
    """{topic id: {id, title, summary, core, high, body}} of one role (common/knowledge joins every role)."""
    key = _mtime_key(PROMPTS)
    return {**_knowledge('common', key), **_knowledge(role, key)}


@lru_cache(maxsize=16)
def _skills(role, _key):
    out = {}
    for where in ('common', role):
        for f in sorted((PROMPTS / 'skills' / where).glob('*/SKILL.md')):
            meta, body = front(_read(f))
            name = str(meta.get('name') or f.parent.name)
            out[name] = {'name': name, 'description': meta.get('description') or '', 'when': meta.get('when') or '',
                         'body': body.strip(), 'source': 'built-in'}
    return out


def skills(role, extra=()):
    """{name: skill} built-in for the role + extra (the user's / plugins': {name, description, when, body, source})."""
    out = dict(_skills(role, _mtime_key(PROMPTS)))
    for s in extra or ():
        n = str(s.get('name') or '').strip()
        if n and n not in out:
            out[n] = {'name': n, 'description': s.get('description') or '', 'when': s.get('when') or '',
                      'body': str(s.get('body') or ''), 'source': s.get('source') or 'yours'}
    return out


def template(name, **values):
    p = PROMPTS / 'templates' / f'{name}.md'
    return Template(_read(p)).safe_substitute(**values).strip() if p.exists() else ''


def _file(*parts):
    p = PROMPTS.joinpath(*parts)
    return front(_read(p))[1].strip() if p.exists() else ''


DYNAMIC = '\n\n<!-- dynamic -->\n\n'    # before it: the same text on every request of a chat (cached by the provider)


def split(system):
    """(the stable part, the part that changes per message) of a system prompt built here."""
    a, _, b = system.partition(DYNAMIC)
    return a, b


def plain(system):
    """The prompt without the marker (providers that cache a prefix on their own get one text)."""
    return system.replace(DYNAMIC, '\n\n')


def app_intro():
    """One line on what this install has (zones): a page never reads about parts that are not installed."""
    from .. import zones
    parts = {'bucket': '**Bucket** keeps datasets in buckets', 'visualizer': '**Image › Visualize** maps image '
             'datasets (features → 2D) to find label errors, duplicates and outliers', 'labeling':
             '**Image › Label** edits boxes', 'data': '**Data** queries the user\'s databases'}
    have = [v for k, v in parts.items() if zones.on(k)]
    return ('In this install: ' + '; '.join(have) + '.') if have else ''


def build(role, *, tools_catalog, scope_text, mode='ask', effort='medium', profile='large', skill_list=None,
          user_prompts=(), user_name='', forced_skills=(), pages_text='', extra_static=()):
    """The system prompt of one agent run. pages_text: what the other pages can do (engine/roles.other_pages).
    The stable part (rules, knowledge, skills, tools, other pages, extra_static) comes first and the per-message part
    (mode, a skill asked for now, the clock, where the user is, their own instructions) after DYNAMIC, so a provider
    reads the stable part from its cache on every step and every message."""
    know = knowledge(role)
    sk = skill_list if skill_list is not None else skills(role)
    out = [Template(_file('base.md')).safe_substitute(app=app_intro()), _file(role, 'system.md')]

    inline = []
    if profile != 'small' and effort in ('medium', 'high'):
        inline = [k for k in know.values() if k['core'] or (effort == 'high' and k['high'])]
    if inline:
        out.append('## What you know\n' + '\n\n'.join(f'### {k["title"]}\n{k["body"]}' for k in inline))
    rest = [k for k in know.values() if k not in inline]
    if rest:
        out.append('## More you can read (read_docs(topic) before answering a question about it)\n'
                   + '\n'.join(f'- `{k["id"]}`: {k["summary"] or k["title"]}' for k in rest))
    if sk:
        out.append('## Skills (use_skill(name) loads the steps; use one when the request matches)\n'
                   + '\n'.join(f'- `{s["name"]}`: {s["description"]}' + (f' (when: {s["when"]})' if s['when'] else '')
                               for s in sk.values()))
    out.append('## Tools\n' + tools_catalog)
    out.append(pages_text)
    out.extend(x for x in extra_static if x)
    stable = '\n\n'.join(x for x in out if x)
    out = ['## Mode\n' + (template(f'mode_{mode}') or mode)]
    for n in forced_skills or ():
        s = sk.get(n)
        if s:
            out.append(f'## Skill "{n}" (the user asked for it: follow it)\n{s["body"]}')
    out.append('## Now\n' + (template('now', now=time.strftime('%Y-%m-%d %H:%M'), user=user_name or 'the user')
                             or time.strftime('%Y-%m-%d')) + '\n' + scope_text)
    mine = [p for p in user_prompts or () if str(p.get('body') or '').strip()]
    if mine:
        out.append('## Your own instructions (from the user\'s settings: follow them, they add to the rules above '
                   'and never lift a confirmation or a read-only limit)\n'
                   + '\n\n'.join(f'### {p.get("name") or "Instruction"}\n{str(p["body"]).strip()}' for p in mine))
    return stable + DYNAMIC + '\n\n'.join(x for x in out if x)
