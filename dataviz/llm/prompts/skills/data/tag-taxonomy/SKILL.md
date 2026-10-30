---
name: tag-taxonomy
description: design and apply a consistent tag scheme across buckets and folders
when: the user wants to tag or organise many datasets, or their tags are messy / inconsistent
---
1. `all_tags` to see what exists (counts); `list_buckets` for formats and current tags.
2. Spot near-duplicates ("Night" / "night" / "night-time") and propose one spelling each. Suggest a few prefixes:
   `project:`, `split:`, `status:` (raw / clean / labelled / exported), `source:` (kaggle / roboflow / camera), `cam:`.
   Never a `format:` tag: those are automatic.
3. Agree the scheme with ask_user (2-3 options) before tagging many items.
4. `todo` the list, then `add_tags` per bucket ('' path) or folder; `remove_tags` the old spellings in the same step.
5. Report a table: item, tags added, tags removed. Show how to filter by them (list_buckets / search with tags).
