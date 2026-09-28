"""
Shared fixtures (tasks T005): local and moto-S3 storage roots, an injectable clock,
and a sleep recorder so retry/grace-period waits never actually sleep.
"""

import datetime as dt

import boto3
import pytest
from moto import mock_aws

TEST_BUCKET = "test-data"


@pytest.fixture
def aws_env(monkeypatch):
    """Fake credentials so boto3 never touches a real account."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def local_store(tmp_path):
    from src.pipeline.storage import open_storage

    return open_storage(f"file://{tmp_path / 'store'}")


@pytest.fixture
def s3_store(aws_env):
    from src.pipeline.storage import open_storage

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=TEST_BUCKET)
        yield open_storage(f"s3://{TEST_BUCKET}/")


@pytest.fixture(params=["local", "s3"])
def store(request):
    """The same test runs against a local directory and against (moto) S3 (FR-008)."""
    return request.getfixturevalue(f"{request.param}_store")


class FrozenClock:
    def __init__(self, start: dt.datetime):
        self.now_value = start

    def __call__(self) -> dt.datetime:
        return self.now_value

    def advance(self, **kwargs) -> None:
        self.now_value = self.now_value + dt.timedelta(**kwargs)


@pytest.fixture
def frozen_clock():
    return FrozenClock(dt.datetime(2026, 10, 5, 13, 0, 0, tzinfo=dt.timezone.utc))


class SleepRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.calls)


@pytest.fixture
def no_sleep():
    return SleepRecorder()
