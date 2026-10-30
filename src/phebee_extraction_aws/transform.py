"""
The `transform` job: one batch's final tool output -> one PheBee import part file.

Takes a single item from plan/batches.json. Merges the original input fields back (tools
drop them), converts to PheBee format with the core, and uploads the part file under the
run's `jsonl/` prefix, which is the shape PheBee's bulk import reads.
"""

import json
import logging
import os

from phebee_extraction import (
    PheBeeConfig,
    merge_original_records,
    read_batch_output,
    write_phebee_jsonl,
)

from phebee_extraction_aws.s3 import S3, S3Uri

logger = logging.getLogger(__name__)


def transform_batch(item: dict, workdir: str, s3: S3 | None = None) -> dict:
    """Transform one planned batch. Returns a small result dict for the Map's result writer."""
    s3 = s3 or S3()
    spec = item["transform"]

    output_path = s3.download(S3Uri.parse(spec["output_uri"]), os.path.join(workdir, "output.json"))
    original_path = s3.download(
        S3Uri.parse(spec["original_input_uri"]), os.path.join(workdir, "input.json")
    )

    output = read_batch_output(output_path)
    with open(original_path) as f:
        original_records = json.load(f)["records"]
    merge_original_records(original_records, output["records"])

    config = PheBeeConfig(
        project_id=spec["project_id"],
        subject_id_column=spec["subject_id_column"],
        image_refs=spec["image_refs"],
    )
    part_path = os.path.join(workdir, "part.json")
    stats = write_phebee_jsonl([output], config, part_path)

    part_uri = S3Uri.parse(spec["part_uri"])
    s3.upload(part_path, part_uri)
    logger.info(f"Wrote {stats.records_written} PheBee records to {part_uri}")

    return {
        "batch_id": item["batch_id"],
        "part_uri": str(part_uri),
        "records_written": stats.records_written,
        "skipped_no_subject": stats.skipped_no_subject,
        "skipped_no_annotations": stats.skipped_no_annotations,
    }
