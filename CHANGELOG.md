# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.1.0] - 2026-11-02

First release: an AWS deployment of [phebee-extraction](https://github.com/nch-cloud/phebee-extraction)
v1.0.0 on Step Functions and AWS Batch. Everything that reads notes, batches them or writes
PheBee records is the core package, unchanged; this repo adds S3 staging, ECR digest lookup,
Batch job definitions and the orchestration. Python 3.11 or later; CI covers 3.11 to 3.14.

### Pipeline

- A `plan` job splits the input into batches on S3 and writes the per-batch plan; a Distributed
  Map runs each batch's tool chain as Batch jobs, in order; a `transform` job turns each batch's
  output into a PheBee import-ready part file under `{prefix}/jsonl/`. The state machine only
  iterates the plan, so the logic lives in Python and is tested without AWS (S3, ECR and Batch
  are faked).
- Tools are specified at run time by image. `plan` registers a Batch job definition per image
  and settings on first use (`{AppName}-{repository}-{hash}`) and reuses it afterwards; the
  stack has no per-tool parameters. Per stage: `gpu`, `vcpus`, `memory_mib`, `environment`,
  `job_role` (short name, expands to `{AppName}-tool-<name>`) or `job_role_arn`, and an explicit
  `job_definition` / `job_queue` to bypass registration.
- Each stage's container environment is built by the plan and passed through unchanged: the
  I/O URIs (both the neutral and the legacy `*_S3_URI` names), the stack's region as
  `AWS_REGION` / `AWS_DEFAULT_REGION`, the run-level `environment`, then the stage's own.
- Provenance matches a local run: `evidence_creator_id` is `repository:tag` (chained tools joined
  with `+`), `evidence_creator_digest` the ECR digests of the images that ran. Each part file's
  `row_num` is its line number; PheBee's import reads neither it nor the emitted `batch_id`.

### Stack and permissions

- SAM template: data bucket, roles, the utility job definition and the state machine. Existing
  Batch queues are passed in. Optional `S3AccessLogBucketName` / `S3AccessLogPrefix` and a
  365-day default log retention let the stack pass CIS/HIPAA/NIST CloudFormation guardrail
  hooks.
- Tools run as a default role that can only read and write under the run's `batches/` prefix.
  Tools needing more (Bedrock callers) run as a role named `{AppName}-tool-<name>`, the only
  prefix the plan job may assign. `docs/tool-role.yaml` creates one per permission set, with
  the default S3 policy, an optional Bedrock `InvokeModel` policy for given model ARNs, and any
  attached managed policies.
- Batch and ECR clients are created in an explicit region (Batch containers on EC2 have no
  `AWS_REGION`), and the utility job definition sets `AWS_REGION` / `AWS_DEFAULT_REGION`.

### Deployment

- The contract is two steps: `scripts/build_image.sh --push --output` builds the utility image
  from pinned wheels, pushes it and writes its digest-pinned URI; `sam deploy` takes that URI as
  `UtilityImageUri`. Images are tagged from `git describe` plus any `--also-tag`, built for
  `linux/arm64` by default (`PLATFORM` overrides; must match the queue's instances), and always
  from a freshly pulled base image (`--pull`).
- `.github/workflows/deploy.yml` runs both steps on GitHub Actions. It is manual only, takes
  its settings from a GitHub environment, and gets AWS credentials through OIDC, so no keys are
  stored. It is reusable (`workflow_call`): the recommended layout keeps this repository public
  and runs deployments from a small private deploy repository holding the environments, via
  `docs/deploy-caller.yml`, so account details never appear in public logs. It registers QEMU,
  so the arm64 image builds on GitHub's x86 runners.
- `docs/deploy-role.yaml` creates the deploy role: trusts GitHub's OIDC provider for one
  repository and one environment, in either GitHub subject format (plain `owner/name`, or the
  immutable `owner@id/repo@id` that repositories created after 15 July 2026 use), with
  permissions limited to pushing the utility image and deploying this stack under the
  application's names.

### Not yet

- Triggering PheBee's bulk import at the end of a run.
- Starting a run from an S3 upload.
- Inputs in buckets other than the data bucket.
