# Data Model: Scheduled Cloud Pricing Pipeline

**Feature**: 003-pipeline-cloud-deployment | **Date**: 2026-09-28

The pipeline stores no records in a database. Its "data model" is a set of JSON documents and data files in the storage root, laid out as described in [contracts/storage-layout.md](./contracts/storage-layout.md). The JSON documents have formal schemas in [contracts/manifest.schema.json](./contracts/manifest.schema.json) and [contracts/latest.schema.json](./contracts/latest.schema.json). This document describes the entities, their relationships, validation rules and state transitions.

```text
Snapshot (provider, snapshot_date)
 ├── ManifestRevision 1..n  (immutable history: manifests/<D>/revisions/<NNNN>.json)
 │     └── current = highest revision, copied to manifests/<D>/manifest.json   ← "Active Manifest" unless status=purged
 │           ├── TableEntry ×5 ── RegionTableEntry ×regions ── DataFile 0..n  (parquet/<table>/snapshot_date=<D>/region=<R>/part-<run_id>.parquet)
 │           └── RawEntry per region ── RawObject 1..n  (raw/<D>/<R>/<run_id>/*.json.zst)
 └── RunClaim 0..1  (claims/<D>.json, transient)

LatestPointer (per provider: manifests/latest.json) ──► one Snapshot's current manifest (status=succeeded)
Run (process execution) ──produces──► 0..1 ManifestRevision; reports RunReport (logs + alert)
```

---

## Snapshot

All pricing data for one provider and one snapshot date.

| Field | Type | Rules |
|---|---|---|
| `provider` | string | `aws` now, with `gcp` and `azure` reserved. Lowercase, `[a-z0-9]+`. |
| `snapshot_date` | date (`YYYY-MM-DD`) | UTC date. By default, the scheduled run's start date. |

**Identity**: `(provider, snapshot_date)`. A snapshot exists once its first revision is written.

**Status**: the snapshot's status is the `status` of its current manifest (below).

## ManifestRevision (the Snapshot Manifest)

An immutable description of the snapshot's data as it stood after one run. It has two locations: `manifests/<D>/revisions/<NNNN>.json`, which is never modified, and `manifests/<D>/manifest.json`, a byte-identical copy of the highest revision.

| Field | Type | Rules |
|---|---|---|
| `manifest_version` | string, semver `MAJOR.MINOR` | `"1.0"` for this feature. Consumers reject an unsupported MAJOR (FR-018). |
| `provider` | string | Equals the Snapshot's provider. |
| `snapshot_date` | date | Equals the Snapshot's date. |
| `run_id` | string | `^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$`. The run that produced this revision. |
| `revision` | int ≥ 1 | Strictly increasing per snapshot, with no gaps. Equals the `<NNNN>` in the file name. |
| `previous_revision` | int or null | `revision − 1`, or null for revision 1. |
| `created_at` | RFC 3339 UTC timestamp | Set when the revision is written. Used as the start of the superseded-file grace period. |
| `origin` | enum `pipeline` \| `backfill` | `backfill` only for revisions written by `upload-history`. |
| `status` | enum `succeeded` \| `partial` \| `failed` \| `purged` | Derived (see state rules below). |
| `regions.requested` | string[] (non-empty, unique, sorted) | For revision 1, the run's regions. Later revisions add this run's regions to the previous revision's list. |
| `regions.succeeded` | string[] | Requested regions whose data entries are present in this revision. |
| `regions.failed` | `{region, reason, attempts}`[] | Requested regions without data. `reason` is at most 500 characters, and `attempts` is the maximum number of download attempts used. |
| `run` | object | Provenance and this run's own results (see **RunInfo**). |
| `raw` | map region → RawEntry, or null | Null for backfills. Otherwise, one entry per region in `regions.succeeded`. |
| `tables` | map table → TableEntry | Keys: `service_dim`, `product_dim`, `product_attribute`, `region_dim`, `price_fact`. The map is empty `{}` when status is `purged` or `failed`. |
| `purged` | object or null | `{purged_at, reason}`. Required if and only if `status=purged`. |

### RunInfo (`run`)

| Field | Type | Rules |
|---|---|---|
| `trigger` | enum `scheduled` \| `manual` \| `backfill` | |
| `mode` | enum `full` \| `regions` \| `transform-only` \| `backfill` \| `retention-purge` | `regions` means a subset of regions was re-run. `retention-purge` is used for revisions written when retention purges a snapshot. |
| `host` | string | Where the run executed: `aws-ecs` on Fargate, otherwise `PIPELINE_HOST_LABEL` (default `local`). Informational only (FR-052, FR-056). |
| `started_at`, `ended_at` | timestamp | `ended_at ≥ started_at`. |
| `pipeline_version` | `{git_sha, image_tag}` | Taken from the `GIT_SHA` and `IMAGE_TAG` environment variables. `"local"` when unset. |
| `region_results` | `{region, outcome, reason?, attempts, services_downloaded, services_without_price_list, unparseable_prices}`[] | `outcome` is `succeeded`, `failed` or `skipped`. Lists only this run's regions. |

### TableEntry (`tables.<table>`)

| Field | Type | Rules |
|---|---|---|
| `schema_version` | int ≥ 1 | From the code's `TABLE_SCHEMA_VERSIONS` (all `1` initially). Consumers reject an unsupported version (FR-018). |
| `row_count` | int ≥ 0 | The sum over regions. |
| `regions` | map region → RegionTableEntry | A key for every region in `regions.succeeded`. |

### RegionTableEntry (`tables.<table>.regions.<R>`)

| Field | Type | Rules |
|---|---|---|
| `written_by_run` | run_id | The run whose files these are. May be older than the manifest's own `run_id` (carry-forward). |
| `row_count` | int ≥ 0 | The sum over its files. `0` with `files: []` means the region had no rows for this table. |
| `files` | DataFile[] | |

### DataFile

| Field | Type | Rules |
|---|---|---|
| `path` | string | Relative to the storage root, `/`-separated. It must match `^<provider>/parquet/<table>/snapshot_date=<D>/region=<R>/part-<run_id>(-[0-9]+)?\.parquet$`. |
| `bytes` | int > 0 | Equals the object's size. |
| `sha256` | 64 lowercase hex characters | The checksum of the object's bytes. |
| `row_count` | int ≥ 0 | Equals the Parquet footer's `num_rows`. |

**Invariant (SC-006)**: every DataFile of an Active Manifest exists and matches `bytes`, `sha256` and `row_count`. `verify` checks this.

### RawEntry (`raw.<R>`)

| Field | Type | Rules |
|---|---|---|
| `location` | string | The prefix `<provider>/raw/<D>/<R>/<run_id>/`. |
| `file_count` | int ≥ 1 | |
| `bytes` | int | Total compressed bytes. |
| `stored_at` | timestamp | When the upload finished. |
| `purge_after` | date | `stored_at` date + `RAW_RETENTION_DAYS` + 1. S3 lifecycle rounds expiry up to the next midnight UTC. |

## Active Manifest

A derived concept, not a separate file. An Active Manifest is the current `manifest.json` of a snapshot whose `status ≠ purged`. The **active set** is the union of all DataFile paths across Active Manifests. Retention never deletes a key in the active set (FR-046), and re-checks this immediately before each delete.

## LatestPointer (`<provider>/manifests/latest.json`)

| Field | Type | Rules |
|---|---|---|
| `manifest_version` | string | Same as the manifests. |
| `provider` | string | |
| `snapshot_date` | date | The newest date whose current manifest has `status=succeeded`. |
| `revision` | int | The revision current when the pointer was written. Consumers read `manifest_path` and accept its revision if it is `≥` this value. |
| `run_id` | string | |
| `manifest_path` | string | `<provider>/manifests/<D>/manifest.json`. |
| `updated_at` | timestamp | |

**Rules**:
- The pointer is written only after a `succeeded` manifest (FR-012), using compare-and-swap.
- `snapshot_date` never moves backwards.
- It is never pointed at a `partial`, `failed` or `purged` manifest (FR-014).
- The date it names is never purged (FR-024).

## RunClaim (`<provider>/claims/<D>.json`)

| Field | Type | Rules |
|---|---|---|
| `run_id` | string | The owner of the claim. |
| `trigger` | enum | As in RunInfo, plus `retention` (retention taking another date's claim). |
| `acquired_at`, `expires_at` | timestamp | `expires_at = acquired_at + RUN_CLAIM_TTL_MINUTES`. |

**Lifecycle**:
1. **Absent**: `put_if_absent` makes it **Held**.
2. **Held and not expired**: any other acquirer is refused.
3. **Held and expired**: `put_if_match(old_etag)` makes it **Held** by the new owner. The loser of a race is refused.
4. **Release**: the owner verifies `run_id`, then deletes it, making it **Absent**.

## Run and RunReport

A Run is one process execution: `run`, `retention`, `upload-history`, or the `run` CLI command in `transform-only` mode. Its outcome is written as JSON to the logs and summarized in the alert and summary message.

| Field | Notes |
|---|---|
| `run_id`, `trigger`, `mode`, `snapshot_date`, `regions` | Inputs. |
| `outcome` | `completed` \| `refused` \| `error`. `completed` covers every snapshot status. |
| `snapshot_status` | The status of the revision written, if any. |
| `region_results` | As in RunInfo. |
| `row_counts` | Per table. |
| `duration_seconds` | |
| `retention` | `{superseded_deleted, orphans_deleted, snapshots_purged, raw_deleted_local, waited_seconds, error?}`. |
| `alerts_sent` | The list of alert kinds. |

## RawObject

A compressed pricing file, `<provider>/raw/<D>/<R>/<run_id>/pricing-<ServiceCode>-<R>.json.zst`. It is written once and never modified. It is deleted by the S3 lifecycle rule (cloud) or the retention step (local) after `RAW_RETENTION_DAYS`.

## Settings (configuration entity)

These are read from the environment by `src/pipeline/config.py`. See [contracts/configuration.md](./contracts/configuration.md) for names, defaults and validation. The key cross-field rules:

- `RUN_CLAIM_TTL_MINUTES > RUN_TIMEOUT_MINUTES`.
- `PRICING_DOWNLOAD_RETRY_STRATEGY` must be `fixed`, `linear_backoff` or `exponential_backoff`.
- Retry counts must be ≥ 0, and delays and grace periods ≥ 0.
- `PARQUET_WEEKLY_RETENTION_MONTHS` must be ≥ 1, and `RAW_RETENTION_DAYS` ≥ 1.

Invalid settings stop the run before it does any work, with exit code 2.

---

## State transitions

### Snapshot status (per new revision)

```text
                 ┌──────────── re-run adds data ────────────┐
                 │                                           ▼
 (none) ──run──► failed ──re-run adds data──► partial ──re-run completes──► succeeded
   │                                              ▲                          │
   ├──run (some regions ok)───────────────────────┘                          │
   └──run (all regions ok) / backfill────────────────────────────────────────┤
                                                                              │
 failed | partial | succeeded (not latest, eligible under thinning) ──retention──► purged (terminal)
```

- **Derivation**: `succeeded` if every requested region has data, `partial` if some do, `failed` if none do.
- **Monotonic**: carry-forward means a revision never removes data for a region that had it. Status can therefore only move rightwards, except to `purged`.
- **Re-run of a `succeeded` date**: produces a new `succeeded` revision whose files for the re-run regions are new. The previous files for those regions become superseded.
- **`purged` is terminal**: a new run for a purged date starts again at revision n+1 with no carried data. `requested` resets to the run's regions.

### Data file lifecycle

```text
written (staging → store) ──listed by current manifest──► active
active ──new revision lists replacement──► superseded ──grace elapsed & not in active set (re-checked)──► deleted
written but never listed (run failed before manifest) ──► orphaned ──grace elapsed──► deleted
active ──snapshot purged (manifest rewritten first)──► unreferenced ──guard re-check──► deleted
```

### Run flow (`run_snapshot`)

1. Validate settings, then acquire the RunClaim. If the claim is held, the run is **refused**.
2. Read the current manifest, if any. For transform-only runs, fail fast if any requested region's raw data is missing.
3. For each region, concurrently: download with retries (R9), then compress and upload the raw files.
4. As each region's raw data becomes ready, one transform at a time: transform into staging, then upload the Parquet files with run-specific names.
5. Build the revision: carry forward, apply this run's successes, then derive the status.
6. Write `revisions/<NNNN>.json`, then `manifest.json`. If the status is `succeeded`, update `latest.json` with compare-and-swap.
7. Run the retention step (inline wait ≤ limit) and catch any error.
8. Send alerts and the summary, release the RunClaim, and exit 0.

A crash between steps 3 and 6 leaves only orphaned files, and the manifests are unchanged. The ECS rule alerts on the crash, and the next retention step removes the orphans.
