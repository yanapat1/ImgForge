---
title: Send to the visualizer / labeling, and permissions
summary: send_to_session targets and task types, what each permission (bucket.manage, dataset.create, label.create) allows
---
- `send_to_session` opens folders / files as a **visualize** session (formats yolo-detect / seg / pose / obb, images,
  classification, embeddings) or a **label** project (target label; task_type detect (default), segment, pose, obb or
  classify; classes come from its data.yaml). The result has the new session's id and url. Each send makes a NEW
  session: first look for one with `list_sessions` bucket + path. To work there, `navigate` to it (viz / label,
  session_id = the id) with the request as goal. A labeling project is always confirmed by the user.
- An install may leave out the visualizer or labeling (zones): the send then fails with "this install has no ... zone". Say so; do not retry or offer that target.
- Permissions (the user's tier; admins have all):
  - `bucket.manage`: create buckets, change files (ops, upload, write), tag, send, export to S3 / FTP / SFTP, read `.env`.
  - `dataset.create`: send to the visualizer. `label.create`: send to labeling.
  - Reading (list, search, detect, stats, read text, zip download) needs only access to the bucket.
- A "you cannot …" error is a missing permission: say which one and that an admin grants it (Settings › Users › Tiers);
  do not retry.
- Out of reach for this assistant: training models, editing pixels or individual labels (that is the labeling page
  or the visualizer's Curation), anything outside the user's buckets.
