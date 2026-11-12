---
name: fix-query
when: the user asks to fix, check or convert their SQL, or their query failed
description: Fix the user's SQL and move it to this database's syntax
---
1. The editor text and its last error: under Now when short, else `read_editor` (or use the SQL the user pasted).
2. `check_sql`. It gives syntax errors with line and column, a fixed version when the text was written for another database (`LIMIT` on SQL Server, `TOP` or `[names]` on PostgreSQL, `ISNULL`, `::` casts ...), the changes, and the database's own check (EXPLAIN; nothing runs).
3. Errors left, or the database names a missing table or column: fix by hand with `search_catalog` / `describe_table` and real names, then `check_sql` again on your version.
4. Put the result in the editor with `set_editor_query` and a `note` ("fixed 2 errors for SQL Server"). Keep the user's own formatting and comments where nothing changed.
5. Say each change in plain words, and that it waits for Apply: the user sees the SQL and the diff, and Ctrl+Z in the editor undoes it. Only if the user asks to run it: `run_in_editor` (it writes the fixed SQL in the editor and runs it, the rows show in the Results tab).
