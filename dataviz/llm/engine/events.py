"""
The one NDJSON protocol of every ◆ chat (POST /api/llm/agent/<role>/chat): one JSON object per line, keyed `type`.
static/ts/core/chat.ts renders it; the session store keeps a compacted copy as the turn's trace.

    meta          {run_id, role, model, provider, kind, mode, effort, think, web, profile, max_steps, in_est, tools}
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
    retry         {attempt, max, reason, wait_s, discard, step}   discard: drop this step's streamed text
    notice        {level, text}
    ping          {}                                         keep-alive while waiting for a confirmation
    error         {detail}                                   ends the turn
    done          {usage: {in, out}, ms, steps}              ends the turn
"""

ENDS = ('done', 'error')
TYPES = ('meta', 'step', 'thinking', 'thinking_end', 'text', 'tool_start', 'tool_end', 'skill', 'search', 'todo',
         'ui_action', 'confirm', 'confirmed', 'ask', 'cannot', 'plan_proposal', 'retry', 'notice', 'ping', 'error',
         'done')
