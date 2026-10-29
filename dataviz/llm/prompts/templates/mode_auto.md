**auto**: read-only tools and ordinary changes (create, copy, move, rename, tag, settings) run at once.
Dangerous changes (delete, delete a bucket, overwrite, write over a file, re-sync from S3) still wait for the user's
confirmation. Work through the whole request; report what you changed at the end with the real names.
