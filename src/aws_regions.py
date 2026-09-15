"""
Configurable region list for the AWS pricing pipeline (FR-001, FR-002, FR-009).

Kept as a plain module-level constant + resolver function, mirroring the
DATA_DIRECTORY_ROOT / resolve_output_dir() override pattern already used in
aws_pricing_api.py, rather than introducing a new configuration mechanism.
"""

import os
from typing import List

DEFAULT_PRICING_REGIONS: List[str] = [
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "eu-west-1",
    "eu-west-2",
    "ap-northeast-1",
]


def resolve_regions() -> List[str]:
    """Return the effective region list.

    Returns DEFAULT_PRICING_REGIONS unless the PRICING_REGIONS environment
    variable is set to a non-blank, comma-separated list of region codes, in
    which case that list is returned instead (order preserved, entries
    stripped of surrounding whitespace). An explicitly empty/whitespace-only
    PRICING_REGIONS falls back to the default rather than yielding zero
    regions.

    Raises ValueError if the region list would still be empty after that
    fallback (i.e. DEFAULT_PRICING_REGIONS itself is empty and PRICING_REGIONS
    isn't set to anything usable) — fails fast with a clear error rather than
    silently doing nothing (spec.md Edge Cases: empty region list).
    """
    raw = os.getenv("PRICING_REGIONS", "")
    if raw.strip():
        regions = [region.strip() for region in raw.split(",") if region.strip()]
        if regions:
            return regions

    if not DEFAULT_PRICING_REGIONS:
        raise ValueError(
            "No AWS regions are configured: DEFAULT_PRICING_REGIONS is empty and the "
            "PRICING_REGIONS environment variable is not set to a non-blank, "
            "comma-separated list of region codes. Configure at least one region "
            "before running the pricing pipeline."
        )
    return list(DEFAULT_PRICING_REGIONS)
