# The utility image: runs `phebee-extract-aws plan` and `phebee-extract-aws transform` as
# Batch jobs. Both wheels are built on the host first (scripts/build_image.sh) and copied in,
# so the build is reproducible from pinned wheels and makes no requests to GitHub. That also
# keeps it working while the core repository is private.
FROM python:3.12-slim

RUN useradd --create-home --uid 1000 phebee
WORKDIR /work

COPY dist/*.whl /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl && rm -rf /tmp/wheels

USER phebee

# The state machine overrides the command with ["phebee-extract-aws", "plan"|"transform"].
CMD ["phebee-extract-aws", "--help"]
