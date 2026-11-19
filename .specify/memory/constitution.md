<!--
Sync Impact Report
- Version change: 1.3.0 → 1.4.0 (MINOR: materially expanded guidance; no principle removed or
  redefined)
- Modified principles: none.
- Modified sections:
  - Development Workflow → "Operational docs" renamed to "Documentation": `docs/` holds current
    behavior and is updated by the feature that changes it; the README is a stable orientation
    page that doesn't grow per feature; each release adds a `CHANGELOG.md` entry; `specs/<NNN>-*/`
    is the feature's design and validation record and isn't updated after the feature ships.
  - Development Workflow → "Contract changes": schema files are updated in
    `src/pipeline/schemas/`, with `docs/data-contract.md` and a changelog entry; the feature's
    `contracts/` holds the proposal.
- Added sections: none. Removed sections: none.
- Trigger: the README restructure of 2026-11-10 (commit 42fe77d), which moved current-state
  documentation from `specs/003-*` and `specs/004-*` contracts into `docs/`.
- Templates checked (read at runtime, not modified): plan-template.md, spec-template.md,
  tasks-template.md, checklist-template.md. None names the README or a docs location, so none
  conflicts; generated tasks now take their documentation targets from this rule.
- Deferred TODOs: none.
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
- The only exception is a credential for a third-party pricing source that the source issues only as a static key (for example, the GCP Cloud Billing API key). Such a key MUST be restricted at the source to the pricing API, stored as an encrypted parameter in SSM Parameter Store, readable only by that provider's run, and rotatable without a redeploy.
- Every resource MUST be namespaced by environment (`dev`, `qa`, `prod`) and tagged for cost attribution. The only exceptions are **account singletons**: resources that can exist only once per AWS account and serve every environment. These are the IaC state bucket, the GitHub OIDC identity provider and the account budget. They MUST be named `cloud-pricing-shared-*` where naming allows, and tagged `environment=shared`. Any other per-account singleton requires an amendment.
- Production changes MUST go through CI with a manual approval.

**Rationale**: this is a personal project with a fixed budget. Reproducibility and cost limits keep it cheap to run, safe to rebuild, and able to recover without memory of past manual steps.

### VI. Provider-Extensible, Production-Gated

Providers are added one at a time, each only after the existing pipeline works end to end in
production. AWS met this gate with feature 003 (release `v0.0.1`, scheduled in `prod`); Azure and
GCP are the next providers.

- The provider MUST be an explicit, modeled dimension: a storage-layout segment, a manifest field
  and a configuration value. It MUST NOT be an implicit assumption baked into shared module names,
  contract field names or infrastructure resource names.
- Code that is inherently provider-specific (pricing API clients, source parsers, per-provider
  table models) MUST be named and located as such, so adding a provider means adding modules, not
  rewriting shared ones.
- Code MUST be organized so shared logic is written once and provider-specific logic is easy to
  find and change:
  1. Shared code under `src/pipeline/` MUST be provider-neutral. It MUST NOT name a provider or
     branch on one (`if provider == ...`). The single exception is `src/pipeline/providers.py`, the
     registry that maps each provider name to its classes.
  2. Where providers vary, a shared module MUST expose the variation as a base class with small
     overridable methods. The base class holds the logic every provider shares.
  3. Provider-specific code MUST live in a package per provider, `src/<provider>/` (for example
     `src/aws/`, `src/azure/`, `src/gcp/`). A file that extends a shared module MUST have the same
     name as that module: `src/pipeline/http_pages.py` is extended by
     `src/<provider>/http_pages.py`. A provider with no custom behavior uses the base class
     directly; empty subclass files MUST NOT be created.
  4. Imports MUST go one way. Provider packages may import `src/pipeline/`. `src/pipeline/` imports
     provider packages only through `providers.py`. Provider packages MUST NOT import each other.
  5. An automated test MUST enforce rules 1 and 4 by scanning imports and provider names under
     `src/`, so a violation fails CI rather than relying on review.
- Each provider MUST have its own data model, documented as a contract, with its own table schema
  versions. Adding or changing one provider MUST NOT change another provider's tables, schema
  versions or published data.
- Each provider MUST run as its own snapshot, with its own run claim, manifest, `latest.json`,
  schedule, retention and alerts. A failure or outage of one provider's source MUST NOT block,
  delay or change another provider's snapshot.

**Rationale**: every provider's history is irreplaceable on its own. Isolating providers keeps one
source's outage from costing another provider's week, and the production gate keeps the project
from widening before what exists is proven.

### VII. Simplicity & YAGNI

- Choose the simplest design that meets the current requirement.
- Do not build generalized multi-provider frameworks, plugin systems, speculative configuration or extra services ahead of a concrete need. Principle VI's modeling discipline is sufficient protection. The base classes that Principle VI's code-organization rules require are not such a framework: one is added only when a shared module has a concrete variation between providers.
- New complexity (a new stack, service, data store, abstraction layer or paid resource) MUST be justified by a current requirement. It MUST be recorded in the plan's "Complexity Tracking", together with the simpler alternative that was rejected.

**Rationale**: principles I–V demand real rigor, around data safety, the contract and cost. Everything beyond that rigor is cost without present value.

## Technical Constraints

- **Language and data**: Python (currently 3.14) and Parquet for tables. Raw source files are stored compressed. Each cloud provider has its own data model, with no shared cross-cloud pricing schema.
- **Cloud and delivery**: AWS hosts the pipeline for every provider. Azure and GCP prices are read from their public pricing APIs only; no Azure or GCP account resources are created. Delivery uses OpenTofu/Terraform for infrastructure, Docker images as the packaging standard (the same image in the cloud and locally), and GitHub Actions for CI/CD.
- **Integration**: the pipeline and `cloud-pricing-app` are separate repos. Their only integration point is the published manifest and data in storage. The pipeline never writes to the app's database.
- **Local development**: the pipeline MUST remain runnable on a laptop against a local directory. The only external dependency is the provider pricing API.
- **Stack changes**: introducing a new language, data format, data store, cloud provider service category or orchestration platform (for example, Kubernetes) requires a constitution amendment, not a choice made inside a feature branch. The approved AWS service baseline (established by feature 003) is: S3, ECS Fargate, ECR, EventBridge (Scheduler and rules), Lambda, SNS, SQS, CloudWatch (Logs and alarms), IAM (including IAM Roles Anywhere), AWS Budgets, VPC networking without NAT gateways, and SSM Parameter Store (standard tier, SecureString parameters, only for third-party pricing-source credentials; added by feature 004). Using a service outside this list requires an amendment.
- **Security**: the data store blocks public access and is encrypted at rest. Readers get read-only access. Secrets and personal data (for example, alert email addresses) are supplied through CI secrets or variables and are never committed. Third-party pricing-source credentials are supplied locally through a git-ignored environment file and in the cloud from SSM Parameter Store (Principle V). Their values MUST NOT appear in IaC state, task definitions, images, logs, alerts or published data.

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
  - VI: the provider is modeled explicitly, providers stay isolated from each other, shared code is provider-neutral, and provider code sits in its own package with the import rules passing.
  - VII: no unjustified complexity.
- **Analyze gate**: specs, plans and tasks are checked against this constitution by `/speckit-analyze`. Unresolved conflicts block `/speckit-implement`.
- **Contract changes**: changes to the manifest, pointer or table schemas require a version bump in the contract, updated schema files in `src/pipeline/schemas/`, an updated `docs/data-contract.md`, a `CHANGELOG.md` entry, and a note for `cloud-pricing-app`. The feature's `contracts/` holds the proposal.
- **Documentation**: `docs/` MUST describe current behavior (data contract, CLI, configuration, operations, development) and MUST be updated by the feature that changes it. The README is a stable orientation page (purpose, getting started, deploy checklist, consumers, reference links) and MUST NOT grow per feature. Each release MUST add a `CHANGELOG.md` entry. `specs/<NNN>-*/` records a feature's design and validation and is not updated after the feature ships.

## Governance

- **Precedence**: this constitution overrides conflicting practices, templates and plans.
- **Amendments**: propose a change through `/speckit-constitution` in a pull request that states what changed and why, and updates the Sync Impact Report. It takes effect on merge.
- **Versioning** (semantic):
  - MAJOR: removing or redefining a principle incompatibly.
  - MINOR: adding a principle or section, or materially expanding guidance.
  - PATCH: clarifications and wording.
- **Compliance**: the Constitution Check in `/speckit-plan` is the main gate, and `/speckit-analyze` re-checks alignment before implementation. Reviewers verify compliance on every pull request. Unjustified violations block merging.
- **Review**: revisit the constitution when a new provider (GCP or Azure), environment, or consumer is added. Keep it aligned with the sibling `cloud-pricing-app` constitution on the shared concerns: pricing data integrity, provider extensibility, and simplicity.

**Version**: 1.4.0 | **Ratified**: 2026-09-28 | **Last Amended**: 2026-11-10
