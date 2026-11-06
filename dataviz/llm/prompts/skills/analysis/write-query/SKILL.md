---
name: write-query
when: the user wants data, or a question needs a new query
description: Write a correct query for this database
---
1. `search_catalog` with the user's words. `describe_table` for each candidate.
2. A column you do not understand: `ask_user`. Offer to save the answer.
3. Write it in this database's syntax with real names, and limit it while exploring.
4. `run_query`. If it fails, read the error, fix the query, and say what was wrong.
5. Answer with `query_result`, and name the result you used.
