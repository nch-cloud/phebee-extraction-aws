# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- Initial scaffold: `plan` and `transform` jobs built on `phebee-extraction` v1.0.0, a Step
  Functions definition that iterates the plan and submits Batch jobs, a SAM template, the
  utility image, and tests that run without AWS.
- Tools are specified at run time by image. `plan` registers a Batch job definition per
  image and settings on first use (`{AppName}-{repository}-{hash}`) and reuses it afterwards;
  the stack has no per-tool parameters. `gpu`, `vcpus` and `memory_mib` can be set per stage,
  and an explicit `job_definition` / `job_queue` bypasses registration.
- A deployment contract for pipelines: `scripts/build_image.sh --push --output` writes the
  pushed image's digest-pinned URI for the deploy step, and `.github/workflows/deploy.yml` is a
  manually triggered GitHub Actions deploy (OIDC role, per-environment variables) that doubles
  as the reference for other CI systems. Images are built for `linux/arm64` by default (`PLATFORM`
  overrides; the deploy workflow takes it from the environment), and must match the queue's
  instance architecture.
- Optional `S3AccessLogBucketName` / `S3AccessLogPrefix` for the data bucket's server access
  logs, and a 365-day default log retention, so the stack passes CIS/HIPAA/NIST CloudFormation
  guardrail hooks (the first deploy into an account with such hooks was rejected for
  `S3_BUCKET_LOGGING_ENABLED`). Both are passed by the deploy workflow only when set.
- The plan job failed on a real Batch instance with `NoRegionError`: Batch containers on EC2
  have no `AWS_REGION`, and the Batch/ECR clients were created without one. Clients are now
  created in an explicit region (the deployment's; for ECR, the registry's own region from the
  image URI), and the utility job definition also sets `AWS_REGION` / `AWS_DEFAULT_REGION`.
- `docs/deploy-role.yaml`: CloudFormation for the deploy role the workflow assumes. Trusts
  GitHub's OIDC provider for one repository and one environment only, with permissions limited
  to pushing the utility image and deploying this stack under the application's names.
- Per-stage `job_role` (short name, expands to `{AppName}-tool-<name>`) or `job_role_arn`, and
  `environment`, for tools that need permissions or settings beyond S3 (Bedrock callers). The
  plan job may only assign roles named `{AppName}-tool-*`; `docs/tool-role.yaml` creates one
  per permission set, with the default S3 policy, an optional Bedrock `InvokeModel` policy for
  the given model ARNs, and any attached managed policies.
  The plan builds each stage's full container environment, layering the I/O variables, the
  stack's region (`AWS_REGION` / `AWS_DEFAULT_REGION`), the run-level `environment` and the
  stage's own; the state machine passes it through unchanged.
