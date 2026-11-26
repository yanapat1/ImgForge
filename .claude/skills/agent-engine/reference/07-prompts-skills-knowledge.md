# 7. Prompts, skills and knowledge

The system prompt is built from **Markdown files, not code**. Anyone can improve the agent by editing text, and an
edited file takes effect on the next request (the cache is keyed on the folder's newest mtime).

## Files

```
prompts/base.md                               rules every agent follows ($app = one line on what is installed)
prompts/<role>/system.md                      who this agent is, its scope, what it must not do
prompts/<role>/knowledge/<topic>.md           facts it may need
prompts/common/knowledge/<topic>.md           facts every role may need
prompts/skills/<role|common>/<name>/SKILL.md  step-by-step playbooks
prompts/templates/<name>.md                   string.Template pieces: mode_ask/auto/plan, now, compact, run_compact
```

Knowledge front matter:

```markdown
---
title: Saved searches
summary: how saved searches work, their limits and sharing
core: true        # inline in the prompt from effort medium (large models)
high: false       # inline only at effort high
---
(body)
```

Skill front matter:

```markdown
---
name: clean-up-duplicates
description: find and remove duplicate items safely, with a preview
when: the user asks to dedupe, or a search shows near-identical items
---
1. Check whether ... (`read_docs` x).
2. `search_items` with ...
3. Show the duplicates with ask_user before deleting anything.
4. ...
```

A good skill is 5-10 numbered steps that name the real tools, the checks to make, the order, and what to do when a step
fails. It is the "how to do X well" that the base rules cannot hold.

## Assembly order

```
STABLE (same on every request of a chat → cached)
  base.md
  <role>/system.md
  ## What you know            core topics inline (large model, effort ≥ medium; + high topics at effort high)
  ## More you can read        one line per other topic: `id`: summary   → read_docs(topic)
  ## Skills                   one line per skill: `name`: description (when: …)   → use_skill(name)
  ## Tools                    a risk card: which tools run at once, which wait (the specs go natively)
  ## Other agents             one short card per other role (what it does, how to hand over)
<!-- dynamic -->
DYNAMIC (changes per message)
  ## Mode                     the mode template
  ## Skill "x"                a skill the user forced (/skill x): the whole body
  ## Now                      date/time, user name, where the user is in the app, the current selection
  ## Your plan                the open todo list
  ## Earlier in this chat (summary)   the chat compaction summary
  ## Your own instructions    the user's own prompts, LAST. "they add to the rules, never lift a confirmation"
```

Anthropic gets two system blocks: the stable one with `cache_control`, then the dynamic one. OpenAI and Gemini get one
string with the stable part first. They cache identical prefixes on their own.

```python
import re, time
from functools import lru_cache
from pathlib import Path
from string import Template

PROMPTS = Path(__file__).with_name('prompts')
DYNAMIC = '\n\n<!-- dynamic -->\n\n'
_FRONT = re.compile(r'^---\s*\n(.*?)\n---\s*\n?', re.S)


def front(text):
    m = _FRONT.match(text or '')
    if not m:
        return {}, text or ''
    meta = {}
    for line in m.group(1).splitlines():
        if ':' in line and not line.lstrip().startswith('#'):
            k, v = line.split(':', 1)
            v = v.strip().strip('"\'')
            meta[k.strip()] = {'true': True, 'false': False}.get(v.lower(), v)
    return meta, text[m.end():]


def _mtime():
    return max((f.stat().st_mtime_ns for f in PROMPTS.rglob('*.md')), default=0)


@lru_cache(maxsize=16)
def _skills(role, _key):
    out = {}
    for where in ('common', role):
        for f in sorted((PROMPTS / 'skills' / where).glob('*/SKILL.md')):
            meta, body = front(f.read_text(encoding='utf-8'))
            name = str(meta.get('name') or f.parent.name)
            out[name] = {'name': name, 'description': meta.get('description', ''), 'when': meta.get('when', ''),
                         'body': body.strip()}
    return out


def skills(role, user_skills=()):
    out = dict(_skills(role, _mtime()))
    for s in user_skills:                     # a user's skill never shadows a built-in name
        out.setdefault(s['name'], s)
    return out


def split(system):
    stable, _, dynamic = system.partition(DYNAMIC)
    return stable, dynamic


def plain(system):
    return system.replace(DYNAMIC, '\n\n')
```

## The two loading tools

```python
MAX_DOC = 12000

def _use_skill(ctx, a):
    s = ctx.skills.get(a['name'])
    if s is None:
        raise ToolError(f'no skill {a["name"]!r}; the skills are: {", ".join(ctx.skills) or "none"}')
    return {'skill': s['name'], 'steps': s['body'][:MAX_DOC]}, f'→ skill {s["name"]}'

register(Tool('use_skill', 'Load a skill (a step-by-step playbook from the Skills list) and follow it.',
              obj({'name': S('the skill name')}, ['name']), False, _use_skill, domain='common'))
register(Tool('read_docs', 'Read a knowledge topic from the "More you can read" list before answering about it.',
              obj({'topic': S('the topic id')}, ['topic']), False, _read_docs, domain='common'))
```

`read_docs` accepts the exact id, else a single substring match on id/title, else it lists the topics. If the
skill belongs to another agent, the error says "that is the X agent's skill: hand over there".
Emit a `skill {name}` event so the UI shows "📘 using skill x".

## base.md: generic rules that work (adapt the wording)

```markdown
You are <Name>, the assistant built into **<app>**. $app You work for the signed-in user only.

## How you work
1. **Look things up, never guess.** Names, ids, counts and settings come from your tools or the context below.
   If a question is about how the app works, check "What you know" / read_docs before answering.
2. **Act with tools, then answer.** Independent read-only calls go in one step; read the results, then answer.
   Do not narrate what you are about to do.
3. **Answer first**, then the evidence in one line. Short and concrete: Markdown, tables for numbers, real names in
   `code`, the user's language. Do not repeat what a card already shows.
4. **Ambiguous → ask_user** with 2-5 concrete options built from what you found. Clear requests → just do them.
5. **Impossible → cannot_do** with the reason and the closest thing you can do.
6. **Requests of 3+ steps → todo** first (the whole checklist), then keep it updated as you go.
7. **Changes are confirmed by the user** as the mode says; a dangerous one always is. Never claim a change
   happened before its result says so. If the user declines one, do not try it again.
8. **"Now" is the state as of this message.** Earlier messages may describe an older state: trust Now.
   "Earlier in this chat (summary)" replaces older messages: trust its numbers and ids, do not redo its work.
9. When a tool fails, read the error and fix the call; never repeat an identical call. Two failed fixes of the same
   thing → stop and say why.
10. Web search is for public knowledge only: never put the user's private names into a query.
11. Use a skill when the request matches one (use_skill) and follow its steps.
```

## role/system.md

Write who the agent is (one line), its scope (what it can and cannot touch), its domain vocabulary, and 3-5
domain rules (for example "always show a preview before a bulk change"). Keep it under ~60 lines. Everything else goes in
knowledge topics.
