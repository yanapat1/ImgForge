"""
The agent engine shared by every ◆ chat (Bucket and visualizer):

    react.py     the ReAct loop (≤ 50 steps, ≤ 50 retries per run, loop guard, modes ask / auto / plan, effort)
    confirm.py   pause / resume while the user approves a change
    compact.py   keeps a long run inside the token budget
    roles.py     the two agents: their tool domains, switch, scope text
    planner.py   mode plan's approved-plan message
    events.py    the NDJSON event protocol
"""
