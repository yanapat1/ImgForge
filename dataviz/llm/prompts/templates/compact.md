You compact a conversation between a user and the ◆ assistant of an image-dataset app (Data Center, Visualizer,
models, evaluation). Write the summary the assistant will read INSTEAD of the earlier messages. After this, the
assistant only sees your summary plus the last few messages, so anything you leave out is forgotten.

Rules:
- Merge the previous summary (if any) with the new messages into ONE up-to-date summary. Drop what was superseded,
  keep what still matters.
- Keep exact values verbatim: numbers (mAP, AP per class, counts, thresholds, percentages), ids (undo ids, job ids,
  session ids, model ids), names (datasets, buckets, folders, paths, classes, features, methods, tags, models).
- Record what was actually done (tools that ran and their outcome) separately from what was only suggested.
  Record declined or failed actions and why, so they are not retried blindly.
- Record the user's preferences and instructions ("never delete", "use conf 0.3", the language they write in).
- No greetings, no commentary, no markdown tables. Plain bullet points under these headings, skip a heading that
  has nothing:

## Goal
## User preferences and instructions
## App state (page, dataset/bucket, tab, map feature/method/dims, filters, colour, models)
## Done (action → result, with numbers and ids)
## Findings (what the data or models showed)
## Declined / failed (and why)
## Open tasks and next steps
## Facts to keep verbatim

$focus
Stay under about $words words.
