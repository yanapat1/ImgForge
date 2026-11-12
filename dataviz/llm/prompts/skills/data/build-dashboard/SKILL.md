---
name: build-dashboard
when: the user asks for a dashboard or charts
description: Build a dashboard from a result
---
1. `profile_result` to find one date, one or two categories, one or two numbers.
2. Propose: a `kind: kpi` for each main total (span 3, x the date for a sparkline), a `kind: area` or `kind: line` over time (span 8), a `kind: donut` for a category's share (few groups, top 6), a `kind: hbar` ranking for a category with many groups (top 10), and `kind: bar` for groups in their own order. When there is a date and the user looks ahead, add a `kind: forecast` chart (x the date, y the number, span 12).
3. `build_dashboard` with titles that state what each chart shows. Use real column names. Give related widgets the same `color: <name>`, and set `span: <12ths>` so a row adds up to 12.
