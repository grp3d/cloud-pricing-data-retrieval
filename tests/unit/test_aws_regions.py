"""
Tests for src/aws_regions.py — the configurable region list (FR-001, FR-002, FR-009).
"""

import pytest

from src.aws_regions import DEFAULT_PRICING_REGIONS, resolve_regions


def test_default_pricing_regions_matches_fr_002():
    assert DEFAULT_PRICING_REGIONS == [
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "eu-west-1",
        "eu-west-2",
        "ap-northeast-1",
    ]


def test_resolve_regions_returns_default_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("PRICING_REGIONS", raising=False)
    assert resolve_regions() == DEFAULT_PRICING_REGIONS


def test_resolve_regions_returns_default_when_env_var_empty(monkeypatch):
    monkeypatch.setenv("PRICING_REGIONS", "   ")
    assert resolve_regions() == DEFAULT_PRICING_REGIONS


def test_resolve_regions_parses_env_var_override(monkeypatch):
    monkeypatch.setenv("PRICING_REGIONS", "us-east-1,eu-west-1")
    assert resolve_regions() == ["us-east-1", "eu-west-1"]


def test_resolve_regions_strips_whitespace_around_entries(monkeypatch):
    monkeypatch.setenv("PRICING_REGIONS", " us-east-1 , eu-west-1 ")
    assert resolve_regions() == ["us-east-1", "eu-west-1"]


def test_resolve_regions_fails_fast_when_no_regions_are_configured(monkeypatch):
    """spec.md Edge Cases: empty region list should fail fast with a clear error rather
    than silently returning nothing (which would make the schedule/sensor silently yield
    zero RunRequests on every tick)."""
    monkeypatch.delenv("PRICING_REGIONS", raising=False)
    monkeypatch.setattr("src.aws_regions.DEFAULT_PRICING_REGIONS", [])

    with pytest.raises(ValueError, match="No AWS regions are configured"):
        resolve_regions()
