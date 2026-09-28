"""Off-AWS hosts must have a correct clock: claims and grace periods compare it with
storage timestamps (spec edge case "clock is wrong"; research R5)."""

import datetime as dt

import pytest
from click.testing import CliRunner

from src.pipeline.clock import check_clock_skew
from src.pipeline.config import PipelineSettings, SettingsError

NOW = dt.datetime(2026, 10, 5, 13, 0, tzinfo=dt.timezone.utc)


class FakeS3:
    is_local = False

    def __init__(self, server):
        self._server = server

    def server_time(self):
        return self._server


def settings(**env):
    return PipelineSettings.from_env(env)


def test_within_limit_passes():
    check_clock_skew(FakeS3(NOW + dt.timedelta(seconds=299)), settings(), NOW)


@pytest.mark.parametrize("offset", [301, -600])
def test_beyond_limit_raises(offset):
    with pytest.raises(SettingsError, match="clock"):
        check_clock_skew(FakeS3(NOW + dt.timedelta(seconds=offset)), settings(), NOW)


def test_limit_is_configurable():
    check_clock_skew(FakeS3(NOW + dt.timedelta(minutes=10)), settings(MAX_CLOCK_SKEW_SECONDS="900"), NOW)


def test_local_roots_and_missing_date_header_skip_the_check(local_store):
    check_clock_skew(local_store, settings(), NOW)
    check_clock_skew(FakeS3(None), settings(), NOW)


def test_cli_run_exits_2_on_skew(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_STORAGE_URI", "s3://some-bucket/")
    monkeypatch.setattr(
        "src.pipeline.storage.S3Storage.server_time",
        lambda self: dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10),
    )
    monkeypatch.setattr("src.pipeline.storage.S3Storage.__init__", lambda self, *a, **k: None)
    result = CliRunner().invoke(__import__("src.pipeline.__main__", fromlist=["cli"]).cli, ["run", "--regions", "us-east-1"])
    assert result.exit_code == 2
