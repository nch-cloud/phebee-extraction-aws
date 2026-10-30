"""
Register Batch job definitions for tool images at plan time.

The execution input names a tool by image; nothing about it is baked into the stack. The
plan job registers a job definition for the image on first use and reuses it afterwards, so
adding a tool or moving to a new tag needs no redeploy. Definitions are named
`{app}-{repository}-{hash}`, where the hash covers every property that goes into the
definition, so a name identifies exactly one configuration and a lookup by name is enough to
know whether it already exists.

A stage may name its own job role (for tools that need more than S3, e.g. Bedrock), either as
a short name that expands to `{app}-tool-{name}` or as a full ARN. The stack lets the plan job
pass only roles named `{app}-tool-*`, so a tool's extra permissions always live in a role
created for that purpose, never in the shared default.

What the stack provides (roles, queues, log group, resource defaults) reaches the plan job as
the PHEBEE_DEPLOYMENT environment variable on the utility job definition.
"""

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from phebee_extraction_aws.ecr import ImageUri

DEPLOYMENT_ENV = "PHEBEE_DEPLOYMENT"
JOB_DEFINITION_NAME_MAX = 128
TOOL_ROLE_INFIX = "-tool-"


@dataclass
class Deployment:
    """Stack-provided settings the plan job needs to register job definitions"""

    app_name: str
    account_id: str
    region: str
    cpu_job_queue_arn: str
    tool_job_role_arn: str
    execution_role_arn: str
    log_group: str
    partition: str = "aws"
    gpu_job_queue_arn: str | None = None
    tool_vcpus: int = 2
    tool_memory_mib: int = 8192
    gpu_tool_vcpus: int = 4
    gpu_tool_memory_mib: int = 16384
    job_timeout_seconds: int = 3600

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Deployment":
        """CloudFormation renders numbers as strings and absent values as "", so coerce."""
        kwargs: dict = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            if f.type == "int" or f.type is int:
                value = int(value)
            elif value == "":
                value = None
            kwargs[f.name] = value
        return cls(**kwargs)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Deployment":
        raw = env.get(DEPLOYMENT_ENV)
        if not raw:
            raise OSError(
                f"{DEPLOYMENT_ENV} is not set; the stack sets it on the utility job definition"
            )
        return cls.from_dict(json.loads(raw))

    def queue_for(self, gpu: bool) -> str:
        if not gpu:
            return self.cpu_job_queue_arn
        if not self.gpu_job_queue_arn:
            raise ValueError("a stage asks for a GPU but the stack has no GPUJobQueueArn")
        return self.gpu_job_queue_arn

    def default_environment(self) -> dict[str, str]:
        """Variables every tool gets unless the run or stage overrides them: the stack's region,
        so tools that default to a hard-coded region call the right one."""
        return {"AWS_REGION": self.region, "AWS_DEFAULT_REGION": self.region}

    def tool_role_arn(self, name: str) -> str:
        """ARN of the tool role with this short name: {app}-tool-{name} in this account"""
        return f"arn:{self.partition}:iam::{self.account_id}:role/{self.app_name}{TOOL_ROLE_INFIX}{name}"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")


def job_definition_name(app_name: str, repository: str, properties: Mapping[str, Any]) -> str:
    """Deterministic: the same image and settings always map to the same name"""
    digest = hashlib.sha256(json.dumps(properties, sort_keys=True).encode()).hexdigest()[:10]
    prefix = f"{app_name}-{_slug(repository)}"
    return f"{prefix[: JOB_DEFINITION_NAME_MAX - len(digest) - 1]}-{digest}"


def job_definition_spec(
    image: str,
    deployment: Deployment,
    gpu: bool = False,
    vcpus: int | None = None,
    memory_mib: int | None = None,
    job_role_arn: str | None = None,
) -> dict:
    """The register_job_definition arguments for a tool image, minus the name"""
    vcpus = vcpus or (deployment.gpu_tool_vcpus if gpu else deployment.tool_vcpus)
    default_memory = deployment.gpu_tool_memory_mib if gpu else deployment.tool_memory_mib
    memory_mib = memory_mib or default_memory
    requirements = [
        {"type": "VCPU", "value": str(vcpus)},
        {"type": "MEMORY", "value": str(memory_mib)},
    ]
    if gpu:
        requirements.append({"type": "GPU", "value": "1"})

    repository = ImageUri.parse(image).repository
    return {
        "type": "container",
        "platformCapabilities": ["EC2"],
        "retryStrategy": {"attempts": 2},
        "timeout": {"attemptDurationSeconds": deployment.job_timeout_seconds},
        "containerProperties": {
            "image": image,
            "jobRoleArn": job_role_arn or deployment.tool_job_role_arn,
            "executionRoleArn": deployment.execution_role_arn,
            "resourceRequirements": requirements,
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": deployment.log_group,
                    "awslogs-stream-prefix": _slug(repository),
                },
            },
        },
    }


def ensure_job_definition(
    image: str,
    deployment: Deployment,
    batch_client: Any | None = None,
    gpu: bool = False,
    vcpus: int | None = None,
    memory_mib: int | None = None,
    job_role_arn: str | None = None,
) -> str:
    """Return the ARN of an ACTIVE job definition for this image and settings, registering one if needed"""
    if batch_client is None:
        import boto3

        # Batch containers on EC2 have no AWS_REGION; never leave the region to the environment
        batch_client = boto3.client("batch", region_name=deployment.region)

    spec = job_definition_spec(
        image, deployment, gpu=gpu, vcpus=vcpus, memory_mib=memory_mib, job_role_arn=job_role_arn
    )
    name = job_definition_name(deployment.app_name, ImageUri.parse(image).repository, spec)

    existing = batch_client.describe_job_definitions(jobDefinitionName=name, status="ACTIVE")
    revisions = existing.get("jobDefinitions", [])
    if revisions:
        return max(revisions, key=lambda d: d["revision"])["jobDefinitionArn"]

    response = batch_client.register_job_definition(jobDefinitionName=name, **spec)
    return response["jobDefinitionArn"]
