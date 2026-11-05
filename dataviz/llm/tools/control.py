"""
Control tools (domain 'control'): answered by the user or by the engine itself, never run as an action.

    ask_user / cannot_do   end the turn (an 'ask' / 'cannot' card)
    todo                   the agent's visible checklist for a multi-step request (a 'todo' card, kept up to date);
                           saved on the chat (sessions.set_plan), so it goes on over page changes and compaction
    submit_plan            mode plan only: the researched plan, shown with Approve (llm/engine/planner.py)
"""

from . import A, S, Tool, _obj, register

ITEM = {'type': 'object', 'additionalProperties': False, 'required': ['text', 'state'],
        'properties': {'text': S('one step, short'),
                       'state': {'type': 'string', 'enum': ['pending', 'active', 'done', 'skipped']},
                       'page': {'type': 'string', 'enum': ['home', 'data', 'viz', 'label'],
                                'description': 'where the step happens, when not here'}}}

register(Tool('ask_user', 'Ask the user a clarifying question with 2-5 concrete options instead of guessing. Ends your turn.',
              _obj({'question': S('one short question'), 'options': A('2-5 concrete answers the user can click')},
                   ['question', 'options']), False, control=True, domain='control'))
register(Tool('cannot_do', 'Say the request cannot be done with these tools, why, and the closest thing you can do. '
                           'Ends your turn.',
              _obj({'reason': S('why not, one sentence'), 'alternative': S('the closest thing you can do (optional)')},
                   ['reason']), False, control=True, domain='control'))
register(Tool('todo', 'Show / update your checklist for a request of 3 or more steps: send the WHOLE list each time '
                      '(page = where a step happens; kept across pages). Does not end your turn.',
              _obj({'items': {'type': 'array', 'items': ITEM, 'description': 'the whole checklist'}}, ['items']),
              False, control=True, domain='control'))
register(Tool('submit_plan', 'Plan mode: after looking things up, submit the plan for the user to approve. Ends your '
                             'turn; nothing is changed until they approve it.',
              _obj({'summary': S('one or two sentences: what the plan does'),
                    'steps': A('the concrete steps, in order, with real names and paths')}, ['summary', 'steps']),
              False, control=True, domain='control'))
