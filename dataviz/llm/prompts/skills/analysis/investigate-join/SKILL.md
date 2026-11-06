---
name: investigate-join
when: totals look wrong after a join, or the user asks what a join dropped
description: Check what a join keeps, cuts or multiplies
---
1. Name the two tables and the join columns.
2. `run_query` for the row count of each side, and the joined count.
3. `query_result` to compare them. More rows than the larger side means the key repeats, so summed columns are inflated.
4. Tell the user the Joins tab draws this, and that rows with no match are cut by an INNER JOIN.
