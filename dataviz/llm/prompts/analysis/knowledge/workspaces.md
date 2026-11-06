---
title: Catalog, Scripts, Analyze, Dashboards, History
summary: what each page of the Data Analysis rail does
core: true
---
- **Catalog**: one table's columns, types, keys and relationships. Descriptions the user writes stay in this app and you read them, so better descriptions give better answers.
- **Scripts**: named command boxes kept with the session. Coming back shows them; nothing runs until Run all, and changes confirm first.
- **Temp tables** live on the session's connection and show with a ⏱ mark in the object explorer. They go when the user closes the session, or 15 minutes after the page is gone. A refresh loses nothing. After a server restart, rerun the script.
- **Analyze** works on the open tab's last result: Profile, Statistic tests, Data quality, Pivot.
- **Dashboards**: add tools, drag fields onto a chart, drag a widget to swap places. Charts read the last result, so they need a run after the session reopens.
- **History** keeps every run's text, searchable, and Open puts it in a new tab.
