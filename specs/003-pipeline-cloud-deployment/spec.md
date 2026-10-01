# Feature Specification: Scheduled Cloud Pricing Pipeline with Snapshot Manifest and Retention

**Feature Branch**: `003-pipeline-cloud-deployment`

**Created**: 2026-09-28

**Status**: Draft

**Input**: User description: "see ../speckit-inputs/cloud-pricing-data-retrieval-cloud-deployment.md" — Run the AWS pricing pipeline in the cloud on a schedule, with a published snapshot manifest and data retention.

## Context

Today the weekly pricing pipeline runs on the owner's laptop. It downloads AWS pricing for 7 default regions every Monday at 13:00 UTC, writes uncompressed raw pricing files (about 3.2 GB per snapshot, compressible roughly 20x) and five Parquet tables (`service_dim`, `product_dim`, `product_attribute`, `region_dim`, `price_fact`, about 145 MB per snapshot) to local disk, and signals completion with `_SUCCESS` / `_REGION_COMPLETE` marker files and a `.locks/` directory (feature 002).

AWS prices from the past cannot be downloaded later. A missed week is lost for good. The pipeline therefore has to run on schedule on its own, independently of the laptop and of the web app (`cloud-pricing-app`), which will be deployed separately.

This feature moves the weekly run to the cloud as a scheduled, pay-per-run job. It stores data in cloud object storage, publishes **one manifest per snapshot** as the only contract with the web app, bounds storage cost with a retention policy, alerts the owner on failures and missed runs, and makes all infrastructure reproducible from code and deployable from CI. It must ship before the matching app feature (`cloud-pricing-app-cloud-deployment`), which consumes the manifest defined here.

## Clarifications

### Session 2026-09-28

- Q: Should Dagster be removed entirely, or kept as an optional local-dev runner? → A: Keep it as an optional local-dev runner that calls the same pipeline entry point as the packaged image. It is not used in the cloud.
- Q: Should the existing local history be uploaded to cloud storage as the starting history? → A: Yes, table (Parquet) snapshots only. The feature delivers an operator-run upload script, not the transfer itself, so the operator decides which snapshots to upload. The script takes a snapshot date and an overwrite option. It creates the snapshot's manifest if one doesn't exist yet, then copies the local files to cloud storage. It aborts if any data for that snapshot date already exists in cloud storage, unless overwrite is set. Raw data is never uploaded.
- Q: When a snapshot date is re-run (in cloud storage or on local disk), should the new revision overwrite existing files and the manifest, or write new files? → A: Each revision writes its data files to new, revision-specific paths and never overwrites the files of an earlier revision. The snapshot's current manifest is switched to the new revision only after all of its files are written, and earlier revision manifests are kept as history. Files no longer referenced by the current revision ("superseded files") are deleted by retention after a grace period, set in minutes and configurable. Consumers MUST use the manifest's file list and never discover files by scanning directories.
- Q: When retention deletes a snapshot's data, what happens to its manifest? → A: The manifest stays where it is, with status `purged`, a purge timestamp, and its file list removed. Because revisions of the same snapshot date can share files (FR-043), a file can appear in more than one manifest. The safeguard is therefore on deletion: retention (for superseded files, snapshot thinning or any other reason) must never delete a file that is listed by any active manifest, and it must re-check this immediately before each deletion.
- Q: If a run starts for a snapshot date that already has a run in progress, should it wait or be refused? → A: Refuse immediately. The refused run writes nothing and logs a clear "run already in progress" reason. The owner is alerted only when the refused run was a scheduled run.
- Q: Should a region's pricing download be retried automatically on temporary errors before the region is marked failed? → A: Yes. The number of retries and the retry strategy (`fixed`, `linear_backoff`, `exponential_backoff`) are both configurable. The settings are named specifically for pricing downloads (prefix `pricing_download_retry_`), so other processes can have their own independent retry settings later.
- Q: When should the retention process run: on its own schedule, or as the last step of each pipeline run? → A: As the last step of every pipeline run; there is no separate retention schedule. If the run superseded files and the grace period is ≤ `superseded_file_inline_wait_max_minutes` (default 5), the step waits out the rest of the grace period and deletes them in the same run. Otherwise they are cleaned up by the next run. Only superseded-file deletion waits. The grace period default is lowered to 5 minutes. A retention failure doesn't change the snapshot's status: it is reported and alerted separately. The run keeps its in-progress claim during the wait. On-demand retention (including dry-run) stays available.
- Q: Can the pipeline run outside AWS (e.g., on a home server) while still publishing to cloud storage, and can the web app read the data from outside AWS? → A: Yes. The AWS schedule can be disabled by configuration while an off-AWS machine runs the same packaged pipeline against cloud storage, producing identical output. Off-AWS machines (pipeline writers and web app readers) get short-lived credentials by assuming dedicated roles with an X.509 certificate (IAM Roles Anywhere), not long-lived access keys. Readers see the same data whichever location produced it.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Weekly snapshot runs in the cloud without the owner (Priority: P1)

As the owner, I want the weekly pricing download to run in the cloud on schedule, for every configured region, and store its output durably. That way no week of prices is lost when my laptop is off or the web app is shut down.

**Why this priority**: Historical prices cannot be recovered later. An unattended, reliable weekly run is the core value of the feature. Everything else builds on it.

**Independent Test**: Provision the environment, trigger the scheduled job (or wait for the schedule), and verify that compressed raw files and all five tables for every configured region exist in cloud storage for that snapshot date, and that no compute stays running afterwards.

**Acceptance Scenarios**:

1. **Given** a provisioned environment with the default schedule (Monday 13:00 UTC) and 7 configured regions, **When** the schedule fires, **Then** one run downloads pricing for all 7 regions and writes compressed raw files and all five tables for that snapshot date to cloud storage.
2. **Given** a run has finished (successfully or not), **When** the owner checks the compute in use, **Then** nothing from that run is still running or billing.
3. **Given** the schedule is changed in configuration (e.g., to Tuesday 06:00 UTC) and redeployed, **When** the new time arrives, **Then** the run starts at the new time and not the old one.
4. **Given** a region is added to the configured region list, **When** the next run happens, **Then** that region is included with no code change.

---

### User Story 2 - Each snapshot is described by a published manifest (Priority: P1)

As the web app (the consumer), I want every snapshot to come with a single manifest that says what data exists, where it is, whether it is complete, and how to verify it. I also want a per-provider "latest" pointer to the newest complete snapshot. That way I never have to list directories or look for marker files.

**Why this priority**: The manifest is the only integration point between the two repos. The app feature cannot start until this contract exists.

**Independent Test**: Run the pipeline for a snapshot date. Then, using only `latest.json` and the manifest it points to, locate every data file, verify each file's size, checksum and row count, and confirm the manifest's status and region lists match what ran.

**Acceptance Scenarios**:

1. **Given** a run where every region succeeds, **When** it finishes, **Then** a manifest with status `succeeded` exists at `<root>/aws/manifests/<snapshot_date>/manifest.json`, and `<root>/aws/manifests/latest.json` points to it.
2. **Given** a manifest, **When** a consumer checks each listed file, **Then** every file exists and matches the manifest's size, checksum and row count.
3. **Given** a run in progress, **When** a consumer reads `latest.json`, **Then** it still points to the previous `succeeded` snapshot, never to a snapshot whose files are not all written.
4. **Given** a manifest with a `manifest_version` or table data schema version the consumer does not support, **When** the consumer reads it, **Then** the contract requires the consumer to reject it (the contract documents this rule).
5. **Given** the manifest is in place, **When** a snapshot is produced, **Then** no `_SUCCESS`, `_REGION_COMPLETE` or `.locks/` artifacts are needed or produced as a consumer-facing signal.

---

### User Story 3 - Failures and missed runs reach the owner quickly (Priority: P1)

As the owner, I want to be alerted when a run fails, partly fails, or doesn't happen at all, so I can fix it before the week's prices are gone.

**Why this priority**: A silent failure means lost data. Alerting is what makes unattended running safe.

**Independent Test**: Force one region to fail and confirm a `partial` manifest and an alert. Separately, disable the schedule and confirm the missed-run alert fires within the configured window.

**Acceptance Scenarios**:

1. **Given** a run where 1 of 7 regions fails, **When** the run ends, **Then** the other 6 regions' data is written, a manifest with status `partial` lists the requested, succeeded and failed regions (each failure with a short reason), `latest.json` is not moved, and the owner receives an alert within 15 minutes.
2. **Given** a run that crashes before writing any data, **When** it ends, **Then** a manifest with status `failed` is written if possible, and the owner is alerted either way.
3. **Given** no `succeeded` snapshot has been published for 8 days (the default window), **When** the window passes, **Then** the owner receives a missed-run alert, even if the job never started.
4. **Given** success summaries are enabled, **When** a run succeeds, **Then** the owner gets a short summary (regions, row counts, duration). **Given** they are disabled, **Then** no success message is sent.
5. **Given** a run has finished, **When** the owner looks for its logs within the configured log retention (default 30 days), **Then** they are available in the cloud.

---

### User Story 4 - Owner repairs a snapshot without a full re-run (Priority: P2)

As the owner, I want to re-run only the failed regions for a snapshot date, or regenerate only the tables from raw data already stored, so I can fix partial failures and transform bugs without repeating (or being unable to repeat) the whole download.

**Why this priority**: Partial failures and transform bugs will happen. Targeted repair keeps data correct at low cost, but the weekly run and alerts come first.

**Independent Test**: After a `partial` run, trigger a re-run for just the failed region and the same snapshot date, and confirm a new `succeeded` revision moves `latest.json`. Separately, trigger a transform-only run for a date whose raw data is still retained, and confirm the tables are rebuilt without any pricing download and the manifest revision increments.

**Acceptance Scenarios**:

1. **Given** a `partial` manifest for 2026-10-05 with `eu-west-1` failed, **When** the owner triggers an on-demand run for `eu-west-1` and snapshot date 2026-10-05, **Then** only that region is downloaded, a new manifest revision covers all 7 regions with status `succeeded`, and `latest.json` points to it.
2. **Given** raw data for a snapshot date is still within retention, **When** the owner triggers a transform-only run for that date, **Then** the tables are rebuilt from the stored raw data with no call to the pricing source, and a new manifest revision is published.
3. **Given** raw data for a snapshot date has already been purged, **When** the owner triggers a transform-only run for that date, **Then** the run refuses with a clear message and changes nothing.
4. **Given** a run for 2026-10-05 is in progress, **When** a second run for the same snapshot date is started, **Then** the second run is refused immediately with a clear "run already in progress" message, writes nothing, and the first run's output and manifest are unaffected. If the refused run was scheduled, the owner receives an alert.
5. **Given** two manifest revisions for the same snapshot date, **When** a consumer compares them, **Then** it can tell them apart by run identifier and revision number, and knows which is newer.
6. **Given** revision 1 of 2026-10-05 lists files p1 and p2, and a consumer is reading them, **When** a re-run writes p3, p4 and p5 and publishes revision 2, **Then** p1 and p2 are unchanged until revision 2's manifest is written, and they remain readable for at least the configured superseded-file grace period after that.
7. **Given** a single-region re-run for `eu-west-1`, **When** revision 2 is published, **Then** it lists new files for `eu-west-1` and the existing, unchanged files for the other regions, and those unchanged files are not treated as superseded.

---

### User Story 5 - Storage cost stays bounded by rule (Priority: P2)

As the owner, I want raw data and old table snapshots removed automatically by a documented rule, and I want to be able to save raw data before it is removed, so storage costs stay predictable without manual cleanup.

**Why this priority**: Costs grow every week without retention. But the first months of running are safe without it, so it ranks after the run, the manifest and alerts.

**Independent Test**: Seed storage with raw data and table snapshots spanning more than 12 months. Run retention in dry-run mode and check the reported deletions against the rules. Then run it for real and confirm storage and manifests match the dry-run report.

**Acceptance Scenarios**:

1. **Given** raw data older than 30 days (the default), **When** retention applies, **Then** that raw data is deleted, and raw data 30 days old or newer is kept.
2. **Given** table snapshots older than 12 months (the default), **When** retention runs, **Then** for each calendar month only the earliest `succeeded` snapshot is kept, and the other snapshots from that month are deleted.
3. **Given** a snapshot is deleted by retention, **When** a consumer looks at its manifest, **Then** the manifest is still at its usual location with status `purged`, a purge timestamp, and no file list, so no active manifest points at deleted data.
7. **Given** a file is listed by the current manifest of any snapshot that isn't purged, **When** retention runs (for superseded files, snapshot thinning or any other reason), **Then** that file is not deleted, and the dry-run report never lists it for deletion.
8. **Given** a re-run supersedes files, and the grace period is 5 minutes with an inline wait limit of 5 minutes, **When** the run's final retention step starts 2 minutes after the new manifest was published, **Then** it waits about 3 minutes, deletes the superseded files, and the run ends.
9. **Given** the grace period is set above the inline wait limit, **When** a run supersedes files, **Then** its retention step doesn't wait, and the files are deleted by the next run's retention step.
10. **Given** the retention step fails after a `succeeded` manifest was published, **When** the run ends, **Then** the manifest stays `succeeded`, `latest.json` still points to it, and the owner receives a retention-error alert.
4. **Given** retention runs in dry-run mode, **When** it finishes, **Then** it reports exactly what it would delete, and nothing is deleted.
5. **Given** `latest.json` points to a snapshot that the age rules would otherwise delete, **When** retention runs, **Then** that snapshot is kept.
6. **Given** raw snapshots in storage, **When** the owner runs the documented listing command, **Then** they see each raw snapshot with its purge date. **When** they run the documented download command for one date, **Then** that snapshot's raw files are copied to a local directory.

---

### User Story 6 - Environment is reproducible from code and deployed from CI (Priority: P2)

As the owner, I want every piece of pipeline infrastructure defined in code, parameterized by environment, and deployed from CI with a manual approval for production, so I can rebuild the environment from scratch and never click through a console.

**Why this priority**: This protects against drift and makes recovery possible. The pipeline can deliver value before CI is fully automated, but not without infrastructure-as-code.

**Independent Test**: In an AWS account that has only the one-time setup applied, apply the pipeline infrastructure and trigger a run that succeeds. Then destroy and re-apply the pipeline stack and confirm the schedule works again and existing data is intact.

**Acceptance Scenarios**:

1. **Given** an empty AWS account, **When** the owner applies the one-time account setup and then the pipeline infrastructure for `prod`, **Then** everything needed exists (storage with retention rules, image registry, schedule, compute, permissions, alerts, logs), and the first manually triggered run succeeds.
2. **Given** the infrastructure is parameterized by environment name, **When** it is planned for `dev` or `qa`, **Then** every resource name is namespaced by that environment and nothing collides with `prod` (only `prod` is actually provisioned now).
3. **Given** a pull request, **When** CI runs, **Then** the tests run and an infrastructure plan is shown for any infrastructure change. **Given** a merge to `main`, **Then** the image is built and published, and infrastructure changes are applied to `prod` only after a manual approval.
4. **Given** CI deploys to AWS, **When** it authenticates, **Then** it uses short-lived federated credentials, and no long-lived AWS keys are stored in CI.
5. **Given** the pipeline stack is destroyed and re-applied, **When** it finishes, **Then** the data storage and its contents survive, and the weekly schedule works again with no manual console steps.
6. **Given** monthly AWS spend reaches the warning threshold (default $15) or the alert threshold (default $25), **When** the budget check runs, **Then** the owner is notified.

---

### User Story 7 - Same pipeline runs locally for development (Priority: P3)

As the owner, I want to run the same packaged pipeline on my laptop against a local directory, with no AWS account needed beyond the public pricing API, so I can develop and reproduce cloud runs locally.

**Why this priority**: The local workflow already exists. This story is about not breaking it and giving cloud and local runs one code path.

**Independent Test**: Run the packaged pipeline locally with a local directory as the storage root, and confirm the same layout, manifest and `latest.json` are produced as in the cloud.

**Acceptance Scenarios**:

1. **Given** a local directory configured as the storage root, **When** the owner runs the packaged pipeline locally for one region, **Then** it produces the same layout, compressed raw files, tables and manifest as a cloud run would, under that directory.
2. **Given** no cloud storage credentials are available, **When** the owner runs locally, **Then** the run succeeds, and the only external dependency is the AWS pricing source.
3. **Given** the existing ad hoc single-region command-line tool, **When** the owner uses it after this feature, **Then** it still works against a local directory.

---

### User Story 8 - Operator uploads existing local history one snapshot at a time (Priority: P3)

As the owner, I want a script that uploads one existing local table snapshot to cloud storage with a proper manifest, so I can choose which of the roughly 20 historical weeks become part of the cloud history. Those prices can't be downloaded again.

**Why this priority**: The history is valuable and can't be recovered, but it's safe on the laptop until uploaded, and the weekly cloud run matters more.

**Independent Test**: For a local snapshot date with no cloud data, run the script and confirm the table files and a manifest appear in cloud storage and pass manifest verification. Run it again without overwrite and confirm it aborts without changing anything. Run it with overwrite and confirm the data is replaced.

**Acceptance Scenarios**:

1. **Given** a local table snapshot for 2026-06-01 with no manifest, and no cloud data for that date, **When** the operator runs the upload script for 2026-06-01, **Then** a manifest is created describing the local table files, and the table files and manifest are copied to cloud storage in the standard layout.
2. **Given** a local snapshot that already has a manifest, **When** the operator uploads it, **Then** the existing manifest is used and not regenerated.
3. **Given** any data or manifest for 2026-06-01 already exists in cloud storage, **When** the operator runs the script without the overwrite option, **Then** it aborts with a clear message before copying anything.
4. **Given** cloud data for 2026-06-01 already exists, **When** the operator runs the script with the overwrite option, **Then** the local snapshot is published as a new revision that supersedes the existing cloud data, and the manifest is written after all data files.
5. **Given** a local snapshot that has raw pricing files alongside its tables, **When** it is uploaded, **Then** no raw data is copied to cloud storage.
6. **Given** the operator names a snapshot date with no local table data, **When** the script runs, **Then** it fails with a clear message and changes nothing.

---

### User Story 9 - Pipeline or web app runs outside AWS against cloud storage (Priority: P3)

As the owner, I want to switch the AWS schedule off for a while and run the weekly pipeline on my home server instead, still publishing to cloud storage. I also want the web app to read that data whether it runs in AWS or on my own machines, with no long-lived AWS keys on any machine outside AWS.

**Why this priority**: This gives flexibility in where compute runs, and a fallback if the cloud schedule has problems. Cloud-only operation already covers the core value.

**Independent Test**: Disable the AWS schedule. On a machine outside AWS, holding only a client certificate, run the packaged pipeline against cloud storage. Then confirm that a web app reader, both in AWS and on a local machine, can find and verify the snapshot through `latest.json` and the manifest, exactly as for a cloud-produced snapshot.

**Acceptance Scenarios**:

1. **Given** the AWS schedule is disabled by configuration and the rest of the cloud infrastructure stays deployed, **When** the scheduled time passes, **Then** no cloud run starts and no "schedule failed" alert is raised.
2. **Given** an off-AWS machine with a valid client certificate registered as a pipeline writer, **When** it runs the packaged pipeline with cloud storage as the root, **Then** it gets short-lived credentials, and the output (layout, manifest, `latest.json`, retention, alerts) is the same as a cloud run's. The only difference is provenance (`run.trigger` and pipeline version).
3. **Given** snapshots produced by a mix of cloud and off-AWS runs, **When** a reader (in AWS via its role, or outside AWS via a reader certificate) follows `latest.json` → manifest → files, **Then** it reads and verifies them identically and needs no knowledge of where they were produced.
4. **Given** a reader certificate, **When** the reader tries to write or delete any object, **Then** access is denied.
5. **Given** a machine's certificate is revoked or removed from the allowed list, **When** that machine next requests credentials, **Then** it is refused, and other machines are unaffected.
6. **Given** the off-AWS writer stops producing snapshots, **When** the missed-run window passes, **Then** the missed-run alert fires, exactly as for cloud runs.
7. **Given** an off-AWS run fails with an unhandled error, **When** the process exits, **Then** it makes a best-effort attempt to send a crash alert before exiting non-zero.

---

### Edge Cases

- **All regions fail**: The manifest status is `failed`, `latest.json` does not move, and an alert is sent.
- **Crash before any manifest is written** (out of memory, killed, image fails to start): The missed-run alert still catches it within the window. A failure alert is sent if the platform reports the failed job.
- **Pricing source returns an empty or truncated file for a region**: This is treated as a temporary error and retried per FR-047. If it still fails after the retries, the region is recorded as failed with a reason. It is never silently recorded as succeeded with zero rows.
- **Temporary errors during a download**: The region is retried per the configured `pricing_download_retry_*` settings. A region that succeeds on a retry counts as succeeded, and its manifest entry notes how many attempts it took.
- **Re-run for a date whose earlier revision was `succeeded`**: A new revision is written. `latest.json` moves only if the new revision is also `succeeded` and is the newest `succeeded` snapshot.
- **Consumer reads a revision just as it is superseded**: The files stay readable for the grace period. A consumer that holds an old manifest longer than the grace period must re-read `latest.json` / the current manifest.
- **Re-run fails after writing some new files**: The current manifest is not switched. The orphaned new files are not listed by any current manifest and are cleaned up by retention like superseded files.
- **Re-run for an older snapshot date while a newer `succeeded` snapshot exists**: `latest.json` keeps pointing to the newest snapshot date, not the most recently written revision.
- **A calendar month older than 12 months with no `succeeded` snapshot**: Nothing is kept as that month's representative because of the `succeeded` rule. Non-succeeded snapshots in that month are deleted.
- **Manifest writing fails after data files are written**: The data is invisible to consumers (no manifest, `latest.json` unchanged), the run is reported as failed, and the owner is alerted.
- **Retention runs while a run for the same snapshot date is in progress**: Retention does not delete data that an in-progress run is writing or has just written.
- **Schedule fires twice for the same week** (e.g., retry after a platform hiccup): If the first run is still in progress, the second is refused and the owner is alerted. If the first run has already finished, the second produces at most one extra revision. There is no corruption either way.
- **A run crashes while holding the in-progress claim**: The claim expires after its bounded time, and a later re-run for that date is accepted.
- **Configured region is invalid or no longer offered**: That region fails with a clear reason, and the others continue.
- **The AWS schedule and an off-AWS writer are both active for the same date**: The run claim refuses whichever starts second. There is no corruption.
- **The off-AWS machine's clock is wrong**: Run-claim expiry and grace periods compare local time with storage timestamps. The machine must keep its clock synchronized (NTP). The run refuses to start if its clock differs from the storage service's time by more than 5 minutes.
- **The off-AWS process is killed (e.g., out of memory) and can't alert**: Only the missed-run alert detects this. Off-AWS operators can shorten the missed-run window while running off-AWS.
- **The client certificate expires**: Credential requests fail, and the run fails before doing any work. The missed-run alert catches a prolonged outage. The documented rotation procedure issues a new certificate before expiry.

## Requirements *(mandatory)*

### Functional Requirements

**Execution & scheduling**

- **FR-001**: The pipeline MUST be packaged as a single deployable image that supports scheduled runs, on-demand runs, transform-only runs and retention runs, selected by command or arguments.
- **FR-002**: The system MUST run the pipeline weekly on a configurable schedule (default Monday 13:00 UTC) with no always-on orchestrator or compute.
- **FR-003**: One scheduled run MUST cover all configured regions. A failure in one region MUST NOT stop the other regions.
- **FR-047**: When a region's pricing download hits a temporary error (timeout, throttling, dropped connection, or an empty or truncated response), the pipeline MUST retry that region's download before marking the region failed, using these settings, which apply only to pricing downloads:
  - `pricing_download_retry_max_retries`: the number of retries after the first attempt (default 3; 0 disables retries).
  - `pricing_download_retry_strategy`: one of `fixed` (wait the base delay before every retry), `linear_backoff` (wait base delay × retry number) or `exponential_backoff` (wait base delay × 2^(retry number − 1)). The default is `exponential_backoff`.
  - `pricing_download_retry_base_delay_seconds`: the base delay used by the strategy (default 30).

  Errors that can't be fixed by retrying (e.g., the region isn't offered, or access is denied) MUST fail the region without retrying. When retries run out, the region's failure reason MUST include the last error and the number of attempts. Any future retrying process MUST use its own separately named settings and MUST NOT reuse these.
- **FR-004**: The owner MUST be able to trigger a run on demand for a given snapshot date, for all regions, a subset of regions, or transform-only from stored raw data.
- **FR-005**: Two concurrent runs for the same snapshot date MUST NOT corrupt each other's data or manifests. A run that starts while another run for the same snapshot date is in progress MUST be refused immediately: it writes no data or manifest and logs a clear "run already in progress" reason. If the refused run was a scheduled run, the owner MUST be alerted. An in-progress claim left behind by a crashed run MUST NOT block later runs forever. It MUST expire after a bounded time, no shorter than the longest expected run.
- **FR-006**: Compute used by a run MUST be released when the run ends, whether it succeeds or fails.
- **FR-007**: The existing local orchestration (Dagster) MUST NOT be required in the cloud. It MUST remain available as an optional local-dev runner that calls the same pipeline entry point as the packaged image, so the local and cloud runs share one pipeline logic.

**Storage**

- **FR-008**: The storage root MUST be configurable as either a local directory or a cloud storage location, with the same pipeline logic used for both.
- **FR-009**: Raw pricing files MUST be stored compressed.
- **FR-010**: The storage layout MUST separate provider (`aws` now, with room for `gcp` and `azure`), data kind (raw, tables, manifests) and snapshot date, so new providers can be added without restructuring existing data.
- **FR-011**: Cloud storage MUST block all public access, MUST be encrypted at rest, and MUST give the web app read-only access.
- **FR-012**: The cloud data store MUST be protected from deletion when the pipeline infrastructure is torn down or re-created.

**Snapshot manifest** (shared contract; this repo is authoritative)

- **FR-013**: Every run that produces or revises a snapshot MUST write a manifest, and only after all of that snapshot's data files are written.
- **FR-014**: The per-provider `latest.json` pointer MUST be updated only after a `succeeded` manifest is written, MUST never point to a `partial`, `failed` or `purged` manifest, and MUST always point to the newest `succeeded` snapshot date.
- **FR-015**: Failed and partial runs MUST also write a manifest (where the run gets far enough to do so) that records what was requested, what succeeded and what failed, with a short reason for each failure.
- **FR-016**: Re-running a snapshot date MUST produce a new manifest revision that consumers can tell apart from earlier ones by run identifier and a monotonically increasing revision number. Earlier revision manifests MUST be kept as history, separate from the current manifest.
- **FR-043**: Each revision MUST write its data files to new, revision-specific paths and MUST NOT modify or overwrite files listed by any earlier revision. A revision MAY list unchanged files from an earlier revision (e.g., regions not re-run) rather than copying them. This applies equally to local-directory and cloud storage, and to the history upload script's overwrite mode.
- **FR-044**: The manifest's file list MUST be the only way consumers find data files. The contract MUST state that consumers never discover data files by listing or scanning directories, since superseded files may still be present.
- **FR-017**: The manifest MUST contain at least the fields defined under **Snapshot Manifest** in Key Entities.
- **FR-018**: The contract MUST state that consumers reject manifests with an unsupported `manifest_version` or table data schema version.
- **FR-019**: The manifest MUST replace `_SUCCESS`, `_REGION_COMPLETE` and `.locks/` as the consumer-facing completion signal, and those markers MUST be removed from the pipeline, coordinated with the app feature.

**Retention**

- **FR-020**: Raw data MUST be deleted automatically after a configurable period (default 30 days), preferably by native storage lifecycle rules rather than custom code.
- **FR-021**: Table snapshots MUST be kept for a configurable period (default 12 months). After that period, only the earliest `succeeded` snapshot in each calendar month MUST be kept, and the rest MUST be deleted.
- **FR-022**: When retention deletes a snapshot's data, the snapshot's current manifest MUST stay at its location, be rewritten with status `purged` and a purge timestamp, and have its file list removed, so no active manifest points at deleted data. Consumers MUST skip `purged` manifests.
- **FR-048**: Retention (superseded and orphaned file cleanup, snapshot thinning, marking manifests `purged`) MUST run as the final step of every pipeline run (scheduled or on-demand, including transform-only runs), after the manifest and `latest.json` are written. There is no separate retention schedule. Raw data deletion stays with the storage lifecycle rules (FR-020). Retention MUST also be runnable on demand on its own, including in dry-run mode (FR-023).
- **FR-049**: If the run superseded files and the grace period is at most `superseded_file_inline_wait_max_minutes` (default 5), the retention step MUST wait only for the remaining grace time (grace period minus time elapsed since the superseding manifest was published) and then delete those files in the same run. If the grace period is longer, the step MUST NOT wait, and those files MUST be deleted by a later run's retention step once their grace period has passed. Thinning and purging MUST NOT wait. The run MUST keep its in-progress claim (FR-005) until the retention step finishes.
- **FR-050**: A retention-step failure MUST NOT change the status of the snapshot the run published, and MUST NOT roll back `latest.json`. It MUST be reported in the run's outcome and alerted to the owner as a retention error.
- **FR-046**: Retention MUST NOT delete any file that is listed by an **active manifest** (the current manifest revision of any snapshot whose status isn't `purged`). The file-removal process MUST compute the set of files referenced by active manifests and MUST re-check a file against that set immediately before deleting it. This guard applies to every deletion path: superseded files (FR-045), orphaned files from failed runs, and snapshot thinning (FR-021).
- **FR-023**: Retention MUST support a dry-run mode that reports what it would delete without deleting anything.
- **FR-024**: Retention MUST NOT delete the snapshot that `latest.json` points to.
- **FR-045**: Retention MUST delete superseded files (files not listed by a snapshot's current manifest revision) only after a configurable grace period, expressed in minutes (`superseded_file_grace_minutes`, default 5), has passed since the revision that superseded them was published. Files listed by the current revision MUST never be treated as superseded. Superseded-file deletions MUST appear in the dry-run report.
- **FR-025**: The owner MUST have documented commands to list raw snapshots with their purge dates and to download one raw snapshot to a local directory.

**Alerting & observability**

- **FR-026**: Failed and partial runs MUST send an alert to the owner by email.
- **FR-027**: The system MUST alert the owner if no `succeeded` snapshot has been published within a configurable window (default 8 days), including when the job never starts.
- **FR-028**: A success summary (regions, row counts, duration) MUST be available and switchable on or off by configuration.
- **FR-029**: Run logs MUST be kept in the cloud for a configurable period (default 30 days).
- **FR-030**: The account MUST have a budget alert with configurable warning and alert thresholds (defaults $15 and $25 per month).

**Infrastructure & delivery**

- **FR-031**: All pipeline infrastructure (storage and lifecycle rules, image registry, schedule, compute, permissions, alerts, log retention) MUST be defined as code and reproducible from it.
- **FR-032**: The infrastructure MUST take an environment name (`dev`, `qa`, `prod`) that namespaces every resource. Only `prod` is provisioned in this feature.
- **FR-033**: A one-time account setup, kept in this repo and applied first, MUST provide shared remote infrastructure state with locking, federated CI access with no long-lived keys, and the budget alert.
- **FR-034**: CI MUST run tests on pull requests, build and publish the image on merges to `main`, show an infrastructure plan for infrastructure changes, and apply to `prod` only after a manual approval.
- **FR-035**: Settings (alert email, regions, schedule, retention periods, superseded-file grace period and inline wait limit, pricing-download retry settings, alert window, log retention, budget thresholds) and secrets MUST come from configuration and MUST NOT be hardcoded.

**Local development**

- **FR-036**: The packaged pipeline MUST run on a laptop against a mounted local directory, with no AWS account needed beyond access to the public pricing source.
- **FR-037**: The existing ad hoc single-region command-line workflow MUST keep working against a local directory.

**Existing history**

- **FR-038**: The feature MUST provide an operator-run upload script that takes one snapshot date and an optional overwrite flag, and uploads that date's local table snapshot to cloud storage. The feature MUST NOT itself transfer any history.
- **FR-039**: If the local snapshot has no manifest, the upload script MUST create one (per FR-017) from the local table files before uploading. If a manifest already exists, the script MUST use it.
- **FR-040**: The upload script MUST abort, before copying anything, if any data or manifest for that snapshot date already exists in cloud storage, unless the overwrite option is given. With overwrite, the local snapshot MUST be published as a new revision for that date (per FR-043), superseding the existing cloud data.
- **FR-041**: The upload script MUST copy data files before the manifest, following the same ordering and `latest.json` rules as a pipeline run (FR-013, FR-014).
- **FR-042**: Raw pricing data MUST never be uploaded from local history to cloud storage.

**Off-AWS operation**

- **FR-051**: The cloud schedule MUST be switchable off and on by configuration alone, without removing any other pipeline infrastructure.
- **FR-052**: The packaged pipeline MUST support running on a machine outside AWS with cloud storage as its root, with behavior identical to a cloud run (claims, revisions, manifest, `latest.json`, inline retention, alerts). Provenance MUST record where it ran: a `run.host` value such as `aws-ecs` or a configured host label.
- **FR-053**: Machines outside AWS MUST get only short-lived AWS credentials, by assuming a dedicated role with an X.509 client certificate issued by an owner-controlled certificate authority. Long-lived access keys MUST NOT be required or created.
- **FR-054**: There MUST be two separate external roles per environment:
  - A **pipeline writer** role: read/write on the data store's provider prefixes, publishing to the alert topic, the pricing-source read permissions the pipeline needs, and pulling the pipeline image.
  - A **data reader** role: read-only on the data store, with the same access as the web app's in-AWS read policy.
- **FR-055**: Which certificates may assume each external role MUST be configurable per environment (by certificate subject). Removing a subject or revoking its certificate MUST stop that machine getting credentials without affecting others. With no subjects configured, nothing outside AWS can assume the role.
- **FR-056**: The published data MUST be independent of where it was produced. Readers MUST NOT need to know, or behave differently based on, whether a snapshot was produced in AWS or outside it.
- **FR-057**: On an unhandled error, the pipeline process MUST make a best-effort attempt to send a crash alert before exiting non-zero, wherever it runs.

### Key Entities *(include if feature involves data)*

- **Snapshot**: All pricing data for one provider and one snapshot date across the configured regions. It has one or more manifest revisions, raw files per region, and table files per table and region.
- **Snapshot Manifest**: The authoritative description of one snapshot revision. It lives at `<root>/<provider>/manifests/<snapshot_date>/manifest.json`. Minimum contents:
  - *Identity*: `manifest_version`, `provider`, `snapshot_date`, `run_id`, `revision`, `created_at`.
  - *Status*: `succeeded` | `partial` | `failed` | `purged`; regions requested, succeeded and failed, each failure with a short reason.
  - *Data*: for each table, its data schema version and its files grouped by region. Each file has a path, size in bytes, checksum (e.g., sha256) and row count.
  - *Provenance*: pipeline version (source revision / image tag), run start and end times, raw data location, and raw data purge date.
  - *Ordering rule*: data files are written first, then the manifest, then `latest.json`.
  - *Revisions*: each revision's data files have revision-specific paths. The current manifest for a date names the current revision, and earlier revision manifests are kept as history. The file list is the only valid way to find data files.
- **Active Manifest**: The current manifest revision of a snapshot whose status isn't `purged`. Only active manifests may be used to find readable data. Historical revision manifests and `purged` manifests are not active and may refer to files that no longer exist.
- **Latest Pointer** (`<root>/<provider>/manifests/latest.json`): Identifies the manifest of the newest `succeeded` snapshot for a provider.
- **Raw Snapshot**: Compressed pricing files as downloaded from the source for one snapshot date and region. They are subject to short retention (default 30 days) and have a known purge date.
- **Run**: One execution of the pipeline (scheduled, on-demand, transform-only or retention), with a unique run identifier, a mode, a target snapshot date, target regions, an outcome and logs.
- **Environment**: A named deployment (`dev`, `qa`, `prod`) whose resources are all namespaced by that name.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Four consecutive weekly runs complete in the cloud with no manual steps, and each publishes a `succeeded` manifest that `latest.json` points to.
- **SC-002**: A simulated single-region failure produces a `partial` manifest and an owner alert within 15 minutes of the run ending. A single-region re-run then produces a `succeeded` revision that becomes latest.
- **SC-003**: With the schedule disabled, the missed-run alert reaches the owner within the configured window (default 8 days) plus at most 1 day.
- **SC-004**: At any time, stored raw data covers no more than about 30 days of snapshots (plus the storage provider's lifecycle lag, at most about 2 days).
- **SC-005**: For a seeded history longer than 12 months, the dry-run retention report matches the retention rules exactly, and the real run deletes exactly what the dry run reported.
- **SC-006**: 100% of files listed in any active manifest exist and match their recorded size, checksum and row count. No active manifest references deleted data, including right after any retention run.
- **SC-007**: The pipeline's own cloud costs (compute, storage, image registry, logs, alerts) stay under $3 per month at the current scale (7 regions, weekly).
- **SC-008**: Destroying and re-applying the pipeline stack (with the data store protected) restores a working weekly schedule with no manual console steps, and all previously stored data is intact.
- **SC-009**: A local run of the packaged pipeline for one region produces the same layout and an equivalent manifest to a cloud run, with no cloud storage credentials configured.
- **SC-010**: With the AWS schedule disabled, an off-AWS writer produces 4 consecutive weekly `succeeded` snapshots in cloud storage. A web app reader, once in AWS and once outside AWS, reads and verifies the latest snapshot with identical results. No long-lived AWS access keys exist on any off-AWS machine.

## Assumptions

- **Decided platform constraints** (from the owner, recorded here rather than in requirements): AWS as the cloud; OpenTofu/Terraform for infrastructure; Docker as the packaging standard, using a `Dockerfile` in this repo; GitHub Actions for CI/CD with OIDC federation. Kubernetes is out of scope for this feature.
- The two repos stay separate. The manifest in cloud storage is the only integration point. There is no shared database, and the pipeline never writes to the app's database.
- Budget: the whole project should cost $15–20/month normally, with a hard cap of $20–30/month. The pipeline's share should be a few dollars at most.
- Cadence stays weekly, with the current 7 regions. More regions may be added later by configuration only.
- Each cloud provider gets its own data model. There is no shared cross-cloud pricing schema.
- Alerts go by email only. Push and chat channels are out of scope for now (email was stated as sufficient).
- The data store is protected from infrastructure teardown (e.g., kept in a separate stack or marked non-destroyable), as implied by the destroy/re-apply success criterion.
- Compute sizing (memory, CPU, duration of a 7-region run) and whether one job runs all regions or one job per region are decided at plan time from measurements. Either option must meet FR-003 and FR-005.
- The exact manifest schema (field names, file encoding details) is finalized at plan time within the minimums above, and the app feature consumes whatever this repo publishes.
- The retention "month" is the calendar month of the snapshot date in UTC.
- Uploaded historical snapshots: the manifest's requested and succeeded regions are the regions present locally, and its status is `succeeded`. Raw location and purge date are recorded as absent (no raw data in the cloud). The pipeline version records the upload script's version, and the manifest marks the snapshot as a backfill. An uploaded snapshot moves `latest.json` only if it is newer than the current latest, and it is subject to the normal retention rules.
- Raw data retention is based on the snapshot date (or object creation time, which is within a day of it).
- The certificate authority for off-AWS access is owner-operated: a self-managed CA whose private key is kept offline. A paid managed CA service would exceed the budget. Client certificates are valid for at most 1 year and are rotated by a documented procedure.
- An off-AWS writer has at least 8 GB of RAM, outbound internet access and a synchronized clock. Its outbound data transfer (about 300 MB per weekly run) and the reader's download traffic are within free or negligible transfer tiers.

## Out of Scope

- GCP and Azure pricing feeds (phase 2). The layout and manifest must leave room for them.
- Deploying the web app, and how it consumes the manifest (covered by the app feature).
- Price trend analytics. Retained history only needs to support a future app feature.
- Actually uploading the local history. Only the upload script is delivered, and the operator decides which snapshots to upload.
- Uploading any raw data from local history.
- Provisioning `dev` and `qa` environments (only parameterization is required).
