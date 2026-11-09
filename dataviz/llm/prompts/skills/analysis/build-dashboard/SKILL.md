---
name: build-dashboard
when: the user asks for a dashboard or charts
description: Build a dashboard from a result
---
1. `profile_result` to find one date, one or two categories, one or two numbers.
2. Propose a KPI for the main total, a line over time and a bar per category. When there is a date and the user looks ahead, add a `forecast` chart (x the date, y the number).
3. `build_dashboard` with titles that state what each chart shows. Use real column names.
