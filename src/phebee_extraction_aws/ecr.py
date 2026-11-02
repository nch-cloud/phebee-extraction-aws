"""
Resolve a container image's digest so AWS runs carry the same provenance as local ones.

The core derives `evidence_creator_id` from the image name and tag, and
`evidence_creator_digest` from the digest. Locally the digest comes from the Docker daemon;
here it comes from ECR's describe-images.
"""

import re
from dataclasses import dataclass
from typing import Any

ECR_HOST = re.compile(r"^(?P<account>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com$")


@dataclass(frozen=True)
class ImageUri:
    """`[registry/]repository[:tag][@digest]`"""

    registry: str | None
    repository: str
    tag: str | None
    digest: str | None

    @classmethod
    def parse(cls, image: str) -> "ImageUri":
        rest, _, digest = image.partition("@")
        # A first path component with a dot or a port is a registry host; a plain name
        # (e.g. "tools/extractor") is a repository with a namespace.
        first, slash, remainder = rest.partition("/")
        registry = None
        if slash and ("." in first or ":" in first or first == "localhost"):
            registry, rest = first, remainder
        repository, colon, tag = rest.rpartition(":")
        if not colon or "/" in tag:
            repository, tag = rest, ""
        return cls(registry, repository, tag or None, digest or None)

    @property
    def name(self) -> str:
        """`repository[:tag]` with no registry: what goes into evidence_creator_id"""
        return f"{self.repository}:{self.tag}" if self.tag else self.repository

    @property
    def ecr_account(self) -> str | None:
        match = ECR_HOST.match(self.registry or "")
        return match.group("account") if match else None

    @property
    def ecr_region(self) -> str | None:
        match = ECR_HOST.match(self.registry or "")
        return match.group("region") if match else None


def resolve_image_ref(image: str, ecr_client: Any | None = None, region: str | None = None) -> str:
    """
    `repository:tag@sha256:...`, the form the core takes as an image ref.

    The registry host is dropped from the name so `evidence_creator_id` reads
    `tools/extractor:tag`, the same in every account. An image already pinned with a digest is
    returned as-is (minus the registry) without calling ECR.

    Without an injected client, one is created in the registry's own region (parsed from the
    ECR host), falling back to `region`. Batch containers on EC2 have no AWS_REGION set, so the
    region must never be left to the environment.
    """
    parsed = ImageUri.parse(image)
    if parsed.digest:
        return f"{parsed.name}@{parsed.digest}"

    if ecr_client is None:
        import boto3

        ecr_client = boto3.client("ecr", region_name=parsed.ecr_region or region)

    kwargs: dict = {
        "repositoryName": parsed.repository,
        "imageIds": [{"imageTag": parsed.tag or "latest"}],
    }
    if parsed.ecr_account:
        kwargs["registryId"] = parsed.ecr_account  # cross-account registries

    details = ecr_client.describe_images(**kwargs)["imageDetails"]
    if not details:
        raise LookupError(f"No image found in ECR for {image}")
    return f"{parsed.name}@{details[0]['imageDigest']}"
