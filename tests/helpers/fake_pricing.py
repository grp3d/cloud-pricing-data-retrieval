"""
Offline stand-in for the AWS pricing downloader (tasks T004).

Each region gets a configured behavior:
- "ok": copy that region's fixture files from tests/fixtures/pricing/
- "permanent_error": the region fails with a non-retryable error
- fail_times(n): the first n download calls for the region fail as if retries were
  exhausted; later calls succeed (simulates a failure then a successful re-run)
- no_price_list(service): succeed, but report `service` as having no price list

Every call is recorded in `calls` so tests can assert no download happened.
"""

import os
import shutil
from dataclasses import dataclass
from typing import Dict, List, Union

from src.pipeline.downloader import FailedDownload, RegionDownload

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures", "pricing")


@dataclass
class fail_times:
    n: int


@dataclass
class no_price_list:
    service: str


Behavior = Union[str, fail_times, no_price_list]


class FakePricingSource:
    def __init__(self, behaviors: Dict[str, Behavior] = None, default: Behavior = "ok"):
        self.behaviors = dict(behaviors or {})
        self.default = default
        self.calls: List[str] = []
        self._failures_so_far: Dict[str, int] = {}

    def download_region(self, region: str, dest_dir: str) -> RegionDownload:
        self.calls.append(region)
        behavior = self.behaviors.get(region, self.default)

        if behavior == "permanent_error":
            return RegionDownload(
                region=region,
                failed_downloads=[
                    FailedDownload("AmazonEC2", "HTTP 403 Forbidden (not retryable)", 1)
                ],
                max_attempts=1,
            )
        if isinstance(behavior, fail_times):
            done = self._failures_so_far.get(region, 0)
            if done < behavior.n:
                self._failures_so_far[region] = done + 1
                return RegionDownload(
                    region=region,
                    failed_downloads=[
                        FailedDownload("AmazonEC2", "ReadTimeout (retries exhausted)", 4)
                    ],
                    max_attempts=4,
                )

        files = self._copy_fixtures(region, dest_dir)
        if not files:
            return RegionDownload(region=region, error=f"no fixtures for region {region}")
        skipped = 0
        if isinstance(behavior, no_price_list):
            skipped = 1
        return RegionDownload(
            region=region, files=files, services_without_price_list=skipped, max_attempts=1
        )

    @staticmethod
    def _copy_fixtures(region: str, dest_dir: str) -> List[str]:
        os.makedirs(dest_dir, exist_ok=True)
        copied = []
        for name in sorted(os.listdir(FIXTURE_DIR)):
            if name.endswith(f"-{region}.json"):
                target = os.path.join(dest_dir, name)
                shutil.copyfile(os.path.join(FIXTURE_DIR, name), target)
                copied.append(target)
        return copied
