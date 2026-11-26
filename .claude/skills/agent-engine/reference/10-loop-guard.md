# 10. Loop guard

Models, mostly smaller ones, sometimes call the same tool with the same arguments again and again. Detect it,
nudge, recover once or twice, then stop with a clear message.

```python
import json

REPEAT_NUDGE, REPEAT_STOP = 3, 5     # the 3rd identical call gets a nudge, the 5th triggers recovery
LOOP_RECOVER_MAX = 2                 # recoveries per answer (beside the 3 run compactions)

def sig(call):
    return call['name'] + ':' + json.dumps(call.get('args'), sort_keys=True, default=str)
```

Per answer, keep `repeats {sig: n}`, `blocked {sig}`, `last_res {sig: content}` and `loops`.

For each call in a step:

1. `repeats[sig] += 1`.
2. `≥ REPEAT_STOP` → stop handling this step's calls and **recover**:
   - if compaction is off, `loops ≥ LOOP_RECOVER_MAX`, or there is nothing to fold → `error` "stopped: the model called X
     with the same arguments 5 times (it is repeating itself). Rephrase, or try a larger model / higher effort".
   - else **run compaction** with `why='loop'`. Add the note "The model was repeating X(args). Its last result was: …"
     to the summary input, and add to the resume message: "You were repeating X(args): do not call it with those
     arguments again. Use its result (above) or change approach." Then `blocked.add(sig)`, `loops += 1`, clear
     `repeats`, and continue.
3. `sig in blocked` → do not run it. Return `(blocked: you called this with the same arguments again after being told
   not to. Its last result: …)`.
4. `≥ REPEAT_NUDGE` → run it, and append to its result: " (you already called this with the same arguments: use the
   result you have, or change approach)".
5. After the step, store `last_res[sig]` for each call.

**Reset `repeats` after any change ran** (the data changed, so looking again is legitimate) and after each run compaction.
