#!/usr/bin/env bash
# Build the utility image and optionally push it to ECR.
#
#   scripts/build_image.sh                                  build phebee-extraction-aws:<tag>
#   scripts/build_image.sh --push REPO_URI                  also push to an ECR repository
#   scripts/build_image.sh --push REPO_URI --output FILE    and write the pushed image URI,
#                                                           pinned by digest, to FILE
#
# IMAGE_TAG sets the tag (default: `git describe`, or the commit SHA in CI). The URI written
# to --output is what a deploy step passes as the UtilityImageUri stack parameter.
# PLATFORM sets the image architecture: linux/arm64 (default; Graviton queues) or
# linux/amd64 (x86 queues). It must match the compute environment behind the CPU queue the
# utility jobs run on, whatever machine builds the image. Pipelines should set it explicitly.
#
# Both wheels are built here on the host and copied into the image, so the Docker build is
# reproducible from pinned wheels and makes no requests to GitHub. It also means the build
# works while the core repository is private: git credentials are only needed on the host.
set -euo pipefail

cd "$(dirname "$0")/.."

PUSH_REPO=""
OUTPUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --push) PUSH_REPO="${2:?--push needs an ECR repository URI}"; shift 2 ;;
    --output) OUTPUT="${2:?--output needs a file path}"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

TAG="${IMAGE_TAG:-$(git describe --tags --always --dirty 2>/dev/null || echo local)}"
PLATFORM="${PLATFORM:-linux/arm64}"
LOCAL_IMAGE="phebee-extraction-aws:${TAG}"

rm -rf dist && mkdir -p dist
echo "Building wheels into dist/"
uv build --wheel --out-dir dist -q
# Build the core's wheel from the git tag pinned in pyproject.toml ([tool.uv.sources]).
CORE_GIT="$(sed -n 's/^phebee-extraction *= *{ *git *= *"\([^"]*\)".*/\1/p' pyproject.toml)"
CORE_TAG="$(sed -n 's/^phebee-extraction *= *{.*tag *= *"\([^"]*\)".*/\1/p' pyproject.toml)"
[[ -n "$CORE_GIT" && -n "$CORE_TAG" ]] || { echo "could not read the core's git source from pyproject.toml" >&2; exit 1; }
uv run --with pip python -m pip wheel --no-deps -q -w dist "phebee-extraction @ git+${CORE_GIT}@${CORE_TAG}"

echo "Building ${LOCAL_IMAGE} for ${PLATFORM}"
docker build --platform "${PLATFORM}" -f docker/Dockerfile -t "${LOCAL_IMAGE}" .
RESULT="${LOCAL_IMAGE}"

if [[ -n "$PUSH_REPO" ]]; then
  REGISTRY="${PUSH_REPO%%/*}"
  REGION="$(echo "$REGISTRY" | sed -E 's/^[0-9]+\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com$/\1/')"
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"
  docker tag "${LOCAL_IMAGE}" "${PUSH_REPO}:${TAG}"
  docker push "${PUSH_REPO}:${TAG}"
  # The digest reference is immutable even if the tag is later moved; deploy with this.
  RESULT="$(docker inspect --format='{{index .RepoDigests 0}}' "${PUSH_REPO}:${TAG}")"
  echo "Pushed ${PUSH_REPO}:${TAG}"
fi

echo "Image: ${RESULT}"
if [[ -n "$OUTPUT" ]]; then
  printf '%s\n' "$RESULT" > "$OUTPUT"
  echo "Wrote ${OUTPUT}"
fi
