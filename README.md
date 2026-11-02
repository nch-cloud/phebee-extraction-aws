# phebee-extraction-aws

AWS deployment of [phebee-extraction](https://github.com/nch-cloud/phebee-extraction): Step
Functions and AWS Batch run the extraction containers over clinical notes in S3 and write
PheBee import-ready output.

It is deliberately small: a single state machine, Batch jobs, S3 in and out, and no data
platform. Most runs of up to a few hundred thousand notes fit here.

## How it works

Everything that reads notes, batches them, or writes PheBee records is the core package,
reused unchanged. This repo adds S3 staging, ECR digest lookup, Batch job definitions, and
the orchestration. See the core's
[ARCHITECTURE.md](https://github.com/nch-cloud/phebee-extraction/blob/main/ARCHITECTURE.md)
for the model it implements.

```
execution input ──► Plan (Batch, utility image)
                     split notes into batches on S3; register a job definition per tool
                     image on first use; write plan/batches.json
                    ──► Distributed Map over batches, MaxConcurrency at a time
                          per batch: tool 1 ──► tool 2 ──► …   (Batch, your ECR tool images)
                                     Transform (Batch, utility image)
                                       merge original fields back, PheBee transform
                                       ──► s3://{bucket}/{prefix}/jsonl/part-{batch}.json
```

The `plan` job decides everything: the chain of input/output URIs for each stage and which
job definition and queue each stage uses. The state machine only iterates the plan and submits
jobs, so the logic lives in Python where it is unit-tested without AWS.

Tools are specified at run time. The execution input names each tool's image; `plan`
registers a Batch job definition for it (named `{AppName}-{repository}-{hash}`) the first
time that image and settings are seen, and reuses it afterwards. Adding a tool or moving to
a new tag needs no redeploy.

The output prefix `{prefix}/jsonl/` with `.json` part files is exactly what PheBee's bulk
import reads, so a run's output can be imported directly.

## Prerequisites

- **AWS Batch job queues** for CPU (and GPU if you run GPU tools), with compute environments
  that can pull from your ECR registry. The stack does not create them.
- **Tool images in ECR** that implement the core's
  [container interface](https://github.com/nch-cloud/phebee-extraction/blob/main/INTERFACE.md).
  Use immutable tags: PheBee records the tag as the evidence creator.
- **Locally:** `uv`, Docker, the AWS CLI and SAM CLI. `uv sync` installs the core
  (`nch-cloud/phebee-extraction`) from its public git tag.

## Deploy

**1. Build and push the utility image.** It contains this package and the core; `plan` and
`transform` run in it.

```bash
aws ecr create-repository --repository-name phebee-extraction-aws   # once
scripts/build_image.sh --push 123456789012.dkr.ecr.us-east-2.amazonaws.com/phebee-extraction-aws
```

The script builds both wheels on your machine and copies them into the image, so the Docker
build is reproducible from pinned wheels and makes no network requests to GitHub. The image is
tagged from `git describe` (the release tag when you are on one, otherwise e.g.
`v0.1.0-5-gabc1234`), and `--also-tag` adds more; the stack is always pointed at the digest,
so tags are labels for humans. Consider creating the ECR repository with
`--image-tag-mutability IMMUTABLE` so a tag can never be moved to a different build; note that
this makes re-pushing a rebuilt image under an existing tag fail, which is the point.

**Architecture.** The image is built for `linux/arm64` by default (Graviton); set
`PLATFORM=linux/amd64` for an x86 queue. It must match the compute environment behind
`CPUJobQueueArn`, whatever machine you build on, so pipelines should set `PLATFORM` explicitly
rather than rely on the default. The same rule applies to every tool image: each is only ever
submitted to one queue and must match that queue's instances. GPU instance families are mostly
x86, so a common setup is an arm64 CPU queue with arm64 utility and CPU-tool images, and an x86
GPU queue with amd64 GPU-tool images.

**2. Deploy the stack.**

```bash
cp samconfig.yaml.example samconfig.yaml   # queue ARNs and the utility image URI
sam build && sam deploy
```

The stack outputs the state machine ARN and the data bucket. Tool images are not deployment
parameters.

If your organisation enforces CloudFormation guardrails (CIS, HIPAA, NIST profiles), two
parameters matter: `S3AccessLogBucketName`, an existing bucket for the data bucket's server
access logs (CIS-style hooks reject a bucket without logging), and `LogRetentionDays`, which
defaults to 365 because compliance profiles typically require at least that. Without such
guardrails, both can be left alone.

### Deploying from a pipeline

The deployment contract is two steps, and any CI system can run them:

1. Build and push the utility image and capture its URI.
   `scripts/build_image.sh --push <repo> --output image-uri.txt` does this and writes the
   digest-pinned URI to the file. It tags the image from `git describe`; any immutable scheme
   works.
2. `sam build && sam deploy` with the stack parameters, passing that URI as `UtilityImageUri`.

[`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) is a working GitHub Actions
version of both steps. It only ever runs when a person starts it, against a GitHub
environment they choose, and it gets AWS credentials by assuming a role through GitHub's
OIDC provider, so no AWS keys are stored anywhere.

**Recommended layout: two repositories.** Keep this repository (or your fork of it) public
and holding only code. Put the account details in a small second repository of your own,
private or internal, that holds the GitHub environments (`dev`, `prod`, ...) and a one-job
workflow calling this one at a release tag.
[`docs/deploy-caller.yml`](docs/deploy-caller.yml) is that workflow, complete. Deploy runs,
their logs and their summaries then belong to the deploy repository, so account IDs, ARNs and
bucket names never appear in public logs, and the environments' protection rules (required
reviewers, deployment from tags only) decide who may deploy to what. To set it up:

1. Create the deploy repository and copy `docs/deploy-caller.yml` to its
   `.github/workflows/deploy.yml`. Pin `uses:` to the release tag you want to deploy.
2. In the target account, create the deploy role (next section) with `GitHubRepository` set
   to the deploy repository, written the way GitHub puts it in the OIDC subject (see there).
3. In the deploy repository, create an environment per target with the variables below.
4. Actions tab, **Run workflow**, choose the environment.

Two requirements: the repository holding this workflow must be public, because the job checks
out its own source to build the image and the caller's token can read nothing else; and your
organisation's Actions policy must allow reusable workflows from it. The simpler alternative,
for a private copy of this repository, is to create the environments in that copy and use its
own **Run workflow** button; account details then appear in its logs, which is fine when the
repository is private.

Whichever repository runs the workflow, each environment holds these variables:

| Variable | Value |
|---|---|
| `AWS_REGION` | Region to deploy to |
| `AWS_DEPLOY_ROLE_ARN` | The deploy role (next section): trusts GitHub's OIDC provider for the repository that runs the workflow, may push the utility image and run CloudFormation for the stack |
| `UTILITY_ECR_REPOSITORY` | ECR repository URI for the utility image |
| `STACK_NAME`, `APP_NAME` | Stack name and the `AppName` parameter |
| `CPU_JOB_QUEUE_ARN`, `GPU_JOB_QUEUE_ARN` | Existing Batch queues (`GPU_JOB_QUEUE_ARN` may be empty) |
| `PLATFORM` | `linux/arm64` or `linux/amd64`, matching the CPU queue's instances (default `linux/arm64`) |
| `S3_ACCESS_LOG_BUCKET` | Optional. Existing bucket for the data bucket's access logs; required where guardrails demand S3 access logging |

Other systems (CodePipeline, Jenkins, GitLab CI) map onto the same two steps; the workflow
file shows the exact commands.

**The AWS side: the deploy role.** [`docs/deploy-role.yaml`](docs/deploy-role.yaml) creates
the role the workflow assumes. Deploy it once per account and environment, by hand, naming the
repository whose Actions runs will deploy (the deploy repository in the layout above). The
name must match the token's subject exactly, and GitHub has two formats: repositories created
after 15 July 2026 get an immutable subject that embeds the owner and repository IDs
(`my-org@188507437/my-deploy-repo@1440223880`); older repositories use plain `owner/name`.
Read the exact value from GitHub rather than guessing:

```bash
gh api /repos/<owner>/<repo>/actions/oidc/customization/sub --jq .sub_claim_prefix
# -> repo:my-org@188507437/my-deploy-repo@1440223880   (drop the leading "repo:")
```

```bash
aws cloudformation deploy --stack-name phebee-extraction-deploy-role-dev \
  --template-file docs/deploy-role.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides GitHubRepository=my-org@188507437/my-deploy-repo@1440223880 \
                        GitHubEnvironment=dev \
                        CreateOidcProvider=false   # true only if the account has no GitHub OIDC provider yet
aws cloudformation describe-stacks --stack-name phebee-extraction-deploy-role-dev \
  --query "Stacks[0].Outputs[?OutputKey=='DeployRoleArn'].OutputValue" --output text
```

Put the printed ARN in the environment's `AWS_DEPLOY_ROLE_ARN`. The role's trust policy
accepts only tokens whose subject is `repo:<repository>:environment:<environment>`. GitHub sets
that subject from the repository a run starts in, even when the job's steps come from a
reusable workflow elsewhere, which is why the deploy repository is the one to name. A wrong
format shows up as `Not authorized to perform sts:AssumeRoleWithWebIdentity` in the
credentials step; the stack's `TrustedSubject` output is the string the role expects. A fork,
another repository, a pull request, or a run against a different environment cannot assume
the role, and a `prod` role is only reachable through the `prod` environment and whatever
reviewer rules you put on it. Its permissions are limited to pushing the utility image and
deploying this one stack and the resources it declares, under this application's names. The
role is public-safe by design: this repository can be public and the role stays usable only
from the repository you named.

## Run

Upload the notes to the data bucket, then start an execution. The input names the tools in
pipeline order by image:

```json
{
  "bucket": "phebee-extraction-123456789012-us-east-2",
  "prefix": "runs/2026-10-29-nightly",
  "input_uri": "s3://phebee-extraction-123456789012-us-east-2/input/notes.csv",
  "project_id": "my-phebee-project",
  "id_column": "note_id",
  "text_column": "note_text",
  "subject_id_column": "mrn",
  "batch_size": 1000,
  "stages": [
    {
      "name": "extractor",
      "image": "123456789012.dkr.ecr.us-east-2.amazonaws.com/tools/extractor:1.4.0"
    },
    {
      "name": "llm-extractor",
      "image": "123456789012.dkr.ecr.us-east-2.amazonaws.com/tools/llm-extractor:2.1.0",
      "gpu": true
    }
  ]
}
```

```bash
aws stepfunctions start-execution --state-machine-arn <StateMachineArn> \
  --name nightly-2026-10-29 --input file://run.json
```

Execution names become Batch job name prefixes, so use letters, digits, `-` and `_`.

Per stage, `name` and `image` are required. Optional: `gpu` (register with one GPU and use
the GPU queue), `vcpus` and `memory_mib` (override the stack's `Tool*` defaults for this
tool), `environment` (extra variables for the tool), `job_role` (a role with permissions
beyond S3, see below), and `job_definition` / `job_queue` to bypass registration and use ones
you manage.

Optional run keys: `input_format` (`auto`, `json`, `jsonl`, `csv`, `parquet`) and
`environment` (variables for every stage; see the next section).

### Tools that need other AWS permissions or settings

By default every tool runs as the stack's `ToolJobRole`, which can only read its stage input
and write its stage output under the run's `batches/` prefix. A tool that calls another AWS
service, for example an LLM-based tool calling Bedrock, needs two things, both set per stage:

1. **A role with the permissions**, created once per *permission set* and shared by every
   tool that needs it. Roles are named `{AppName}-tool-<name>`; the plan job may only assign
   roles with that prefix, so tool permissions always live in a role made for the purpose,
   never in the shared default. [`docs/tool-role.yaml`](docs/tool-role.yaml) creates one with
   the default S3 policy plus the permissions you choose. For the common case, tools that call
   `bedrock-runtime`, pass the model ARNs and it writes the `InvokeModel` policy for you:

   ```bash
   aws cloudformation deploy --stack-name phebee-extraction-tool-bedrock \
     --template-file docs/tool-role.yaml --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides RoleName=bedrock DataBucketName=<DataBucketName output> \
       BedrockModelArns=arn:aws:bedrock:us-east-2:123456789012:inference-profile/us.anthropic.claude-3-haiku-20240307-v1:0
   ```

   Other permissions go in managed policies you attach with `ManagedPolicyArns`. You can also
   create the role any other way, as long as it trusts `ecs-tasks.amazonaws.com`, has the S3
   statement, and follows the naming.

2. **Its configuration**, via `environment`. A Bedrock tool typically reads its model from a
   variable such as `BEDROCK_MODEL_ID` (check the tool's documentation for the exact names).
   Variables can be set once for the whole run (top-level `environment`) or per stage,
   and are layered in this order, later overriding earlier for the same name: the
   `INPUT_URI` / `OUTPUT_URI` variables (always set, never overridable) → `AWS_REGION` and
   `AWS_DEFAULT_REGION` from the stack's region → the run's `environment` → the stage's
   `environment`. So a region never has to be set by hand, and a model used everywhere is set
   once.

Then refer to the role by its short name:

```json
{
  "name": "llm-extractor",
  "image": "123456789012.dkr.ecr.us-east-2.amazonaws.com/tools/llm-extractor:2.1.0",
  "gpu": true,
  "job_role": "bedrock",
  "environment": { "BEDROCK_MODEL_ID": "us.anthropic.claude-3-haiku-20240307-v1:0" }
}
```

`job_role` expands to `arn:aws:iam::<account>:role/{AppName}-tool-bedrock`; `job_role_arn`
takes a full ARN instead if the role lives elsewhere. A different role or environment is a
different job definition, registered and reused like any other.

### Job definitions registered at run time

`plan` registers one Batch job definition per distinct image and settings, named
`{AppName}-{repository}-{hash}`, and reuses it on later runs. They are not part of the
CloudFormation stack, so deleting the stack does not remove them; they are free to keep. To
list them:

```bash
aws batch describe-job-definitions --status ACTIVE \
  --query "jobDefinitions[?starts_with(jobDefinitionName, 'phebee-extraction-')].[jobDefinitionName,revision,containerProperties.image]" \
  --output table
```

Because the job definition is built from the same `image` string used for the ECR digest
lookup, `evidence_creator_digest` in the output is the digest of the image that ran.

### What a run leaves in S3

```
s3://{bucket}/{prefix}/
  batches/                 batch inputs, per-stage intermediates, final tool outputs
  plan/manifest.json       the core's manifest
  plan/batches.json        the plan the Map iterated
  plan/plan.json           run config, resolved stages (job definitions, image digests), items
  plan/results/            Step Functions' per-batch results (succeeded / failed)
  jsonl/part-*.json        PheBee import input
```

To import into PheBee, point its bulk import at `{prefix}/jsonl/`. Provenance in the records
matches a local run: `evidence_creator_id` is `repository:tag` (chained tools joined with
`+`), `evidence_creator_digest` the ECR digests.

**Row numbers.** Each part file's `row_num` is its 1-based line number. PheBee's import does not
read it (and derives its own `batch_id` from the file name), so part files need no coordination.

## Failure handling

- A failed tool job fails that batch's child execution; the other batches continue.
  `ToleratedFailurePercentage` (default 0) decides whether the run as a whole is then marked
  failed. Per-batch outcomes are in `plan/results/`.
- Batch retries each job attempt once on infrastructure failure; Step Functions retries job
  submission on Batch service errors.
- Nothing is resumed automatically. Re-run the execution with the same `prefix` to redo the
  whole run, or re-run a single batch by submitting its stages by hand from `plan/batches.json`.

## Development

To iterate against a stack of your own, the loop is: build and push the image
(`scripts/build_image.sh --push <repo> --output image-uri.txt`), set `UtilityImageUri` in your
`samconfig.yaml` to the file's contents, and `sam build && sam deploy`. Only changes under
`src/`, `pyproject.toml`, `uv.lock` or `docker/` need a new image.

```bash
uv sync
uv run pytest            # no AWS needed: S3, ECR and Batch are faked
uv run ruff check . && uv run ruff format --check .
sam validate --lint      # cfn-lint on the template
```

CI runs the same on pushes and pull requests.

```
src/phebee_extraction_aws/
  s3.py           S3Uri and the three S3 operations the jobs need
  ecr.py          image URI parsing; digest lookup via describe-images
  batch.py        Deployment settings; register-or-reuse job definitions for tool images
  plan.py         the plan job: split on S3, resolve stages, write the per-batch plan
  transform.py    the transform job: one batch's tool output -> one part file
  cli.py          phebee-extract-aws plan | transform
statemachine/extraction.asl.json   the Step Functions definition
template.yaml                      SAM: bucket, roles, utility job definition, state machine
docs/deploy-role.yaml              the OIDC deploy role for the GitHub Actions workflow
docs/deploy-caller.yml             the one-job workflow for a separate deploy repository
docs/tool-role.yaml                a job role for a tool needing permissions beyond S3
docker/Dockerfile, scripts/build_image.sh   the utility image
tests/                             fakes for S3/ECR/Batch; plan -> transform end to end;
                                   ASL <-> template consistency
```

Bumping the core version is one line in `pyproject.toml` (`[tool.uv.sources]`), then
`uv lock`, then rebuild the image.

## Not yet

- Triggering PheBee's bulk import at the end of a run.
- Starting a run from an S3 upload.
- Inputs in buckets other than the data bucket (the job roles are scoped to it).

## License

BSD 3-Clause. See [LICENSE](LICENSE).
