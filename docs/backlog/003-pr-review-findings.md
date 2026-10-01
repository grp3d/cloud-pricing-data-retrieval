# PR review findings: feature 003 (cloud pipeline deployment)

Open findings from the GitHub Copilot review of the `003-pipeline-cloud-deployment` PR, for a
future release. Each has an assessment with evidence, so it isn't re-investigated from scratch.

**Recorded:** 2026-10-01
**Context:** the owner plans to adopt a set of engineering standards and re-assess the deployment
pipeline against them. Decide these items, especially the design-level ones, in that review.

## Summary

| # | Finding | File | Copilot severity | Assessment | Recommendation |
|---|---|---|---|---|---|
| R1 | ECR repository must exist before the first deploy, and a pipeline-stack rebuild removes it | `.github/workflows/deploy.yml`, `infra/pipeline/ecr.tf` | High | **Valid** (needs a design decision) | Fix in the next release |
| R2 | `aws_signing_helper` download path and checksums are wrong | `Dockerfile` | High | **Not valid** (disproved) | No change; note for upgrades |
| R3 | OIDC trust patterns lack a literal `context:` segment | `infra/bootstrap/github_oidc.tf` | High | **Not valid** (disproved) | No change; confirm `run`/`apply` on first use |
| R4 | `upload-history` follows manifest paths without containment checks | `src/pipeline/backfill.py` | High | **Valid** (low practical risk) | Fix in the next release |
| R5 | `archive_file` output folder `.build/` won't exist in CI | `infra/pipeline/watchdog.tf` | (previous round) | **Not valid** (disproved) | No change |
| R6 | botocore model test should use fake AWS credentials | `tests/unit/test_storage_local_s3.py` | Medium (previous round) | Valid but cosmetic | Optional one-word fix |

Fixed in the same review round, but **not yet pushed** at the time of writing (5 files, tests pass):
foreign-region prices leaking into `price_fact`, and `upload-history` accepting non-`succeeded`
snapshots. See the branch history once committed.

---

## R1: ECR repository bootstrap and pipeline-stack rebuild

**Finding.** `deploy.yml` logs in to ECR, checks for an existing image and pushes the new image
*before* applying the pipeline stack, which is what creates `aws_ecr_repository.pipeline`. On a fresh
environment the deploy can't bootstrap itself.

**Assessment: valid.**
- For the current prod environment it doesn't matter: the repository was created once by hand
  (`tofu apply -target=aws_ecr_repository.pipeline`), as `infra/README.md` documents.
- Correction to the finding: "check for an existing image" doesn't fail on a missing repository (a
  missing repository reads as "no image"). The **push** fails.
- **Related and more important:** quickstart B8 (destroy and re-create the pipeline stack, SC-008)
  also deletes the repository. The repository has no `force_delete`, so `tofu destroy` will most
  likely **stop with an error** while images exist. If it is force-deleted, the next deploy has
  nowhere to push. Either way SC-008 ("rebuild with no manual console steps") doesn't fully hold
  as built.

**Options.**
1. Move ECR to the `data` stack, the long-lived stack, so it survives pipeline rebuilds like the
   bucket. Cleaner: images are release artifacts, not disposable compute.
2. Keep ECR in the pipeline stack: add a `tofu apply -target=aws_ecr_repository.pipeline` step to
   `deploy.yml` before the build, and set `force_delete = true`, accepting that a rebuild discards
   old images (rollback then rebuilds them from tags).

**Recommendation:** option 1. Update quickstart B8 and `infra/README.md` accordingly, and add a
`tofu test` that asserts where the repository lives.

## R2: `aws_signing_helper` path and checksums (`Dockerfile`)

**Finding.** Linux releases are under an `Amzn2023` path, and the 1.7.0 checksums differ from
the pinned values, so every build gets a 404 or a checksum failure.

**Assessment: not valid.** Evidence, re-checked 2026-10-01:

| URL (version 1.7.0) | HTTP | sha256 of the served file |
|---|---|---|
| `…/releases/1.7.0/Aarch64/Linux/aws_signing_helper` (in use) | 200 | `800fc208b74cdb64…` = pinned `SIGNING_HELPER_SHA256_ARM64` |
| `…/releases/1.7.0/X86_64/Linux/aws_signing_helper` (in use) | 200 | `e932f029b73f9752…` = pinned `SIGNING_HELPER_SHA256_AMD64` |
| `…/releases/1.7.0/Aarch64/Amzn2023/aws_signing_helper` (suggested) | 403 | n/a |
| `…/releases/1.7.0/X86_64/Amzn2023/aws_signing_helper` (suggested) | 403 | n/a |

Both architecture images also built locally with the checksum check passing, and the PR's
`ci / docker` job, which builds both architectures, passed.

**Keep in mind for upgrades:** versions after 1.7.0 weren't available at the `…/Linux/` path when
checked (HTTP 403). Moving to a newer helper means finding its current download location,
recomputing both checksums and rebuilding both architectures.

## R3: OIDC trust patterns and the `context:` segment (`infra/bootstrap/github_oidc.tf`)

**Finding.** With `include_claim_keys = ["repo", "context", "ref"]`, GitHub issues subjects like
`…:context:pull_request:ref:…`, which the trust patterns don't match.

**Assessment: not valid.**
- GitHub's documentation shows the context claim expanding to its value with no `context:` label.
  Its example for `["repo", "context", "job_workflow_ref"]` is
  `repo:octo-org/octo-repo:environment:prod:job_workflow_ref:…`
  (<https://docs.github.com/en/actions/reference/security/oidc>).
- **Direct evidence:** the PR's `ci / plan` job **succeeded**. It assumes
  `cloud-pricing-gha-plan-prod` through the `…:pull_request:ref:refs/pull/*/merge` pattern, which
  couldn't happen if the subject contained `context:`.
- The repository's subject template was confirmed through the GitHub API
  (`use_default: false`, `include_claim_keys: ["repo","context","ref"]`,
  `sub_claim_prefix: repo:grp3d@5554338/cloud-pricing-data-retrieval@1378571708`).

**Still to confirm on first use**, as planned: the `run` role (run the `oidc-subject` workflow on
`main` and compare its `sub` with `tofu output gha_trust_subjects`) and the `apply` role (the first
`v*` release deploy). If either fails to assume its role, compare the token's `sub` with the
patterns before changing anything.

## R4: Path containment in `upload-history` (`src/pipeline/backfill.py`)

**Finding.** For a new-layout source, each `DataFile.path` from the local manifest is joined to
`snap.root` without validation. A path containing `..`, or an absolute path, could make
`upload-history` read and publish a file outside the source directory.

**Assessment: valid.**
- **Practical risk is low:** the manifest is local and the operator runs the command on their own
  machine. Someone who can edit that manifest can already upload any file they like.
- **Still worth fixing:** it's cheap, it hardens an input boundary, and it catches a corrupted or
  hand-edited manifest early. There's currently no check (no `realpath`/`commonpath` validation).

**Recommended fix (test-first):**
1. Validate each path against the layout pattern in `contracts/manifest.schema.json`
   (`<provider>/parquet/<table>/snapshot_date=<D>/region=<R>/part-<run_id>(-n).parquet`), and check
   that the date and region match the snapshot being uploaded.
2. Resolve each source path with `os.path.realpath` and require it to be inside `realpath(snap.root)`.
   This also rejects symlinks that point outside.
3. Refuse with `BackfillPrecondition` (exit 3) before any upload.
4. Tests: a `..` path, an absolute path, a symlink out of the root, and a path for a different
   date or region. Each must be refused with the store unchanged.

## R5: `.build/` folder for the watchdog archive (`infra/pipeline/watchdog.tf`)

**Finding (previous round).** `archive_file` doesn't create the parent of `output_path`, and
`.build/` is git-ignored, so plan and deploy jobs fail.

**Assessment: not valid.** The PR's `ci / plan` job succeeded on a fresh checkout, and planning the
pipeline stack builds this archive. If the folder couldn't be created, the plan would have failed.
The archive provider creates the output directory. No change.

## R6: Fake credentials in the botocore model test (`tests/unit/test_storage_local_s3.py`)

**Finding (previous round).** `test_installed_botocore_supports_conditional_s3_operations` creates a
boto3 client without the `aws_env` fixture. On a host with no credentials, credential lookup could
fail.

**Assessment: valid but cosmetic.** Creating a client doesn't fail without credentials, and the CI
`test` job passes. The suggested fix is one word (add the `aws_env` fixture argument) and isolates
the test from the host. **Optional.**

---

## Suggested handling in the next release

1. **Decide R1** (where ECR lives) as part of the standards-based re-assessment, then implement it with
   tests and updated docs.
2. **Fix R4**, test-first.
3. **Optional:** R6.
4. **Record R2, R3 and R5 as answered**, with the evidence above, so they aren't re-opened unless
   that evidence changes (for example a helper upgrade for R2, or a failed `run`/`apply` login for R3).
