---
name: drive-the-app
description: open the right page / view, run a model, map, scan or stats, read the result
when: do / show something elsewhere, or run something
---
1. **Where the answer is**

| The user wants… | Page / view | How |
|---|---|---|
| sessions, open a dataset | Home | `navigate` home |
| files, folders, buckets, tags, formats, upload, export | Data Center | `navigate` data (+ bucket, path) |
| the map, clusters, outliers, a class's boxes | Visualizer › Explore | `open_tab` explore, `set_projection` |
| class counts, sizes, balance | Visualizer › Class stats | `compute_stats`, `class_stats` |
| duplicates, leaks, box problems | Visualizer › Curation | `run_scan` curation, `issue_summary` |
| wrong labels | Find issues | `run_scan` cleanlab, `show_issue` cl-issues |
| a model's errors | Visualizer › Model | `run_model`, `eval_summary` |
| which model is best | Visualizer › Evaluation | `run_eval_all`, `eval_summary` |
| A vs B datasets | Visualizer › Compare (combined datasets) | `open_tab` compare, `compare_tests` |
| boxes of one image | Labeling | `navigate` label (its own Label assistant edits boxes) |

Another page: find the session (`list_sessions`) or the bucket first, then call `navigate` **last**, with a
`goal` that says what to do there ("run yolov8s on val and rank the classes"). Say in one line where you go.
It is the same chat on the new page: that page's tools answer the goal. A step you cannot do here is never
"impossible": it is on another page.

Several independent look-ups here (when `delegate` is in your tools: the user turned Sub-agents on): one `delegate`
with every task instead of calling the tools one by one; each task names exactly what to find. Changes stay yours.

Across pages (one chat, a `todo` with a page on each step when it spans pages):
- "open session a1 in the visualizer": `list_sessions` q=a1 → `navigate` viz, session_id, goal = the rest.
- Data Center, "visualize this folder with t-SNE": `list_sessions` bucket + path. One found → `navigate` viz with
  goal "t-SNE projection". None → `send_to_session` target visualize (the user approves in ask), then `navigate`
  to the new id it returned. A labeling project (target label) is always confirmed.
- Visualizer, "go to ../.. of this folder": `data_location` → `navigate` data, bucket, path = parents[1]
  (".." = parents[0]); the Data Center tools are there.
- Only a question about another page ("mAP of yolov8s in a1?", "how many images in bucket cars?"): `consult`
  (viz needs the session_id) and answer here; the user stays. `navigate` when they want to see or change it.
- The Plan of this chat shows the steps: after a page change continue the ▶ one.

2. **Run it**
- Check names first: `list_models`, `list_features`, `list_methods`. Never invent an id.
- More than one step: `todo` with the whole list first (the user sees it in the Tasks strip).
- Mode **ask**: a run waits for the user's Approve. Say what it will do and roughly how long (a model on a
  big dataset or a new feature space: minutes). Mode **auto**: it runs at once; dangerous changes still ask.
- Mode **plan**: you cannot run anything. Research, then `submit_plan`.

3. **Read the result**
- The page reports how a run ended. Done → read the numbers with `eval_summary`, `issue_summary`,
  `class_stats` or `projection_quality`, then answer with them (a table, the 2-3 findings, the next step).
- "still running" → tell the user it continues on the page and offer to read it later. Do not start it again.
- Failed → read the reason. If it is your arguments, fix them once. A busy server is retried for you
  already. If it fails again, or the reason is outside your reach (no models, a permission, a missing file),
  call `cannot_do` with the page's reason and what the user can do (the button, the Settings section).

4. **What you never do**
Train models, add or delete models (Settings › Models), change Settings, or delete data without the user's
confirmation. Say so with `cannot_do` and point to the control.
