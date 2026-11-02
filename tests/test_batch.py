import json
import re

import pytest
from fakes import FakeBatchClient

from phebee_extraction_aws.batch import (
    DEPLOYMENT_ENV,
    JOB_DEFINITION_NAME_MAX,
    Deployment,
    ensure_job_definition,
    job_definition_name,
)

IMAGE = "595912345678.dkr.ecr.us-east-2.amazonaws.com/tools/extractor:1.4.0_c58cc41"


@pytest.fixture
def deployment():
    return Deployment(
        app_name="phebee-extraction",
        account_id="123456789012",
        region="us-east-2",
        cpu_job_queue_arn="arn:q:cpu",
        gpu_job_queue_arn="arn:q:gpu",
        tool_job_role_arn="arn:role:tool",
        execution_role_arn="arn:role:exec",
        log_group="/aws/batch/phebee-extraction",
    )


# ---------------------------------------------------------------------------- Deployment


def test_deployment_from_env_coerces_cloudformation_strings(monkeypatch):
    """CloudFormation's !Sub renders numbers as strings and an unset queue as "" """
    monkeypatch.setenv(
        DEPLOYMENT_ENV,
        json.dumps(
            {
                "app_name": "app",
                "account_id": "123456789012",
                "region": "us-east-2",
                "cpu_job_queue_arn": "arn:q:cpu",
                "gpu_job_queue_arn": "",
                "tool_job_role_arn": "arn:role:tool",
                "execution_role_arn": "arn:role:exec",
                "log_group": "/lg",
                "tool_vcpus": "2",
                "tool_memory_mib": "8192",
                "gpu_tool_vcpus": "4",
                "gpu_tool_memory_mib": "16384",
                "job_timeout_seconds": "3600",
            }
        ),
    )
    d = Deployment.from_env()
    assert d.gpu_job_queue_arn is None
    assert (d.tool_vcpus, d.tool_memory_mib, d.job_timeout_seconds) == (2, 8192, 3600)
    assert type(d.gpu_tool_vcpus) is int


def test_deployment_from_env_requires_the_variable(monkeypatch):
    monkeypatch.delenv(DEPLOYMENT_ENV, raising=False)
    with pytest.raises(OSError, match=DEPLOYMENT_ENV):
        Deployment.from_env()


def test_queue_for_gpu_needs_a_gpu_queue(deployment):
    assert deployment.queue_for(False) == "arn:q:cpu"
    assert deployment.queue_for(True) == "arn:q:gpu"
    deployment.gpu_job_queue_arn = None
    with pytest.raises(ValueError, match="GPUJobQueueArn"):
        deployment.queue_for(True)


# ---------------------------------------------------------------------------- ensure_job_definition


def test_registers_once_and_reuses_the_active_definition(deployment):
    batch = FakeBatchClient()

    first = ensure_job_definition(IMAGE, deployment, batch)
    second = ensure_job_definition(IMAGE, deployment, batch)

    assert first == second
    assert len(batch.registered) == 1
    assert first.startswith(
        "arn:aws:batch:us-east-2:123456789012:job-definition/phebee-extraction-tools-extractor-"
    )


def test_registered_definition_runs_the_image_with_the_stack_roles_and_logging(deployment):
    batch = FakeBatchClient()
    ensure_job_definition(IMAGE, deployment, batch)
    (spec,) = batch.registered

    props = spec["containerProperties"]
    assert props["image"] == IMAGE
    assert props["jobRoleArn"] == "arn:role:tool"
    assert props["executionRoleArn"] == "arn:role:exec"
    assert props["logConfiguration"]["options"]["awslogs-group"] == "/aws/batch/phebee-extraction"
    assert props["logConfiguration"]["options"]["awslogs-stream-prefix"] == "tools-extractor"
    assert props["resourceRequirements"] == [
        {"type": "VCPU", "value": "2"},
        {"type": "MEMORY", "value": "8192"},
    ]
    assert spec["timeout"] == {"attemptDurationSeconds": 3600}
    assert spec["type"] == "container" and spec["platformCapabilities"] == ["EC2"]


def test_gpu_definition_requests_a_gpu_and_uses_gpu_defaults(deployment):
    batch = FakeBatchClient()
    ensure_job_definition(IMAGE, deployment, batch, gpu=True)
    (spec,) = batch.registered
    assert spec["containerProperties"]["resourceRequirements"] == [
        {"type": "VCPU", "value": "4"},
        {"type": "MEMORY", "value": "16384"},
        {"type": "GPU", "value": "1"},
    ]


def test_explicit_resources_override_defaults(deployment):
    batch = FakeBatchClient()
    ensure_job_definition(IMAGE, deployment, batch, vcpus=8, memory_mib=30000)
    assert batch.registered[0]["containerProperties"]["resourceRequirements"][:2] == [
        {"type": "VCPU", "value": "8"},
        {"type": "MEMORY", "value": "30000"},
    ]


def test_different_image_or_resources_get_a_different_definition(deployment):
    batch = FakeBatchClient()
    a = ensure_job_definition(IMAGE, deployment, batch)
    b = ensure_job_definition(IMAGE.replace("c58cc41", "deadbee"), deployment, batch)
    c = ensure_job_definition(IMAGE, deployment, batch, vcpus=4)

    assert len({a, b, c}) == 3
    assert len(batch.registered) == 3


def test_stage_job_role_replaces_the_default_and_changes_the_definition(deployment):
    """A tool needing Bedrock runs as its own role; that is a different job definition"""
    batch = FakeBatchClient()
    default = ensure_job_definition(IMAGE, deployment, batch)
    custom = ensure_job_definition(
        IMAGE,
        deployment,
        batch,
        job_role_arn="arn:aws:iam::123456789012:role/phebee-extraction-tool-llm-extractor",
    )

    assert default != custom
    by_arn = {d["jobDefinitionArn"]: d for d in batch.registered}
    assert by_arn[default]["containerProperties"]["jobRoleArn"] == "arn:role:tool"
    assert by_arn[custom]["containerProperties"]["jobRoleArn"].endswith(
        "role/phebee-extraction-tool-llm-extractor"
    )


def test_job_definition_name_is_valid_for_batch(deployment):
    name = job_definition_name("phebee-extraction", "tools/extractor", {"k": "v"})
    assert re.fullmatch(r"[A-Za-z0-9_-]+", name)
    assert name.startswith("phebee-extraction-tools-extractor-")

    long_name = job_definition_name("a" * 100, "r" * 100, {"k": "v"})
    assert len(long_name) <= JOB_DEFINITION_NAME_MAX
    assert re.fullmatch(r"[A-Za-z0-9_-]+", long_name)


def test_job_definition_name_is_deterministic_and_property_sensitive():
    base = {"image": "x", "n": 1}
    assert job_definition_name("app", "repo", base) == job_definition_name(
        "app", "repo", dict(base)
    )
    assert job_definition_name("app", "repo", base) != job_definition_name(
        "app", "repo", {"image": "y", "n": 1}
    )
