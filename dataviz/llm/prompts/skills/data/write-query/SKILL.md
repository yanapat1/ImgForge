---
name: write-query
when: the user wants data, or a question needs a new query
description: Write a correct query for this database
---
1. If the user names one of their categories (scope line) or a group of data, `list_favorites` with that word; otherwise `search_catalog` with the user's words. Unclear or several matches: `ask_user`. Then `describe_table` for each candidate.
2. A column you do not understand: `ask_user`. Offer to save the answer.
3. Write it in this database's syntax with real names, and limit it while exploring. Unsure of the syntax: `check_sql` with your SQL.
4. `run_query`. If it fails, read the error, fix the query, and say what was wrong.
5. Answer with `query_result`, and name the result you used.
6. When the user wants the query itself, show it with `set_editor_query` (`mode` new_tab when the editor holds their own work). When they want to run it or see its rows on the page, `run_in_editor` instead: it writes it in the SQL tab and runs it into the Results tab.
