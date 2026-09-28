"""
pricing_weekly_schedule and the pricing_snapshot op (FR-007, research R16).

Dagster is an optional local runner. CI installs requirements-dagster.txt so these tests
run there (T074); developers without the extra installed skip them.
"""

import pytest

pytest.importorskip("dagster")

import datetime as dt  # noqa: E402

from dagster import build_op_context, build_schedule_context  # noqa: E402

from src.aws_regions import resolve_regions  # noqa: E402
from src.dagster_app.assets.pricing_assets import SnapshotRunConfig, run_pricing_snapshot  # noqa: E402
from src.dagster_app.resources import PricingRegionsResource  # noqa: E402
from src.dagster_app.schedules.pricing_schedules import pricing_weekly_schedule  # noqa: E402
from src.pipeline.runner import RunReport  # noqa: E402


def _config(run_request):
    return run_request.run_config["ops"]["run_pricing_snapshot"]["config"]


def _context(regions=None):
    resource = (
        PricingRegionsResource(regions=regions) if regions is not None else PricingRegionsResource()
    )
    return build_schedule_context(
        resources={"pricing_regions": resource},
        scheduled_execution_time=dt.datetime(2026, 10, 5, 13, 0, tzinfo=dt.timezone.utc),
    )


def test_one_run_per_tick_covering_all_configured_regions(monkeypatch):
    monkeypatch.delenv("PRICING_REGIONS", raising=False)
    run_requests = list(pricing_weekly_schedule(_context()))
    assert len(run_requests) == 1
    assert _config(run_requests[0])["regions"] == resolve_regions()
    assert _config(run_requests[0])["trigger"] == "scheduled"
    assert run_requests[0].run_key == "snapshot-20261005T130000Z"


def test_respects_env_var_region_override(monkeypatch):
    monkeypatch.setenv("PRICING_REGIONS", "us-east-1,eu-west-1")
    (rr,) = pricing_weekly_schedule(_context())
    assert _config(rr)["regions"] == ["us-east-1", "eu-west-1"]


def test_respects_direct_resource_config_override(monkeypatch):
    monkeypatch.delenv("PRICING_REGIONS", raising=False)
    (rr,) = pricing_weekly_schedule(_context(regions=["us-west-1", "ap-northeast-1"]))
    assert _config(rr)["regions"] == ["us-west-1", "ap-northeast-1"]


def test_cron_schedule_is_weekly_not_daily():
    assert pricing_weekly_schedule.cron_schedule == "0 13 * * 1"


def _report(status="succeeded", outcome="completed"):
    return RunReport(
        run_id="20261005T130000Z-111111",
        trigger="manual",
        mode="full",
        snapshot_date="2026-10-05",
        regions=["us-east-1"],
        outcome=outcome,
        snapshot_status=status,
    )


def _invoke_op(monkeypatch, report, config=None):
    seen = {}

    def fake_run_snapshot(settings, request, **kwargs):
        seen["request"] = request
        return report

    monkeypatch.setattr("src.dagster_app.assets.pricing_assets.run_snapshot", fake_run_snapshot)
    context = build_op_context(resources={"pricing_regions": PricingRegionsResource(regions=["us-east-1"])})
    result = run_pricing_snapshot(context, config or SnapshotRunConfig())
    return result, seen


def test_op_calls_the_shared_entry_point(monkeypatch):
    result, seen = _invoke_op(monkeypatch, _report())
    assert result["status"] == "succeeded"
    assert seen["request"].regions == ["us-east-1"]
    assert seen["request"].snapshot_date is None


def test_op_passes_launchpad_config(monkeypatch):
    _, seen = _invoke_op(
        monkeypatch,
        _report(),
        SnapshotRunConfig(snapshot_date="2026-10-05", regions=["eu-west-1"], transform_only=True),
    )
    assert seen["request"].snapshot_date == "2026-10-05"
    assert seen["request"].regions == ["eu-west-1"]
    assert seen["request"].transform_only is True


@pytest.mark.parametrize("report", [_report(status="failed"), _report(outcome="refused")])
def test_op_raises_on_failed_or_refused(monkeypatch, report):
    with pytest.raises(RuntimeError):
        _invoke_op(monkeypatch, report)
