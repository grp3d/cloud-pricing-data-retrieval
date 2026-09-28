"""Runs raise the right alerts; unhandled errors send RUN CRASHED (US3; FR-026, FR-057)."""

import datetime as dt

from click.testing import CliRunner

from src.pipeline.__main__ import cli
from src.pipeline.config import PipelineSettings
from src.pipeline.notify import RUN_CRASHED, RUN_FAILED, RUN_PARTIAL, RUN_SUCCEEDED
from src.pipeline.runner import RunRequest, run_snapshot
from tests.helpers.fake_pricing import FakePricingSource

T0 = dt.datetime(2026, 10, 5, 13, 0, 0, tzinfo=dt.timezone.utc)
DATE = "2026-10-05"


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send(self, kind, report, detail=None):
        self.sent.append(kind)
        return True


def _run(store, tmp_path, source, summary=False, regions=("us-east-1", "eu-west-1")):
    settings = PipelineSettings.from_env(
        {
            "PIPELINE_STORAGE_URI": store.uri,
            "PRICING_REGIONS": ",".join(regions),
            "SUCCESS_SUMMARY_ENABLED": str(summary).lower(),
        }
    )
    notifier = RecordingNotifier()
    report = run_snapshot(
        settings,
        RunRequest(snapshot_date=DATE, trigger="scheduled"),
        store=store,
        downloader=source,
        now=lambda: T0,
        sleep=lambda s: None,
        work_dir=str(tmp_path),
        notifier=notifier,
    )
    return report, notifier


def test_partial_run_alerts(local_store, tmp_path):
    report, notifier = _run(local_store, tmp_path, FakePricingSource({"eu-west-1": "permanent_error"}))
    assert notifier.sent == [RUN_PARTIAL]
    assert report.alerts_sent == [RUN_PARTIAL]


def test_all_failed_alerts(local_store, tmp_path):
    _, notifier = _run(local_store, tmp_path, FakePricingSource(default="permanent_error"))
    assert notifier.sent == [RUN_FAILED]


def test_carried_forward_but_failed_attempt_still_alerts(local_store, tmp_path):
    _run(local_store, tmp_path, FakePricingSource())
    # Second run for the same date: us-east-1 fails, but its earlier data is carried forward.
    report, notifier = _run(
        local_store, tmp_path, FakePricingSource({"us-east-1": "permanent_error"}), regions=("us-east-1",)
    )
    assert report.snapshot_status == "succeeded"
    assert notifier.sent == [RUN_PARTIAL]


def test_success_summary_toggle(local_store, tmp_path):
    _, quiet = _run(local_store, tmp_path, FakePricingSource())
    assert quiet.sent == []
    _, chatty = _run(local_store, tmp_path, FakePricingSource(), summary=True)
    assert chatty.sent == [RUN_SUCCEEDED]


def test_cli_crash_sends_run_crashed_and_exits_nonzero(monkeypatch, tmp_path):
    sent = []

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr("src.pipeline.runner.run_snapshot", boom)
    monkeypatch.setattr(
        "src.pipeline.notify.Notifier.send",
        lambda self, kind, report, detail=None: sent.append((kind, detail)) or True,
    )
    monkeypatch.setenv("PIPELINE_STORAGE_URI", f"file://{tmp_path}")
    result = CliRunner().invoke(cli, ["run", "--snapshot-date", DATE])
    assert result.exit_code == 1
    assert sent and sent[0][0] == RUN_CRASHED
    assert "unexpected failure" in sent[0][1]
