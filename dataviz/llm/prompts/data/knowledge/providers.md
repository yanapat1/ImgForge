---
title: Database providers
summary: syntax differences between SQL Server, PostgreSQL and MongoDB, and OPENQUERY
core: false
---
- SQL Server: `SELECT TOP (10) ...`, `[names]`, `#temp` tables, `TRY_CAST`, `OPENQUERY(link, 'text')` runs its text on a linked server. A `SELECT INTO` a real table is a change.
- PostgreSQL: `LIMIT 10`, `"names"`, `CREATE TEMP TABLE`, `::` casts, `STDDEV`, `VARIANCE`. Reads run in a read-only transaction, so the database itself refuses a hidden write.
- MongoDB: `db.getCollection("name").find({...})`, `.aggregate([...])`, joins are `$lookup`, casts are `$convert`. `$out`, `$merge`, `$where` and `$function` count as changes. Fields are sampled, so a rare field may be missing from describe_table.
- One connection can reach many databases. Tables keep their own schema, not only dbo.
- Adding a database: Settings › Connections › Add, kind PostgreSQL, SQL Server or MongoDB (port 27017). Leave Database blank on SQL Server or MongoDB to list every database. Test checks the login and shows the server version. Connections are private to their owner; a read-only login is the safest choice.
- "Our syntax" / "the system's syntax" means the open connection's database. Text written for another database (MySQL `LIMIT x, y`, Oracle, PostgreSQL on SQL Server and the reverse) is moved with check_sql: `LIMIT` ↔ `TOP`, `"x"` ↔ `[x]`, `ISNULL` → `COALESCE`, `::` → `CAST`, `ILIKE` → `LOWER(...) LIKE LOWER(...)` on SQL Server, `GETDATE()` ↔ `CURRENT_TIMESTAMP`. A function the parser does not know is reported, not rewritten; the database's check then says whether it exists.
