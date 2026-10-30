"""
AWS deployment of phebee-extraction.

The core package does the work (loading notes, batching, the container contract, the PheBee
transform). This package adds what AWS needs around it:

    s3         move files between S3 and the local disk of a Batch job
    ecr        resolve an image's digest so provenance matches a local run
    plan       the `plan` job: split the input into batches on S3 and write the per-batch
               execution plan the Step Functions state machine iterates over
    transform  the `transform` job: turn one batch's tool output into an import-ready part file
    cli        `phebee-extract-aws plan|transform`, the utility image's command

The state machine itself (statemachine/extraction.asl.json) only submits Batch jobs and
iterates the plan; every decision is made here, where it can be unit-tested.
"""

__version__ = "0.1.0"
