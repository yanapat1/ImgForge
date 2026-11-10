"""
A side run: one read-only question answered by a specialist on the server, inside the user's turn. Its tool calls and
their results stay in its own short run; only the answer comes back, so the main chat's history (sent again on every
step) grows by a few lines instead of every result. `consult` (tools/nav.py) asks the other page's specialist;
`delegate` (tools/agents.py) splits independent look-ups across copies of this page's specialist.

    run_sub(ctx, role, question, ...)  -> {answer, steps, tools, ok, cannot, usage: {in, out}}

It never writes to `ctx`: delegate runs several at once, so the caller adds their usage and events afterwards.
"""

from ..providers import LLMError
from ..tools import ToolError

NO_NESTING = ('consult', 'delegate')     # a side run neither consults nor delegates again


def run_sub(ctx, role, question, *, state=None, scope=None, steps=6, chars=1500, provider=None, label='assistant'):
    """Answer `question` with `role`'s read-only tools (at most `steps` provider calls). ToolError when it fails."""
    from . import react
    p = provider or ctx.provider
    opts = react.options({'mode': 'ask', 'effort': 'low', 'web': 0}, p)
    opts['max_steps'] = min(opts['max_steps'], steps)
    sub = react.prepare(ctx.user, role, p, react.history([{'role': 'user', 'text': question}]), scope or {}, opts,
                        state=state, app=ctx.app, limit=ctx.limit)
    sub.tools = {k: t for k, t in sub.tools.items() if (t.risk == 'read' and k not in NO_NESTING) or k == 'cannot_do'}
    sub.ctx.depth = 1
    sub.ctx.roles_on = getattr(ctx, 'roles_on', None)
    if getattr(ctx, 'cancel', None) is not None:          # the user's Stop ends the side runs too
        sub.cancel = ctx.cancel
        sub.ctx.cancel = ctx.cancel
    texts, used, n, cannot, usage = [], [], 0, None, {'in': 0, 'out': 0}
    try:
        for ev in react.events(sub):
            t = ev.get('type')
            if t == 'step':
                n, texts = ev.get('n', n), []          # only the last step's text is the answer
            elif t == 'text':
                texts.append(str(ev.get('text') or ''))
            elif t == 'tool_start':
                used.append(ev.get('name'))
            elif t == 'cannot':
                cannot = ev.get('reason')
            elif t == 'error':
                raise ToolError(f'the {label} failed: {str(ev.get("detail"))[:300]}')
            elif t == 'done':
                u = ev.get('usage') or {}
                usage = {k: int(u.get(k) or 0) for k in ('in', 'out')}
    except LLMError as e:
        raise ToolError(f'the {label} failed: {str(e)[:300]}') from None
    answer = ''.join(texts).strip() or (f'cannot: {cannot}' if cannot else '(no answer)')
    if len(answer) > chars:
        answer = answer[:chars] + '…'
    return {'answer': answer, 'steps': n, 'tools': sorted({x for x in used if x}), 'ok': not cannot, 'cannot': cannot,
            'usage': usage}
