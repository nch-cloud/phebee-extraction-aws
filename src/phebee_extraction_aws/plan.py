"""
The `plan` job: split the input into batches on S3 and write the execution plan.

Everything the state machine needs to run a batch is decided here and written to
`plan/batches.json`, one item per batch: the chain of stage input/output URIs, the job
definition, queue and environment for each stage, and the settings the transform step needs.
The state machine iterates the array and submits jobs; it makes no decisions of its own.

Tools are named by image in the execution input. A job definition for each image is
registered on first use (see batch.py), so adding a tool needs no redeploy. A stage may also
name its own job role and environment variables, for tools that need more than S3.

Environment variables reach a tool container in this order, later entries overriding earlier
ones with the same name:

    1. the I/O variables (INPUT_URI etc.), always set and never overridable
    2. deployment defaults: AWS_REGION / AWS_DEFAULT_REGION from the stack's region
    3. the run's `environment` (applies to every stage)
    4. the stage's own `environment`

Layout under s3://{bucket}/{prefix}/:

    batches/{batch_id}_input.json      written here; the first stage reads it
    batches/{batch_id}_stage{k}.json   written by stage k, read by stage k+1
    batches/{batch_id}_output.json     written by the last stage
    plan/manifest.json                 the core's manifest
    plan/batches.json                  JSON array of items, read by the Distributed Map
    plan/plan.json                     the run config, resolved stages and the items
    jsonl/part-{batch_id}.json         written by the transform step: PheBee import input
"""

import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from phebee_extraction import (
    create_batches,
    get_stage_paths,
    load_input_data,
    validate_input_records,
    write_manifest,
)

from phebee_extraction_aws.batch import Deployment, ensure_job_definition
from phebee_extraction_aws.ecr import resolve_image_ref
from phebee_extraction_aws.s3 import S3, S3Uri

logger = logging.getLogger(__name__)


# Environment variables every tool container receives: the neutral names and, for older
# images, the legacy AWS-pipeline names (see INTERFACE.md in the core).
INPUT_ENV_VARS = ("INPUT_URI", "INPUT_S3_URI")
OUTPUT_ENV_VARS = ("OUTPUT_URI", "OUTPUT_S3_URI")
RESERVED_ENV_VARS = frozenset(INPUT_ENV_VARS + OUTPUT_ENV_VARS)


@dataclass
class StageSpec:
    """One tool in the pipeline, in order. Only `name` and `image` are required."""

    name: str  # short label used in Batch job names, e.g. "extractor"
    image: str  # ECR image URI to run; also used to resolve the digest for provenance
    gpu: bool = False  # register with one GPU and submit to the GPU queue
    vcpus: int | None = None  # override the stack's defaults for this tool
    memory_mib: int | None = None
    # Role the tool runs as, for tools that need more than S3. Either a short name that
    # expands to {AppName}-tool-{job_role} in this account, or a full ARN (which must still be
    # named {AppName}-tool-*). Default: the stack's S3-only role.
    job_role: str | None = None
    job_role_arn: str | None = None
    # Variables for this tool only; override the run's `environment` and the defaults
    environment: dict[str, str] = field(default_factory=dict)
    job_definition: str | None = None  # escape hatch: use this definition instead of registering
    job_queue: str | None = None  # escape hatch: use this queue instead of the CPU/GPU default


@dataclass
class RunConfig:
    """The state machine's execution input, as the plan job receives it"""

    bucket: str
    prefix: str
    input_uri: str
    project_id: str
    stages: list[StageSpec]
    batch_size: int = 1000
    input_format: str = "auto"
    id_column: str = "note_id"
    text_column: str = "text"
    subject_id_column: str = "subject_id"
    # Input column holding each note's clinical date, written as PheBee's note_timestamp.
    # Notes without a value get null; the date is never invented.
    note_timestamp_column: str = "note_timestamp"
    # Variables every stage receives (model ids, regions, ...); a stage may override them
    environment: dict[str, str] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)  # unknown keys, kept so nothing is lost

    @classmethod
    def from_dict(cls, data: dict) -> "RunConfig":
        data = dict(data)
        known = {f.name for f in cls.__dataclass_fields__.values()} - {"extra"}
        stages = [StageSpec(**s) for s in data.pop("stages", [])]
        if not stages:
            raise ValueError("stages must list at least one tool")
        for stage in stages:
            if stage.job_role and stage.job_role_arn:
                raise ValueError(f"stage {stage.name!r} sets both job_role and job_role_arn")
            _reject_reserved(stage.environment, f"stage {stage.name!r}")
        _reject_reserved(data.get("environment") or {}, "the run")
        kwargs = {k: data.pop(k) for k in list(data) if k in known}
        return cls(stages=stages, extra=data, **kwargs)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(d.pop("extra"))
        return d

    @property
    def run_prefix(self) -> S3Uri:
        return S3Uri.from_prefix(self.bucket, self.prefix)


def _reject_reserved(environment: dict, owner: str) -> None:
    clash = RESERVED_ENV_VARS & set(environment)
    if clash:
        raise ValueError(f"{owner} sets reserved environment variables {sorted(clash)}")


def stage_environment(
    input_uri: str, output_uri: str, *layers: dict[str, Any]
) -> list[dict[str, str]]:
    """
    A container's environment in Batch's Name/Value shape.

    The I/O variables come first and cannot be overridden; then each layer in turn, a later
    layer overriding an earlier one for the same name (defaults, run, stage).
    """
    merged: dict[str, str] = {}
    for name in INPUT_ENV_VARS:
        merged[name] = input_uri
    for name in OUTPUT_ENV_VARS:
        merged[name] = output_uri
    for layer in layers:
        for name, value in layer.items():
            if name not in RESERVED_ENV_VARS:
                merged[name] = str(value)
    return [{"Name": name, "Value": value} for name, value in merged.items()]


def resolve_stages(
    config: RunConfig,
    deployment: Deployment,
    ecr_client: Any | None = None,
    batch_client: Any | None = None,
) -> list[dict]:
    """
    Per stage: the job definition and queue to submit to, the image ref with digest, the role,
    and the stage's merged environment (without the per-batch I/O variables).

    One ECR lookup and (at most) one job definition registration per tool, not per batch.
    """
    resolved = []
    for spec in config.stages:
        job_role_arn = spec.job_role_arn or (
            deployment.tool_role_arn(spec.job_role) if spec.job_role else None
        )
        job_definition = spec.job_definition or ensure_job_definition(
            spec.image,
            deployment,
            batch_client,
            gpu=spec.gpu,
            vcpus=spec.vcpus,
            memory_mib=spec.memory_mib,
            job_role_arn=job_role_arn,
        )
        job_queue = spec.job_queue or deployment.queue_for(spec.gpu)
        environment = {
            **deployment.default_environment(),
            **config.environment,
            **spec.environment,
        }
        resolved.append(
            {
                "name": spec.name,
                "image": spec.image,
                "image_ref": resolve_image_ref(spec.image, ecr_client, region=deployment.region),
                "job_definition": job_definition,
                "job_queue": job_queue,
                "job_role_arn": job_role_arn or deployment.tool_job_role_arn,
                "environment": environment,
            }
        )
    logger.info(f"Resolved stages: {[(s['name'], s['job_definition']) for s in resolved]}")
    return resolved


def plan_run(
    config: RunConfig,
    workdir: str,
    deployment: Deployment,
    s3: S3 | None = None,
    ecr_client: Any | None = None,
    batch_client: Any | None = None,
) -> dict:
    """Split the input, upload batch files, resolve the stages, write the plan. Returns it."""
    s3 = s3 or S3()
    run = config.run_prefix
    batches_prefix = run.join("batches")

    # Load and validate the notes, using the core exactly as `run` does
    input_uri = S3Uri.parse(config.input_uri)
    local_input = s3.download(input_uri, os.path.join(workdir, "input", input_uri.name))
    records = load_input_data(
        local_input,
        format=config.input_format,
        id_column=config.id_column,
        text_column=config.text_column,
        string_columns={config.subject_id_column},
    )
    validate_input_records(records)

    # Batch files, named exactly as a local run names them
    local_batches = os.path.join(workdir, "batches")
    batches = create_batches(records, config.batch_size, local_batches)
    manifest_path = write_manifest(batches, local_batches, len(records), config.batch_size)

    stages = resolve_stages(config, deployment, ecr_client, batch_client)
    image_refs = [s["image_ref"] for s in stages]

    items = []
    num_stages = len(stages)
    for batch in batches:
        s3.upload(batch.input_path, batches_prefix.join(os.path.basename(batch.input_path)))

        item_stages = []
        for stage_num, stage in enumerate(stages, start=1):
            local_in, local_out = get_stage_paths(batch, stage_num, num_stages)
            stage_in = str(batches_prefix.join(os.path.basename(local_in)))
            stage_out = str(batches_prefix.join(os.path.basename(local_out)))
            item_stages.append(
                {
                    "name": stage["name"],
                    "job_definition": stage["job_definition"],
                    "job_queue": stage["job_queue"],
                    "input_uri": stage_in,
                    "output_uri": stage_out,
                    "environment": stage_environment(stage_in, stage_out, stage["environment"]),
                }
            )

        items.append(
            {
                "batch_id": batch.batch_id,
                "batch_num": batch.batch_num,
                "record_count": batch.record_count,
                "stages": item_stages,
                "transform": {
                    "output_uri": item_stages[-1]["output_uri"],
                    "original_input_uri": item_stages[0]["input_uri"],
                    "part_uri": str(run.join("jsonl", f"part-{batch.batch_id}.json")),
                    "project_id": config.project_id,
                    "subject_id_column": config.subject_id_column,
                    "note_timestamp_column": config.note_timestamp_column,
                    "image_refs": image_refs,
                },
            }
        )

    plan = {
        "config": config.to_dict(),
        "stages": stages,
        "image_refs": image_refs,
        "total_records": len(records),
        "batch_count": len(items),
        "items": items,
    }
    s3.upload(manifest_path, run.join("plan", "manifest.json"))
    s3.put_json(items, run.join("plan", "batches.json"))
    s3.put_json(plan, run.join("plan", "plan.json"))
    logger.info(f"Planned {len(items)} batches ({len(records)} records) under {run}")
    return plan
