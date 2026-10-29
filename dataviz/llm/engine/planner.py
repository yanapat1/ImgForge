"""
Mode plan: the agent gets only read-only tools plus submit_plan, researches, and ends its turn with a
'plan_proposal' card (summary + steps). Approve sends the plan back as the next user message in mode ask or
auto, where an executor run carries it out (dangerous steps are still confirmed one card at a time).
"""


def approved_message(summary, steps, note=''):
    """The user message an approved plan becomes (the page sends it; the history keeps it readable)."""
    lines = [f'Approved plan: {summary}'.strip(), 'Carry it out now, step by step, with the real names you found:']
    lines += [f'{k}. {s}' for k, s in enumerate(steps or [], start=1)]
    if note:
        lines.append(f'Change to the plan: {note}')
    return '\n'.join(lines)
