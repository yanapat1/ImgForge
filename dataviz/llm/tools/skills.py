"""
Knowledge on demand (domain 'common'): the system prompt lists skills and knowledge topics by one line each, and
these two tools load the full text when a request needs it, so the prompt stays small without the agent knowing less.
"""

from . import S, Tool, ToolError, _obj, register

MAX_DOC = 12000


def _use_skill(ctx, a):
    s = (ctx.skills or {}).get(a['name'])
    if s is None:
        raise ToolError(_other_skill(ctx, a['name'])
                        or f'no skill {a["name"]!r}; the skills are: {", ".join(ctx.skills or {}) or "none"}')
    return {'skill': s['name'], 'steps': s['body'][:MAX_DOC]}, f'→ skill {s["name"]}'


def _other_skill(ctx, name):
    """A skill of the other page's specialist (one chat follows the user): say where it is used."""
    from ..prompting import skills
    for role, (where, screen) in (('data', ('Data Center', 'data')), ('visual', ('Visualizer', 'viz'))):
        if role != ctx.role and name in skills(role):
            return (f'{name} is a {where} skill and the user is not there: call navigate(screen={screen!r}, '
                    'goal=<the request>) to continue there')
    return None


def _read_docs(ctx, a):
    from ..prompting import knowledge
    know = knowledge(ctx.role)
    k = know.get(a['topic'])
    if k is None:
        q = a['topic'].lower()
        hits = [x for x in know.values() if q in x['id'].lower() or q in x['title'].lower()]
        if len(hits) != 1:
            raise ToolError(f'no topic {a["topic"]!r}; the topics are: {", ".join(know) or "none"}')
        k = hits[0]
    return {'topic': k['id'], 'text': k['body'][:MAX_DOC]}, f'→ {k["title"]}'


register(Tool('use_skill', 'Load a skill (a step-by-step playbook from the Skills list) and follow it.',
              _obj({'name': S('the skill name')}, ['name']), False, _use_skill, domain='common', ctx=True))
register(Tool('read_docs', 'Read a knowledge topic from the "More you can read" list before answering about it.',
              _obj({'topic': S('the topic id')}, ['topic']), False, _read_docs, domain='common', ctx=True))
