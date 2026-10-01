<!--
Sync Impact Report
- Version change: 1.1.0 → 1.1.1 (PATCH: scope clarifications, no principle added or removed)
- Modified principles:
  - V. Reproducible, Cost-Bounded Operations: the environment-namespacing rule now defines
    "account singletons" (IaC state bucket, GitHub OIDC provider, account budget) as the only
    exception. They are named `cloud-pricing-shared-*` and tagged `environment=shared`.
- Modified sections:
  - Technical Constraints → Stack changes: lists the approved AWS service baseline, established
    by feature 003
- Trigger: /speckit-analyze findings C1 (CRITICAL) and U2 on specs/003-pipeline-cloud-deployment
- Previous amendment (1.1.0): principles VI and VII adopted from cloud-pricing-app; faithful
  transformation added to I; test-first added to IV; review checklist and analyze gate added
- Templates checked (read at runtime, not modified): plan-template.md ✅, spec-template.md ✅,
  tasks-template.md ✅
- Deferred TODOs: none
-->

# Cloud Pricing Data Retrieval Constitution

## Core Principles

### I. Price History Is Irreplaceable

Cloud providers do not publish historical prices. A missed or corrupted snapshot is lost for good.

- Scheduled collection MUST keep running independently of any consumer (for example, the web app).
- Every run outcome other than a full success MUST be visible to the owner: partial, failed, refused, crashed or missed. Silently dropping data (skipping a region, service or file without recording it) is forbidden.
- Code MUST NOT overwrite or delete published data except through documented, rule-based retention. Retention MUST support a dry run, and MUST never delete data that a current manifest references.
- Recovery paths (re-running a region, rebuilding from raw data) MUST exist and be documented.
- Transformations MUST be faithful to the source. The pipeline MUST NOT invent, estimate, default or "fix" pricing values. A value the source doesn't provide, or that can't be parsed, MUST be stored as an explicit null (or reported), never as a guessed value.
- Every published value MUST be traceable to its snapshot revision, the run that produced it, the pipeline version, and the raw source data (while that is retained).

**Rationale**: the project exists to accumulate history that can't be recovered. Losing data is the one failure that can't be fixed later.

### II. The Manifest Is the Contract

Consumers integrate only through published, versioned documents, never through directory structure, marker files or shared databases.

- Every published snapshot MUST be described by a manifest listing each data file with its path, size, checksum and row count.
- Data files MUST be written before the manifest that lists them, and that manifest before any "latest" pointer.
- Published data files MUST be immutable. Changes are made by publishing a new revision.
- The manifest format and each table's schema MUST carry explicit versions. A breaking change MUST bump the version, and consumers MUST reject versions they don't support.
- Published data MUST NOT depend on where it was produced (cloud, laptop or home server).
- This repo is authoritative for the contract. Changes MUST be coordinated with `cloud-pricing-app` before release.

**Rationale**: two independently deployed repos stay decoupled only if the contract is explicit, versioned and verifiable.

### III. Orchestration-Independent Core

Business logic (downloading, transforming, publishing, retention) MUST live in plain Python modules with no dependency on any orchestrator or runtime environment.

- CLIs, the container entry point and Dagster MUST be thin wrappers over the same core functions.
- Storage MUST be accessed through one abstraction, so local directories and cloud storage run identical code paths.
- Behavior MUST be driven by configuration (environment variables and IaC variables) with safe defaults. Values that differ by environment, such as regions, schedules, retention periods, retry settings or addresses, MUST NOT be hard-coded.
- Settings that serve one purpose MUST be named for that purpose (for example, `pricing_download_retry_*`), so unrelated processes never share them by accident.

**Rationale**: the pipeline runs in several places (laptop, container, cloud, home server). One core keeps them behaving identically and testable offline.

### IV. Tested Data Correctness

- Every behavior change MUST ship with automated tests that fail without the change.
- Test-first (red-green-refactor) is **non-negotiable** for logic that transforms pricing data, builds manifests or revisions, derives snapshot status, moves the latest pointer, or decides what retention deletes. A failing test that demonstrates the requirement comes first, then the minimal implementation. Wiring, CLI plumbing and infrastructure code MAY have their tests written afterwards.
- Tests MUST run offline: network and cloud dependencies are replaced by fakes or local emulators (for example, a fake downloader or moto).
- Failure paths MUST be tested as thoroughly as the success path: partial failures, retries, concurrent runs, crashes between steps, and retention edge cases.
- Contract documents (manifest and pointer schemas) MUST be validated in tests against what the code actually writes.
- CI MUST run the full suite on every pull request, and a failing suite blocks merging.

**Rationale**: errors in pricing data are silent and spread to every consumer. Only tests of the failure paths make the guarantees in Principles I and II believable.

### V. Reproducible, Cost-Bounded Operations

- All cloud infrastructure MUST be defined as code and reproducible from it. Manual console changes are forbidden, except for documented one-time steps such as confirming an email subscription.
- Stacks MUST be separated by lifecycle, so irreplaceable data is protected from teardown of the compute stacks.
- Compute MUST be pay-per-use, with no always-on servers or gateways, unless an amendment justifies them.
- The whole project MUST stay within its budget (normally $15–20/month, with a hard cap of $30/month), enforced by an account budget alert. Every new paid resource MUST have its monthly cost estimated in the plan.
- Credentials MUST be short-lived: OIDC for CI, IAM roles in AWS, and Roles Anywhere outside AWS. Long-lived access keys MUST NOT be created.
- Every resource MUST be namespaced by environment (`dev`, `qa`, `prod`) and tagged for cost attribution. The only exceptions are **account singletons**: resources that can exist only once per AWS account and serve every environment. These are the IaC state bucket, the GitHub OIDC identity provider and the account budget. They MUST be named `cloud-pricing-shared-*` where naming allows, and tagged `environment=shared`. Any other per-account singleton requires an amendment.
- Production changes MUST go through CI with a manual approval.

**Rationale**: this is a personal project with a fixed budget. Reproducibility and cost limits keep it cheap to run, safe to rebuild, and able to recover without memory of past manual steps.

### VI. Provider-Extensible, AWS First

AWS is implemented first. No other provider is implemented until the AWS pipeline works end to end in production.

- The provider MUST still be an explicit, modeled dimension: a storage-layout segment, a manifest field and a configuration value. It MUST NOT be an implicit assumption baked into shared module names, contract field names or infrastructure resource names.
- Code that is inherently provider-specific (pricing API clients, source parsers, per-provider table models) MUST be named and located as such, so adding a provider means adding modules, not rewriting shared ones.

**Rationale**: GCP and Azure are known future needs. Careful naming and modeling now makes them an extension rather than a migration, without writing multi-cloud code today.

### VII. Simplicity & YAGNI

- Choose the simplest design that meets the current requirement.
- Do not build generalized multi-provider frameworks, plugin systems, speculative configuration or extra services ahead of a concrete need. Principle VI's modeling discipline is sufficient protection.
- New complexity (a new stack, service, data store, abstraction layer or paid resource) MUST be justified by a current requirement. It MUST be recorded in the plan's "Complexity Tracking", together with the simpler alternative that was rejected.

**Rationale**: principles I–V demand real rigor, around data safety, the contract and cost. Everything beyond that rigor is cost without present value.

## Technical Constraints

- **Language and data**: Python (currently 3.14) and Parquet for tables. Raw source files are stored compressed. Each cloud provider has its own data model, with no shared cross-cloud pricing schema.
- **Cloud and delivery**: AWS, with OpenTofu/Terraform for infrastructure, Docker images as the packaging standard (the same image in the cloud and locally), and GitHub Actions for CI/CD.
- **Integration**: the pipeline and `cloud-pricing-app` are separate repos. Their only integration point is the published manifest and data in storage. The pipeline never writes to the app's database.
- **Local development**: the pipeline MUST remain runnable on a laptop against a local directory. The only external dependency is the provider pricing API.
- **Stack changes**: introducing a new language, data format, data store, cloud provider service category or orchestration platform (for example, Kubernetes) requires a constitution amendment, not a choice made inside a feature branch. The approved AWS service baseline (established by feature 003) is: S3, ECS Fargate, ECR, EventBridge (Scheduler and rules), Lambda, SNS, SQS, CloudWatch (Logs and alarms), IAM (including IAM Roles Anywhere), AWS Budgets and VPC networking without NAT gateways. Using a service outside this list requires an amendment.
- **Security**: the data store blocks public access and is encrypted at rest. Readers get read-only access. Secrets and personal data (for example, alert email addresses) are supplied through CI secrets or variables and are never committed.

## Development Workflow

- **Spec Kit flow**: features follow `/speckit-specify` → `/speckit-clarify` → `/speckit-plan` → `/speckit-tasks` → `/speckit-implement`, on a numbered feature branch with artifacts under `specs/<NNN>-<name>/`.
- **Constitution Check**: every plan MUST evaluate its design against these principles in its "Constitution Check" section. Any deviation MUST be listed under "Complexity Tracking" with its justification and the simpler alternative that was rejected.
- **Pull requests**: all changes land through pull requests with passing CI (tests, image build, and an infrastructure plan when infrastructure changes).
- **Review checklist**: reviewers verify, per principle:
  - I: no silent data loss, and no invented or estimated values.
  - II: write order, immutability and version bumps.
  - III: logic stays out of wrappers, and nothing is hard-coded.
  - IV: tests exist, and test-first was followed where it's required.
  - V: infrastructure is code only, costs are estimated, and there are no long-lived credentials.
  - VI: the provider is modeled explicitly.
  - VII: no unjustified complexity.
- **Analyze gate**: specs, plans and tasks are checked against this constitution by `/speckit-analyze`. Unresolved conflicts block `/speckit-implement`.
- **Contract changes**: changes to the manifest, pointer or table schemas require a version bump in the contract, updated schema files under the feature's `contracts/`, and a note for `cloud-pricing-app`.
- **Operational docs**: the README and feature quickstarts MUST stay accurate for running, re-running, recovering and verifying the pipeline.

## Governance

- **Precedence**: this constitution overrides conflicting practices, templates and plans.
- **Amendments**: propose a change through `/speckit-constitution` in a pull request that states what changed and why, and updates the Sync Impact Report. It takes effect on merge.
- **Versioning** (semantic):
  - MAJOR: removing or redefining a principle incompatibly.
  - MINOR: adding a principle or section, or materially expanding guidance.
  - PATCH: clarifications and wording.
- **Compliance**: the Constitution Check in `/speckit-plan` is the main gate, and `/speckit-analyze` re-checks alignment before implementation. Reviewers verify compliance on every pull request. Unjustified violations block merging.
- **Review**: revisit the constitution when a new provider (GCP or Azure), environment, or consumer is added. Keep it aligned with the sibling `cloud-pricing-app` constitution on the shared concerns: pricing data integrity, provider extensibility, and simplicity.

**Version**: 1.1.1 | **Ratified**: 2026-09-28 | **Last Amended**: 2026-09-28
