"""Concurrent runs for the same snapshot date are refused, never interleaved (FR-005, US4 AS4)."""

import datetime as dt
import threading

from click.testing import CliRunner

from src.pipeline import claims, layout
from src.pipeline.config import PipelineSettings
from src.pipeline.notify import RUN_REFUSED
from src.pipeline.runner import RunRequest, run_snapshot
from tests.helpers.fake_pricing import FakePricingSource

DATE = "2026-10-05"
T0 = dt.datetime(2026, 10, 5, 13, 0, 0, tzinfo=dt.timezone.utc)
HOLDER = "20261005T120000Z-abcdef"


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send(self, kind, report, detail=None):
        self.sent.append(kind)
        return True


def _run(store, tmp_path, trigger="manual", now=T0, source=None):
    settings = PipelineSettings.from_env({"PIPELINE_STORAGE_URI": store.uri, "PRICING_REGIONS": "us-east-1"})
    notifier = RecordingNotifier()
    report = run_snapshot(
        settings,
        RunRequest(snapshot_date=DATE, trigger=trigger),
        store=store,
        downloader=source or FakePricingSource(),
        now=lambda: now,
        sleep=lambda s: None,
        work_dir=str(tmp_path),
        notifier=notifier,
    )
    return report, notifier


def _hold(store, now=T0):
    claims.acquire(store, "aws", DATE, HOLDER, "manual", 180, now)


def test_refused_while_another_run_holds_the_claim(store, tmp_path):
    _hold(store)
    before = sorted(o.key for o in store.list("aws/"))
    report, notifier = _run(store, tmp_path)
    assert report.outcome == "refused"
    assert report.refused_by == HOLDER
    assert sorted(o.key for o in store.list("aws/")) == before  # wrote nothing
    assert notifier.sent == []  # manual trigger: no alert


def test_scheduled_refusal_alerts(store, tmp_path):
    _hold(store)
    report, notifier = _run(store, tmp_path, trigger="scheduled")
    assert notifier.sent == [RUN_REFUSED]
    assert report.alerts_sent == [RUN_REFUSED]


def test_refused_cli_exits_zero(local_store, tmp_path, monkeypatch):
    _hold(local_store, now=dt.datetime.now(dt.timezone.utc))
    monkeypatch.setenv("PIPELINE_STORAGE_URI", local_store.uri)
    from src.pipeline.__main__ import cli

    result = CliRunner().invoke(cli, ["run", "--snapshot-date", DATE, "--regions", "us-east-1"])
    assert result.exit_code == 0


def test_expired_claim_is_taken_over(store, tmp_path):
    _hold(store, now=T0 - dt.timedelta(hours=4))
    report, _ = _run(store, tmp_path)
    assert report.outcome == "completed"
    assert report.snapshot_status == "succeeded"
    assert store.head(layout.claim_key("aws", DATE)) is None


def test_two_simultaneous_runs_one_refused(local_store, tmp_path):
    gate = threading.Event()

    class SlowSource(FakePricingSource):
        def download_region(self, region, dest_dir):
            gate.wait(5)
            return super().download_region(region, dest_dir)

    results = []

    def worker(i):
        report, _ = _run(local_store, tmp_path / f"w{i}", source=SlowSource())
        results.append(report.outcome)

    (tmp_path / "w0").mkdir()
    (tmp_path / "w1").mkdir()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    # Let both threads reach the claim; the loser returns immediately.
    for _ in range(50):
        if results:
            break
        threading.Event().wait(0.05)
    gate.set()
    for t in threads:
        t.join()
    assert sorted(results) == ["completed", "refused"]
