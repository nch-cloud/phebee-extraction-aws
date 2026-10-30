"""
Consistency checks between statemachine/extraction.asl.json and template.yaml, so a renamed
substitution or a mistyped integration cannot reach `sam deploy`.
"""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
ASL = ROOT / "statemachine" / "extraction.asl.json"
TEMPLATE = ROOT / "template.yaml"


class CfnLoader(yaml.SafeLoader):
    """Accept CloudFormation's short-form intrinsic tags (!Ref, !Sub, !If, ...)"""


def _construct_any(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


for tag in ["Ref", "Sub", "GetAtt", "If", "Equals", "Not", "And", "Or", "Join", "Select",
            "Split", "FindInMap", "Condition", "ImportValue", "Base64"]:  # fmt: skip
    CfnLoader.add_constructor(f"!{tag}", _construct_any)


def load_template():
    return yaml.load(TEMPLATE.read_text(), Loader=CfnLoader)


def state_machine_resource():
    resources = load_template()["Resources"]
    machines = [r for r in resources.values() if r["Type"] == "AWS::Serverless::StateMachine"]
    assert len(machines) == 1, "expected exactly one state machine"
    return machines[0]


def test_asl_is_valid_json_after_substitution():
    text = ASL.read_text()
    # Numeric substitutions (MaxConcurrency, ...) are unquoted in the file; fill them in.
    filled = re.sub(
        r"\$\{(\w+)\}", lambda m: "1" if m.group(1).startswith(("Max", "Tolerated")) else "x", text
    )
    json.loads(filled)


def test_every_asl_substitution_is_provided_by_the_template_and_vice_versa():
    used = set(re.findall(r"\$\{(\w+)\}", ASL.read_text()))
    provided = set(state_machine_resource()["Properties"]["DefinitionSubstitutions"])
    assert used == provided, f"only in ASL: {used - provided}; only in template: {provided - used}"


def test_template_points_at_the_asl_file():
    assert (
        state_machine_resource()["Properties"]["DefinitionUri"]
        == "statemachine/extraction.asl.json"
    )


def test_only_expected_integrations_are_used():
    text = ASL.read_text()
    resources = set(re.findall(r'"Resource": "(arn:aws:states:::[^"]+)"', text))
    assert resources == {
        "arn:aws:states:::batch:submitJob.sync",
        "arn:aws:states:::s3:getObject",
        "arn:aws:states:::s3:putObject",
    }


def test_tool_stages_take_their_environment_from_the_plan():
    """The plan builds each stage's full environment (I/O URIs + tool settings); the ASL
    passes it through rather than naming variables itself. The variable names themselves are
    checked in test_plan_transform."""
    asl = json.loads(
        re.sub(
            r"\$\{(\w+)\}",
            lambda m: "1" if m.group(1).startswith(("Max", "Tolerated")) else "x",
            ASL.read_text(),
        )
    )
    run_stage = asl["States"]["ProcessBatches"]["ItemProcessor"]["States"]["RunStages"][
        "ItemProcessor"
    ]["States"]["RunStage"]
    overrides = run_stage["Parameters"]["ContainerOverrides"]
    assert overrides == {"Environment.$": "$.stage.environment"}


def test_template_outputs_what_a_run_needs():
    outputs = load_template()["Outputs"]
    for name in ("StateMachineArn", "DataBucketName", "UtilityJobDefinitionArn"):
        assert name in outputs, name
