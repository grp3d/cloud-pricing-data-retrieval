"""
Tests for pricing_weekly_schedule (FR-003, FR-004, FR-008, FR-009).
"""

from dagster import build_schedule_context

from src.aws_regions import resolve_regions
from src.dagster_app.resources import PricingRegionsResource
from src.dagster_app.schedules.pricing_schedules import pricing_weekly_schedule


def _region(run_request):
    return run_request.run_config["ops"]["download_pricing"]["config"]["region"]


def _context(regions=None):
    """Build a schedule context with the pricing_regions resource wired in.

    regions=None -> PricingRegionsResource()'s own default (aws_regions.resolve_regions(),
    i.e. DEFAULT_PRICING_REGIONS / PRICING_REGIONS env var — set/clear the env var with
    monkeypatch *before* calling this).
    regions=[...] -> exercises the direct Dagster-resource-config override path (no env var
    involved at all), the fix for the "region list isn't reachable via Dagster's own config"
    gap.
    """
    resource = (
        PricingRegionsResource(regions=regions) if regions is not None else PricingRegionsResource()
    )
    return build_schedule_context(resources={"pricing_regions": resource})


def test_yields_one_run_request_per_configured_region(monkeypatch):
    monkeypatch.delenv("PRICING_REGIONS", raising=False)
    context = _context()

    run_requests = list(pricing_weekly_schedule(context))

    regions = resolve_regions()
    assert len(run_requests) == len(regions)
    assert sorted(_region(rr) for rr in run_requests) == sorted(regions)


def test_each_run_request_has_a_distinct_run_key(monkeypatch):
    monkeypatch.delenv("PRICING_REGIONS", raising=False)
    context = _context()

    run_requests = list(pricing_weekly_schedule(context))
    run_keys = [rr.run_key for rr in run_requests]

    assert len(run_keys) == len(set(run_keys))
    for rr in run_requests:
        assert rr.run_key.endswith(f"-{_region(rr)}")


def test_download_and_transform_config_region_match_per_run_request(monkeypatch):
    monkeypatch.delenv("PRICING_REGIONS", raising=False)
    context = _context()

    for rr in pricing_weekly_schedule(context):
        download_region = rr.run_config["ops"]["download_pricing"]["config"]["region"]
        transform_region = rr.run_config["ops"]["transform_to_parquet"]["config"]["region"]
        assert download_region == transform_region


def test_respects_env_var_region_override(monkeypatch):
    """PRICING_REGIONS env var still works — it's the default source PricingRegionsResource
    reads from when no explicit resource config is given."""
    monkeypatch.setenv("PRICING_REGIONS", "us-east-1,eu-west-1")
    context = _context()

    run_requests = list(pricing_weekly_schedule(context))

    assert sorted(_region(rr) for rr in run_requests) == ["eu-west-1", "us-east-1"]


def test_respects_direct_resource_config_override(monkeypatch):
    """The region list is also settable directly via Dagster's resource config — not only
    the PRICING_REGIONS env var — e.g. Definitions(resources={"pricing_regions":
    PricingRegionsResource(regions=[...])}). No env var involved in this test at all."""
    monkeypatch.delenv("PRICING_REGIONS", raising=False)
    context = _context(regions=["us-west-1", "ap-northeast-1"])

    run_requests = list(pricing_weekly_schedule(context))

    assert sorted(_region(rr) for rr in run_requests) == ["ap-northeast-1", "us-west-1"]


def test_each_run_request_carries_the_concurrency_pool_tag(monkeypatch):
    """SC-002/SC-006: concurrency is tagged so it's automatically verifiable, not just
    observable manually in the Dagster Runs view (see quickstart.md Step 5)."""
    monkeypatch.delenv("PRICING_REGIONS", raising=False)
    context = _context()

    run_requests = list(pricing_weekly_schedule(context))

    assert run_requests, "expected at least one RunRequest"
    for rr in run_requests:
        assert rr.tags.get("dagster/concurrency_key") == "pricing-region-download"


def test_cron_schedule_is_weekly_not_daily():
    """FR-008: fires once per week (Monday 13:00 UTC), not once per day."""
    assert pricing_weekly_schedule.cron_schedule == "0 13 * * 1"
