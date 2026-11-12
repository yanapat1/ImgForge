---
title: Exporting (zip, S3, FTP, SFTP)
summary: Download zip vs Export to… S3 / RustFS / FTP / SFTP, the destination layout, overwrite, create bucket, jobs
---
- **Download zip** (page: Send to… › Export to… › Download zip, also in the bucket's Edit dialog): a folder or file
  as a zip streamed to the browser, names relative to that folder. Works on streamed S3 buckets too. Hidden names and the trash are left out;
  `.env` files only with bucket.manage. The assistant cannot download for the user: point to the button.
- **Export to…** (`export_to`, needs bucket.manage): copies a folder or file to S3 / RustFS, FTP or SFTP through one of
  the **user's own** connections (`connection` id or name), as a background job (bk-export, cancellable).
  - Layout: the destination is `prefix` (S3) or `path` (FTP / SFTP) + the path **relative to the bucket root**. So
    `data/images/a.jpg` exported with prefix `dev/v1/` lands at `dev/v1/data/images/a.jpg`, whether `data/`,
    `data/images/` or the whole bucket was exported.
  - S3: `s3_bucket` is the target bucket; `create_bucket` makes it when missing (S3 naming rules).
  - A non-empty destination fails unless `overwrite` (dangerous: always confirmed). One export per bucket at a time.
- The result gives the files, bytes and the destination URI. Already-uploaded files stay if the job is cancelled.
