"""
Tests for src/dagster_app/resources.py — PricingRegionsResource (FR-001, FR-009).
"""

import pytest

from src.dagster_app.resources import PricingRegionsResource


def test_get_regions_returns_explicit_override():
    resource = PricingRegionsResource(regions=["us-east-1", "eu-west-1"])
    assert resource.get_regions() == ["us-east-1", "eu-west-1"]


def test_get_regions_falls_back_to_resolve_regions_when_empty(monkeypatch):
    from src.aws_regions import DEFAULT_PRICING_REGIONS

    monkeypatch.delenv("PRICING_REGIONS", raising=False)
    resource = PricingRegionsResource(regions=[])
    assert resource.get_regions() == DEFAULT_PRICING_REGIONS


def test_get_regions_fails_fast_when_no_regions_are_configured_anywhere(monkeypatch):
    """spec.md Edge Cases: empty region list should fail fast with a clear error — verified
    at the PricingRegionsResource boundary too, not just aws_regions.resolve_regions()."""
    monkeypatch.delenv("PRICING_REGIONS", raising=False)
    monkeypatch.setattr("src.aws_regions.DEFAULT_PRICING_REGIONS", [])

    resource = PricingRegionsResource(regions=[])
    with pytest.raises(ValueError, match="No AWS regions are configured"):
        resource.get_regions()
