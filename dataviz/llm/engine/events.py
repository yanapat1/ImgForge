"""
The one NDJSON protocol of every ◆ chat (POST /api/llm/agent/<role>/chat): one JSON object per line, keyed `type`.
static/ts/core/chat.ts renders it; the session store keeps a compacted copy as the turn's trace.

    meta          {run_id, role, model, provider, kind, mode, effort, think, web, profile, max_steps, in_est, tools,
                   limit, session_id?, title?, removed?}    the saved chat's fields: added by the chat route
    context       {used, limit, exact}                       the next provider call's input tokens (the page's
                                                             context ring): after meta and each step; exact = the
                                                             provider's own count, else our estimate
    session       {session_id, title, removed, user_seq}     right after meta (saved chats): removed = the chat the
                                                             limit soft-deleted for this one {id, title,
                                                             last_opened_at} or null; user_seq = the seq the user
                                                             message was stored under (regenerate / edit send it
                                                             back as from_seq; the answer is user_seq + 1)
    step          {n, max}                                   a provider call starts
    thinking      {text}                                     streamed reasoning (where the provider returns it)
    thinking_end  {ms}
    text          {text}                                     streamed answer tokens
    tool_start    {id, name, args, risk, summary?}           risk: read | change | danger
    tool_end      {id, name, ok, summary, ms, preview?, undo_id?, url?, retagged?, errors?}
    skill         {name}                                     a skill was loaded
    search        {query, engine, results: [{title, url}], blocked?}
    todo          {items: [{text, state}]}                   the agent's checklist
    ui_action     {id, action, args, label, applied}         a visualizer action (Apply button / applied)
    confirm       {confirm_id, actions: [{id, call_id, tool, args, summary, risk}], timeout_s}   the run waits
    confirmed     {confirm_id, results: [{id, ok, summary, declined?, undo_id?}], status}  decided | stopped | timed out
    ask           {question, options}                        ends the turn
    cannot        {reason, alternative}                      ends the turn
    plan_proposal {summary, steps}                           mode plan: ends the turn, the page offers Approve
    mode          {mode: 'plan', reason}                     auto switched itself to plan mode (enter_plan_mode):
                                                             the page shows it and its mode chip says plan
    retry         {attempt, max, reason, wait_s, discard, step}   discard: drop this step's streamed text
    consult       {page, steps, tools, ok}                   another page's specialist answered a consult (its own
                                                             steps are not streamed: only its answer comes back)
    agent         {task, ok, steps, tools, in, out}          a sub-agent of delegate finished (tools/agents.py)
    compact       {state: start|done, auto, summary?, covered?, before?, after?, turns?}   the older messages were
                                                             folded into the chat's summary (engine/summarize.py)
    task_result   {summary, show}                            a background agent finished (finish_task; bgtasks reads it)
    job           {id, label, state, pct}                    a background job a tool started (state running) or
                                                             job_status looked at (queued | running | done | error
                                                             | cancelled): the dock's Tasks strip shows its row
    notice        {level, text}
    ping          {}                                         keep-alive while waiting for a confirmation
    error         {detail}                                   ends the turn
    done          {usage: {in, out}, ms, steps, ctx, limit}  ends the turn; usage = billed over every step,
                                                             ctx = the last step's context
"""

ENDS = ('done', 'error')
TYPES = ('meta', 'context', 'session', 'step', 'thinking', 'thinking_end', 'text', 'tool_start', 'tool_end', 'skill', 'search', 'todo',
         'ui_action', 'confirm', 'confirmed', 'ask', 'cannot', 'plan_proposal', 'retry', 'consult', 'agent', 'compact', 'mode', 'job', 'task_result', 'notice', 'ping', 'error',
         'done')
