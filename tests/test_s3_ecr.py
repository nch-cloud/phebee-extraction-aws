import json
from pathlib import Path

import pytest
from fakes import FakeEcrClient, FakeS3Client

from phebee_extraction_aws.ecr import ImageUri, resolve_image_ref
from phebee_extraction_aws.s3 import S3, S3Uri

# ---------------------------------------------------------------------------- S3Uri


def test_s3uri_parse_and_str():
    uri = S3Uri.parse("s3://my-bucket/runs/r1/notes.csv")
    assert (uri.bucket, uri.key, uri.name) == ("my-bucket", "runs/r1/notes.csv", "notes.csv")
    assert str(uri) == "s3://my-bucket/runs/r1/notes.csv"


@pytest.mark.parametrize("bad", ["notes.csv", "s3://", "s3://bucket", "s3://bucket/", "http://x/y"])
def test_s3uri_rejects_non_object_uris(bad):
    with pytest.raises(ValueError):
        S3Uri.parse(bad)


def test_s3uri_join_normalises_slashes():
    base = S3Uri.from_prefix("b", "/runs/r1/")
    assert str(base.join("plan", "/batches.json")) == "s3://b/runs/r1/plan/batches.json"
    assert str(base.join("", "jsonl")) == "s3://b/runs/r1/jsonl"


def test_s3_roundtrip_through_fake_client(tmp_path):
    client = FakeS3Client()
    s3 = S3(client)

    src = tmp_path / "a.json"
    src.write_text('{"x": 1}')
    s3.upload(str(src), S3Uri("b", "in/a.json"))
    s3.put_json({"y": 2}, S3Uri("b", "in/b.json"))

    out = s3.download(S3Uri("b", "in/a.json"), str(tmp_path / "nested/dir/a.json"))
    assert json.loads(Path(out).read_text()) == {"x": 1}
    assert json.loads(client.objects[("b", "in/b.json")]) == {"y": 2}


# ---------------------------------------------------------------------------- ImageUri


@pytest.mark.parametrize(
    "image,registry,repository,tag,digest",
    [
        ("595912345678.dkr.ecr.us-east-2.amazonaws.com/tools/extractor:1.4.0_c58cc41",
         "595912345678.dkr.ecr.us-east-2.amazonaws.com", "tools/extractor", "1.4.0_c58cc41", None),
        ("tools/extractor:v1", None, "tools/extractor", "v1", None),
        ("extractor", None, "extractor", None, None),
        ("localhost:5000/tool:1.0", "localhost:5000", "tool", "1.0", None),
        ("tool@sha256:abc", None, "tool", None, "sha256:abc"),
        ("reg.example.com/ns/tool:2@sha256:def", "reg.example.com", "ns/tool", "2", "sha256:def"),
    ],
)  # fmt: skip
def test_image_uri_parse(image, registry, repository, tag, digest):
    assert ImageUri.parse(image) == ImageUri(registry, repository, tag, digest)


def test_ecr_region_is_parsed_from_the_host():
    """The ECR lookup must go to the registry's region; Batch containers have no AWS_REGION"""
    assert (
        ImageUri.parse("123456789012.dkr.ecr.eu-west-1.amazonaws.com/r:t").ecr_region == "eu-west-1"
    )
    assert ImageUri.parse("reg.example.com/r:t").ecr_region is None
    assert ImageUri.parse("r:t").ecr_region is None


def test_ecr_account_is_only_set_for_ecr_hosts():
    assert (
        ImageUri.parse("123456789012.dkr.ecr.us-east-2.amazonaws.com/r:t").ecr_account
        == "123456789012"
    )
    assert ImageUri.parse("reg.example.com/r:t").ecr_account is None
    assert ImageUri.parse("r:t").ecr_account is None


# ---------------------------------------------------------------------------- resolve_image_ref


def test_resolve_image_ref_strips_registry_and_adds_digest():
    ecr = FakeEcrClient({("tools/extractor", "v1"): "sha256:aaa"})
    ref = resolve_image_ref("595912345678.dkr.ecr.us-east-2.amazonaws.com/tools/extractor:v1", ecr)

    assert ref == "tools/extractor:v1@sha256:aaa"
    assert ecr.calls[0]["registryId"] == "595912345678"  # cross-account lookups work


def test_resolve_image_ref_passes_pinned_digest_through_without_ecr():
    ecr = FakeEcrClient({})
    assert resolve_image_ref("reg.example.com/ns/tool:1@sha256:zzz", ecr) == "ns/tool:1@sha256:zzz"
    assert ecr.calls == []


def test_resolve_image_ref_defaults_to_latest_tag():
    ecr = FakeEcrClient({("tool", "latest"): "sha256:bbb"})
    assert resolve_image_ref("tool", ecr) == "tool@sha256:bbb"


def test_resolve_image_ref_fails_loudly_when_missing():
    with pytest.raises(LookupError):
        resolve_image_ref("tool:nope", FakeEcrClient({}))
