"""In-memory stand-ins for the boto3 clients the jobs use. Same method signatures."""


class FakeS3Client:
    """Objects live in `self.objects[(bucket, key)] = bytes`."""

    def __init__(self):
        self.objects = {}

    def put(self, bucket, key, body):
        self.objects[(bucket, key)] = body if isinstance(body, bytes) else body.encode()

    def download_file(self, bucket, key, path):
        if (bucket, key) not in self.objects:
            raise FileNotFoundError(f"s3://{bucket}/{key}")
        with open(path, "wb") as f:
            f.write(self.objects[(bucket, key)])

    def upload_file(self, path, bucket, key):
        with open(path, "rb") as f:
            self.objects[(bucket, key)] = f.read()

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = Body

    def keys(self, bucket):
        return sorted(k for b, k in self.objects if b == bucket)


class FakeBatchClient:
    """Job definitions by name, each a list of revisions; records every registration."""

    def __init__(self):
        self.definitions = {}
        self.registered = []

    def describe_job_definitions(self, jobDefinitionName, status="ACTIVE"):
        revisions = self.definitions.get(jobDefinitionName, [])
        return {"jobDefinitions": [d for d in revisions if d["status"] == status]}

    def register_job_definition(self, jobDefinitionName, **spec):
        revision = len(self.definitions.get(jobDefinitionName, [])) + 1
        arn = f"arn:aws:batch:us-east-2:123456789012:job-definition/{jobDefinitionName}:{revision}"
        definition = {
            **spec,
            "jobDefinitionName": jobDefinitionName,
            "revision": revision,
            "jobDefinitionArn": arn,
            "status": "ACTIVE",
        }
        self.definitions.setdefault(jobDefinitionName, []).append(definition)
        self.registered.append(definition)
        return {
            "jobDefinitionArn": arn,
            "jobDefinitionName": jobDefinitionName,
            "revision": revision,
        }


class FakeEcrClient:
    """`digests[(repository, tag)] = digest`; records registryId per call."""

    def __init__(self, digests):
        self.digests = digests
        self.calls = []

    def describe_images(self, repositoryName, imageIds, registryId=None):
        self.calls.append(
            {"repositoryName": repositoryName, "imageIds": imageIds, "registryId": registryId}
        )
        tag = imageIds[0]["imageTag"]
        digest = self.digests.get((repositoryName, tag))
        return {"imageDetails": [{"imageDigest": digest}] if digest else []}
