"""
Tests for trigger_configured_regions_sensor — the on-demand companion to
pricing_weekly_schedule that launches one run per configured region without
waiting for the weekly cron tick.
"""

from dagster import DefaultSensorStatus, build_sensor_context

from src.aws_regions import resolve_regions
from src.dagster_app.resources import PricingRegionsResource
from src.dagster_app.sensors.pricing_sensors import trigger_configured_regions_sensor


def _region(run_request):
    return run_request.run_config["ops"]["download_pricing"]["config"]["region"]


def _context(regions=None):
    resource = (
        PricingRegionsResource(regions=regions) if regions is not None else PricingRegionsResource()
    )
    return build_sensor_context(resources={"pricing_regions": resource})


def test_defaults_to_stopped_so_it_never_fires_on_its_own():
    """It's a manual/on-demand trigger, not a periodic automation."""
    assert trigger_configured_regions_sensor.default_status == DefaultSensorStatus.STOPPED


def test_yields_one_run_request_per_configured_region(monkeypatch):
    monkeypatch.delenv("PRICING_REGIONS", raising=False)
    context = _context()

    run_requests = list(trigger_configured_regions_sensor(context))

    regions = resolve_regions()
    assert len(run_requests) == len(regions)
    assert sorted(_region(rr) for rr in run_requests) == sorted(regions)


def test_respects_direct_resource_config_override(monkeypatch):
    monkeypatch.delenv("PRICING_REGIONS", raising=False)
    context = _context(regions=["us-west-1", "ap-northeast-1"])

    run_requests = list(trigger_configured_regions_sensor(context))

    assert sorted(_region(rr) for rr in run_requests) == ["ap-northeast-1", "us-west-1"]


def test_produces_the_same_shape_as_the_schedule(monkeypatch):
    """The sensor and pricing_weekly_schedule share build_region_run_requests, so their
    output must be structurally identical (tags, run_key format, config shape)."""
    monkeypatch.delenv("PRICING_REGIONS", raising=False)
    context = _context()

    run_requests = list(trigger_configured_regions_sensor(context))

    assert run_requests
    for rr in run_requests:
        assert rr.tags.get("dagster/concurrency_key") == "pricing-region-download"
        assert rr.run_key.endswith(f"-{_region(rr)}")
        download_region = rr.run_config["ops"]["download_pricing"]["config"]["region"]
        transform_region = rr.run_config["ops"]["transform_to_parquet"]["config"]["region"]
        assert download_region == transform_region
