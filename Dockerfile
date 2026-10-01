# syntax=docker/dockerfile:1.7
# Cloud pricing pipeline image (research R17). The same image runs on ECS Fargate
# (linux/arm64), a laptop or a home server (linux/amd64):
#
#   docker run --rm -v "$PWD/.localdata:/data" -e PIPELINE_STORAGE_URI=file:///data \
#     -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
#     <image> run --regions us-east-1
#
# Python 3.14 is required for the stdlib `compression.zstd` module (research R8).
FROM python:3.14-slim

ARG GIT_SHA=local
ARG IMAGE_TAG=local
ENV GIT_SHA=${GIT_SHA} \
    IMAGE_TAG=${IMAGE_TAG} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# IAM Roles Anywhere credential helper for off-AWS hosts (research R22). Pinned version,
# checksum-verified per architecture. On a home server, mount a certificate, key and an
# AWS config whose profile uses `credential_process = aws_signing_helper credential-process ...`:
#   docker run -v ~/.pricing:/creds:ro -e AWS_CONFIG_FILE=/creds/aws-config \
#     -e AWS_PROFILE=pricing-writer ... <image> run --trigger scheduled
ARG TARGETARCH
ARG SIGNING_HELPER_VERSION=1.7.0
ARG SIGNING_HELPER_SHA256_ARM64=800fc208b74cdb64b8c7270c41aa0c9d2f9bf5bba498539513acb30f8eb8f164
ARG SIGNING_HELPER_SHA256_AMD64=e932f029b73f97523c1dea2e78e9543d9e2753c387c4c46e77be8c1d9424db0f
RUN python - <<'PY'
import hashlib, os, urllib.request
arch = os.environ.get("TARGETARCH") or {"aarch64": "arm64", "x86_64": "amd64"}[os.uname().machine]
name, sha = {
    "arm64": ("Aarch64", os.environ["SIGNING_HELPER_SHA256_ARM64"]),
    "amd64": ("X86_64", os.environ["SIGNING_HELPER_SHA256_AMD64"]),
}[arch]
url = f"https://rolesanywhere.amazonaws.com/releases/{os.environ['SIGNING_HELPER_VERSION']}/{name}/Linux/aws_signing_helper"
data = urllib.request.urlopen(url, timeout=60).read()
if hashlib.sha256(data).hexdigest() != sha:
    raise SystemExit(f"aws_signing_helper checksum mismatch for {arch}")
with open("/usr/local/bin/aws_signing_helper", "wb") as fh:
    fh.write(data)
os.chmod("/usr/local/bin/aws_signing_helper", 0o755)
PY

COPY src/ ./src/

RUN useradd --create-home --uid 10001 pipeline \
    && mkdir -p /data /work \
    && chown pipeline:pipeline /data /work
USER pipeline
ENV TMPDIR=/work

ENTRYPOINT ["python", "-m", "src.pipeline"]
CMD ["run", "--trigger", "scheduled"]
