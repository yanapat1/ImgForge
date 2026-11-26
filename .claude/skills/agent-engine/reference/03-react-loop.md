# 3. The ReAct loop

One turn is one **generator** that yields events. The HTTP route writes each one as a JSON line and flushes it.
Because it is a generator, waiting for the user (a confirmation) is just a loop that yields `ping`s, and the turn goes
on in the same stream once they decide.

## Order of one step

1. **Limits.** Past the step limit: compact the run and go on (09), else stop with an error. Cancelled: `notice
   Stopped` + `done`.
2. **Fit** (from step 2 on): stub old tool results until the request fits. Past 90% of the budget, compact the run.
   If it still does not fit, error "ask for less at once".
3. **Stream** one provider call (`_step`). Text and thinking stream out as events. Tool calls are collected.
4. **No tool call:**
   - empty text + stop reason *cut off* → retry once with twice the output room plus a nudge ("act sooner"), else error.
   - empty text → error "empty answer".
   - something changed this turn + effort high + not yet verified → add the answer and a `(check)` user message
     ("verify with read-only tools that the changes did what was asked"), then loop once more.
   - else → `done`.
5. **Tool calls:** give each a unique id. Append the assistant message (signed thinking + text + calls).
6. **Control tools that end the turn** (`ask_user`, `cannot_do`, `submit_plan`, `finish_task`): the first valid one
   wins. Yield its card and `done`.
7. **For each call:** unknown tool → error result (with "navigate there" if another agent owns it). Args not an
   object → error result. Loop guard (10). `todo` / `more_tools` / `enter_plan_mode` are handled by the engine. A
   **read** runs now. A **change / danger / run** is validated + previewed now and queued.
8. **Changes:** if one change of the step failed validation, **none runs** (the model resends the set fixed). In
   auto, ordinary changes run in model order. From the first one that needs the user, the rest go on **one
   confirm card**. Wait, then run the approved ones. A declined one gets "the user declined: do not do it again
   unless they ask".
9. Append one `tool` message with every result **in call order**, then loop.
10. After the loop: "stopped after N steps without an answer: try a narrower request or a higher effort".

## Sketch: one streamed step with retries

```python
import time

MAX_RETRIES = 50            # per run (tunable default)


def _step(run, state, step_no):
    """Streams one provider call -> yields events; the answer lands in state['ans'] (None = stopped)."""
    specs = [t.spec() for t in run.tools.values()]
    while True:
        text, thinking, calls, usage, stop, streamed = [], [], [], {}, None, False
        try:
            out = state.get('max_out') or output_room(run.provider, run.think, state.get('used'))
            for kind, val in stream_tools(run.provider, run.system, run.msgs, specs, think=run.think, max_tokens=out):
                if run.cancel.is_set():
                    break
                if kind == 'thinking':
                    thinking.append(val)
                    streamed = True
                    yield {'type': 'thinking', 'text': val.get('text', '')}
                elif kind == 'text':
                    text.append(val)
                    streamed = True
                    yield {'type': 'text', 'text': val}
                elif kind == 'tool_call':
                    calls.append(val)
                elif kind == 'stop':
                    stop = val
                elif kind == 'usage':
                    usage = val or {}
            state['ans'] = {'text': ''.join(text), 'thinking': thinking, 'calls': calls, 'usage': usage, 'stop': stop}
            return
        except LLMError as e:
            if not e.transient:
                raise
            err = e
        except (TimeoutError, ConnectionError) as e:
            err = LLMError(f'{type(e).__name__}: {e}', 502, transient=True)
        state['retries'] += 1
        if state['retries'] > MAX_RETRIES:
            raise LLMError(f'gave up after {MAX_RETRIES} retries: {err}', 502)
        n = state['retries']
        wait = max(0.0, min(float(err.retry_after or min(2 ** (n - 1), 20)), 60.0))
        # discard: the UI drops this step's half-streamed text
        yield {'type': 'retry', 'attempt': n, 'max': MAX_RETRIES, 'reason': str(err)[:300],
               'wait_s': round(wait, 1), 'discard': streamed, 'step': step_no}
        if run.cancel.wait(wait):                      # an Event: wakes at once on stop
            state['ans'] = None
            return
```

## Sketch: the loop (compaction, loop guard and confirmation shown as calls)

```python
VERIFY = ('(check) Before you finish: verify with read-only tools that the changes did what was asked, '
          'then give the short final answer.')
TURN_ENDERS = ('ask_user', 'cannot_do', 'submit_plan', 'finish_task')


def events(run):
    st, usage, step, changed, verified = {'retries': 0}, {'in': 0, 'out': 0}, 0, False, False
    yield {'type': 'meta', 'run_id': run.run_id, 'mode': run.mode, 'effort': run.effort, 'max_steps': run.max_steps}
    while True:
        if step >= run.max_steps:
            if not (yield from limit_reached(run, 'steps')):      # compacts and raises max_steps, or False
                break
            continue
        step += 1
        if run.cancel.is_set():
            yield {'type': 'notice', 'level': 'info', 'text': 'Stopped.'}
            yield {'type': 'done', 'usage': usage, 'steps': step - 1}
            return
        if step > 1 and not (yield from fit_or_compact(run)):     # 09: stub, then compact at 90%
            yield {'type': 'error', 'detail': 'the tool results grew over the token budget: ask for less at once'}
            return
        yield {'type': 'step', 'n': step, 'max': run.max_steps}
        yield from _step(run, st, step)
        ans = st['ans']
        if ans is None:
            yield {'type': 'done', 'usage': usage, 'steps': step}
            return
        for k in ('in', 'out'):
            usage[k] += int((ans['usage'] or {}).get(k) or 0)
        text, calls = ans['text'].strip(), ans['calls']
        if not calls:
            if not text and ans['stop'] in ('length', 'max_tokens', 'MAX_TOKENS') and not st.get('max_out'):
                st['max_out'] = 2 * out_limit(run.provider)           # once, with twice the room
                run.msgs.append({'role': 'user', 'parts': [{'text': CUT_NUDGE}]})
                continue
            if not text:
                yield {'type': 'error', 'detail': 'the model returned an empty answer: try again or rephrase'}
                return
            if changed and not verified and EFFORTS[run.effort]['verify']:
                verified = True
                run.msgs += [{'role': 'assistant', 'parts': [{'text': text}]},
                             {'role': 'user', 'parts': [{'text': VERIFY}]}]
                continue
            yield {'type': 'done', 'usage': usage, 'steps': step}
            return
        for k, c in enumerate(calls):
            c['id'] = str(c.get('id') or f'call_{step}_{k}')
        signed = [{'thinking': t} for t in ans['thinking'] if t.get('sig')]
        run.msgs.append({'role': 'assistant', 'parts': signed + ([{'text': text}] if text else [])
                         + [{'tool_call': c} for c in calls]})
        ender = next((c for c in calls if c['name'] in TURN_ENDERS and c['name'] in run.tools
                      and validate(run.tools[c['name']], c.get('args'))[1] is None), None)
        if ender:
            yield card_event(ender)                                   # ask / cannot / plan_proposal / task_result
            yield {'type': 'done', 'usage': usage, 'steps': step}
            return
        results, did_change = yield from run_calls(run, calls)        # 04 + 06 + 10: reads, changes, confirm
        changed |= did_change
        run.msgs.append({'role': 'tool', 'parts': [{'tool_result': r} for r in results]})
    yield {'type': 'error', 'detail': f'stopped after {step} steps without an answer: try a narrower request '
                                      'or a higher effort'}
```

`CUT_NUDGE` tells the model: "(system) Your last answer ran out of output tokens while you were still thinking, so
nothing reached the user. Think less and act now: call the tool you need or answer."

## Output room for thinking

For OpenAI-style and local models, give a thinking step `max_tokens` by level (low 4k / medium 8k / high 16k), up to
an admin ceiling (16k default). Never give more than the context window leaves after the input (minus a 512 margin).
Anthropic and Gemini add their own thinking budget on top of the answer room.

## One run per chat

`claim_chat(session_id, cancel)` refuses a second run on the same chat (HTTP 409 "answering in another tab"). A
claim frees itself after an hour in case a stream never ended. Keep `_runs[run_id] = {user_id, cancel}` so
`POST /stop {run_id}` can set the event, and only for its own user.
