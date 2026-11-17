---
title: Send to the visualizer / labeling, and permissions
summary: send_to_session targets and task types, what each permission (bucket.manage, dataset.create, label.create) allows
---
- **One session, usually both sides.** `send_to_session` target **both** (what the Image page's Create session does
  by default) makes one linked session from a folder: a Label project that owns the labels (the folder's own YOLO /
  COCO labels are imported into it, a job) and a Visualize session that reads that project live, so a box fixed in
  Label shows in Visualize with no export. The result has `visualize.url`, `label.url` and `label.import_job_id`.
  `session_check` bucket, path says first what works on the folder: its format and counts, `can.visualize /
  can.label / can.linked` with the reason, whether its labels are imported, and what it can be re-formatted to.
  A folder with only images works for both (Visualize shows whole images, Label starts empty). COCO opens in
  Visualize only through a linked session; YOLO OBB, classification folders and embeddings cannot be linked.
- Sessions are made only from data in a bucket: new data goes into Bucket first (upload, link a folder, an S3
  link, import from the web), then Image › Create session picks the bucket folder.
- `send_to_session` also opens folders / files as a **visualize** session alone (formats yolo-detect / seg / pose / obb, images,
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
