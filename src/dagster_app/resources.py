"""
Dagster-native configuration surface for the AWS regions the pricing
pipeline processes (FR-001, FR-009).

Exposed as a Dagster resource — not only a plain Python function — so the
region list is visible and overridable through Dagster's own config system
(Definitions(resources=...), per-deployment resource config), not only the
PRICING_REGIONS environment variable that aws_regions.resolve_regions()
reads as its default source.
"""

from typing import List

from dagster import ConfigurableResource
from pydantic import Field

from src.aws_regions import resolve_regions


class PricingRegionsResource(ConfigurableResource):
    """Holds the list of AWS regions pricing_weekly_schedule fans out over.

    Defaults to aws_regions.resolve_regions() (DEFAULT_PRICING_REGIONS,
    itself overridable via the PRICING_REGIONS env var) at resource
    construction time. Can also be overridden directly through Dagster's own
    resource config without touching schedule/job code, e.g.:

        Definitions(..., resources={
            "pricing_regions": PricingRegionsResource(regions=["us-east-1", "eu-west-1"]),
        })
    """

    regions: List[str] = Field(default_factory=resolve_regions)

    def get_regions(self) -> List[str]:
        return list(self.regions) if self.regions else resolve_regions()
