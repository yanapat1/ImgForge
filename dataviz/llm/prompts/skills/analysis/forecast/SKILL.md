---
name: forecast
when: the user asks what will happen next, for a forecast, a projection, a trend ahead, or when a total will reach a level
description: Forecast a measure over time and say how far to trust it
---
1. `profile_result` (or `run_query` first): find the date column and the number. If there is no date, say the query needs one and offer a query that keeps it.
2. Pick the step that fits the question (daily sales → day; a quarterly plan → month or quarter). Leave it on auto when unsure. The result must hold the whole period: a query cut at the row limit or filtered to a few days gives a short history, so aggregate in the query (GROUP BY the date) when the table is large.
3. Run `forecast`. For "which shop / product will grow", pass `group`.
4. Answer with the next value and its likely range, the total over the horizon, the model's tested accuracy against repeating the past, and every warning. Name the unusual steps if they matter to the question.
5. Say what a forecast cannot know (promotions, prices, events it has not seen). Offer the Forecast widget for a dashboard with `build_dashboard` kind `forecast`.
