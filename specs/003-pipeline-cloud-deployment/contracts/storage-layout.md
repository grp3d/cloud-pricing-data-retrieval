# Contract: Storage Layout

**Applies to**: the local directory and S3 alike (FR-008). `<root>` is the path or prefix given by `PIPELINE_STORAGE_URI`: `file:///data` → `/data`, or `s3://cloud-pricing-data-prod/` → the bucket root.

```text
<root>/
└── <provider>/                                   # "aws" now; "gcp", "azure" later (FR-010)
    ├── raw/                                      # compressed source files; expire after RAW_RETENTION_DAYS
    │   └── <YYYY-MM-DD>/<region>/<run_id>/
    │       └── pricing-<ServiceCode>-<region>.json.zst
    ├── parquet/                                  # the five tables, Hive-style partitions
    │   └── <table>/snapshot_date=<YYYY-MM-DD>/region=<region>/
    │       └── part-<run_id>[-<n>].parquet       # file name is unique per run; never overwritten
    ├── manifests/
    │   ├── latest.json                           # → newest succeeded snapshot (latest.schema.json)
    │   └── <YYYY-MM-DD>/
    │       ├── manifest.json                     # current revision (manifest.schema.json)
    │       └── revisions/<NNNN>.json             # immutable history, zero-padded, 0001…
    └── claims/
        └── <YYYY-MM-DD>.json                     # transient run claim (internal)
```

`<table>` is one of `service_dim`, `product_dim`, `product_attribute`, `region_dim` or `price_fact`.

## Consumer rules (cloud-pricing-app)

1. **Start from `manifests/latest.json`**, then read the `manifest_path` it names. For history, read `manifests/<date>/manifest.json`.
2. **Find data files only through the manifest's `tables.<t>.regions.<r>.files[].path`.** Never list or glob `parquet/` directories. During the superseded-file grace period, those directories can hold files from more than one revision, and a glob would double-count rows.
3. **Reject** a manifest whose `manifest_version` MAJOR, or any table `schema_version`, is unsupported.
4. **Skip** manifests with `status` `purged` or `failed`. Only use `partial` manifests if you deliberately handle missing regions.
5. **Optionally verify** each file's `bytes` and `sha256` before use.
6. **Ignore** `claims/` and `revisions/`. They are internal and history, not a data source.
7. **Re-read the manifest** if you hold it longer than `SUPERSEDED_FILE_GRACE_MINUTES`. Files of a superseded revision may be deleted after that time.
8. **Treat "access denied" like "not found"** for a missing key (for example, `latest.json` before the first snapshot). The read-only policy can list only `manifests/` and `parquet/`, so S3 reports a missing key as access denied.

## Producer guarantees (this repo)

- **Write order**: data files, then `revisions/<NNNN>.json`, then `manifest.json`, then `latest.json`.
- **Immutable data files**: a data file is never modified after it is written. It is either listed by manifests or deleted.
- **No dangling paths**: no Active Manifest lists a path that doesn't exist. Purging rewrites the manifest before any deletes.
- **`latest.json` never goes backwards**: it only names `succeeded` snapshots, and it names a date that retention will not purge.
- **Access**: the bucket blocks public access and is encrypted at rest (SSE-S3). The web app gets read-only access via the IAM policy `cloud-pricing-data-read-<env>`.

## Access from any environment (FR-056)

The data is identical whoever produced it: a Fargate task or an off-AWS writer such as a home server. Readers get the same data wherever they run. Only the way they obtain credentials differs:

| Reader location | Credentials |
|---|---|
| In AWS (e.g., the app's ECS task) | Attach the managed policy `cloud-pricing-data-read-<env>` to the app's role. |
| Outside AWS (laptop, home server) | Assume the role `cloud-pricing-data-reader-<env>` through IAM Roles Anywhere, with a client certificate whose subject CN is listed in `external_reader_subjects`. It has the same permissions as the policy above and returns short-lived credentials. |

Off-AWS **writers** use the role `cloud-pricing-pipeline-writer-<env>` in the same way, with CNs listed in `external_writer_subjects`. See research R22.

## Removed (FR-019)

`_SUCCESS`, `_REGION_COMPLETE` and `.locks/` are no longer produced in the new layout. The legacy local tree `$DATA_DIRECTORY_ROOT/pricing_aws/` is left untouched. `upload-history` reads it as a source only.
