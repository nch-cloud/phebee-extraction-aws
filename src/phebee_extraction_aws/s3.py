"""Small S3 helpers over a boto3 client. Tests pass a fake client with the same methods."""

import json
import os
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class S3Uri:
    bucket: str
    key: str

    @classmethod
    def parse(cls, uri: str) -> "S3Uri":
        if not uri.startswith("s3://"):
            raise ValueError(f"Not an S3 URI: {uri!r}")
        bucket, _, key = uri[5:].partition("/")
        if not bucket or not key:
            raise ValueError(f"S3 URI needs a bucket and a key: {uri!r}")
        return cls(bucket, key)

    @classmethod
    def from_prefix(cls, bucket: str, prefix: str) -> "S3Uri":
        """A prefix given as separate bucket and key-prefix strings"""
        prefix = prefix.strip("/")
        if not bucket or not prefix:
            raise ValueError("bucket and prefix are both required")
        return cls(bucket, prefix)

    def join(self, *parts: str) -> "S3Uri":
        key = "/".join([self.key.rstrip("/")] + [p.strip("/") for p in parts if p])
        return S3Uri(self.bucket, key)

    @property
    def name(self) -> str:
        """Last path component"""
        return self.key.rsplit("/", 1)[-1]

    def __str__(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


class S3:
    """The three operations the jobs need. `client` is a boto3 S3 client or a test double."""

    def __init__(self, client: Optional[Any] = None):
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.client = client

    def download(self, uri: S3Uri, path: str) -> str:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.client.download_file(uri.bucket, uri.key, path)
        return path

    def upload(self, path: str, uri: S3Uri) -> S3Uri:
        self.client.upload_file(path, uri.bucket, uri.key)
        return uri

    def put_json(self, obj: Any, uri: S3Uri) -> S3Uri:
        self.client.put_object(
            Bucket=uri.bucket,
            Key=uri.key,
            Body=json.dumps(obj, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        return uri
