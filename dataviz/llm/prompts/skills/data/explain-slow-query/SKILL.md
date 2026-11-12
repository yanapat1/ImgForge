---
name: explain-slow-query
when: a query is slow or the user asks about its cost
description: Find why a query is slow
---
1. `explain_query`.
2. Name the costliest steps and their share. These are estimates: say so.
3. Suggest a narrower filter, fewer columns, or an index on the join and filter columns.
4. Creating an index is a change: propose it with `run_change`, never run it quietly.
