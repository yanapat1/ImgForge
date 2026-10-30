---
title: Tags (user tags and format:* tags)
summary: where tags live, user vs automatic format tags, cleaning rules, reusing names, a good taxonomy
---
- A tag sits on a bucket (`path` '') or on a folder / file. `add_tags` / `remove_tags` change user tags; `all_tags`
  lists every tag with counts (reuse an existing spelling instead of inventing a near-duplicate).
- **Automatic** tags `format:<format>` (e.g. `format:yolo-detect`) are written only by format detection on the
  bucket root, swapped when the format changes, and cannot be added or removed by anyone.
- User tags are cleaned: trimmed, inner spaces collapsed, at most 32 characters, duplicates dropped ignoring case
  (the first spelling is kept), at most 20 per call. Tags follow moves and renames and come back with an undone
  delete.
- Filters: list_buckets / search take `tags` (all must match, case-insensitive) and `formats` (any).
- A useful taxonomy: a few prefixes, lower-case, `-` inside words: `project:parking`, `split:val`, `status:raw` /
  `status:clean` / `status:labelled`, `source:kaggle`, `cam:north`. Prefixes keep the list short and searchable.
  Never use the `format:` prefix for your own tags.
- Not the same thing: a **session or labeling project on Home** has up to 20 tags of its own (same cleaning rules),
  shown under its name on the card. Its owner edits them with the card's Edit button (the full edit form's Tags
  field, saved with the form; a click on a tag on a card filters Home by it). No tool here changes them: tell the
  user where to do it.
