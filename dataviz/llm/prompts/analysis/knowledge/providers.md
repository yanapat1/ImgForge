---
title: Database providers
summary: syntax differences between SQL Server, PostgreSQL and MongoDB, and OPENQUERY
core: false
---
- SQL Server: `SELECT TOP (10) ...`, `[names]`, `#temp` tables, `TRY_CAST`, `OPENQUERY(link, 'text')` runs its text on a linked server. A `SELECT INTO` a real table is a change.
- PostgreSQL: `LIMIT 10`, `"names"`, `CREATE TEMP TABLE`, `::` casts, `STDDEV`, `VARIANCE`. Reads run in a read-only transaction, so the database itself refuses a hidden write.
- MongoDB: `db.getCollection("name").find({...})`, `.aggregate([...])`, joins are `$lookup`, casts are `$convert`. `$out`, `$merge`, `$where` and `$function` count as changes. Fields are sampled, so a rare field may be missing from describe_table.
- One connection can reach many databases. Tables keep their own schema, not only dbo.
