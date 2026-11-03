"""
plan -> (pretend the tools ran) -> transform, end to end through fake S3, ECR and Batch.

The pretend tool mirrors real ones: it keeps only note_id/text and drops subject_id, so the
merge-back of original fields is exercised, and it annotates by keyword.
"""

import json

import pytest
from fakes import FakeBatchClient, FakeEcrClient, FakeS3Client

from phebee_extraction_aws import cli
from phebee_extraction_aws.batch import DEPLOYMENT_ENV, Deployment
from phebee_extraction_aws.plan import RunConfig, plan_run
from phebee_extraction_aws.s3 import S3
from phebee_extraction_aws.transform import transform_batch

NOTES = [
    {"note_id": "n1", "subject_id": "p1", "text": "Patient presents with fever and cough."},
    {"note_id": "n2", "subject_id": "p1", "text": "Pneumonia on imaging."},
    {"note_id": "n3", "subject_id": "p2", "text": "Chronic headaches."},
    {"note_id": "n4", "subject_id": "p3", "text": "Developmental delay noted."},
    {"note_id": "n5", "subject_id": "p3", "text": "Well child visit."},
]
KEYWORDS = {"fever": "HP:0001945", "cough": "HP:0012735", "pneumonia": "HP:0002090",
            "headaches": "HP:0002315", "developmental delay": "HP:0001263"}  # fmt: skip
REGISTRY = "595912345678.dkr.ecr.us-east-2.amazonaws.com"
EXTRACTOR = f"{REGISTRY}/tools/extractor:v1"
CONTEXT = f"{REGISTRY}/tools/context:v2"

DEPLOYMENT = Deployment(
    app_name="phebee-extraction",
    account_id="123456789012",
    region="us-east-2",
    cpu_job_queue_arn="arn:q:cpu",
    gpu_job_queue_arn="arn:q:gpu",
    tool_job_role_arn="arn:role:tool",
    execution_role_arn="arn:role:exec",
    log_group="/aws/batch/phebee-extraction",
)


def base_config(**overrides):
    """Stages are named by image only; the plan registers job definitions for them."""
    cfg = {
        "bucket": "data",
        "prefix": "runs/r1",
        "input_uri": "s3://data/in/notes.jsonl",
        "project_id": "proj",
        "batch_size": 2,
        "stages": [
            {"name": "extractor", "image": EXTRACTOR},
            {"name": "context", "image": CONTEXT, "gpu": True},
        ],
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture
def clients():
    s3c = FakeS3Client()
    s3c.put("data", "in/notes.jsonl", "".join(json.dumps(n) + "\n" for n in NOTES))
    ecr = FakeEcrClient(
        {("tools/extractor", "v1"): "sha256:aaa", ("tools/context", "v2"): "sha256:bbb"}
    )
    return s3c, ecr, FakeBatchClient()


def run_plan(clients, tmp_path, **overrides):
    s3c, ecr, batch = clients
    config = RunConfig.from_dict(base_config(**overrides))
    return plan_run(config, str(tmp_path / "plan"), DEPLOYMENT, S3(s3c), ecr, batch)


def pretend_tool_run(s3c, item):
    """Run every stage of one item like a real tool would: read input_uri, write output_uri."""
    for stage in item["stages"]:
        bucket, key = stage["input_uri"][5:].split("/", 1)
        data = json.loads(s3c.objects[(bucket, key)])
        records = []
        for r in data["records"]:
            text = r["text"]
            annotations = r.get("annotations") or [
                {"start": text.lower().index(k), "end": text.lower().index(k) + len(k), "text": k,
                 "term_id": tid, "label": k, "ontology": "hpo",
                 "contexts": {"negated": 0, "family": 0, "hypothetical": 0}}
                for k, tid in KEYWORDS.items() if k in text.lower()
            ]  # fmt: skip
            records.append(
                {"note_id": r["note_id"], "text": text, "annotations": annotations, "error": None}
            )
        out = {"records": records, "run_metadata": {"run_id": "x", "annotator": stage["name"],
               "annotator_version": "0", "timestamp": "2026-01-01T00:00:00Z"}}  # fmt: skip
        ob, ok = stage["output_uri"][5:].split("/", 1)
        s3c.put(ob, ok, json.dumps(out))


# ---------------------------------------------------------------------------- plan


def test_plan_writes_batches_manifest_and_plan(tmp_path, clients):
    s3c, ecr, batch = clients
    plan = run_plan(clients, tmp_path)

    assert plan["total_records"] == 5 and plan["batch_count"] == 3
    assert plan["image_refs"] == [
        "tools/extractor:v1@sha256:aaa",
        "tools/context:v2@sha256:bbb",
    ]
    assert len(ecr.calls) == 2  # one digest lookup per tool, not per batch
    assert len(batch.registered) == 2  # one job definition per tool, not per batch

    keys = s3c.keys("data")
    assert "runs/r1/plan/batches.json" in keys
    assert "runs/r1/plan/manifest.json" in keys
    assert "runs/r1/plan/plan.json" in keys
    assert [k for k in keys if k.startswith("runs/r1/batches/")] == [
        "runs/r1/batches/batch_000001_input.json",
        "runs/r1/batches/batch_000002_input.json",
        "runs/r1/batches/batch_000003_input.json",
    ]
    batches = json.loads(s3c.objects[("data", "runs/r1/plan/batches.json")])
    assert isinstance(batches, list) and len(batches) == 3  # a bare array: what ItemReader reads
    assert [b["record_count"] for b in batches] == [2, 2, 1]


def test_plan_registers_job_definitions_for_the_images_and_picks_queues(tmp_path, clients):
    s3c, ecr, batch = clients
    plan = run_plan(clients, tmp_path)

    extractor, context = plan["stages"]
    registered = {d["containerProperties"]["image"]: d for d in batch.registered}
    assert set(registered) == {EXTRACTOR, CONTEXT}  # the definitions run exactly these images
    assert extractor["job_definition"] == registered[EXTRACTOR]["jobDefinitionArn"]
    assert context["job_definition"] == registered[CONTEXT]["jobDefinitionArn"]
    assert extractor["job_queue"] == "arn:q:cpu"
    assert context["job_queue"] == "arn:q:gpu"  # gpu: true
    assert {
        r["type"] for r in registered[CONTEXT]["containerProperties"]["resourceRequirements"]
    } == {"VCPU", "MEMORY", "GPU"}
    # provenance comes from the same image the definition runs
    assert extractor["image_ref"] == "tools/extractor:v1@sha256:aaa"


def test_plan_reuses_job_definitions_across_runs(tmp_path, clients):
    s3c, ecr, batch = clients
    run_plan(clients, tmp_path)
    run_plan(clients, tmp_path / "again", prefix="runs/r2")
    assert len(batch.registered) == 2  # second run found the ACTIVE definitions by name


def test_plan_honours_an_explicit_job_definition_and_queue(tmp_path, clients):
    s3c, ecr, batch = clients
    plan = run_plan(
        clients,
        tmp_path,
        stages=[
            {
                "name": "extractor",
                "image": EXTRACTOR,
                "job_definition": "arn:jd:mine",
                "job_queue": "arn:q:mine",
            }
        ],
    )
    (stage,) = plan["stages"]
    assert (stage["job_definition"], stage["job_queue"]) == ("arn:jd:mine", "arn:q:mine")
    assert batch.registered == []


def test_plan_fails_for_a_gpu_stage_without_a_gpu_queue(tmp_path, clients):
    s3c, ecr, batch = clients
    no_gpu = Deployment(**{**DEPLOYMENT.__dict__, "gpu_job_queue_arn": None})
    with pytest.raises(ValueError, match="GPUJobQueueArn"):
        plan_run(RunConfig.from_dict(base_config()), str(tmp_path), no_gpu, S3(s3c), ecr, batch)


def test_plan_items_chain_stage_uris_and_carry_everything_the_jobs_need(tmp_path, clients):
    plan = run_plan(clients, tmp_path)
    item = plan["items"][1]

    s1, s2 = item["stages"]
    assert s1["name"] == "extractor" and s2["name"] == "context"
    assert s1["job_definition"] == plan["stages"][0]["job_definition"]
    assert s2["job_queue"] == "arn:q:gpu"
    assert s1["input_uri"] == "s3://data/runs/r1/batches/batch_000002_input.json"
    assert s1["output_uri"] == "s3://data/runs/r1/batches/batch_000002_stage1.json"
    assert s2["input_uri"] == s1["output_uri"]  # stage 2 reads what stage 1 wrote
    assert s2["output_uri"] == "s3://data/runs/r1/batches/batch_000002_output.json"

    t = item["transform"]
    assert t["output_uri"] == s2["output_uri"]
    assert t["original_input_uri"] == s1["input_uri"]
    assert t["part_uri"] == "s3://data/runs/r1/jsonl/part-batch_000002.json"
    assert t["image_refs"] == plan["image_refs"]
    assert (t["project_id"], t["subject_id_column"]) == ("proj", "subject_id")
    assert "start_row_num" not in t and "phebee_batch_id" not in t  # PheBee reads neither


def test_stage_environment_has_both_uri_name_pairs_then_the_tools_own_variables(tmp_path, clients):
    """Older tool images read INPUT_S3_URI, newer ones INPUT_URI: both are set. A stage's own
    variables (model ids, regions) follow, in Batch's Name/Value shape."""
    plan = run_plan(
        clients,
        tmp_path,
        stages=[
            {
                "name": "llm-extractor",
                "image": EXTRACTOR,
                "environment": {
                    "BEDROCK_MODEL_ID": "us.anthropic.claude-3-haiku",
                    "AWS_REGION": "us-east-2",
                },
            }
        ],
    )
    (stage,) = plan["items"][0]["stages"]
    env = {e["Name"]: e["Value"] for e in stage["environment"]}

    assert env["INPUT_URI"] == env["INPUT_S3_URI"] == stage["input_uri"]
    assert env["OUTPUT_URI"] == env["OUTPUT_S3_URI"] == stage["output_uri"]
    assert env["BEDROCK_MODEL_ID"] == "us.anthropic.claude-3-haiku"
    assert env["AWS_REGION"] == "us-east-2"
    assert [e["Name"] for e in stage["environment"]][:4] == [
        "INPUT_URI",
        "INPUT_S3_URI",
        "OUTPUT_URI",
        "OUTPUT_S3_URI",
    ]


def test_environment_layers_defaults_then_run_then_stage(tmp_path, clients):
    """Region comes from the deployment; the run's environment applies to every stage; a stage
    overrides individual variables; nothing overrides the I/O variables."""
    plan = run_plan(
        clients,
        tmp_path,
        environment={"BEDROCK_MODEL_ID": "run-model", "SHARED": "run"},
        stages=[
            {"name": "a", "image": EXTRACTOR},
            {
                "name": "b",
                "image": CONTEXT,
                "environment": {"BEDROCK_MODEL_ID": "stage-model", "AWS_REGION": "eu-west-1"},
            },
        ],
    )
    a, b = plan["items"][0]["stages"]
    env_a = {e["Name"]: e["Value"] for e in a["environment"]}
    env_b = {e["Name"]: e["Value"] for e in b["environment"]}

    # deployment default, present on every stage
    assert env_a["AWS_REGION"] == env_a["AWS_DEFAULT_REGION"] == "us-east-2"
    # run-level applies to both
    assert env_a["BEDROCK_MODEL_ID"] == "run-model" and env_a["SHARED"] == "run"
    assert env_b["SHARED"] == "run"
    # stage overrides the run value and even the region default
    assert env_b["BEDROCK_MODEL_ID"] == "stage-model"
    assert env_b["AWS_REGION"] == "eu-west-1" and env_b["AWS_DEFAULT_REGION"] == "us-east-2"
    # I/O variables first and untouched
    assert [e["Name"] for e in a["environment"]][:4] == [
        "INPUT_URI",
        "INPUT_S3_URI",
        "OUTPUT_URI",
        "OUTPUT_S3_URI",
    ]
    assert env_a["INPUT_URI"] == a["input_uri"]


def test_run_may_not_override_the_io_variables():
    with pytest.raises(ValueError, match="the run sets reserved environment variables"):
        RunConfig.from_dict(base_config(environment={"OUTPUT_S3_URI": "x"}))


def test_stage_may_not_override_the_io_variables():
    with pytest.raises(ValueError, match="reserved environment variables"):
        RunConfig.from_dict(
            base_config(
                stages=[{"name": "t", "image": EXTRACTOR, "environment": {"INPUT_URI": "x"}}]
            )
        )


def test_stage_job_role_is_used_for_that_tools_definition_only(tmp_path, clients):
    s3c, ecr, batch = clients
    plan = run_plan(
        clients,
        tmp_path,
        stages=[
            {"name": "extractor", "image": EXTRACTOR},
            {
                "name": "llm-extractor",
                "image": CONTEXT,
                "job_role_arn": "arn:aws:iam::123456789012:role/phebee-extraction-tool-llm-extractor",
            },
        ],
    )
    extractor, llm = plan["stages"]
    assert extractor["job_role_arn"] == "arn:role:tool"  # the stack's default
    assert llm["job_role_arn"].endswith("tool-llm-extractor")
    roles = {
        d["containerProperties"]["image"]: d["containerProperties"]["jobRoleArn"]
        for d in batch.registered
    }
    assert roles[EXTRACTOR] == "arn:role:tool"
    assert roles[CONTEXT].endswith("tool-llm-extractor")


def test_stage_job_role_short_name_expands_to_the_tool_role_in_this_account(tmp_path, clients):
    s3c, ecr, batch = clients
    plan = run_plan(
        clients,
        tmp_path,
        stages=[{"name": "llm-extractor", "image": EXTRACTOR, "job_role": "bedrock"}],
    )
    (stage,) = plan["stages"]
    expected = "arn:aws:iam::123456789012:role/phebee-extraction-tool-bedrock"
    assert stage["job_role_arn"] == expected
    assert batch.registered[0]["containerProperties"]["jobRoleArn"] == expected


def test_stage_may_not_set_both_job_role_forms():
    with pytest.raises(ValueError, match="both job_role and job_role_arn"):
        RunConfig.from_dict(
            base_config(
                stages=[{"name": "t", "image": EXTRACTOR, "job_role": "a", "job_role_arn": "arn:b"}]
            )
        )


def test_plan_requires_at_least_one_stage():
    with pytest.raises(ValueError, match="stages"):
        RunConfig.from_dict(base_config(stages=[]))


def test_run_config_keeps_unknown_keys():
    cfg = RunConfig.from_dict(base_config(comment="nightly", max_concurrency=5))
    assert cfg.to_dict()["comment"] == "nightly"
    assert cfg.to_dict()["max_concurrency"] == 5


# ---------------------------------------------------------------------------- transform


def test_transform_produces_an_import_ready_part_file(tmp_path, clients):
    s3c, ecr, batch = clients
    plan = run_plan(clients, tmp_path)
    item = plan["items"][0]
    pretend_tool_run(s3c, item)

    result = transform_batch(item, str(tmp_path / "t"), S3(s3c))

    assert result["batch_id"] == "batch_000001"
    assert result["records_written"] == 3  # fever, cough, pneumonia
    body = s3c.objects[("data", "runs/r1/jsonl/part-batch_000001.json")].decode()
    lines = [json.loads(line) for line in body.splitlines()]
    assert [line["row_num"] for line in lines] == [1, 2, 3]
    first = lines[0]
    assert first["project_id"] == "proj"
    assert first["project_subject_id"] == "p1"  # merged back: the pretend tool dropped it
    ev = first["evidence"][0]
    assert ev["evidence_creator_id"] == "tools/extractor:v1+tools/context:v2"
    assert ev["evidence_creator_digest"] == "sha256:aaa+sha256:bbb"
    assert ev["contexts"] == {"negated": False, "family": False, "hypothetical": False}


def test_each_part_file_numbers_its_rows_from_one(tmp_path, clients):
    """row_num is a per-file line number (PheBee does not read it), so no cross-part offsets"""
    s3c, ecr, batch = clients
    plan = run_plan(clients, tmp_path)
    for item in plan["items"]:
        pretend_tool_run(s3c, item)
        transform_batch(item, str(tmp_path / item["batch_id"]), S3(s3c))

    for item in plan["items"]:
        body = s3c.objects[("data", item["transform"]["part_uri"][len("s3://data/") :])].decode()
        row_nums = [json.loads(line)["row_num"] for line in body.splitlines()]
        assert row_nums == list(range(1, len(row_nums) + 1))


def test_cli_plan_and_transform_via_env_vars(tmp_path, clients, monkeypatch, capsys):
    """The way Batch runs them: config, item and deployment all arrive as env vars"""
    s3c, ecr, batch = clients
    monkeypatch.setenv(cli.RUN_CONFIG_ENV, json.dumps(base_config()))
    monkeypatch.setenv(DEPLOYMENT_ENV, json.dumps(DEPLOYMENT.__dict__))
    assert (
        cli.main(
            ["plan", "--workdir", str(tmp_path / "p")],
            s3=S3(s3c),
            ecr_client=ecr,
            batch_client=batch,
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert (summary["total_records"], summary["batch_count"]) == (5, 3)
    assert [s["name"] for s in summary["stages"]] == ["extractor", "context"]

    items = json.loads(s3c.objects[("data", "runs/r1/plan/batches.json")])
    pretend_tool_run(s3c, items[2])
    monkeypatch.setenv(cli.BATCH_ITEM_ENV, json.dumps(items[2]))
    assert cli.main(["transform", "--workdir", str(tmp_path / "t")], s3=S3(s3c)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["batch_id"] == "batch_000003"
    assert result["records_written"] == 0  # "Well child visit." has no terms
    assert result["skipped_no_annotations"] == 1


def test_cli_plan_from_file_argument(tmp_path, clients, capsys):
    s3c, ecr, batch = clients
    cfg = tmp_path / "run.json"
    cfg.write_text(json.dumps(base_config()))
    rc = cli.main(
        ["plan", "--config", f"@{cfg}", "--workdir", str(tmp_path / "p")],
        s3=S3(s3c), ecr_client=ecr, batch_client=batch, deployment=DEPLOYMENT,
    )  # fmt: skip
    assert rc == 0


def test_cli_requires_config(monkeypatch):
    monkeypatch.delenv(cli.RUN_CONFIG_ENV, raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["plan"])
    assert exc.value.code == 2


def test_cli_plan_fails_without_deployment_settings(tmp_path, clients, monkeypatch):
    s3c, ecr, batch = clients
    monkeypatch.setenv(cli.RUN_CONFIG_ENV, json.dumps(base_config()))
    monkeypatch.delenv(DEPLOYMENT_ENV, raising=False)
    assert (
        cli.main(
            ["plan", "--workdir", str(tmp_path)], s3=S3(s3c), ecr_client=ecr, batch_client=batch
        )
        == 1
    )


def test_cli_failure_exits_1(tmp_path, clients, monkeypatch):
    s3c, ecr, batch = clients
    monkeypatch.setenv(
        cli.RUN_CONFIG_ENV, json.dumps(base_config(input_uri="s3://data/in/missing.jsonl"))
    )
    rc = cli.main(
        ["plan", "--workdir", str(tmp_path)],
        s3=S3(s3c),
        ecr_client=ecr,
        batch_client=batch,
        deployment=DEPLOYMENT,
    )
    assert rc == 1


# ---------------------------------------------------------------------------- note dates


def test_note_timestamp_column_reaches_the_transform_item(tmp_path, clients):
    plan = run_plan(clients, tmp_path, note_timestamp_column="note_date")
    t = plan["items"][0]["transform"]
    assert t["note_timestamp_column"] == "note_date"
    assert plan["config"]["note_timestamp_column"] == "note_date"


def test_note_timestamp_column_defaults_to_note_timestamp(tmp_path, clients):
    plan = run_plan(clients, tmp_path)
    assert plan["items"][0]["transform"]["note_timestamp_column"] == "note_timestamp"


def test_transform_writes_the_notes_own_date_and_null_when_it_has_none(tmp_path, clients):
    """The date comes from the named input column (the pretend tool drops it, as real ones do);
    a note with no value is null, never the run date"""
    s3c, ecr, batch = clients
    dated = [
        {"note_id": "n1", "subject_id": "p1", "note_date": "2024-03-04", "text": "Fever."},
        {"note_id": "n2", "subject_id": "p1", "note_date": None, "text": "Chronic headaches."},
    ]
    s3c.put("data", "in/notes.jsonl", "".join(json.dumps(n) + "\n" for n in dated))
    plan = run_plan(clients, tmp_path, note_timestamp_column="note_date", batch_size=10)
    item = plan["items"][0]
    pretend_tool_run(s3c, item)

    result = transform_batch(item, str(tmp_path / "t"), S3(s3c))

    body = s3c.objects[("data", "runs/r1/jsonl/part-batch_000001.json")].decode()
    by_term = {
        json.loads(line)["term_iri"].rsplit("/", 1)[-1]: json.loads(line)["evidence"][0]
        for line in body.splitlines()
    }
    assert by_term["HP_0001945"]["note_timestamp"] == "2024-03-04"
    assert by_term["HP_0002315"]["note_timestamp"] is None
    assert result["missing_note_timestamp"] == 1
    assert "NaN" not in body
