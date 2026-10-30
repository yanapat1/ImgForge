---
title: Send to the visualizer / labeling, and permissions
summary: send_to_session targets and task types, what each permission (data.manage, dataset.create, label.create) allows
---
- `send_to_session` opens folders / files as a **visualize** session (formats yolo-detect / seg / pose / obb, images,
  classification, embeddings) or a **label** project (target label; task_type detect (default), segment, pose, obb or
  classify; classes come from its data.yaml). The result has the new session's url: give it to the user.
- Permissions (the user's tier; admins have all):
  - `data.manage`: create buckets, change files (ops, upload, write), tag, send, export to S3 / FTP / SFTP, read `.env`.
  - `dataset.create`: send to the visualizer. `label.create`: send to labeling.
  - Reading (list, search, detect, stats, read text, zip download) needs only access to the bucket.
- A "you cannot …" error is a missing permission: say which one and that an admin grants it (Settings › Users › Tiers);
  do not retry.
- Out of reach for this assistant: training models, editing pixels or individual labels (that is the labeling page
  or the visualizer's Curation), anything outside the user's buckets.
