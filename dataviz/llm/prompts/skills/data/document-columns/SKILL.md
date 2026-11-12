---
name: document-columns
when: columns have no description, or the user wants to document a table
description: Help the user describe their columns
---
1. `describe_table` and list the columns with no description.
2. For each one, look at its name and `profile_result`, and suggest a meaning as a question.
3. `ask_user` when you are not sure. Save confirmed answers with `set_column_description`.
