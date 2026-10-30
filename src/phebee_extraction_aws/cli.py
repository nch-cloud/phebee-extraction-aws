"""
`phebee-extract-aws`, the command the utility image runs as a Batch job.

    phebee-extract-aws plan        reads the run config from PHEBEE_RUN_CONFIG (JSON) or --config
    phebee-extract-aws transform   reads one plan item from PHEBEE_BATCH_ITEM (JSON) or --item

The state machine passes both as environment variables via Batch container overrides. Results
are printed to stdout as JSON; logs go to stderr.
"""

import argparse
import json
import logging
import os
import sys
import tempfile
from typing import Any, Optional

from phebee_extraction_aws.batch import Deployment
from phebee_extraction_aws.plan import RunConfig, plan_run
from phebee_extraction_aws.s3 import S3
from phebee_extraction_aws.transform import transform_batch

logger = logging.getLogger(__name__)

RUN_CONFIG_ENV = "PHEBEE_RUN_CONFIG"
BATCH_ITEM_ENV = "PHEBEE_BATCH_ITEM"


def _load_json_arg(parser: argparse.ArgumentParser, value: Optional[str], env_var: str, what: str):
    raw = value if value is not None else os.environ.get(env_var)
    if not raw:
        parser.error(f"{what} is required: pass it as an argument or set {env_var}")
    if raw.startswith("@"):
        with open(raw[1:]) as f:
            raw = f.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        parser.error(f"{what} is not valid JSON: {e}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phebee-extract-aws", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="split the input on S3 and write the batch plan")
    plan.add_argument(
        "--config",
        help=f"run config as JSON, or @file; default: ${RUN_CONFIG_ENV}",
    )
    plan.add_argument("--workdir", help="scratch directory (default: a temp dir)")

    transform = sub.add_parser(
        "transform", help="convert one batch's tool output to a PheBee part file"
    )
    transform.add_argument(
        "--item",
        help=f"plan item as JSON, or @file; default: ${BATCH_ITEM_ENV}",
    )
    transform.add_argument("--workdir", help="scratch directory (default: a temp dir)")
    return parser


def main(
    argv: Optional[list[str]] = None,
    s3: Optional[S3] = None,
    ecr_client: Optional[Any] = None,
    batch_client: Optional[Any] = None,
    deployment: Optional[Deployment] = None,
) -> int:
    """Entry point. The clients and the deployment are injectable for tests."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", stream=sys.stderr
    )
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = args.workdir or tmp
            if args.command == "plan":
                config = RunConfig.from_dict(
                    _load_json_arg(parser, args.config, RUN_CONFIG_ENV, "run config")
                )
                plan = plan_run(
                    config,
                    workdir,
                    deployment or Deployment.from_env(),
                    s3=s3,
                    ecr_client=ecr_client,
                    batch_client=batch_client,
                )
                result = {k: plan[k] for k in ("total_records", "batch_count", "stages")}
            else:
                item = _load_json_arg(parser, args.item, BATCH_ITEM_ENV, "plan item")
                result = transform_batch(item, workdir, s3=s3)
    except Exception as e:
        logger.error(f"{args.command} failed: {e}", exc_info=True)
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
